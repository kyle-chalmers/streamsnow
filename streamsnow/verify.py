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


def run_query_snow(sql: str) -> list[dict]:
    """Run one statement via the ``snow`` CLI and return rows as dicts.

    ``snow sql --format json`` prints a JSON array of row objects for a single
    statement (an array of arrays for multi-statement input — flattened here).
    """
    proc = subprocess.run(  # noqa: S603 - sql comes from validated config values
        ["snow", "sql", "-q", sql, "--format", "json"],
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


def _show_streamlit(cfg: Config, slug: str, run_query: RunQuery) -> dict | None:
    o = cfg.snowflake.objects
    name = streamlit_fqn(cfg, slug).rsplit(".", 1)[-1]
    rows = run_query(f"SHOW STREAMLITS LIKE '{name}' IN SCHEMA {o.app_database}.{o.app_schema}")
    return rows[0] if rows else None


def check_exists(row: dict | None, fqn: str) -> dict:
    ok = row is not None
    return {
        "name": "exists",
        "ok": ok,
        "level": "block",
        "findings": [] if ok else [f"{fqn} not found — the deploy did not create the object"],
    }


def check_live_version(row: dict | None, fqn: str) -> dict:
    """A NULL/empty ``live_version_location_uri`` means Snowsight cannot render
    the app even though it exists. The column being absent from SHOW output is a
    warn-skip (edition/version differences), never a hard fail."""
    if row is None:
        return {"name": "live-version", "ok": False, "level": "block", "findings": ["no row"]}
    if not any(k.lower() == "live_version_location_uri" for k in row):
        return {
            "name": "live-version",
            "ok": True,
            "level": "warn",
            "findings": ["live_version_location_uri not in SHOW output — skipped"],
        }
    uri = _get(row, "live_version_location_uri")
    ok = bool(uri) and str(uri).lower() not in ("null", "none")
    return {
        "name": "live-version",
        "ok": ok,
        "level": "block",
        "findings": []
        if ok
        else [
            f"{fqn} has no live version — the app exists but Snowsight cannot render it. "
            f"Fix: ALTER STREAMLIT {fqn} ADD LIVE VERSION FROM LAST;"
        ],
    }


def check_version_source(row: dict | None, fqn: str, sha: str) -> dict:
    """Stage-copy: some version-source URI must contain ``/commits/<sha>/`` —
    otherwise the object points at an old stage path and viewers see stale code.
    (The git-repository source pins freshness via fetch+PULL instead; callers
    skip this check there.) Absent URI columns warn-skip."""
    if row is None:
        return {"name": "version-source", "ok": False, "level": "block", "findings": ["no row"]}
    uri_keys = (
        "default_version_source_location_uri",
        "live_version_location_uri",
        "root_location",
    )
    uris = [str(_get(row, k)) for k in uri_keys if _get(row, k)]
    if not uris:
        return {
            "name": "version-source",
            "ok": True,
            "level": "warn",
            "findings": ["no version-source URI columns in SHOW output — skipped"],
        }
    needle = f"/commits/{sha}"
    ok = any(needle in u for u in uris)
    return {
        "name": "version-source",
        "ok": ok,
        "level": "block",
        "findings": []
        if ok
        else [
            f"{fqn}: no version-source URI contains {needle!r} — the deployed object "
            f"does not point at the merged commit (saw: {uris})"
        ],
    }


def check_service_logs(log_text: str | None, fqn: str) -> dict:
    """Scan a container service log tail for crash-loop signatures. ``None``
    (logs unavailable) is a warn-skip — this check is strictly best-effort."""
    if log_text is None:
        return {
            "name": "service-logs",
            "ok": True,
            "level": "warn",
            "findings": ["service logs unavailable — skipped (best-effort check)"],
        }
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
    return {"name": "service-logs", "ok": not findings, "level": "block", "findings": findings}


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
    absorb container cold start. Returns ``{"app", "ok", "checks"}``."""
    fqn = streamlit_fqn(cfg, slug)
    row: dict | None = None
    checks: list[dict] = []
    for attempt in range(attempts):
        try:
            row = _show_streamlit(cfg, slug, run_query)
        except Exception as exc:
            row = None
            if attempt == attempts - 1:
                return {
                    "app": slug,
                    "ok": False,
                    "checks": [
                        {
                            "name": "exists",
                            "ok": False,
                            "level": "block",
                            "findings": [f"could not query {fqn}: {exc}"],
                        }
                    ],
                }
        exists = check_exists(row, fqn)
        live = check_live_version(row, fqn)
        if (exists["ok"] and live["ok"]) or attempt == attempts - 1:
            checks = [exists, live]
            break
        sleep(delay)  # container cold start after a version bump takes 1–3 min

    if sha and cfg.deploy.source == "stage-copy":
        checks.append(check_version_source(row, fqn, sha))

    if cfg.runtime == "container":
        checks.append(check_service_logs(_fetch_service_logs(cfg, slug, run_query), fqn))

    return {"app": slug, "ok": all(c["ok"] for c in checks), "checks": checks}
