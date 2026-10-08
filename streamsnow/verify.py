"""Post-deploy health verification — because "deploy succeeded" ≠ "app serves".

Four production failure modes motivate this module, all invisible to a deploy
pipeline that stops at "the SQL ran":

1. **No live version** — an app whose ``live_version_location_uri`` is NULL
   exists in Snowflake but renders nothing in Snowsight. ``ADD LIVE VERSION
   FROM LAST`` is emitted by the generated deploy SQL, but a hand-rolled or
   interrupted deploy can skip it silently.
2. **Wrong source** — the object deployed fine but points at an old stage path
   or an unfetched branch, so viewers see stale code. Cross-checking the
   version-source URI against the merge SHA catches the drift.
3. **Container crash-loop** — a container app can crash-loop on startup (e.g.
   the base image passes a launcher flag the pinned Streamlit version rejects)
   while the backing service still reports healthy. Only the service logs show
   the ``No such option`` signature.
4. **Stalled dynamic table** — a dynamic table in the app's app data that was
   suspended (or never refreshed) keeps serving its last rows with no error
   anywhere. Warn-only: the app itself deployed fine.

Checks 1–2 retry to absorb the 1–3 minute container cold start that follows a
version bump; the log scan (3) is strictly best-effort and fail-open — log
access varies by role and edition, and a verification step must never block a
deploy over its own permissions.

``SHOW STREAMLITS`` answers "does it exist"; checks 1 and 2 read ``DESCRIBE
STREAMLIT``. SHOW used to carry the version URIs, but current Snowflake returns
only artifact_repositories, comment, created_on, database_name,
idle_auto_shutdown_time_seconds, name, owner, owner_role_type, query_warehouse,
scheduled_tasks, schema_name, title and url_id (observed 2026-09-27), so both
checks printed a check mark while reporting they had been skipped.

Every check reports a ``status``: ``pass``, ``fail`` or ``skipped``. A check
that could not run is ``skipped``, never ``pass``: its ``ok`` is false and the
summary line counts it apart from the passes. Skips do not fail the run (the
exit code tracks failures only), but they are never shown as a pass.

Each check also carries a ``level``. A ``block`` check that fails fails the
run. A ``warn`` check that fails is reported and counted as a warning, and the
run still passes: ``stage-files`` is warn-only so a repo whose stage-copy
workflow predates ``stage-bundle`` keeps passing while it is told to update.

All Snowflake access goes through an injected ``run_query`` callable so the
check logic stays pure and unit-testable.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .app_data import is_plain_fqn
from .config import Config
from .deploy import _safe_sha, stage_path, streamlit_fqn
from .stage_bundle import (
    BundleError,
    app_artifact_entries,
    excluded_reason,
    select_app_files,
)

RunQuery = Callable[[str], list[dict]]

# Startup-log signatures that mean the container is crash-looping, not serving.
_CRASH_SIGNATURES = (
    "no such option",  # launcher flag rejected by the pinned Streamlit version
    "traceback (most recent call last)",
    "modulenotfounderror",
)
# N+ repeated Streamlit start banners in one log tail = restart loop.
_START_BANNER = re.compile(r"you can now view your streamlit app", re.IGNORECASE)
_RESTART_LOOP_THRESHOLD = 3


def run_query_snow(sql: str, *, temporary_connection: bool = False) -> list[dict]:
    """Run one statement via the ``snow`` CLI and return rows as dicts.

    ``snow sql --format json`` prints a JSON array of row objects for a single
    statement (an array of arrays for multi-statement input — flattened here).

    ``temporary_connection`` adds ``--temporary-connection``, so ``snow`` builds
    the connection from ``SNOWFLAKE_*`` environment variables instead of a named
    connection in ``config.toml``. The generated deploy workflow asks for it
    because a CI runner has no ``config.toml``; a local run leaves it off and
    uses the default connection.
    """
    argv = ["snow", "sql", "-q", sql, "--format", "json"]
    if temporary_connection:
        argv.append("--temporary-connection")
    proc = subprocess.run(  # noqa: S603 - sql comes from validated config values
        argv, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise RuntimeError(f"snow sql failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
    data = json.loads(proc.stdout or "[]")
    if isinstance(data, list) and data and isinstance(data[0], list):
        data = data[0]
    return [row for row in data if isinstance(row, dict)]


def _get(row: dict, key: str) -> object:
    """Case-insensitive column lookup (snow JSON casing varies by version)."""
    for k, v in row.items():
        if k.lower() == key:
            return v
    return None


def _has(row: dict, key: str) -> bool:
    return any(k.lower() == key for k in row)


PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"


BLOCK = "block"
WARN = "warn"


def _check(name: str, status: str, findings: list[str], level: str = BLOCK) -> dict:
    """One check result. ``ok`` is true only for a check that ran and passed.
    ``level`` is ``block`` (a failure fails the run) or ``warn`` (a failure is
    reported as a warning and the run still passes)."""
    return {
        "name": name,
        "status": status,
        "ok": status == PASS,
        "level": level,
        "findings": findings,
    }


def _skipped(name: str, why: str) -> dict:
    return _check(name, SKIPPED, [f"not checked: {why}"], level=WARN)


def _blocks(check: dict) -> bool:
    """A failed check that fails the run (a failed warn-level check does not)."""
    return check["status"] == FAIL and check.get("level", BLOCK) == BLOCK


def _show_streamlit(cfg: Config, slug: str, run_query: RunQuery) -> dict | None:
    o = cfg.snowflake.objects
    name = streamlit_fqn(cfg, slug).rsplit(".", 1)[-1]
    rows = run_query(f"SHOW STREAMLITS LIKE '{name}' IN SCHEMA {o.app_database}.{o.app_schema}")
    return rows[0] if rows else None


def _describe_streamlit(fqn: str, run_query: RunQuery) -> dict | None:
    """The ``DESCRIBE STREAMLIT`` row, which carries the live and version-source
    URIs that ``SHOW STREAMLITS`` no longer returns."""
    rows = run_query(f"DESCRIBE STREAMLIT {fqn}")
    return rows[0] if rows else None


def check_exists(row: dict | None, fqn: str) -> dict:
    if row is not None:
        return _check("exists", PASS, [])
    return _check("exists", FAIL, [f"{fqn} not found: the deploy did not create the object"])


def check_live_version(desc: dict | None, fqn: str) -> dict:
    """A NULL/empty ``live_version_location_uri`` in ``DESCRIBE STREAMLIT`` means
    Snowsight cannot render the app even though it exists. No DESCRIBE row, or
    no such column in it, means the check could not run: skipped, never a pass."""
    if desc is None:
        return _skipped("live-version", "DESCRIBE STREAMLIT returned no row")
    if not _has(desc, "live_version_location_uri"):
        return _skipped("live-version", "no live_version_location_uri in DESCRIBE STREAMLIT output")
    uri = _get(desc, "live_version_location_uri")
    if uri and str(uri).strip().lower() not in ("null", "none"):
        return _check("live-version", PASS, [])
    return _check(
        "live-version",
        FAIL,
        [
            f"{fqn} has no live version, so the app exists but Snowsight cannot render it. "
            f"Fix: ALTER STREAMLIT {fqn} ADD LIVE VERSION FROM LAST;"
        ],
    )


# The stage-copy deploy runs CREATE OR REPLACE STREAMLIT ... FROM
# '@<stage>/commits/<sha>/apps/<slug>/', then ADD LIVE VERSION FROM LAST, so the
# live version is built from LAST: last_version_source_location_uri is the column
# that must point at the merged commit. default_version_source_location_uri is
# read only when the last column is missing (observed: both carry the stage path).
_LAST_SOURCE_KEY = "last_version_source_location_uri"
_DEFAULT_SOURCE_KEY = "default_version_source_location_uri"
_COMMIT_SEGMENT = re.compile(r"/commits/([0-9A-Za-z]+)/")


def _points_at(uri: str, sha: str) -> bool:
    """True when ``uri`` has a ``/commits/<segment>/`` path for this commit. A
    short SHA matches as a prefix of the segment, so a local run can pass the
    abbreviated SHA that git prints."""
    want = sha.lower()
    return any(seg.lower().startswith(want) for seg in _COMMIT_SEGMENT.findall(uri))


def check_version_source(desc: dict | None, fqn: str, sha: str) -> dict:
    """Stage-copy: a version-source URI in ``DESCRIBE STREAMLIT`` must contain
    ``/commits/<sha>/``, otherwise the object points at an old stage path and
    viewers see stale code. (The git-repository source is checked by
    ``check_git_commit`` instead.) No DESCRIBE row, or
    neither source column in it, means the check could not run: skipped."""
    if desc is None:
        return _skipped("version-source", "DESCRIBE STREAMLIT returned no row")
    key = next((k for k in (_LAST_SOURCE_KEY, _DEFAULT_SOURCE_KEY) if _has(desc, k)), None)
    if key is None:
        return _skipped("version-source", "no version-source URI columns in DESCRIBE STREAMLIT")
    uris = [str(_get(desc, key))] if _get(desc, key) else []
    if any(_points_at(u, sha) for u in uris):
        return _check("version-source", PASS, [])
    return _check(
        "version-source",
        FAIL,
        [
            f"{fqn}: {key} does not contain '/commits/{sha}/', so the live version "
            f"was not built from the merged commit (saw: {uris or 'no source URI'})"
        ],
    )


# The git-repository deploy runs CREATE OR REPLACE STREAMLIT ... FROM
# '@<repo>/branches/<branch>/apps/<slug>/' (Snowflake rejects a /commits/<sha>/
# path there), so the source URI names a branch, not a commit. DESCRIBE reports
# the commit the branch pointed at in last_version_git_commit_hash (observed
# 2026-10-03, with default_version_git_commit_hash carrying the same value).
_LAST_GIT_KEY = "last_version_git_commit_hash"
_DEFAULT_GIT_KEY = "default_version_git_commit_hash"


def check_git_commit(desc: dict | None, fqn: str, sha: str) -> dict:
    """Git-repository: the commit hash in ``DESCRIBE STREAMLIT`` must be this
    deploy's commit. A different hash means the branch moved before
    ``snow git fetch`` ran (the newer commit's own deploy will ship it) or the
    fetch did not pick up the merge, so viewers may not see this change. A short
    SHA matches as a prefix. No row or no hash column: skipped."""
    if desc is None:
        return _skipped("version-source", "DESCRIBE STREAMLIT returned no row")
    key = next((k for k in (_LAST_GIT_KEY, _DEFAULT_GIT_KEY) if _has(desc, k)), None)
    if key is None:
        return _skipped("version-source", "no git commit hash column in DESCRIBE STREAMLIT")
    got = str(_get(desc, key) or "")
    if len(sha) < 7:
        return _check(
            "version-source", FAIL, [f"{fqn}: --sha {sha!r} is too short to identify a commit"]
        )
    if got and got.lower().startswith(sha.lower()):
        return _check("version-source", PASS, [])
    return _check(
        "version-source",
        FAIL,
        [
            f"{fqn}: {key} is {got or 'empty'}, not {sha}: the live version was not built "
            "from this commit. If a newer commit reached the branch before `snow git fetch`, "
            "its own deploy run ships it; otherwise check that the fetch succeeded."
        ],
    )


def check_service_logs(log_text: str | None, fqn: str) -> dict:
    """Scan a container service log tail for crash-loop signatures. ``None``
    (logs unavailable) is skipped, not passed: this check is strictly
    best-effort, so a skip never fails the run."""
    if log_text is None:
        return _skipped(
            "service-logs",
            "service logs unavailable: no single matching service, or no log access "
            "(best-effort check)",
        )
    lowered = log_text.lower()
    findings = [
        f"{fqn} service log contains {sig!r} — startup failure signature"
        for sig in _CRASH_SIGNATURES
        if sig in lowered
    ]
    banners = len(_START_BANNER.findall(log_text))
    if banners >= _RESTART_LOOP_THRESHOLD:
        findings.append(
            f"{fqn} service log shows {banners} Streamlit start banners in one tail — "
            "restart loop (the service can report healthy while the app crash-loops)"
        )
    return _check("service-logs", FAIL if findings else PASS, findings)


# The stage-copy deploy uploads to '@<stage>/commits/<sha>/apps/<slug>/'. LIST
# names each file with the stage name first (observed lowercased), so the
# app-relative path is whatever follows '<sha>/apps/<slug>/'.
_FULL_SHA_LEN = 40


def _list_stage_files(cfg: Config, slug: str, sha: str, run_query: RunQuery) -> list[str]:
    """App-relative POSIX paths the stage holds for this app at this commit."""
    marker = f"{sha.lower()}/apps/{slug}/"
    rows = run_query(f"LIST '{stage_path(cfg)}/commits/{sha}/apps/{slug}/'")
    out: list[str] = []
    for row in rows:
        name = str(_get(row, "name") or "")
        i = name.lower().find(marker)
        if i >= 0 and name[i + len(marker) :]:
            out.append(name[i + len(marker) :])
    return sorted(out)


def check_stage_files(listed: list[str], app_dir: Path, stage_dir: str) -> dict:
    """Warn-only: compare the files on the stage with what the deploy bundle ships.

    Two drifts are reported. Each file ``stage-bundle`` would ship for this
    app (computed from the local app, nothing written) must be on the stage;
    one missing means the deployed app cannot import or read it. The
    comparison is per file: checking that each artifacts entry matched some
    staged file let one staged page satisfy all of ``pages/``. A staged file that
    ``stage-bundle`` would leave out (AGENTS.md, sql_review/, ...) means the
    workflow still uploads the whole ``apps/`` tree, so internal docs sit on
    the stage at every commit. Warn-level so a repo on the old workflow is
    told, not failed. An app holding a symlink that resolves outside the repo
    is reported too: ``stage-bundle`` refuses it, so the next deploy fails.
    """
    entries = app_artifact_entries(app_dir)
    try:
        expected, _ = select_app_files(app_dir)
    except BundleError as exc:
        return _check("stage-files", FAIL, [f"stage-bundle refuses this app: {exc}"], level=WARN)
    on_stage = set(listed)
    missing = [rel for rel in sorted(expected) if rel not in on_stage]
    findings: list[str] = [
        f"{m} is not on the stage at {stage_dir}: the deployed app cannot load it" for m in missing
    ]
    for rel in listed:
        reason = excluded_reason(rel, entries)
        if reason:
            findings.append(
                f"{rel} is on the stage but the deploy bundle leaves it out ({reason}). "
                "Re-render deploy.yml with `streamsnow update --apply` so the workflow "
                "uploads `streamsnow stage-bundle` output instead of apps/"
            )
    return _check("stage-files", FAIL if findings else PASS, findings, level=WARN)


def _stage_files(cfg: Config, slug: str, sha: str, app_dir: Path, run_query: RunQuery) -> dict:
    if cfg.deploy.source != "stage-copy":
        return _skipped(
            "stage-files",
            "the git-repository source builds from the committed repo folder, so there is "
            "no uploaded stage listing to check",
        )
    try:
        _safe_sha(sha)
    except ValueError as exc:
        return _skipped("stage-files", str(exc))
    if len(sha) < _FULL_SHA_LEN:
        return _skipped("stage-files", "needs the full commit SHA to find the staged files")
    stage_dir = f"{stage_path(cfg)}/commits/{sha}/apps/{slug}/"
    try:
        listed = _list_stage_files(cfg, slug, sha, run_query)
    except Exception as exc:
        return _skipped("stage-files", f"LIST {stage_dir} failed: {exc}")
    return check_stage_files(listed, app_dir, stage_dir)


def _fetch_service_logs(cfg: Config, slug: str, run_query: RunQuery) -> str | None:
    """Best-effort container log fetch. Any failure returns None (warn-skip)."""
    try:
        name = streamlit_fqn(cfg, slug).rsplit(".", 1)[-1]
        o = cfg.snowflake.objects
        services = run_query(
            f"SHOW SERVICES LIKE '%{name}%' IN SCHEMA {o.app_database}.{o.app_schema}"
        )
        if len(services) != 1:
            return None
        svc = _get(services[0], "name")
        db = _get(services[0], "database_name") or o.app_database
        schema = _get(services[0], "schema_name") or o.app_schema
        rows = run_query(
            f"SELECT SYSTEM$GET_SERVICE_LOGS('{db}.{schema}.{svc}', 0, 'streamlit', 200) AS log"
        )
        return str(_get(rows[0], "log")) if rows else None
    except Exception:
        return None


def _describe_blocker(exists: dict, describe_error: str) -> str:
    """Why the DESCRIBE-based checks cannot run, or "" when they can."""
    if not exists["ok"]:
        return "the app does not exist"
    return describe_error


def _show_dynamic_table(fqn: str, run_query: RunQuery) -> dict | None:
    """The ``SHOW DYNAMIC TABLES`` row for one app-data table. Only a plain three-part
    name is rendered into SQL (anything else raises, so the check is skipped); the
    exact-name match drops LIKE's ``_`` wildcard."""
    if not is_plain_fqn(fqn):
        raise ValueError(f"{fqn!r} is not a plain DB.SCHEMA.NAME")
    database, schema, name = fqn.split(".")
    rows = run_query(f"SHOW DYNAMIC TABLES LIKE '{name}' IN SCHEMA {database}.{schema}")
    return next((r for r in rows if str(_get(r, "name") or "").upper() == name.upper()), None)


#: Healthy ``scheduling_state`` values: the docs say RUNNING; ACTIVE is accepted as well.
#: SUSPENDED, FAILED, empty or anything else means the table stopped refreshing.
_SCHEDULED = frozenset({"RUNNING", "ACTIVE"})


def check_app_data_refresh(found: dict[str, dict | None]) -> dict:
    """Warn when a dynamic table this app reads is not refreshing (#79).

    The deploy created it with ``INITIALIZE = ON_CREATE``, so it held data then
    (a later ``CREATE OR ALTER`` does not refresh it at once; the next refresh
    follows its target lag). A suspended table (Snowflake suspends one after
    repeated refresh failures, or someone ran SUSPEND) or one with no
    ``data_timestamp`` stops changing, and the app keeps serving its last rows
    with no error anywhere. Warn-level: the app itself deployed fine.
    Docs: https://docs.snowflake.com/en/sql-reference/sql/show-dynamic-tables
    """
    findings: list[str] = []
    for fqn, row in found.items():
        if row is None:
            findings.append(
                f"{fqn} not found: the deploy job's objects-sql step did not build it, or the "
                "CI role cannot see it"
            )
            continue
        state = str(_get(row, "scheduling_state") or "").strip().upper()
        if state not in _SCHEDULED:
            findings.append(
                f"{fqn} scheduling_state is {state or 'empty'}, not RUNNING: it no longer "
                f"refreshes, so pages show stale data. Fix the cause, then run "
                f"ALTER DYNAMIC TABLE {fqn} RESUME;"
            )
        ts = _get(row, "data_timestamp")
        if not ts or str(ts).strip().lower() in ("null", "none"):
            findings.append(f"{fqn} has never refreshed (no data_timestamp): pages read nothing")
    return _check("app-data-refresh", FAIL if findings else PASS, findings, level=WARN)


def verify_app(
    cfg: Config,
    slug: str,
    sha: str | None = None,
    run_query: RunQuery = run_query_snow,
    attempts: int = 3,
    delay: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    app_dir: Path | None = None,
    app_data_tables: Sequence[str] = (),
) -> dict:
    """Run all post-deploy checks for one app; retries exists/live-version to
    absorb container cold start. Returns ``{"app", "ok", "checks"}``, where
    ``ok`` means no block-level check failed (a skipped check, or a failed
    warn-level one, is reported, not failed). ``app_dir`` (the local
    ``apps/<slug>``) with ``sha`` adds the warn-only ``stage-files`` check.
    ``app_data_tables`` (the dynamic tables this app owns in app data) adds the
    warn-only ``app-data-refresh`` check."""
    fqn = streamlit_fqn(cfg, slug)
    attempts = max(1, attempts)
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            row = _show_streamlit(cfg, slug, run_query)
        except Exception as exc:
            row = None
            if last:
                return {
                    "app": slug,
                    "ok": False,
                    "checks": [_check("exists", FAIL, [f"could not query {fqn}: {exc}"])],
                }
        exists = check_exists(row, fqn)
        desc, describe_error = None, ""
        if exists["ok"]:
            try:
                desc = _describe_streamlit(fqn, run_query)
            except Exception as exc:
                describe_error = f"DESCRIBE STREAMLIT {fqn} failed: {exc}"
        blocker = _describe_blocker(exists, describe_error)
        live = _skipped("live-version", blocker) if blocker else check_live_version(desc, fqn)
        # A missing column will not appear on a retry; a failed live version or
        # a DESCRIBE error can, during the cold start that follows a version bump.
        if last or (exists["ok"] and live["status"] != FAIL and not describe_error):
            break
        sleep(delay)  # container cold start after a version bump takes 1 to 3 min

    checks = [exists, live]
    if sha:
        blocker = _describe_blocker(exists, describe_error)
        check = check_version_source if cfg.deploy.source == "stage-copy" else check_git_commit
        checks.append(_skipped("version-source", blocker) if blocker else check(desc, fqn, sha))
        if app_dir is not None:
            checks.append(_stage_files(cfg, slug, sha, app_dir, run_query))

    if app_data_tables:
        try:
            found = {fqn: _show_dynamic_table(fqn, run_query) for fqn in app_data_tables}
            checks.append(check_app_data_refresh(found))
        except Exception as exc:  # a check that cannot run is skipped, never passed
            checks.append(_skipped("app-data-refresh", f"SHOW DYNAMIC TABLES failed: {exc}"))

    if cfg.runtime == "container":
        checks.append(check_service_logs(_fetch_service_logs(cfg, slug, run_query), fqn))

    return {"app": slug, "ok": not any(_blocks(c) for c in checks), "checks": checks}


def summary_line(result: dict) -> str:
    """``PASS: <app> (3 passed; 1 skipped: service-logs)``. Skipped checks, and
    failed warn-level checks (``1 warned: stage-files``), are counted and named
    on their own, never folded into the passes or the failures."""
    warned = "warned"
    by_status: dict[str, list[str]] = {PASS: [], FAIL: [], warned: [], SKIPPED: []}
    for c in result["checks"]:
        key = warned if c["status"] == FAIL and not _blocks(c) else c["status"]
        by_status[key].append(c["name"])
    parts = [f"{len(by_status[PASS])} passed"]
    for status, word in ((FAIL, "failed"), (warned, "warned"), (SKIPPED, "skipped")):
        if by_status[status]:
            parts.append(f"{len(by_status[status])} {word}: {', '.join(by_status[status])}")
    verdict = "PASS" if result["ok"] else "FAIL"
    return f"{verdict}: {result['app']} ({'; '.join(parts)})"
