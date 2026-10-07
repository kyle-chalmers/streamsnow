"""``apps/<slug>/sql_review/index.yaml``: the single source of truth for SQL review.

The index lists, for each app page, the metrics that page shows in on-screen
order, the app query feeding each metric, sample values for the query's
``{TOKENS}``, the review value for each ``:bind``, and the objects it reads. It
also declares the review window, the app-specific reporting objects whose DDL
lives under ``sql_review/app_specific_reporting_objects/``, and the query files
that are inlined fragments rather than runnable queries.

Everything ``sql-review generate`` writes (page files, README tables, the
"Used by" line of each DDL file) is derived from this file, and ``sql-review
check`` validates it. Two numbers are derived, never stored: a page's number is
its position in the app's navigation (``app_nav.extract_nav``), and a metric's
number is its position in the page's ``metrics:`` list. Reordering either
renumbers the generated files and tags; the metric ``key`` is what stays stable.

Validation problems are returned as findings (``kind: index``), never raised:
``check`` runs inside pre-commit, CI and ``validate-app``, and must report every
problem at once rather than stop at the first.

Schema (``schema_version: 2``)::

    schema_version: 2
    app: <slug>
    review_window:                     # one params CTE per section; omit it when no bind
                                       # uses params.*
      start_date: "DATEADD(day, -30, CURRENT_DATE())"
      end_date: "CURRENT_DATE()"
    pages:
      - path: pages/overview.py        # an st.Page path from streamlit_app.py
        metrics:
          - key: total_revenue         # snake_case, five words or fewer
            query: queries/total_revenue.sql
            tokens: {REGION_FILTER: ""}    # mirrors the page default: "All" renders as ""
            binds: {"1": params.start_date, "2": params.end_date}
            notes: "Excludes refunds; booked date, not ship date."
            reads: [ANALYTICS.REPORTING.APP_REVENUE_DAILY]
    objects:
      - name: ANALYTICS.REPORTING.APP_REVENUE_DAILY
        grants: [ROLE_APP_READER]
        reason: performance            # app-data objects: performance | shared_logic
    fragments:
      - file: queries/_shared_ctes.sql
        reason: "inlined via {SHARED_CTES}; not runnable alone"
"""

from __future__ import annotations

import ast
import contextlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .app_nav import extract_nav

SCHEMA_VERSION = 2
INDEX_NAME = "index.yaml"

KIND_INDEX = "index"
KIND_COVERAGE = "coverage"
KIND_MARKER = "marker"

#: The marker call in page code that ties a visual to its index entry.
MARKER_FUNC = "review_value"

# snake_case, at most five words: `total_revenue`, `orders_by_region_and_week`.
_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){0,4}$")
# App-relative query path, resolved strictly inside queries/ (no traversal).
_QUERY_PATH_RE = re.compile(r"^queries/[A-Za-z0-9_][A-Za-z0-9_.-]*\.sql$")
_TOKEN_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_WINDOW_KEY_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_BIND_KEY_RE = re.compile(r"^(?:\d+|[A-Za-z_][A-Za-z0-9_]*)$")
_PARAMS_REF_RE = re.compile(r"^params\.([a-z_][a-z0-9_]*)$")
_IDENT = r"(?:[A-Za-z_][A-Za-z0-9_$]*|\"[^\"]+\")"
_FQN_RE = re.compile(rf"^{_IDENT}\.{_IDENT}\.{_IDENT}$")

_TOP_KEYS = frozenset({"schema_version", "app", "review_window", "pages", "objects", "fragments"})
_PAGE_KEYS = frozenset({"path", "metrics"})
_METRIC_KEYS = frozenset({"key", "query", "tokens", "binds", "notes", "reads"})
_OBJECT_KEYS = frozenset({"name", "grants", "reason"})
_FRAGMENT_KEYS = frozenset({"file", "reason"})


@dataclass
class Metric:
    key: str
    query: str  # app-relative, e.g. "queries/total_revenue.sql"
    tokens: dict[str, str] = field(default_factory=dict)
    binds: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    reads: list[str] = field(default_factory=list)
    number: int = 0  # 1-based position on its page (derived)


@dataclass
class Page:
    path: str  # app-relative page file, e.g. "pages/overview.py"
    metrics: list[Metric] = field(default_factory=list)
    title: str = ""
    number: int = 0  # 1-based position in the app's navigation (0 = not in nav)

    @property
    def stem(self) -> str:
        """``pages/Revenue Overview.py`` → ``revenue_overview``."""
        raw = Path(self.path).stem.lower()
        return re.sub(r"[^a-z0-9]+", "_", raw).strip("_") or "page"

    @property
    def filename(self) -> str:
        """The generated page file, ``NN_<stem>.sql``."""
        return f"{self.number:02d}_{self.stem}.sql"


@dataclass
class ReportingObject:
    name: str
    grants: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class Fragment:
    file: str  # app-relative, e.g. "queries/_shared_ctes.sql"
    reason: str


@dataclass
class Index:
    """A loaded index plus everything wrong with it.

    ``exists`` is False when the app has no ``index.yaml``; the other fields are
    then empty. Pages whose path is not in the app's navigation keep
    ``number == 0`` and are excluded from :attr:`numbered_pages`.

    ``objects_complete`` is False when index.yaml exists but its ``objects:`` did
    not load in full, so a caller that needs the whole declared inventory
    (tombstones, teardown) must not trust it.
    """

    app: Path
    exists: bool = False
    schema_version: int | None = None
    review_window: dict[str, str] = field(default_factory=dict)
    pages: list[Page] = field(default_factory=list)
    objects: list[ReportingObject] = field(default_factory=list)
    objects_complete: bool = True
    fragments: list[Fragment] = field(default_factory=list)
    nav: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.app / "sql_review" / INDEX_NAME

    @property
    def numbered_pages(self) -> list[Page]:
        """Index pages that appear in the navigation, in navigation order."""
        return sorted((p for p in self.pages if p.number), key=lambda p: p.number)

    def metric_queries(self) -> set[str]:
        return {m.query for p in self.pages for m in p.metrics}


def _rel(app: Path, *parts: str) -> str:
    return "/".join(("apps", app.name, *parts))


def _line_of(text: str, needle: str) -> int:
    """First line of ``text`` containing ``needle`` (1 when absent).

    ``yaml.safe_load`` keeps no positions; a best-effort line is still far more
    useful in a pre-commit failure than ``:1`` on every finding.
    """
    for i, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return i
    return 1


def query_tokens(sql: str) -> list[str]:
    """Distinct ``{TOKEN}`` placeholders in a query, header and comment lines excluded."""
    from .sql_review import template_tokens  # noqa: PLC0415  (import cycle)

    return template_tokens(sql)


def query_binds(sql: str) -> list[str]:
    """Distinct ``:bind`` names (``1`` or ``start_date``) in executable SQL."""
    from .sql_review import _BIND_RE, _mask_strings_and_comments  # noqa: PLC0415  (cycle)

    seen: list[str] = []
    for m in _BIND_RE.finditer(_mask_strings_and_comments(sql)):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def scan_markers(source: str) -> list[tuple[str | None, int]]:
    """``review_value("<key>", …)`` calls in page source: (key or None, line).

    AST only, never an import: ``check`` must not execute app code. A call whose
    first argument is not a string literal yields ``None``, because a computed
    key cannot be matched to the index.
    """
    tree = ast.parse(source)
    found: list[tuple[str | None, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name != MARKER_FUNC:
            continue
        first = node.args[0] if node.args else None
        if first is None:
            for kw in node.keywords:
                if kw.arg == "key":
                    first = kw.value
        key = (
            first.value
            if isinstance(first, ast.Constant) and isinstance(first.value, str)
            else None
        )
        found.append((key, node.lineno))
    return sorted(found, key=lambda kv: kv[1])


def _nav(app: Path, out: list[dict]) -> list[dict]:
    try:
        # extract_nav warns on stderr for dynamic navigation; the coverage
        # finding below names the consequence, so keep the gate output clean.
        with contextlib.redirect_stderr(io.StringIO()):
            return extract_nav(app)
    except FileNotFoundError:
        out.append(
            {
                "kind": KIND_INDEX,
                "file": _rel(app, "streamlit_app.py"),
                "line": 1,
                "detail": "no streamlit_app.py, so page order (and page file numbers) "
                "cannot be derived",
            }
        )
    except SyntaxError as exc:
        out.append(
            {
                "kind": KIND_INDEX,
                "file": _rel(app, "streamlit_app.py"),
                "line": exc.lineno or 1,
                "detail": f"streamlit_app.py is not valid Python ({exc.msg}), so page "
                "order cannot be derived",
            }
        )
    return []


def _str_map(value: object, where: str, problem) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problem(f"{where} must be a mapping")
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(v, str | int | float) or isinstance(v, bool):
            problem(f"{where}[{k!r}] must be a string")
            continue
        out[str(k)] = str(v)
    return out


def _str_list(value: object, where: str, problem) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problem(f"{where} must be a list of strings")
        return []
    return list(value)


def _unknown(entry: dict, allowed: frozenset, where: str, problem) -> None:
    extra = sorted(str(k) for k in entry if k not in allowed)
    if extra:
        problem(f"{where} has unknown key(s) {extra}; allowed: {sorted(allowed)}")


def _query_file(app: Path, rel: str) -> Path | None:
    """Resolve an app-relative query path strictly inside the app, or None."""
    return _contained(app, rel)


def _contained(app: Path, rel: str) -> Path | None:
    """``app / rel`` when it is a regular file inside the app (no symlink), else None."""
    path = app / rel
    if path.is_symlink():
        return None
    try:
        if not path.resolve().is_relative_to(app.resolve()):
            return None
    except OSError:
        return None
    return path if path.is_file() else None


def load_index(app: Path) -> Index:
    """Load and validate ``sql_review/index.yaml`` for the app at ``app``."""
    idx = Index(app=app)
    if not idx.path.is_file():
        return idx
    idx.exists = True
    rel_index = _rel(app, "sql_review", INDEX_NAME)
    try:
        text = idx.path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        idx.findings.append(
            {"kind": KIND_INDEX, "file": rel_index, "line": 1, "detail": f"unreadable: {exc}"}
        )
        idx.objects_complete = False
        return idx

    def problem(detail: str, needle: str | None = None) -> None:
        idx.findings.append(
            {
                "kind": KIND_INDEX,
                "file": rel_index,
                "line": _line_of(text, needle) if needle else 1,
                "detail": detail,
            }
        )

    if not isinstance(data, dict):
        problem("index.yaml must be a mapping (schema_version, app, review_window, pages, …)")
        idx.objects_complete = False
        return idx
    _unknown(data, _TOP_KEYS, "index.yaml", problem)
    idx.schema_version = data.get("schema_version")
    if idx.schema_version != SCHEMA_VERSION:
        problem(
            f"schema_version must be {SCHEMA_VERSION} (got {idx.schema_version!r})",
            "schema_version",
        )
    if data.get("app") != app.name:
        problem(f"app must be {app.name!r} (got {data.get('app')!r})", "app:")

    window = _str_map(data.get("review_window"), "review_window", problem)
    for k, v in window.items():
        if not _WINDOW_KEY_RE.match(k):
            problem(f"review_window key {k!r} must be a lower-case SQL identifier", k)
        elif not v.strip():
            problem(f"review_window[{k!r}] must be a non-empty SQL expression", k)
        else:
            idx.review_window[k] = v.strip()

    nav = _nav(app, idx.findings)
    idx.nav = nav
    nav_pos = {entry["path"]: (i, entry.get("title") or "") for i, entry in enumerate(nav, 1)}

    pages = data.get("pages")
    if pages is None:
        pages = []
    if not isinstance(pages, list):
        problem("pages must be a list of {path, metrics}", "pages")
        pages = []
    seen_paths: set[str] = set()
    used_queries: set[str] = set()
    for pi, entry in enumerate(pages):
        where = f"pages[{pi}]"
        if not isinstance(entry, dict):
            problem(f"{where} must be a mapping with path and metrics")
            continue
        _unknown(entry, _PAGE_KEYS, where, problem)
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            problem(f"{where}.path is required (an st.Page path from streamlit_app.py)")
            continue
        if path in seen_paths:
            problem(f"page {path!r} is listed more than once", path)
            continue
        seen_paths.add(path)
        page = Page(path=path)
        if path in nav_pos:
            page.number, page.title = nav_pos[path]
        elif nav:
            problem(
                f"page {path!r} is not in the app's navigation (streamlit_app.py); "
                "fix the path or remove the page",
                path,
            )
        metrics = entry.get("metrics")
        if metrics is None:
            metrics = []
        if not isinstance(metrics, list):
            problem(f"{where}.metrics must be a list", path)
            metrics = []
        seen_keys: set[str] = set()
        for mi, raw in enumerate(metrics):
            mwhere = f"{path} metrics[{mi}]"
            if not isinstance(raw, dict):
                problem(f"{mwhere} must be a mapping with key and query", path)
                continue
            metric = _load_metric(app, raw, mwhere, problem, idx.review_window)
            if metric is None:
                continue
            if metric.key in seen_keys:
                problem(f"metric key {metric.key!r} appears twice on {path}", metric.key)
                continue
            seen_keys.add(metric.key)
            metric.number = len(page.metrics) + 1
            page.metrics.append(metric)
            used_queries.add(metric.query)
        idx.pages.append(page)

    for entry in nav:
        if entry["path"] not in seen_paths:
            idx.findings.append(
                {
                    "kind": KIND_COVERAGE,
                    "file": rel_index,
                    "line": 1,
                    "detail": f"page {entry['path']!r} ({entry.get('title') or 'untitled'}) "
                    "is in the app's navigation but not in index.yaml; list it under "
                    "pages: (with metrics: [] if it shows no data)",
                }
            )

    _load_objects(data.get("objects"), idx, problem)
    _load_fragments(app, data.get("fragments"), idx, problem, used_queries)
    return idx


def _load_metric(app: Path, raw: dict, where: str, problem, window: dict) -> Metric | None:
    _unknown(raw, _METRIC_KEYS, where, problem)
    key = raw.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        problem(
            f"{where}.key {key!r} must be snake_case and five words or fewer "
            "(e.g. revenue_by_region)",
            str(key) if key else None,
        )
        return None
    query = raw.get("query")
    if not isinstance(query, str) or not _QUERY_PATH_RE.match(query):
        problem(
            f"metric {key!r}: query {query!r} must be an app-relative path like queries/<name>.sql",
            key,
        )
        return None
    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        problem(f"metric {key!r}: notes must be a string", key)
        notes = ""
    metric = Metric(
        key=key,
        query=query,
        tokens=_str_map(raw.get("tokens"), f"metric {key!r} tokens", problem),
        binds=_str_map(raw.get("binds"), f"metric {key!r} binds", problem),
        notes=notes.strip(),
        reads=_str_list(raw.get("reads"), f"metric {key!r} reads", problem),
    )
    for name in metric.reads:
        if not _FQN_RE.match(name):
            problem(f"metric {key!r}: reads entry {name!r} must be DATABASE.SCHEMA.OBJECT", key)
    qpath = _query_file(app, query)
    if qpath is None:
        problem(f"metric {key!r}: {query} does not exist (or resolves outside the app)", key)
        return metric
    try:
        sql = qpath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        problem(f"metric {key!r}: cannot read {query} ({exc})", key)
        return metric

    for tok in metric.tokens:
        if not _TOKEN_KEY_RE.match(tok):
            problem(f"metric {key!r}: token {tok!r} must be UPPER_CASE like the query's {{TOKEN}}")
    used_tokens = query_tokens(sql)
    for tok in used_tokens:
        if tok not in metric.tokens:
            problem(
                f"metric {key!r}: {query} uses {{{tok}}} but tokens: has no sample value for it",
                key,
            )
    for tok in metric.tokens:
        if tok not in used_tokens:
            problem(f"metric {key!r}: token {tok!r} is not used by {query}; remove it", key)

    used_binds = query_binds(sql)
    for b in metric.binds:
        if not _BIND_KEY_RE.match(b):
            problem(f"metric {key!r}: bind {b!r} must be a position (1) or a name (start_date)")
    for b in used_binds:
        if b not in metric.binds:
            problem(
                f"metric {key!r}: {query} uses :{b} but binds: has no value for it "
                "(e.g. params.start_date)",
                key,
            )
    for b, value in metric.binds.items():
        if b not in used_binds:
            problem(f"metric {key!r}: bind {b!r} is not used by {query}; remove it", key)
        ref = _PARAMS_REF_RE.match(value.strip())
        if ref and ref.group(1) not in window:
            problem(
                f"metric {key!r}: bind {b!r} references params.{ref.group(1)}, which "
                "review_window does not define",
                key,
            )
        elif not value.strip():
            problem(f"metric {key!r}: bind {b!r} needs a value", key)
    return metric


def _load_objects(raw: object, idx: Index, problem) -> None:
    if raw is None:
        return
    if not isinstance(raw, list):
        problem("objects must be a list of {name, grants}", "objects")
        idx.objects_complete = False
        return
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            problem(f"objects[{i}] must be a mapping with name and grants")
            idx.objects_complete = False
            continue
        _unknown(entry, _OBJECT_KEYS, f"objects[{i}]", problem)
        name = entry.get("name")
        if not isinstance(name, str) or not _FQN_RE.match(name):
            problem(f"objects[{i}].name {name!r} must be DATABASE.SCHEMA.OBJECT")
            idx.objects_complete = False
            continue
        if name.upper() in seen:
            problem(f"object {name!r} is listed more than once", name)
            # The later entry is dropped, so the loaded list is not what the file says: the
            # app-data loader must fail closed rather than build from the first entry.
            idx.objects_complete = False
            continue
        seen.add(name.upper())
        grants = _str_list(entry.get("grants"), f"object {name!r} grants", problem)
        reason = entry.get("reason", "")
        if not isinstance(reason, str):
            problem(f"object {name!r}: reason must be a string (performance or shared_logic)", name)
            reason = ""
        idx.objects.append(ReportingObject(name=name, grants=grants, reason=reason.strip()))


def _load_fragments(app: Path, raw: object, idx: Index, problem, used: set[str]) -> None:
    """A fragment declaration exempts a query file from coverage, so it is
    validated strictly: an exact queries/ path that exists, with a reason."""
    if raw is None:
        return
    if not isinstance(raw, list):
        problem("fragments must be a list of {file, reason}", "fragments")
        return
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            problem(f"fragments[{i}] must be a mapping with file and reason")
            continue
        _unknown(entry, _FRAGMENT_KEYS, f"fragments[{i}]", problem)
        file = entry.get("file")
        if not isinstance(file, str) or not _QUERY_PATH_RE.match(file):
            problem(f"fragments[{i}].file {file!r} must be a path like queries/<name>.sql")
            continue
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problem(f"fragment {file}: reason is required (why it has no runnable section)", file)
            continue
        if file in seen:
            problem(f"fragment {file} is declared more than once", file)
            continue
        seen.add(file)
        if _query_file(app, file) is None:
            problem(f"fragment {file} does not exist; drop the stale declaration", file)
            continue
        if file in used:
            problem(
                f"fragment {file} is also a metric's query; a query is either runnable "
                "or an inlined fragment, not both",
                file,
            )
            continue
        idx.fragments.append(Fragment(file=file, reason=reason.strip()))
