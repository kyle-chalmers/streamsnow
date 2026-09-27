"""Post-deploy health verification — because "deploy succeeded" ≠ "app serves".

Three production failure modes motivate this module, all invisible to a deploy
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

All Snowflake access goes through an injected ``run_query`` callable so the
check logic stays pure and unit-testable.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable

from .config import Config
from .deploy import streamlit_fqn

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
        argv,
        capture_output=True,
        text=True,
        timeout=120,
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


def _check(name: str, status: str, findings: list[str], level: str = "block") -> dict:
    """One check result. ``ok`` is true only for a check that ran and passed."""
    return {
        "name": name,
        "status": status,
        "ok": status == PASS,
        "level": level,
        "findings": findings,
    }


def _skipped(name: str, why: str) -> dict:
    return _check(name, SKIPPED, [f"not checked: {why}"], level="warn")


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
# '@<stage>/commits/<sha>/apps/<slug>/', and DESCRIBE reports that path in both
# source columns.
_SOURCE_URI_KEYS = ("default_version_source_location_uri", "last_version_source_location_uri")
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
    viewers see stale code. (The git-repository source pins freshness via
    fetch+PULL instead; callers skip this check there.) No DESCRIBE row, or
    neither source column in it, means the check could not run: skipped."""
    if desc is None:
        return _skipped("version-source", "DESCRIBE STREAMLIT returned no row")
    if not any(_has(desc, k) for k in _SOURCE_URI_KEYS):
        return _skipped("version-source", "no version-source URI columns in DESCRIBE STREAMLIT")
    uris = [str(_get(desc, k)) for k in _SOURCE_URI_KEYS if _get(desc, k)]
    if any(_points_at(u, sha) for u in uris):
        return _check("version-source", PASS, [])
    return _check(
        "version-source",
        FAIL,
        [
            f"{fqn}: no version-source URI contains '/commits/{sha}/', so the deployed "
            f"object does not point at the merged commit (saw: {uris or 'no source URI'})"
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


def verify_app(
    cfg: Config,
    slug: str,
    sha: str | None = None,
    run_query: RunQuery = run_query_snow,
    attempts: int = 3,
    delay: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Run all post-deploy checks for one app; retries exists/live-version to
    absorb container cold start. Returns ``{"app", "ok", "checks"}``, where
    ``ok`` means no check failed (a skipped check is reported, not failed)."""
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
    if sha and cfg.deploy.source == "stage-copy":
        blocker = _describe_blocker(exists, describe_error)
        checks.append(
            _skipped("version-source", blocker) if blocker else check_version_source(desc, fqn, sha)
        )

    if cfg.runtime == "container":
        checks.append(check_service_logs(_fetch_service_logs(cfg, slug, run_query), fqn))

    return {"app": slug, "ok": not any(c["status"] == FAIL for c in checks), "checks": checks}


def summary_line(result: dict) -> str:
    """``PASS: <app> (3 passed; 1 skipped: service-logs)``. Skipped checks are
    counted and named on their own, never folded into the passes."""
    by_status: dict[str, list[str]] = {PASS: [], FAIL: [], SKIPPED: []}
    for c in result["checks"]:
        by_status[c["status"]].append(c["name"])
    parts = [f"{len(by_status[PASS])} passed"]
    for status, word in ((FAIL, "failed"), (SKIPPED, "skipped")):
        if by_status[status]:
            parts.append(f"{len(by_status[status])} {word}: {', '.join(by_status[status])}")
    verdict = "PASS" if result["ok"] else "FAIL"
    return f"{verdict}: {result['app']} ({'; '.join(parts)})"
