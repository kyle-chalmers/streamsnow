"""Live SQL review: ``probe``, ``run``, ``bench`` and ``log`` against Snowflake.

(``compare``, which holds the screen to ``run`` without a connection, lives
in ``sql_review_compare``.)

Why this exists
---------------
``sql-review check`` proves the committed review SQL matches what the app runs.
It cannot prove the SQL is *right*: that the objects exist, that the app's role
can read them, that a section returns rows, or that the numbers on screen are
the numbers in the data. A dashboard can pass every offline gate and still show
zero rows in production because a grant is missing, or a total that is wrong
because a join fans out. The live review closes that gap, and keeps the
evidence honest:

- **Facts come from tools, not from an agent's reading of a result set.** Each
  verb emits compact JSON with a stable ``id`` (``probe:01#1``,
  ``probe:DB.SCHEMA.OBJECT``, ``run:01#1``, ``bench:01#1:before``). Reviewer
  agents make judgement calls that must cite those IDs, and ``log`` refuses a
  finding whose evidence is not in the run directory.
- **No data rows leave Snowflake.** ``run`` and ``bench`` wrap each section in
  an aggregate (``COUNT(*)``, ``HASH_AGG`` and per-column ``SUM``), computed in
  the warehouse, so only a row count, a hash and column totals come back. The
  committed log shows totals only for a single all-numeric row (a KPI) or a
  result of ten rows or more. A total over two to nine rows is a small-group
  breakdown, and is withheld.
- **The session is pinned.** Every call goes through ``streamsnow.sf_exec``:
  an explicit connection, the app's role with secondary roles off, a query tag,
  a statement timeout, and the read-only and schema-deny guards before
  anything is sent.

Run directory
-------------
Each review is one run: ``.streamsnow/sql-review/<slug>/<run_id>/`` with
``run_id = YYYYMMDD-HHMMSS-<shortsha>``. ``meta.json`` records the commit,
whether the app's files had uncommitted changes, and the connection, role and
warehouse the session actually used. Verbs write ``probe.json``,
``run-NN.json`` (one per page, so parallel page reviewers never write the same
file) and ``bench-NN-N.json``; ``compare`` adds ``compare.json``, from the
review preview captures in ``capture/`` and the browser walk's
``screen.json`` (agent-written, so it never holds a citable id). The folder
holds only aggregates, and a
``.gitignore`` of ``*`` keeps it out of commits even in repos whose root
``.gitignore`` predates it. The committed record is the review log.

Headline contract (``compare`` holds the screen to it)
-----------------------------------------------------
For each section ``run`` reports ``rows`` (the row count) and ``totals``:
``{COLUMN: decimal string | null}`` for every numeric column (``NUMBER`` and
its aliases, ``FLOAT``/``DOUBLE``/``REAL``, ``DECFLOAT``), keyed by the column
name as Snowflake reports it (``DESCRIBE RESULT`` de-duplicates repeats as
``NAME_1``). A sum over no non-null values is ``null``. ``FLOAT`` columns are
listed in ``float_columns``: their sums depend on evaluation order, so compare
them with a tolerance.

Bench
-----
``bench`` measures one section with the result cache off, before and (with
``--sql-file``) after a candidate rewrite, interleaved, and reports medians of
elapsed time and bytes scanned (query history) and the actual partitions
scanned and total (``GET_QUERY_OPERATOR_STATS``). The timed statement is the
``COUNT(*)`` + ``HASH_AGG`` wrapper, since fetching the bare rows is exactly
what this tool must not do. ``equivalent`` is true only when the row count, the
order-insensitive hash and the column names and types all match.

Exit codes: 0 = every check passed, 1 = a failing check or a refused log,
2 = tool error (nothing ran, or the run could not complete).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import difflib
import hashlib
import json
import os
import re
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from .. import sf_exec as sx
from ..config import ConfigError, find_config, load_config
from ..policy import SchemaPolicy
from . import sql_review as sr
from . import sql_review_index as sri

RUNS_DIR = Path(".streamsnow") / "sql-review"
#: Files the verbs write. Agents write findings and verdicts into the same
#: folder; only these may mint citable ids, so facts keep coming from tools.
_RESULT_FILE_RE = re.compile(r"^(probe|run-\d{2}|bench-\d{2}-\d+|compare(-\d{2})?)\.json$")
META = "meta.json"
REVIEW_LOG_DIR = "review_log"
_RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{7,40}$")
_TAG_LINE_RE = re.compile(r"^--(\d+)_([a-z][a-z0-9_]*)\s*$")
_METRIC_REF_RE = re.compile(r"^(\d{1,2})#(\d{1,3})$")
_NUMERIC_TYPE_RE = re.compile(
    r"^(NUMBER|DECIMAL|NUMERIC|INT|INTEGER|BIGINT|SMALLINT|TINYINT|BYTEINT|"
    r"FLOAT|FLOAT4|FLOAT8|DOUBLE|DOUBLE PRECISION|REAL|DECFLOAT)\b",
    re.IGNORECASE,
)
_FLOAT_TYPE_RE = re.compile(r"^(FLOAT|FLOAT4|FLOAT8|DOUBLE|REAL)\b", re.IGNORECASE)
_UNWRAPPABLE_ROOTS = frozenset({"SHOW", "DESCRIBE", "DESC", "EXPLAIN"})
_PLACEHOLDER_RE = re.compile(r"\bYOUR_TABLE\b")
#: Committed-log totals: a single all-numeric row (a KPI) or at least this many rows.
MIN_ROWS_FOR_TOTALS = 10
DEFAULT_SLOW_S = 10
SEVERITIES = ("blocker", "major", "minor")
_FINDING_KEYS = frozenset(
    {"id", "severity", "page", "metric", "object", "claim", "evidence", "suggested_fix"}
)
_FINDING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:#-]{0,63}$")
_LATEST_START = "<!-- sql-review-latest:start -->"
_LATEST_END = "<!-- sql-review-latest:end -->"
_RUN_MARK = "<!-- sql-review-run: {} -->"


class ToolError(sr.ToolError):
    """Nothing ran, or the run could not complete. Exit 2."""


# --------------------------------------------------------------------------- #
# Configuration and session
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    connection: str
    role: str | None
    warehouse: str | None
    policy: SchemaPolicy | None
    warnings: list[str] = field(default_factory=list)


def settings(repo: Path, args: argparse.Namespace) -> Settings:
    """Connection, role, warehouse and governance from the config plus flags.

    The role defaults to ``snowflake.roles.ci_role``: the deployed app runs as
    that role (owner's rights), so reviewing as anything else checks the wrong
    grants. ``--role`` overrides it for a person who does not hold that role.
    """
    cfg = None
    path = find_config(repo)
    if path is not None:
        try:
            cfg = load_config(path)
        except ConfigError as exc:
            raise ToolError(f"streamsnow.config.yaml is invalid: {exc}") from exc
    warnings: list[str] = []
    connection = args.connection or (cfg.snowflake.connection_name if cfg else None)
    if not connection:
        raise ToolError(
            "no Snowflake connection: pass --connection, or set snowflake.connection_name "
            "in streamsnow.config.yaml (`streamsnow configure`)"
        )
    role = args.role or (cfg.snowflake.roles.ci_role if cfg else None)
    warehouse = args.warehouse or (cfg.snowflake.objects.default_warehouse if cfg else None)
    policy = SchemaPolicy.from_governance(cfg.governance) if cfg else None
    if cfg is None:
        warnings.append(
            "no streamsnow.config.yaml: the governance schema denylist is not applied and the "
            "connection's default role and warehouse are used"
        )
    return Settings(connection, role, warehouse, policy, warnings)


def make_exec(slug: str, st: Settings, timeout_s: int, runner: sx.Runner | None) -> sx.SnowExec:
    try:
        session = sx.Session(
            connection=st.connection,
            role=st.role,
            warehouse=st.warehouse,
            query_tag=f"streamsnow:sql-review:{slug}",
            timeout_s=timeout_s,
        )
    except sx.SnowError as exc:
        raise ToolError(str(exc)) from exc
    return sx.SnowExec(session, st.policy, runner=runner)


_SESSION_SQL = (
    "SELECT CURRENT_ROLE() AS ROLE, CURRENT_WAREHOUSE() AS WAREHOUSE, "
    "CURRENT_SECONDARY_ROLES() AS SECONDARY_ROLES"
)


# --------------------------------------------------------------------------- #
# Batching: one `snow` call (one login) per step, isolating failures
# --------------------------------------------------------------------------- #
def run_batch(
    ex: sx.SnowExec, items: list[tuple[str, list[str]]], *, result_cache: bool = True
) -> dict[str, list[sx.ResultSet] | sx.SnowError]:
    """Run every item's statements in one call; on failure, each item alone.

    ``snow`` stops at the first failing statement, so one bad section would
    otherwise sink every other section's result. The retry costs one login per
    item, and only on the failure path.
    """
    if not items:
        return {}
    flat = [s for _, stmts in items for s in stmts]
    # Guard everything first: a refusal must stop the whole step before any
    # call, not just the item it is in (the per-item retry would send the rest).
    for stmt in flat:
        sx.guard(stmt, ex.policy)
    try:
        results = ex.run(flat, result_cache=result_cache)
    except sx.SnowError as exc:
        # No Snowflake error code means no SQL ran at all: no CLI, an unknown
        # connection, a failed login. Retrying item by item would only repeat
        # the login (a browser window per item under SSO) and then report every
        # section as a failed check. Nothing ran, so stop.
        if not _SQL_ERROR_RE.search(str(exc)):
            raise
        out: dict[str, list[sx.ResultSet] | sx.SnowError] = {}
        session = [stmts for key, stmts in items if key == "__session"]
        if session:
            # The session prefix (role, warehouse) runs before every item: if it
            # alone fails, every item would, so this too is "nothing ran".
            out["__session"] = ex.run(session[0], result_cache=result_cache)
        if len(items) == 1:
            return {items[0][0]: exc}
        for key, stmts in items:
            if key == "__session":
                continue
            try:
                out[key] = ex.run(stmts, result_cache=result_cache)
            except sx.SnowError as one:
                out[key] = one
        return out
    out = {}
    i = 0
    for key, stmts in items:
        out[key] = results[i : i + len(stmts)]
        i += len(stmts)
    return out


#: A Snowflake statement error carries its code and SQLSTATE: `002003 (42S02): ...`.
_SQL_ERROR_RE = re.compile(r"\b\d{6} \([0-9A-Z]{5}\)")
#: Snowflake query ids look like a UUID.
_QID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _refused(exc: sx.SnowError) -> bool:
    """A guard refusal: nothing was sent, so this is a tool error, not a finding."""
    return str(exc).startswith("refused")


# --------------------------------------------------------------------------- #
# Run directory
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def runs_root(repo: Path, slug: str) -> Path:
    return repo / RUNS_DIR / slug


def _ensure_gitignore(repo: Path) -> None:
    gi = repo / RUNS_DIR / ".gitignore"
    if not gi.is_file():
        gi.parent.mkdir(parents=True, exist_ok=True)
        gi.write_text(
            "# streamsnow sql-review scratch evidence: aggregates only, never committed.\n*\n",
            encoding="utf-8",
            newline="\n",
        )


def write_json(path: Path, data: dict) -> None:
    """Atomic: parallel reviewers may read while another verb writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
            fh.write("\n")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: a parallel reader holding the file open blocks the
                # replace for a moment.
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolError(f"{path.name} is not a JSON object")
    return data


def resolve_run(repo: Path, app: Path, run: str | None, st: Settings | None) -> Path:
    """The run directory: ``--run <id>``, ``--run latest``, or a new run."""
    root = runs_root(repo, app.name)
    if run == "latest":
        runs = (
            sorted(
                p
                for p in root.iterdir()
                if _RUN_ID_RE.match(p.name)
                and any(_RESULT_FILE_RE.match(f.name) for f in p.glob("*.json"))
            )
            if root.is_dir()
            else []
        )
        if not runs:
            raise ToolError(f"no runs yet for {app.name}; start one with `sql-review probe`")
        return runs[-1]
    if run is not None:
        if not _RUN_ID_RE.match(run):
            raise ToolError(f"--run {run!r} is not a run id (YYYYMMDD-HHMMSS-<sha>) or 'latest'")
        path = root / run
        if not (path / META).is_file():
            raise ToolError(f"no run {run} for {app.name}")
        return path
    _ensure_gitignore(repo)
    sha = _git(repo, "rev-parse", "HEAD") or "0" * 40
    dirty = bool(_git(repo, "status", "--porcelain", "--", f"apps/{app.name}"))
    run_id = f"{_now():%Y%m%d-%H%M%S}-{sha[:7]}"
    path = root / run_id
    suffix = 0
    while path.exists():  # two runs in the same second
        suffix += 1
        run_id = f"{_now():%Y%m%d-%H%M%S}-{sha[:7]}{suffix:x}"
        path = root / run_id
    meta = {
        "run_id": run_id,
        "app": app.name,
        "commit": sha,
        "dirty": dirty,
        "started": _now().isoformat(timespec="seconds"),
        "streamsnow": __version__,
        "connection": st.connection if st else None,
        "session": None,
    }
    write_json(path / META, meta)
    return path


def _record_session(run_dir: Path, rows: list[sx.ResultSet] | sx.SnowError) -> list[str]:
    """Store the role/warehouse the session really used; warn if it changed."""
    if isinstance(rows, sx.SnowError) or not rows:
        return ["could not read the session's role and warehouse"]
    row = sx.first_row(rows[0])
    facts = {
        "role": row.get("ROLE"),
        "warehouse": row.get("WAREHOUSE"),
        "secondary_roles": row.get("SECONDARY_ROLES"),
    }
    meta = read_json(run_dir / META)
    warnings: list[str] = []
    if meta.get("session") and meta["session"] != facts:
        warnings.append(
            f"this call ran as {facts}, but the run started as {meta['session']}; "
            "keep one role and warehouse per run"
        )
    else:
        meta["session"] = facts
        write_json(run_dir / META, meta)
    return warnings


# --------------------------------------------------------------------------- #
# Sections from the committed page files
# --------------------------------------------------------------------------- #
@dataclass
class Section:
    page: sri.Page
    metric: sri.Metric
    sql: str  # without the trailing ';'

    @property
    def ref(self) -> str:
        return f"{self.page.number:02d}#{self.metric.number}"

    @property
    def root(self) -> str:
        masked = sr._mask_strings_and_comments(self.sql).split()
        return masked[0].upper() if masked else ""

    def base(self) -> dict:
        return {
            "page": f"{self.page.number:02d}",
            "page_file": self.page.filename,
            "stem": self.page.stem,
            "n": self.metric.number,
            "key": self.metric.key,
        }


def load_app(repo: Path, slug: str) -> tuple[Path, sri.Index]:
    app = sr._app_dir(repo, slug)
    index = sri.load_index(app)
    if not index.exists:
        raise ToolError(f"{slug} has no sql_review/index.yaml; nothing to review live")
    problems = [f for f in index.findings if f["kind"] == sri.KIND_INDEX]
    if problems:
        raise ToolError(
            "index.yaml is invalid; fix it (`streamsnow sql-review check`) before a live run: "
            + "; ".join(f"{f['file']}:{f['line']} {f['detail']}" for f in problems[:5])
        )
    if _PLACEHOLDER_RE.search(index.path.read_text(encoding="utf-8")):
        raise ToolError(
            "index.yaml still points at the scaffold's YOUR_TABLE placeholder; point the review "
            "window and metrics at real objects, regenerate, then run the live review"
        )
    return app, index


def page_sections(
    repo: Path, app: Path, index: sri.Index, page_filter: str | None
) -> list[Section]:
    """Sections exactly as committed, refusing stale or hand-edited page files.

    Reviewers read and re-run the committed page files, so that is what runs
    live. A page whose provenance does not match is refused: its SQL is not
    what the app runs, and a review of it would certify the wrong thing.
    """
    from . import sql_review_lint as srl  # noqa: PLC0415

    pages = sr._expected_pages(index)
    if page_filter is not None:
        if not re.fullmatch(r"\d{1,2}", page_filter):
            raise ToolError(f"--page {page_filter!r} must be a page number like 01")
        pages = [p for p in pages if p.number == int(page_filter)]
        if not pages:
            raise ToolError(f"{app.name} has no page {page_filter} with metrics in index.yaml")
    stale = [
        f
        for f in sr._check_page_files(repo, app, index, srl.config_text(repo))
        if f["kind"] in (sr.KIND_PROVENANCE, sr.KIND_READONLY, sr.KIND_BIND)
    ]
    wanted = {sr._rel(app, "sql_review", p.filename) for p in pages}
    stale = [f for f in stale if f["file"] in wanted]
    if stale:
        raise ToolError(
            "the committed page files do not match what the app runs; run "
            f"`streamsnow sql-review generate {app.name}` and commit first: "
            + "; ".join(f"{f['file']}: {f['detail']}" for f in stale[:3])
        )
    out: list[Section] = []
    for page in pages:
        text = (sr._review_dir(app) / page.filename).read_text(encoding="utf-8")
        chunks = split_page(text)
        for metric in page.metrics:
            sql = chunks.get((metric.number, metric.key))
            if sql is None:
                raise ToolError(f"{page.filename} has no section --{metric.number}_{metric.key}")
            out.append(Section(page, metric, sql))
    return out


def split_page(text: str) -> dict[tuple[int, str], str]:
    """``--N_key`` tag -> that section's SQL (no trailing ``;``)."""
    chunks: dict[tuple[int, str], list[str]] = {}
    current: list[str] | None = None
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("-- Provenance: "):
            break
        m = _TAG_LINE_RE.match(line)
        if m:
            current = chunks.setdefault((int(m.group(1)), m.group(2)), [])
            continue
        if current is not None:
            current.append(line)
    return {k: sx._without_terminator("\n".join(v)) for k, v in chunks.items()}


# --------------------------------------------------------------------------- #
# SQL shapes (verified against Snowflake; see the module docstring)
# --------------------------------------------------------------------------- #
def describe_sql(section_sql: str) -> list[str]:
    """Compile the section without running it; read back its columns."""
    return [f"SELECT * FROM (\n{section_sql}\n) WHERE 1 = 0", "DESCRIBE RESULT LAST_QUERY_ID()"]


@dataclass
class Column:
    position: int  # 1-based
    name: str
    type: str

    @property
    def numeric(self) -> bool:
        return bool(_NUMERIC_TYPE_RE.match(self.type))

    @property
    def is_float(self) -> bool:
        return bool(_FLOAT_TYPE_RE.match(self.type))


def parse_columns(result: sx.ResultSet) -> list[Column]:
    return [
        Column(i, str(r.get("NAME", "")), str(r.get("TYPE", "")))
        for i, r in enumerate(sx.upper_rows(result), start=1)
    ]


def measure_sql(section_sql: str, columns: list[Column], *, sums: bool = True) -> str:
    """One row of aggregates: row count, order-insensitive hash, column totals.

    Positional references (``$n``) keep duplicate column names unambiguous;
    the hash takes columns sorted by name, so a reordered projection hashes the
    same. Everything is cast to text in Snowflake so no precision is lost on
    the way through JSON.
    """
    ordered = sorted(columns, key=lambda c: (c.name, c.position))
    parts = ['COUNT(*) AS "__ROWS"']
    if ordered:
        refs = ", ".join(f"${c.position}" for c in ordered)
        parts.append(f'TO_VARCHAR(HASH_AGG({refs})) AS "__HASH"')
    if sums:
        for c in columns:
            if c.numeric:
                parts.append(f'COUNT(${c.position}) AS "__C{c.position}_N"')
                parts.append(f'TO_VARCHAR(SUM(${c.position})) AS "__C{c.position}_SUM"')
    select = ",\n    ".join(parts)
    return f"SELECT\n    {select}\nFROM (\n{section_sql}\n)"


_LAST_QID_SQL = 'SELECT LAST_QUERY_ID() AS "QUERY_ID"'
# BY_USER, not BY_SESSION: when a failed batch is retried section by section,
# each retry is a new session. The hour window keeps the scan small.
_HISTORY_SQL = (
    "SELECT QUERY_ID, TOTAL_ELAPSED_TIME, COMPILATION_TIME, EXECUTION_TIME, BYTES_SCANNED, "
    "ROWS_PRODUCED FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER("
    "END_TIME_RANGE_START => DATEADD('hour', -1, CURRENT_TIMESTAMP()), RESULT_LIMIT => 10000))"
)


def _opstats_sql(qid: str) -> str:
    return (
        "SELECT SUM(OPERATOR_STATISTICS:pruning:partitions_scanned::NUMBER) AS "
        "PARTITIONS_SCANNED, SUM(OPERATOR_STATISTICS:pruning:partitions_total::NUMBER) AS "
        f"PARTITIONS_TOTAL FROM TABLE(GET_QUERY_OPERATOR_STATS({_lit(qid)})) "
        "WHERE OPERATOR_TYPE = 'TableScan'"
    )


def query_stats(
    ex: sx.SnowExec, qids: list[str | None], *, partitions: bool = False
) -> tuple[dict[str, dict], list[str]]:
    """Timing (and partitions) for measured queries, by id, in one more call.

    A separate call, not the tail of the measure batch: when the role cannot
    read query history, the measures have still run and must not be repeated.
    Query ids are validated, so they are safe inside a literal.
    """
    ids = [q for q in dict.fromkeys(qids) if q and _QID_RE.match(q)]
    if not ids:
        return {}, []
    hist = f"{_HISTORY_SQL} WHERE QUERY_ID IN ({', '.join(_lit(q) for q in ids)})"
    stmts = [hist] + ([_opstats_sql(q) for q in ids] if partitions else [])
    warnings: list[str] = []
    try:
        got = ex.run(stmts)
    except sx.SnowError:
        if not partitions:
            return {}, [_HISTORY_WARNING]
        try:
            got = ex.run([hist]) + [[] for _ in ids]
        except sx.SnowError:
            return {}, [_HISTORY_WARNING]
        warnings.append("operator stats were not readable; partition counts are missing")
    stats = {str(r.get("QUERY_ID")): dict(r) for r in sx.upper_rows(got[0])}
    if partitions:
        for qid, ops in zip(ids, got[1:], strict=True):
            stats.setdefault(qid, {}).update(sx.first_row(ops))
    if any("TOTAL_ELAPSED_TIME" not in stats.get(q, {}) for q in ids):
        warnings.append(_HISTORY_WARNING)
    return stats, warnings


_HISTORY_WARNING = (
    "query history was not readable for some queries (the role may not see "
    "SNOWFLAKE.INFORMATION_SCHEMA, or history lagged); their timings are missing"
)


def _int(value: object) -> int | None:
    try:
        return int(str(value).split(".")[0]) if value not in (None, "") else None
    except ValueError:
        return None


def parse_measure(row: dict, columns: list[Column]) -> dict:
    totals: dict[str, str | None] = {}
    seen: dict[str, int] = {}
    for c in columns:
        if not c.numeric:
            continue
        key = f"__C{c.position}_SUM"
        if key not in row:
            continue
        name = c.name
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name = f"{name}#{seen[name]}"
        value = row.get(key)
        totals[name] = None if value in (None, "") else str(value)
    return {
        "rows": _int(row.get("__ROWS")),
        "hash": None if row.get("__HASH") in (None, "") else str(row.get("__HASH")),
        "totals": totals,
        "float_columns": [c.name for c in columns if c.is_float],
    }


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
_NAME_PART_BAD = re.compile(r"[\x00-\x1f\x7f;]")


def split_fqn(fqn: str) -> tuple[str, str, str]:
    """``DB.SCHEMA."Obj"`` -> its three parts, quotes kept. Validated by the index."""
    parts = re.findall(r'"[^"]+"|[^.]+', fqn)
    if len(parts) != 3 or any(_NAME_PART_BAD.search(p) for p in parts):
        raise ToolError(f"object name {fqn!r} must be DATABASE.SCHEMA.OBJECT without ';'")
    return parts[0], parts[1], parts[2]


def _lit(value: str) -> str:
    """A single-quoted SQL literal; Snowflake treats ``\\`` as an escape inside one."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _bare(part: str) -> tuple[str, bool]:
    """(name as SHOW reports it, case-sensitive?)."""
    if part.startswith('"') and part.endswith('"'):
        return part[1:-1], True
    return part.upper(), False


_KINDS = {  # SHOW OBJECTS kind -> (GET_DDL type, SHOW GRANTS ON keyword, has a query body)
    "TABLE": ("TABLE", "TABLE", False),
    "DYNAMIC_TABLE": ("DYNAMIC_TABLE", "DYNAMIC TABLE", True),
    "VIEW": ("VIEW", "VIEW", True),
    "MATERIALIZED_VIEW": ("VIEW", "MATERIALIZED VIEW", True),
}


def _normalise_sql(text: str) -> str:
    """Comments dropped, everything outside ``'...'`` literals upper-cased.

    Literals and double-quoted identifiers are kept verbatim: their case is
    meaning, not formatting.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        two = text[i : i + 2]
        if two == "--" or two == "//":
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif two == "/*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
        elif ch in "'\"":
            j = i + 1
            while j < n:
                if text[j] == "\\" and ch == "'":
                    j += 2
                    continue
                if text[j] == ch:
                    if text[j + 1 : j + 2] == ch:
                        j += 2
                        continue
                    break
                j += 1
            out.append(text[i : j + 1])
            i = j + 1
        else:
            out.append(ch.upper())
            i += 1
    return "".join(out)


_TOKEN_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"]|\"\")*\"|[A-Za-z0-9_$.]+|\S")


def _ddl_body(text: str) -> list[str]:
    """Normalised lines of a view's query body (after the first top-level AS).

    Only the first CREATE statement counts: a DDL file may also hold USE or
    GRANT statements around it.
    """
    norm = _normalise_sql(text.replace("\r\n", "\n"))
    masked = sr._mask_strings_and_comments(norm)
    start = 0
    for chunk in masked.split(";"):
        if chunk.strip().upper().startswith("CREATE"):
            lead = len(chunk) - len(chunk.lstrip())
            norm = norm[start + lead : start + len(chunk)]
            masked = chunk[lead:]
            break
        start += len(chunk) + 1
    m = re.search(r"\bAS\b", masked)
    body = norm[m.end() :] if m else norm
    lines = [" ".join(ln.split()) for ln in body.split("\n")]
    return [ln for ln in lines if ln]


def ddl_drift(committed: str, live: str) -> dict:
    """Drift = a different token sequence: spacing and line breaks never count."""
    a, b = _ddl_body(committed), _ddl_body(live)
    if _TOKEN_RE.findall(" ".join(a)) == _TOKEN_RE.findall(" ".join(b)):
        return {"status": "pass", "drift": False, "diff": ""}
    diff = list(difflib.unified_diff(a, b, "committed", "live", lineterm="", n=1))
    clipped = diff[:40]
    if len(diff) > 40:
        clipped.append(f"... {len(diff) - 40} more diff lines")
    return {"status": "fail", "drift": True, "diff": "\n".join(clipped)}


def _ddl_files(app: Path) -> dict[str, Path]:
    return {p.name[: -len(".sql")].upper(): p for p in sr._ddl_files(app)}


def cmd_probe(args: argparse.Namespace, runner: sx.Runner | None = None) -> int:
    repo = Path(args.dir).resolve()
    app, index = load_app(repo, args.slug)
    st = settings(repo, args)
    sections = page_sections(repo, app, index, None)
    ex = make_exec(app.name, st, args.timeout, runner)
    run_dir = resolve_run(repo, app, args.run, st)
    warnings = list(st.warnings)
    results: list[dict] = []

    # Step 1 (one login): session facts, every section's columns, every object's SHOW.
    objects: dict[str, tuple[str, str, str]] = {}
    for name in sorted(
        {r for s in sections for r in s.metric.reads} | {o.name for o in index.objects}
    ):
        objects[name] = split_fqn(name)
    denied: set[str] = set()
    if st.policy is not None:
        from .check_schema_refs import find_denied_refs  # noqa: PLC0415

        denied = {n for n in objects if find_denied_refs(n, st.policy)}
    items: list[tuple[str, list[str]]] = [("__session", [_SESSION_SQL])]
    for s in sections:
        if s.root not in _UNWRAPPABLE_ROOTS:
            items.append((f"col:{s.ref}", describe_sql(s.sql)))
    for name, (db, schema, obj) in objects.items():
        if name not in denied:
            bare, _ = _bare(obj)
            items.append(
                (f"show:{name}", [f"SHOW OBJECTS LIKE {_lit(bare)} IN SCHEMA {db}.{schema}"])
            )
    try:
        step1 = run_batch(ex, items)
    except sx.SnowError as exc:
        raise ToolError(str(exc)) from exc
    warnings += _record_session(run_dir, step1.pop("__session", sx.SnowError("missing")))
    for value in step1.values():
        if isinstance(value, sx.SnowError) and _refused(value):
            raise ToolError(str(value))

    for s in sections:
        entry = {"id": f"probe:{s.ref}", **s.base()}
        got = step1.get(f"col:{s.ref}")
        if got is None:
            entry.update(status="skipped", detail=f"{s.root} section: nothing to compile-check")
        elif isinstance(got, sx.SnowError):
            entry.update(status="fail", detail=str(got))
        else:
            entry.update(
                status="pass",
                columns=[{"name": c.name, "type": c.type} for c in parse_columns(got[1])],
            )
        results.append(entry)

    # Step 2 (one login): grants and live DDL for the objects that exist.
    ddl_files = _ddl_files(app)
    declared = {o.name.upper(): o for o in index.objects}
    found: dict[str, dict] = {}
    items = []
    for name, (db, schema, obj) in objects.items():
        if name in denied:
            continue
        got = step1.get(f"show:{name}")
        if isinstance(got, sx.SnowError) or got is None:
            found[name] = {"error": str(got) if got else "not checked"}
            continue
        bare, exact = _bare(obj)
        rows = [
            r
            for r in sx.upper_rows(got[0])
            if (str(r.get("NAME", "")) == bare if exact else str(r.get("NAME", "")).upper() == bare)
        ]
        if not rows:
            found[name] = {}
            continue
        row = rows[0]
        kind = str(row.get("KIND", "")).upper().replace(" ", "_")
        if kind == "TABLE" and str(row.get("IS_DYNAMIC", "N")).upper() == "Y":
            kind = "DYNAMIC_TABLE"
        found[name] = {"kind": kind}
        if kind not in _KINDS:
            continue
        ddl_type, grant_kw, _ = _KINDS[kind]
        fq = f"{db}.{schema}.{obj}"
        items.append((f"grants:{name}", [f"SHOW GRANTS ON {grant_kw} {fq}"]))
        if name.upper() in ddl_files:
            items.append((f"ddl:{name}", [f"SELECT GET_DDL({_lit(ddl_type)}, {_lit(fq)}) AS DDL"]))
    try:
        step2 = run_batch(ex, items)
    except sx.SnowError as exc:
        raise ToolError(str(exc)) from exc

    for name in objects:
        entry: dict = {"id": f"probe:{name}", "object": name}
        if name in denied:
            entry.update(
                status="fail",
                exists=None,
                detail="reads a governance-denied schema; not queried (move the read or add "
                "an exact read exception in streamsnow.config.yaml)",
            )
            results.append(entry)
            continue
        info = found.get(name, {})
        if "error" in info:
            entry.update(
                status="fail", exists=None, detail=f"could not list the schema: {info['error']}"
            )
            results.append(entry)
            continue
        if "kind" not in info:
            entry.update(
                status="fail",
                exists=False,
                detail=f"not found, or not visible to role {st.role or '(connection default)'}",
            )
            results.append(entry)
            continue
        kind = info["kind"]
        entry.update(exists=True, kind=kind)
        expected = {
            g.upper() for g in (declared[name.upper()].grants if name.upper() in declared else [])
        }
        if st.role:
            expected.add(st.role.upper())
        entry["grants"] = _grant_status(step2.get(f"grants:{name}"), expected)
        if name.upper() in ddl_files:
            entry["drift"] = _drift_status(kind, step2.get(f"ddl:{name}"), ddl_files[name.upper()])
        bad = entry["grants"]["status"] == "fail" or entry.get("drift", {}).get("status") == "fail"
        entry["status"] = "fail" if bad else "pass"
        results.append(entry)

    data = {
        "verb": "probe",
        "run_id": run_dir.name,
        "app": app.name,
        "results": results,
        "warnings": warnings,
    }
    write_json(run_dir / "probe.json", data)
    print(json.dumps(data, indent=2))
    return 1 if any(r["status"] == "fail" for r in results) else 0


def _grant_status(got: list[sx.ResultSet] | sx.SnowError | None, expected: set[str]) -> dict:
    if got is None:
        return {"status": "skipped", "detail": "object kind has no grant check"}
    if isinstance(got, sx.SnowError):
        return {"status": "skipped", "detail": f"SHOW GRANTS not permitted for this role: {got}"}
    holders = {
        str(r.get("GRANTEE_NAME", "")).upper()
        for r in sx.upper_rows(got[0])
        if str(r.get("PRIVILEGE", "")).upper() in ("SELECT", "OWNERSHIP")
    }
    # Every role inherits PUBLIC, so a grant to it reaches all of them. Shared
    # data (SNOWFLAKE_SAMPLE_DATA) is usually readable this way and no other.
    if "PUBLIC" in holders:
        return {"status": "pass", "roles": sorted(expected), "via_public": True}
    missing = sorted(expected - holders)
    if not missing:
        return {"status": "pass", "roles": sorted(expected)}
    return {
        "status": "warn",
        "missing_direct": missing,
        "detail": "no direct SELECT grant to these roles; SHOW GRANTS lists direct grants "
        "only, so the role may still read it through a role hierarchy (the run step proves it)",
    }


def _drift_status(kind: str, got: list[sx.ResultSet] | sx.SnowError | None, ddl: Path) -> dict:
    if not _KINDS.get(kind, ("", "", False))[2]:
        return {"status": "skipped", "detail": f"a {kind} has no query body to compare"}
    if got is None or isinstance(got, sx.SnowError):
        return {"status": "skipped", "detail": f"GET_DDL not available: {got}"}
    live = str(sx.first_row(got[0]).get("DDL") or "")
    return ddl_drift(ddl.read_text(encoding="utf-8"), live)


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def measure_sections(
    ex: sx.SnowExec, sections: list[Section], *, with_session: bool
) -> tuple[dict[str, dict], list[sx.ResultSet] | sx.SnowError | None, list[str]]:
    """Three logins: compile + columns, every measure, then their timings.

    Measures run with the result cache off: the wrapper text is the same each
    time, so a cached rerun would read as instant and hide a slow section.
    """
    items: list[tuple[str, list[str]]] = [("__session", [_SESSION_SQL])] if with_session else []
    for s in sections:
        if s.root not in _UNWRAPPABLE_ROOTS:
            items.append((s.ref, describe_sql(s.sql)))
    try:
        cols = run_batch(ex, items)
    except sx.SnowError as exc:
        raise ToolError(str(exc)) from exc
    session = cols.pop("__session", None)
    out: dict[str, dict] = {}
    measures: list[tuple[str, list[str]]] = []
    columns: dict[str, list[Column]] = {}
    for s in sections:
        got = cols.get(s.ref)
        if got is None:
            out[s.ref] = {"status": "skipped", "detail": f"{s.root} section is not measurable"}
        elif isinstance(got, sx.SnowError):
            out[s.ref] = {"status": "fail", "detail": str(got)}
        else:
            columns[s.ref] = parse_columns(got[1])
            measures.append((s.ref, [measure_sql(s.sql, columns[s.ref]), _LAST_QID_SQL]))
    if not measures:
        return out, session, []
    try:
        got_all = run_batch(ex, measures, result_cache=False)
    except sx.SnowError as exc:
        raise ToolError(str(exc)) from exc
    qids: dict[str, str | None] = {}
    for ref, _ in measures:
        got = got_all.get(ref)
        section = next(s for s in sections if s.ref == ref)
        overflow = False
        if isinstance(got, sx.SnowError):
            # A SUM can overflow NUMBER(38); retry once without totals.
            try:
                got = ex.run(
                    [measure_sql(section.sql, columns[ref], sums=False), _LAST_QID_SQL],
                    result_cache=False,
                )
                overflow = True
            except sx.SnowError as exc:
                out[ref] = {"status": "fail", "detail": str(exc)}
                continue
        row = sx.first_row(got[0])
        qids[ref] = str(sx.first_row(got[1]).get("QUERY_ID") or "") or None
        entry = {"status": "pass", **parse_measure(row, columns[ref])}
        if overflow:
            entry["totals"] = None
            entry["totals_detail"] = "a column total overflowed, so no totals were computed"
        entry["columns"] = [{"name": c.name, "type": c.type} for c in columns[ref]]
        entry["query_id"] = qids[ref]
        out[ref] = entry
    stats, warnings = query_stats(ex, list(qids.values()))
    for ref, qid in qids.items():
        hist = stats.get(qid or "", {})
        out[ref]["elapsed_ms"] = _int(hist.get("TOTAL_ELAPSED_TIME"))
        out[ref]["bytes_scanned"] = _int(hist.get("BYTES_SCANNED"))
    return out, session, warnings


def cmd_run(args: argparse.Namespace, runner: sx.Runner | None = None) -> int:
    repo = Path(args.dir).resolve()
    app, index = load_app(repo, args.slug)
    st = settings(repo, args)
    sections = page_sections(repo, app, index, args.page)
    ex = make_exec(app.name, st, args.timeout, runner)
    run_dir = resolve_run(repo, app, args.run, st)
    measured, session, stat_warnings = measure_sections(ex, sections, with_session=True)
    warnings = list(st.warnings) + _record_session(run_dir, session or sx.SnowError("missing"))
    warnings += stat_warnings
    by_page: dict[str, list[dict]] = {}
    for s in sections:
        entry = {"id": f"run:{s.ref}", **s.base(), **measured[s.ref]}
        ms = entry.get("elapsed_ms")
        entry["slow"] = bool(ms is not None and ms > args.slow_s * 1000)
        by_page.setdefault(entry["page"], []).append(entry)
    all_results: list[dict] = []
    for page, results in sorted(by_page.items()):
        data = {
            "verb": "run",
            "run_id": run_dir.name,
            "app": app.name,
            "page": page,
            "results": results,
            "warnings": warnings,
        }
        write_json(run_dir / f"run-{page}.json", data)
        all_results += results
    print(
        json.dumps(
            {
                "verb": "run",
                "run_id": run_dir.name,
                "app": app.name,
                "results": all_results,
                "warnings": warnings,
            },
            indent=2,
        )
    )
    return 1 if any(r["status"] == "fail" for r in all_results) else 0


# --------------------------------------------------------------------------- #
# bench
# --------------------------------------------------------------------------- #
def _median(values: list[int | None]) -> int | None:
    vals = [v for v in values if v is not None]
    return int(statistics.median(vals)) if vals else None


def cmd_bench(args: argparse.Namespace, runner: sx.Runner | None = None) -> int:
    repo = Path(args.dir).resolve()
    app, index = load_app(repo, args.slug)
    m = _METRIC_REF_RE.match(args.metric or "")
    if not m:
        raise ToolError("--metric must look like 01#2 (page number # metric number)")
    page_no, metric_no = int(m.group(1)), int(m.group(2))
    sections = [
        s for s in page_sections(repo, app, index, f"{page_no:02d}") if s.metric.number == metric_no
    ]
    if not sections:
        raise ToolError(f"no metric {args.metric} in {app.name}'s index.yaml")
    before = sections[0]
    if before.root in _UNWRAPPABLE_ROOTS:
        raise ToolError(f"metric {args.metric} is a {before.root} section; nothing to benchmark")
    variants: list[tuple[str, str]] = [("before", before.sql)]
    if args.sql_file:
        try:
            text = Path(args.sql_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ToolError(f"cannot read --sql-file: {exc}") from exc
        # Rendered exactly like the app's query file: same tokens, binds and
        # params CTE, and refused unless it is a single statement.
        after_sql = sx._without_terminator(sr.render_section(app, index, before.metric, text=text))
        variants.append(("after", after_sql))
    if not 1 <= args.runs <= 5:
        raise ToolError("--runs must be between 1 and 5")
    st = settings(repo, args)
    ex = make_exec(app.name, st, args.timeout, runner)
    run_dir = resolve_run(repo, app, args.run, st)

    try:
        cols = run_batch(
            ex, [("__session", [_SESSION_SQL])] + [(n, describe_sql(sql)) for n, sql in variants]
        )
    except sx.SnowError as exc:
        raise ToolError(str(exc)) from exc
    warnings = list(st.warnings) + _record_session(run_dir, cols.pop("__session"))
    columns: dict[str, list[Column]] = {}
    for name, _ in variants:
        got = cols[name]
        if isinstance(got, sx.SnowError):
            raise ToolError(f"{name} does not compile: {got}")
        columns[name] = parse_columns(got[1])

    # Interleaved before/after with the result cache off; per run, the measure
    # and its query id. Timings and actual partitions follow in one more call.
    stmts: list[str] = []
    order: list[str] = []
    for _ in range(args.runs):
        for name, sql in variants:
            stmts += [measure_sql(sql, columns[name], sums=False), _LAST_QID_SQL]
            order.append(name)
    try:
        got = ex.run(stmts, result_cache=False)
    except sx.SnowError as exc:
        raise ToolError(f"benchmark failed: {exc}") from exc
    measured = [
        (
            name,
            sx.first_row(got[2 * i]),
            str(sx.first_row(got[2 * i + 1]).get("QUERY_ID") or "") or None,
        )
        for i, name in enumerate(order)
    ]
    stats, stat_warnings = query_stats(ex, [q for _, _, q in measured], partitions=True)
    warnings += stat_warnings
    samples: dict[str, list[dict]] = {name: [] for name, _ in variants}
    for name, row, qid in measured:
        hist = stats.get(qid or "", {})
        samples[name].append(
            {
                "rows": _int(row.get("__ROWS")),
                "hash": row.get("__HASH"),
                "query_id": qid,
                "elapsed_ms": _int(hist.get("TOTAL_ELAPSED_TIME")),
                "bytes_scanned": _int(hist.get("BYTES_SCANNED")),
                "partitions_scanned": _int(hist.get("PARTITIONS_SCANNED")),
                "partitions_total": _int(hist.get("PARTITIONS_TOTAL")),
            }
        )
    results = []
    ref = before.ref
    for name, _ in variants:
        runs = samples[name]
        results.append(
            {
                "id": f"bench:{ref}:{name}",
                **before.base(),
                "variant": name,
                "status": "pass",
                "runs": len(runs),
                "rows": runs[0]["rows"] if runs else None,
                "hash": runs[0]["hash"] if runs else None,
                "columns": [{"name": c.name, "type": c.type} for c in columns[name]],
                "median_elapsed_ms": _median([r["elapsed_ms"] for r in runs]),
                "median_bytes_scanned": _median([r["bytes_scanned"] for r in runs]),
                "partitions_scanned": _median([r["partitions_scanned"] for r in runs]),
                "partitions_total": _median([r["partitions_total"] for r in runs]),
                "query_ids": [r["query_id"] for r in runs],
                "timed_statement": "COUNT(*) + HASH_AGG wrapper, result cache off",
            }
        )
    data: dict = {
        "verb": "bench",
        "run_id": run_dir.name,
        "app": app.name,
        "metric": ref,
        "results": results,
        "warnings": warnings,
    }
    code = 0
    if len(results) == 2:
        eq, notes = equivalence(results[0], results[1], columns["before"], columns["after"])
        results[1]["equivalent"] = eq
        results[1]["equivalence_notes"] = notes
        if not eq:
            results[1]["status"] = "fail"
            code = 1
    write_json(run_dir / f"bench-{before.page.number:02d}-{before.metric.number}.json", data)
    print(json.dumps(data, indent=2))
    return code


def equivalence(
    before: dict, after: dict, cb: list[Column], ca: list[Column]
) -> tuple[bool, list[str]]:
    notes: list[str] = []
    if before["rows"] != after["rows"]:
        notes.append(f"row count differs: {before['rows']} vs {after['rows']}")
    if sorted((c.name, c.type) for c in cb) != sorted((c.name, c.type) for c in ca):
        notes.append("column names or types differ")
    if before["hash"] != after["hash"]:
        notes.append("result hash differs (order-insensitive)")
        if any(c.is_float for c in cb):
            notes.append("FLOAT columns can hash differently between plans; compare totals")
    return (not notes), notes


# --------------------------------------------------------------------------- #
# log
# --------------------------------------------------------------------------- #
def evidence_ids(run_dir: Path) -> set[str]:
    """Every ``id`` in the run's result files (open to later verbs' files)."""
    ids: set[str] = set()
    for path in sorted(run_dir.glob("*.json")):
        if not _RESULT_FILE_RE.match(path.name):
            continue
        for r in read_json(path).get("results", []):
            if isinstance(r, dict) and isinstance(r.get("id"), str):
                ids.add(r["id"])
    return ids


def validate_findings(
    raw: object, known: set[str], index: sri.Index
) -> tuple[list[dict], list[str]]:
    findings = raw.get("findings") if isinstance(raw, dict) else raw
    if not isinstance(findings, list):
        return [], ["findings must be a JSON list (or an object with a findings list)"]
    keys = {(f"{p.number:02d}", m.key) for p in index.numbered_pages for m in p.metrics}
    problems: list[str] = []
    seen: set[str] = set()
    for i, f in enumerate(findings):
        where = f"finding {i + 1}"
        if not isinstance(f, dict):
            problems.append(f"{where}: not an object")
            continue
        fid = f.get("id")
        where = f"finding {fid!r}" if isinstance(fid, str) else where
        extra = set(f) - _FINDING_KEYS
        if extra:
            problems.append(f"{where}: unknown keys {sorted(extra)}")
        missing = {"id", "severity", "claim", "evidence"} - set(f)
        if missing:
            problems.append(f"{where}: missing {sorted(missing)}")
            continue
        if not isinstance(fid, str) or not _FINDING_ID_RE.match(fid):
            problems.append(f"{where}: id must be a short identifier")
        elif fid in seen:
            problems.append(f"{where}: duplicate id")
        else:
            seen.add(fid)
        if f.get("severity") not in SEVERITIES:
            problems.append(f"{where}: severity must be one of {', '.join(SEVERITIES)}")
        if not isinstance(f.get("claim"), str) or not f["claim"].strip():
            problems.append(f"{where}: claim must be non-empty text")
        if (
            "suggested_fix" in f
            and f["suggested_fix"] is not None
            and not isinstance(f["suggested_fix"], str)
        ):
            problems.append(f"{where}: suggested_fix must be text")
        ev = f.get("evidence")
        if not isinstance(ev, list) or not ev or not all(isinstance(e, str) for e in ev):
            problems.append(f"{where}: evidence must be a non-empty list of result ids")
        else:
            unknown = [e for e in ev if e not in known]
            if unknown:
                problems.append(f"{where}: evidence {unknown} is not a result in this run")
        page, metric = f.get("page"), f.get("metric")
        page_ok = page is None or (isinstance(page, str) and re.fullmatch(r"\d{2}", page))
        if not page_ok:
            problems.append(f'{where}: page must be a two-digit page number like "01" or null')
        if metric is not None and (
            not isinstance(metric, str) or page is None or not page_ok or (page, metric) not in keys
        ):
            problems.append(f"{where}: metric {metric!r} is not a metric on page {page}")
        obj = f.get("object")
        if obj is not None and (not isinstance(obj, str) or not sri._FQN_RE.match(obj)):
            problems.append(f"{where}: object must be DATABASE.SCHEMA.OBJECT or null")
        for k in ("claim", "suggested_fix"):
            if isinstance(f.get(k), str) and len(f[k]) > 2000:
                problems.append(f"{where}: {k} is over 2000 characters")
    return findings, problems


def _secondary(value: object) -> str:
    """``CURRENT_SECONDARY_ROLES()`` JSON -> a short label (``none`` when off)."""
    if value in (None, ""):
        return "none"
    try:
        roles = json.loads(str(value)).get("roles", "")
    except (ValueError, AttributeError):
        return str(value)
    return str(roles) if roles else "none"


def _cell(text: object) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def headline(entry: dict) -> str:
    """The committed log's headline cell: never a small-group breakdown."""
    rows = entry.get("rows")
    if rows is None:
        return "—"
    if rows == 0:
        return "no rows"
    if entry.get("totals") is None and entry.get("totals_detail"):
        return "totals not computed (a column total overflowed)"
    totals = entry.get("totals") or {}
    if not totals:
        return "no numeric columns"
    columns = entry.get("columns") or []
    kpi = rows == 1 and columns and all(_NUMERIC_TYPE_RE.match(c.get("type", "")) for c in columns)
    if not (kpi or rows >= MIN_ROWS_FOR_TOTALS):
        return f"withheld ({rows} rows: a small-group breakdown)"
    shown = [f"{k} = {v if v is not None else 'null'}" for k, v in list(totals.items())[:3]]
    more = f" (+{len(totals) - 3} more)" if len(totals) > 3 else ""
    return _cell(", ".join(shown) + more)


def render_log(run_dir: Path, app: Path, index: sri.Index, findings: list[dict], today: str) -> str:
    meta = read_json(run_dir / META)
    session = meta.get("session") or {}
    run_results: dict[str, dict] = {}
    for path in sorted(run_dir.glob("run-*.json")):
        for r in read_json(path).get("results", []):
            run_results[r.get("id", "")] = r
    probe = read_json(run_dir / "probe.json") if (run_dir / "probe.json").is_file() else {}
    screen = _screen_cells(run_dir)
    sha = str(meta.get("commit") or "")[:7]
    dirty = " (the app had uncommitted changes when it ran)" if meta.get("dirty") else ""
    secondary = _secondary(session.get("secondary_roles"))
    lines = [
        f"# SQL review: {app.name}, {today}",
        "",
        _RUN_MARK.format(run_dir.name),
        "",
        "| | |",
        "|---|---|",
        f"| Reviewed commit | `{sha}`{dirty} |",
        f"| Connection | `{_cell(meta.get('connection') or 'unknown')}` |",
        f"| Role | `{_cell(session.get('role') or 'unknown')}` (secondary roles: {_cell(secondary)}) |",
        f"| Warehouse | `{_cell(session.get('warehouse') or 'unknown')}` |",
        f"| Run | `{run_dir.name}` (evidence stays local, under .streamsnow/sql-review/) |",
        f"| Tool | streamsnow {meta.get('streamsnow') or __version__} |",
        "",
        "Totals are shown only for a single all-numeric row or for results of "
        f"{MIN_ROWS_FOR_TOTALS} rows or more. No row-level data is recorded.",
        "",
        *(
            [
                "Screen match holds what each visual received in review preview mode to the "
                "run: within 0.5% or the displayed rounding, integers exactly.",
                "",
            ]
            if screen
            else []
        ),
        "## Pages",
        "",
    ]
    by_metric: dict[tuple[str, str], list[str]] = {}
    for f in findings:
        if f.get("page") and f.get("metric"):
            by_metric.setdefault((f["page"], f["metric"]), []).append(f["id"])
    for page in sr._expected_pages(index):
        nn = f"{page.number:02d}"
        lines += [
            f"### `{page.filename}`: {_cell(page.title or page.stem)}",
            "",
            "| Metric | SQL status | Rows | Headline | Screen match | Findings |",
            "|---|---|---|---|---|---|",
        ]
        for metric in page.metrics:
            r = run_results.get(f"run:{nn}#{metric.number}", {})
            status = r.get("status", "not run")
            rows = r.get("rows") if r.get("rows") is not None else "—"
            ids = ", ".join(by_metric.get((nn, metric.key), [])) or "—"
            match = screen.get(f"compare:{nn}#{metric.number}", "n/a")
            lines.append(
                f"| {metric.number} `{metric.key}` | {status} | {rows} | {headline(r)} "
                f"| {match} | {ids} |"
            )
        lines.append("")
    objs = [r for r in probe.get("results", []) if r.get("object")]
    if objs:
        lines += ["## Objects", "", "| Object | Exists | Grants | DDL drift |", "|---|---|---|---|"]
        for r in objs:
            exists = {True: "yes", False: "no", None: "not checked"}[r.get("exists")]
            grants = (r.get("grants") or {}).get("status", "—")
            drift = (r.get("drift") or {}).get("status", "—")
            lines.append(f"| `{_cell(r['object'])}` | {exists} | {grants} | {drift} |")
        lines.append("")
    lines += ["## Findings", ""]
    for sev in SEVERITIES:
        lines += [f"### {sev.capitalize()}", ""]
        group = [f for f in findings if f["severity"] == sev]
        if not group:
            lines += ["None.", ""]
            continue
        for f in group:
            where = " ".join(
                x
                for x in (
                    f"page {f['page']}" if f.get("page") else "",
                    f"`{f['metric']}`" if f.get("metric") else "",
                    f"`{f['object']}`" if f.get("object") else "",
                )
                if x
            )
            lines.append(f"- **{f['id']}**{f' ({where})' if where else ''}: {_cell(f['claim'])}")
            if f.get("suggested_fix"):
                lines.append(f"  Suggested fix: {_cell(f['suggested_fix'])}")
            lines.append("  Evidence: " + ", ".join(f"`{e}`" for e in f["evidence"]))
        lines.append("")
    lines += [
        "## Sign-off",
        "",
        "A person fills this in after reading the findings. Claude never does.",
        "",
        "Reviewer:",
        "Date:",
        "Decision: approve / changes needed",
        "",
    ]
    return "\n".join(lines)


_SCREEN_WORDS = {
    "match": "match",
    "mismatch": "mismatch",
    "not_captured": "not captured",
    "unsupported": "unsupported",
}


def run_digest(path: Path) -> str | None:
    """Fingerprint of a ``run-NN.json``: tells a compare made from it from a stale one."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else None


def _compare_results(run_dir: Path) -> dict | None:
    from .sql_review_compare import COMPARE_FILE  # noqa: PLC0415

    path = run_dir / COMPARE_FILE
    return read_json(path) if path.is_file() else None


def _screen_cells(run_dir: Path) -> dict[str, str]:
    """``compare:NN#n`` -> its Screen match cell: a status word, never a value.

    A page whose ``run-NN.json`` changed after the compare (a reviewer re-ran
    it) is ``stale``: its comparison no longer describes these results. The
    browser walk never shows here; it is a cross-check, not evidence.
    """
    data = _compare_results(run_dir)
    if data is None:
        return {}
    digests = data.get("run_digests") or {}
    cells: dict[str, str] = {}
    for r in data.get("results", []):
        page = str(r.get("page"))
        if digests.get(page) != run_digest(run_dir / f"run-{page}.json"):
            cells[str(r.get("id"))] = "stale"
        else:
            cells[str(r.get("id"))] = _SCREEN_WORDS.get(str(r.get("status")), "n/a")
    return cells


def _leaks(text: str) -> list[str]:
    from .check_export_clean import _EMAIL_OK_DOMAINS, _EMAIL_OK_TLDS, _EMAIL_RE  # noqa: PLC0415
    from .check_path_leaks import PATTERNS  # noqa: PLC0415

    hits = [name for name, pat in PATTERNS if pat.search(text)]
    for m in _EMAIL_RE.finditer(text):
        domain = m.group(1).lower()
        if not (domain in _EMAIL_OK_DOMAINS or domain.rsplit(".", 1)[-1] in _EMAIL_OK_TLDS):
            hits.append("email address")
    return hits


def latest_block(name: str, today: str, sha: str, counts: dict[str, int]) -> str:
    tally = ", ".join(f"{counts.get(s, 0)} {s}" for s in SEVERITIES)
    return "\n".join(
        [
            _LATEST_START,
            f"[{today} at `{sha}`]({REVIEW_LOG_DIR}/{name}): {tally} finding(s). "
            "The sign-off is in the log.",
            _LATEST_END,
        ]
    )


def splice_latest(existing: str, block: str) -> str:
    lines = existing.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.strip() == _LATEST_START]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == _LATEST_END]
    if (len(starts), len(ends)) == (0, 0):
        return existing.rstrip("\n") + "\n\n## Latest review\n\n" + block + "\n"
    if (len(starts), len(ends)) != (1, 1) or ends[0] < starts[0]:
        raise ToolError(
            f"sql_review/README.md has {len(starts)} start / {len(ends)} end latest-review "
            "markers (or they are reversed); expected exactly one pair"
        )
    return "\n".join([*lines[: starts[0]], block, *lines[ends[0] + 1 :]])


def _log_path(app: Path, today: str, sha: str, run_id: str) -> Path:
    folder = sr._review_dir(app) / REVIEW_LOG_DIR
    base = f"{today}_{sha}"
    n = 1
    while True:
        path = folder / (f"{base}.md" if n == 1 else f"{base}_{n}.md")
        if not path.is_file() or _RUN_MARK.format(run_id) in path.read_text(encoding="utf-8"):
            return path
        n += 1


def _today() -> str:
    return _dt.date.today().isoformat()


def cmd_log(args: argparse.Namespace, runner: sx.Runner | None = None) -> int:
    del runner  # log never touches Snowflake
    repo = Path(args.dir).resolve()
    app = sr._app_dir(repo, args.slug)
    index = sri.load_index(app)
    if not index.exists:
        raise ToolError(f"{args.slug} has no sql_review/index.yaml")
    run_dir = resolve_run(repo, app, args.run or "latest", None)
    try:
        raw = json.loads(Path(args.findings).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(f"cannot read --findings: {exc}") from exc
    if not list(run_dir.glob("run-*.json")):
        raise ToolError(f"run {run_dir.name} has no `run` results; run the sections first")
    findings, problems = validate_findings(raw, evidence_ids(run_dir), index)
    if problems:
        print(json.dumps({"ok": False, "run_id": run_dir.name, "problems": problems}, indent=2))
        return 1
    today = _today()
    text = render_log(run_dir, app, index, findings, today)
    leaks = _leaks(text)
    if leaks:
        print(
            json.dumps(
                {
                    "ok": False,
                    "run_id": run_dir.name,
                    "problems": [
                        f"the log would contain a {h}; remove it from the findings"
                        for h in sorted(set(leaks))
                    ],
                },
                indent=2,
            )
        )
        return 1
    # Screen mismatches are candidate findings for the page reviewers, never
    # logged on their own: name the ones no kept finding cites.
    cited = {e for f in findings for e in f["evidence"]}
    cells = _screen_cells(run_dir)
    uncited = [
        r["id"]
        for r in (_compare_results(run_dir) or {}).get("results", [])
        if r.get("status") == "mismatch"
        and r.get("id") not in cited
        and cells.get(str(r.get("id"))) != "stale"
    ]
    if getattr(args, "dry_run", False):
        print(
            json.dumps(
                {
                    "ok": True,
                    "run_id": run_dir.name,
                    "dry_run": True,
                    "screen_mismatches_uncited": uncited,
                    "log": text,
                },
                indent=2,
            )
        )
        return 0
    sha = str(read_json(run_dir / META).get("commit") or "0" * 7)[:7]
    path = _log_path(app, today, sha, run_dir.name)
    sr._write(path, text)
    readme = sr._review_dir(app) / "README.md"
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    if readme.is_file():
        current = readme.read_text(encoding="utf-8").replace("\r\n", "\n")
        updated = splice_latest(current, latest_block(path.name, today, sha, counts))
        if updated != current:
            sr._write(readme, updated)
    rel = sr._rel(app, "sql_review", REVIEW_LOG_DIR, path.name)
    print(
        json.dumps(
            {
                "ok": True,
                "run_id": run_dir.name,
                "log": rel,
                "counts": counts,
                "screen_mismatches_uncited": uncited,
            },
            indent=2,
        )
    )
    return 0


# --------------------------------------------------------------------------- #
# CLI dispatch (the parsers live in sql_review._build_parser, where the
# CLI-surface snapshot reads them)
# --------------------------------------------------------------------------- #
def _cmd_compare(args: argparse.Namespace, runner: sx.Runner | None = None) -> int:
    from . import sql_review_compare as cmp  # noqa: PLC0415  (it imports this module)

    return cmp.cmd_compare(args, runner)


LIVE_COMMANDS = {
    "probe": cmd_probe,
    "run": cmd_run,
    "bench": cmd_bench,
    "compare": _cmd_compare,
    "log": cmd_log,
}


def dispatch(args: argparse.Namespace, runner: sx.Runner | None = None) -> int:
    try:
        return LIVE_COMMANDS[args.cmd](args, runner)
    except sx.SnowError as exc:
        raise ToolError(str(exc)) from exc
