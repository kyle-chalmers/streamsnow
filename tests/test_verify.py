"""Tests for post-deploy health verification (streamsnow/verify.py).

All checks are pure functions over canned SHOW/log rows — no snow CLI, no
network. verify_app gets an injected run_query and a no-op sleep.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from streamsnow.config import Config
from streamsnow.verify import (
    check_exists,
    check_live_version,
    check_service_logs,
    check_version_source,
    verify_app,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"
FQN = "DB.SCH.MY_APP"


def _cfg() -> Config:
    return Config.from_dict(yaml.safe_load(EXAMPLE.read_text()))


def _healthy_row(sha: str = "abc1234") -> dict:
    return {
        "name": "MY_APP",
        "live_version_location_uri": f"stage://commits/{sha}/apps/my-app/",
        "default_version_source_location_uri": f"@ST/commits/{sha}/apps/my-app/",
    }


# ---- exists ----------------------------------------------------------------


def test_exists_pass_and_fail():
    assert check_exists(_healthy_row(), FQN)["ok"]
    res = check_exists(None, FQN)
    assert not res["ok"]
    assert "not found" in res["findings"][0]


# ---- live-version ----------------------------------------------------------


def test_live_version_null_fails_with_fix_sql():
    res = check_live_version({"live_version_location_uri": None}, FQN)
    assert not res["ok"]
    assert "ADD LIVE VERSION FROM LAST" in res["findings"][0]


def test_live_version_present_passes():
    assert check_live_version(_healthy_row(), FQN)["ok"]


def test_live_version_column_absent_warn_skips():
    res = check_live_version({"name": "MY_APP"}, FQN)
    assert res["ok"]
    assert res["level"] == "warn"


# ---- version-source --------------------------------------------------------


def test_version_source_matching_sha_passes():
    assert check_version_source(_healthy_row("deadbee"), FQN, "deadbee")["ok"]


def test_version_source_wrong_sha_fails():
    res = check_version_source(_healthy_row("deadbee"), FQN, "cafef00")
    assert not res["ok"]
    assert "/commits/cafef00" in res["findings"][0]


def test_version_source_no_uri_columns_warn_skips():
    res = check_version_source({"name": "MY_APP"}, FQN, "deadbee")
    assert res["ok"]
    assert res["level"] == "warn"


# ---- service-logs ----------------------------------------------------------


def test_service_logs_crash_signature_fails():
    res = check_service_logs("Error: No such option: --server.someNewFlag\n", FQN)
    assert not res["ok"]
    assert "no such option" in res["findings"][0].lower()


def test_service_logs_restart_loop_fails():
    tail = "You can now view your Streamlit app\n" * 3
    res = check_service_logs(tail, FQN)
    assert not res["ok"]
    assert "restart loop" in res["findings"][0]


def test_service_logs_single_start_banner_passes():
    assert check_service_logs("You can now view your Streamlit app in your browser.\n", FQN)["ok"]


def test_service_logs_unavailable_warn_skips():
    res = check_service_logs(None, FQN)
    assert res["ok"]
    assert res["level"] == "warn"


# ---- verify_app orchestration ----------------------------------------------


def _run_query_factory(show_rows_by_attempt: list[list[dict]], services: list[dict] | None = None):
    """Return a run_query stub that serves SHOW STREAMLITS per attempt."""
    state = {"i": 0}

    def run_query(sql: str) -> list[dict]:
        s = sql.upper()
        if s.startswith("SHOW STREAMLITS"):
            i = min(state["i"], len(show_rows_by_attempt) - 1)
            state["i"] += 1
            return show_rows_by_attempt[i]
        if s.startswith("SHOW SERVICES"):
            return services or []
        if "GET_SERVICE_LOGS" in s:
            return [{"LOG": "You can now view your Streamlit app\n"}]
        raise AssertionError(f"unexpected query: {sql}")

    return run_query


def test_verify_app_passes_on_healthy_deploy():
    cfg = _cfg()
    result = verify_app(
        cfg,
        "my-app",
        sha="abc1234",
        run_query=_run_query_factory([[_healthy_row("abc1234")]]),
        sleep=lambda _: None,
    )
    assert result["ok"], result["checks"]
    names = [c["name"] for c in result["checks"]]
    assert names[:2] == ["exists", "live-version"]
    assert "version-source" in names  # example config is stage-copy
    assert "service-logs" in names  # example config is container runtime


def test_verify_app_retries_through_cold_start():
    cfg = _cfg()
    slept: list[float] = []
    # First attempt: object not visible yet; second attempt: healthy.
    result = verify_app(
        cfg,
        "my-app",
        sha="abc1234",
        run_query=_run_query_factory([[], [_healthy_row("abc1234")]]),
        attempts=3,
        delay=5.0,
        sleep=slept.append,
    )
    assert result["ok"], result["checks"]
    assert slept == [5.0]


def test_verify_app_fails_after_exhausted_retries():
    cfg = _cfg()
    result = verify_app(
        cfg,
        "my-app",
        run_query=_run_query_factory([[]]),
        attempts=2,
        sleep=lambda _: None,
    )
    assert not result["ok"]
    by_name = {c["name"]: c for c in result["checks"]}
    assert not by_name["exists"]["ok"]


def test_verify_app_query_error_is_a_block_finding():
    cfg = _cfg()

    def boom(sql: str) -> list[dict]:
        raise RuntimeError("snow sql failed")

    result = verify_app(cfg, "my-app", run_query=boom, attempts=1, sleep=lambda _: None)
    assert not result["ok"]
    assert "could not query" in result["checks"][0]["findings"][0]


def test_verify_app_skips_version_source_without_sha():
    cfg = _cfg()
    result = verify_app(
        cfg,
        "my-app",
        run_query=_run_query_factory([[_healthy_row()]]),
        sleep=lambda _: None,
    )
    assert "version-source" not in [c["name"] for c in result["checks"]]


def test_verify_app_service_log_fetch_failure_is_warn_not_fail():
    cfg = _cfg()

    def run_query(sql: str) -> list[dict]:
        s = sql.upper()
        if s.startswith("SHOW STREAMLITS"):
            return [_healthy_row("abc1234")]
        raise RuntimeError("no SHOW SERVICES privilege")

    result = verify_app(cfg, "my-app", sha="abc1234", run_query=run_query, sleep=lambda _: None)
    assert result["ok"], result["checks"]
    logs = next(c for c in result["checks"] if c["name"] == "service-logs")
    assert logs["level"] == "warn"
