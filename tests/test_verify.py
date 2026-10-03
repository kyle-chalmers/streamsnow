"""Tests for post-deploy health verification (streamsnow/verify.py).

All checks are pure functions over canned SHOW/DESCRIBE/log rows, with no snow
CLI and no network. verify_app gets an injected run_query and a no-op sleep.
The row shapes are the ones current Snowflake returns (observed 2026-09-27 on a
real GitHub Actions deploy): SHOW STREAMLITS carries no version URIs, DESCRIBE
STREAMLIT does.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from streamsnow import verify
from streamsnow.cli import app
from streamsnow.config import Config
from streamsnow.verify import (
    check_exists,
    check_live_version,
    check_service_logs,
    check_version_source,
    run_query_snow,
    summary_line,
    verify_app,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"
FQN = "STREAMSNOW_APPS.DASHBOARDS.MY_APP"
STAGE = "@STREAMSNOW_APPS.DASHBOARDS.STREAMSNOW_CODE_STAGE"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _cfg() -> Config:
    return Config.from_dict(yaml.safe_load(EXAMPLE.read_text()))


def _show_row() -> dict:
    """Every column SHOW STREAMLITS returns today. No version URI among them."""
    return {
        "artifact_repositories": None,
        "comment": None,
        "created_on": "2026-09-27 10:00:00.000 -0700",
        "database_name": "STREAMSNOW_APPS",
        "idle_auto_shutdown_time_seconds": 259200,
        "name": "MY_APP",
        "owner": "STREAMSNOW_CI_ROLE",
        "owner_role_type": "ROLE",
        "query_warehouse": "STREAMSNOW_WH",
        "scheduled_tasks": None,
        "schema_name": "DASHBOARDS",
        "title": "My App",
        "url_id": "acmeurlid0000000000",
    }


def _describe_row(
    sha: str = SHA, live: str | None = f"snow://streamlit/{FQN}/versions/live/"
) -> dict:
    """The DESCRIBE STREAMLIT row a stage-copy deploy of ``sha`` leaves behind."""
    source = f"{STAGE}/commits/{sha}/apps/my-app/"
    return {
        "title": "My App",
        "main_file": "streamlit_app.py",
        "query_warehouse": "STREAMSNOW_WH",
        "url_id": "acmeurlid0000000000",
        "compute_pool": "SYSTEM_COMPUTE_POOL_CPU",
        "runtime_name": "SYSTEM$ST_CONTAINER_RUNTIME_PY3_11",
        "default_version_name": "VERSION$1",
        "last_version_name": "VERSION$1",
        "live_version_location_uri": live,
        "default_version_source_location_uri": source,
        "last_version_source_location_uri": source,
    }


# ---- exists ----------------------------------------------------------------


def test_exists_pass_and_fail():
    assert check_exists(_show_row(), FQN)["ok"]
    res = check_exists(None, FQN)
    assert not res["ok"] and res["status"] == "fail"
    assert "not found" in res["findings"][0]


# ---- live-version ----------------------------------------------------------


def test_live_version_null_fails_with_fix_sql():
    res = check_live_version(_describe_row(live=None), FQN)
    assert res["status"] == "fail" and not res["ok"]
    assert "ADD LIVE VERSION FROM LAST" in res["findings"][0]


def test_live_version_empty_string_fails():
    assert check_live_version(_describe_row(live=""), FQN)["status"] == "fail"


def test_live_version_present_passes():
    res = check_live_version(_describe_row(), FQN)
    assert res["status"] == "pass" and res["ok"]


def test_live_version_column_absent_is_skipped_not_passed():
    # The SHOW row is what 0.7.1 read: no version URI, so it could not decide.
    res = check_live_version(_show_row(), FQN)
    assert res["status"] == "skipped"
    assert not res["ok"]
    assert res["level"] == "warn"
    assert res["findings"][0].startswith("not checked:")


# ---- version-source --------------------------------------------------------


def test_version_source_matching_sha_passes():
    assert check_version_source(_describe_row(SHA), FQN, SHA)["status"] == "pass"


def test_version_source_accepts_the_short_sha_git_prints():
    assert check_version_source(_describe_row(SHA), FQN, SHA[:7])["status"] == "pass"


def test_version_source_wrong_sha_fails():
    other = "f" * 40
    res = check_version_source(_describe_row(SHA), FQN, other)
    assert res["status"] == "fail" and not res["ok"]
    assert f"/commits/{other}/" in res["findings"][0]


def test_version_source_is_not_fooled_by_a_sha_that_extends_the_deployed_one():
    # "/commits/<sha>/" is anchored: a longer SHA sharing a prefix is a different commit.
    assert check_version_source(_describe_row("abc1234"), FQN, "abc12345")["status"] == "fail"


def test_version_source_follows_the_last_version_the_live_one_is_built_from():
    # The deploy runs ADD LIVE VERSION FROM LAST, so LAST decides freshness.
    older = "e" * 40
    desc = _describe_row(SHA)
    desc["default_version_source_location_uri"] = desc[
        "default_version_source_location_uri"
    ].replace(SHA, older)
    assert check_version_source(desc, FQN, SHA)["status"] == "pass"

    desc = _describe_row(SHA)
    desc["last_version_source_location_uri"] = desc["last_version_source_location_uri"].replace(
        SHA, older
    )
    res = check_version_source(desc, FQN, SHA)
    assert res["status"] == "fail" and "last_version_source_location_uri" in res["findings"][0]


def test_version_source_null_uris_fail():
    desc = _describe_row()
    desc["default_version_source_location_uri"] = None
    desc["last_version_source_location_uri"] = None
    res = check_version_source(desc, FQN, SHA)
    assert res["status"] == "fail"


def test_version_source_no_uri_columns_is_skipped_not_passed():
    res = check_version_source(_show_row(), FQN, SHA)
    assert res["status"] == "skipped"
    assert not res["ok"]
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


def test_service_logs_unavailable_is_skipped_not_passed():
    res = check_service_logs(None, FQN)
    assert res["status"] == "skipped"
    assert not res["ok"]
    assert res["level"] == "warn"
    assert "best-effort" in res["findings"][0]


# ---- verify_app orchestration ----------------------------------------------


def _run_query_factory(
    show_rows_by_attempt: list[list[dict]],
    describe_rows: list[dict] | None = None,
    services: list[dict] | None = None,
    seen: list[str] | None = None,
):
    """Return a run_query stub: SHOW STREAMLITS per attempt, then DESCRIBE."""
    state = {"i": 0}

    def run_query(sql: str) -> list[dict]:
        if seen is not None:
            seen.append(sql)
        s = sql.upper()
        if s.startswith("SHOW STREAMLITS"):
            i = min(state["i"], len(show_rows_by_attempt) - 1)
            state["i"] += 1
            return show_rows_by_attempt[i]
        if s.startswith("DESCRIBE STREAMLIT"):
            return [_describe_row()] if describe_rows is None else describe_rows
        if s.startswith("SHOW SERVICES"):
            return services or []
        if "GET_SERVICE_LOGS" in s:
            return [{"LOG": "You can now view your Streamlit app\n"}]
        raise AssertionError(f"unexpected query: {sql}")

    return run_query


def _by_name(result: dict) -> dict[str, dict]:
    return {c["name"]: c for c in result["checks"]}


def test_verify_app_passes_on_healthy_deploy():
    cfg = _cfg()
    seen: list[str] = []
    result = verify_app(
        cfg,
        "my-app",
        sha=SHA,
        run_query=_run_query_factory([[_show_row()]], seen=seen),
        sleep=lambda _: None,
    )
    assert result["ok"], result["checks"]
    names = [c["name"] for c in result["checks"]]
    assert names[:2] == ["exists", "live-version"]
    assert "version-source" in names  # example config is stage-copy
    assert "service-logs" in names  # example config is container runtime
    checks = _by_name(result)
    # The version checks ran against DESCRIBE, not the SHOW row that lacks the URIs.
    assert checks["live-version"]["status"] == "pass"
    assert checks["version-source"]["status"] == "pass"
    assert f"DESCRIBE STREAMLIT {FQN}" in seen


def test_verify_app_fails_on_a_missing_live_version():
    result = verify_app(
        _cfg(),
        "my-app",
        sha=SHA,
        run_query=_run_query_factory([[_show_row()]], describe_rows=[_describe_row(live=None)]),
        attempts=2,
        sleep=lambda _: None,
    )
    assert not result["ok"]
    live = _by_name(result)["live-version"]
    assert live["status"] == "fail"
    assert "ADD LIVE VERSION FROM LAST" in live["findings"][0]


def test_verify_app_fails_on_a_stale_version_source():
    stale = _describe_row("e" * 40)
    result = verify_app(
        _cfg(),
        "my-app",
        sha=SHA,
        run_query=_run_query_factory([[_show_row()]], describe_rows=[stale]),
        sleep=lambda _: None,
    )
    assert not result["ok"]
    assert _by_name(result)["version-source"]["status"] == "fail"
    assert _by_name(result)["live-version"]["status"] == "pass"


def test_verify_app_skips_describe_checks_when_describe_lacks_the_columns():
    # A future DESCRIBE that drops the URIs must read as skipped, not as a pass,
    # and must not burn the cold-start retries (the column will not appear).
    slept: list[float] = []
    result = verify_app(
        _cfg(),
        "my-app",
        sha=SHA,
        run_query=_run_query_factory([[_show_row()]], describe_rows=[_show_row()]),
        sleep=slept.append,
    )
    checks = _by_name(result)
    assert checks["live-version"]["status"] == "skipped"
    assert checks["version-source"]["status"] == "skipped"
    assert result["ok"]  # nothing failed: skips are reported, never failed or passed
    assert slept == []


def test_verify_app_describe_error_is_skipped_with_the_error_after_retries():
    cfg = _cfg()
    slept: list[float] = []

    def run_query(sql: str) -> list[dict]:
        s = sql.upper()
        if s.startswith("SHOW STREAMLITS"):
            return [_show_row()]
        if s.startswith("DESCRIBE STREAMLIT"):
            raise RuntimeError("snow sql failed (1): insufficient privileges")
        return []

    result = verify_app(cfg, "my-app", sha=SHA, run_query=run_query, delay=5.0, sleep=slept.append)
    checks = _by_name(result)
    for name in ("live-version", "version-source"):
        assert checks[name]["status"] == "skipped", name
        assert "insufficient privileges" in checks[name]["findings"][0]
    assert slept == [5.0, 5.0]  # a DESCRIBE error can be the cold start: retried


def _git_data() -> dict:
    data = yaml.safe_load(EXAMPLE.read_text())
    data["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
        "git_origin": "https://github.com/acme/dashboards.git",
    }
    return data


def _git_desc(commit: str | None) -> dict:
    """DESCRIBE for a git-sourced app, shaped like the live row observed on
    2026-10-03: a branch source URI plus the commit hash it resolved to."""
    row = _describe_row()
    source = "@STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO/branches/main/apps/my-app/"
    row["default_version_source_location_uri"] = source
    row["last_version_source_location_uri"] = source
    if commit is not None:
        row["default_version_git_commit_hash"] = commit
        row["last_version_git_commit_hash"] = commit
    return row


@pytest.mark.parametrize(
    ("commit", "want"),
    [(SHA, "pass"), (SHA[:7], "pass"), ("0" * 40, "fail"), (None, "skipped")],
)
def test_verify_app_git_repository_source_checks_the_deployed_commit(commit, want):
    """A git deploy builds from a branch path, so the commit is proven by
    DESCRIBE's last_version_git_commit_hash; a missing column is skipped."""
    expected = SHA[:7] if commit == SHA[:7] else SHA
    desc = _git_desc(SHA if commit == SHA[:7] else commit)
    result = verify_app(
        Config.from_dict(_git_data()),
        "my-app",
        sha=expected,
        run_query=_run_query_factory([[_show_row()]], describe_rows=[desc]),
        attempts=1,
        sleep=lambda _: None,
    )
    check = _by_name(result)["version-source"]
    assert check["status"] == want
    if want == "fail":
        assert "last_version_git_commit_hash" in check["findings"][0]
        assert result["ok"] is False


def test_verify_app_git_repository_rejects_a_too_short_sha():
    result = verify_app(
        Config.from_dict(_git_data()),
        "my-app",
        sha=SHA[:1],
        run_query=_run_query_factory([[_show_row()]], describe_rows=[_git_desc(SHA)]),
        attempts=1,
        sleep=lambda _: None,
    )
    assert _by_name(result)["version-source"]["status"] == "fail"


def test_verify_app_git_repository_without_sha_checks_live_version_only():
    result = verify_app(
        Config.from_dict(_git_data()),
        "my-app",
        run_query=_run_query_factory([[_show_row()]], describe_rows=[_git_desc(SHA)]),
        attempts=1,
        sleep=lambda _: None,
    )
    checks = _by_name(result)
    assert "version-source" not in checks
    assert checks["live-version"]["status"] == "pass"


def test_verify_app_retries_through_cold_start():
    cfg = _cfg()
    slept: list[float] = []
    # First attempt: object not visible yet; second attempt: healthy.
    result = verify_app(
        cfg,
        "my-app",
        sha=SHA,
        run_query=_run_query_factory([[], [_show_row()]]),
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
        sha=SHA,
        run_query=_run_query_factory([[]]),
        attempts=2,
        sleep=lambda _: None,
    )
    assert not result["ok"]
    checks = _by_name(result)
    assert checks["exists"]["status"] == "fail"
    # Nothing to DESCRIBE: the version checks say so instead of failing twice.
    assert checks["live-version"]["status"] == "skipped"
    assert checks["version-source"]["status"] == "skipped"
    assert "does not exist" in checks["live-version"]["findings"][0]


def test_verify_app_query_error_is_a_block_finding():
    cfg = _cfg()

    def boom(sql: str) -> list[dict]:
        raise RuntimeError("snow sql failed")

    result = verify_app(cfg, "my-app", run_query=boom, attempts=1, sleep=lambda _: None)
    assert not result["ok"]
    assert result["checks"][0]["status"] == "fail"
    assert "could not query" in result["checks"][0]["findings"][0]


def test_verify_app_skips_version_source_without_sha():
    cfg = _cfg()
    result = verify_app(
        cfg,
        "my-app",
        run_query=_run_query_factory([[_show_row()]]),
        sleep=lambda _: None,
    )
    assert "version-source" not in [c["name"] for c in result["checks"]]


def test_verify_app_service_log_fetch_failure_is_skipped_not_failed():
    cfg = _cfg()

    def run_query(sql: str) -> list[dict]:
        s = sql.upper()
        if s.startswith("SHOW STREAMLITS"):
            return [_show_row()]
        if s.startswith("DESCRIBE STREAMLIT"):
            return [_describe_row()]
        raise RuntimeError("no SHOW SERVICES privilege")

    result = verify_app(cfg, "my-app", sha=SHA, run_query=run_query, sleep=lambda _: None)
    assert result["ok"], result["checks"]
    logs = _by_name(result)["service-logs"]
    assert logs["status"] == "skipped" and logs["level"] == "warn"


# ---- summary line ------------------------------------------------------------


def test_summary_line_never_counts_a_skip_as_a_pass():
    result = {
        "app": "store-sales",
        "ok": True,
        "checks": [
            {"name": "exists", "status": "pass"},
            {"name": "live-version", "status": "pass"},
            {"name": "version-source", "status": "pass"},
            {"name": "service-logs", "status": "skipped"},
        ],
    }
    assert summary_line(result) == "PASS: store-sales (3 passed; 1 skipped: service-logs)"
    result["ok"] = False
    result["checks"][1]["status"] = "fail"
    assert summary_line(result) == (
        "FAIL: store-sales (2 passed; 1 failed: live-version; 1 skipped: service-logs)"
    )


# ---- snow invocation: --temporary-connection -------------------------------
# CI has SNOWFLAKE_* environment variables and no config.toml, where snow
# without --temporary-connection fails ("Connection default is not
# configured"). Locally the flag must stay off so the default connection is used.


def _fake_snow(calls: list[list[str]], describe: dict | None = None, services=()):
    """A subprocess.run stand-in for `snow sql -q ... --format json` that answers
    by statement, with the row shapes current Snowflake returns."""
    desc = _describe_row() if describe is None else describe

    def run(argv, **_kwargs):
        calls.append(list(argv))
        sql = argv[3].upper() if len(argv) > 3 else ""
        if sql.startswith("SHOW STREAMLITS"):
            rows = [_show_row()]
        elif sql.startswith("DESCRIBE STREAMLIT"):
            rows = [desc]
        elif sql.startswith("SHOW SERVICES"):
            rows = list(services)
        else:
            rows = []
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")

    return run


def test_run_query_snow_adds_temporary_connection_only_when_requested(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(verify.subprocess, "run", _fake_snow(calls))

    run_query_snow("SELECT 1")
    run_query_snow("SELECT 1", temporary_connection=True)

    assert calls[0] == ["snow", "sql", "-q", "SELECT 1", "--format", "json"]
    assert calls[1] == [
        "snow",
        "sql",
        "-q",
        "SELECT 1",
        "--format",
        "json",
        "--temporary-connection",
    ]


@pytest.mark.parametrize("flag", [True, False])
def test_verify_deploy_threads_temporary_connection_into_every_snow_call(monkeypatch, flag):
    calls: list[list[str]] = []
    monkeypatch.setattr(verify.subprocess, "run", _fake_snow(calls))
    argv = ["verify-deploy", "my-app", "--config", str(EXAMPLE), "--attempts", "1"]
    argv += ["--sha", SHA]
    if flag:
        argv.append("--temporary-connection")

    result = CliRunner().invoke(app, argv)

    assert result.exit_code in (0, 1), result.output
    assert calls, "verify-deploy never called snow"
    assert all(c[:2] == ["snow", "sql"] for c in calls)
    assert any(c[3].startswith("DESCRIBE STREAMLIT") for c in calls), calls
    assert all(("--temporary-connection" in c) is flag for c in calls), calls


# ---- verify-deploy rendering -------------------------------------------------
# 0.7.1 printed "✓ live-version" above "live_version_location_uri not in SHOW
# output, skipped" on the first real CI deploy: a check that never ran read as
# a pass.


def _verify_deploy(monkeypatch, describe: dict | None = None) -> tuple[int, str]:
    monkeypatch.setattr(verify.subprocess, "run", _fake_snow([], describe=describe))
    argv = ["verify-deploy", "my-app", "--config", str(EXAMPLE), "--attempts", "1"]
    result = CliRunner().invoke(app, [*argv, "--sha", SHA, "--temporary-connection"])
    return result.exit_code, result.output


def test_verify_deploy_output_on_a_healthy_deploy_marks_skips_apart(monkeypatch):
    code, out = _verify_deploy(monkeypatch)
    assert code == 0, out
    assert "  ✓ exists" in out
    assert "  ✓ live-version" in out
    assert "  ✓ version-source" in out
    # No service matched, so the best-effort log scan could not run.
    assert "  ○ service-logs (skipped)" in out
    assert "✓ service-logs" not in out
    assert "PASS: my-app (3 passed; 1 skipped: service-logs)" in out


def test_verify_deploy_output_never_marks_an_unrunnable_check_as_passed(monkeypatch):
    code, out = _verify_deploy(monkeypatch, describe=_show_row())  # no URI columns
    assert code == 0, out
    assert "✓ live-version" not in out and "✓ version-source" not in out
    assert "  ○ live-version (skipped)" in out
    assert "  ○ version-source (skipped)" in out
    assert "PASS: my-app (1 passed; 3 skipped: live-version, version-source, service-logs)" in out


def test_verify_deploy_output_and_exit_code_on_a_missing_live_version(monkeypatch):
    code, out = _verify_deploy(monkeypatch, describe=_describe_row(live=None))
    assert code == 1, out
    assert "  ✗ live-version" in out
    assert "ADD LIVE VERSION FROM LAST" in out
    assert "FAIL: my-app (2 passed; 1 failed: live-version; 1 skipped: service-logs)" in out


def test_verify_deploy_json_reports_status_per_check(monkeypatch):
    monkeypatch.setattr(verify.subprocess, "run", _fake_snow([]))
    argv = ["verify-deploy", "my-app", "--config", str(EXAMPLE), "--attempts", "1"]
    result = CliRunner().invoke(app, [*argv, "--sha", SHA, "--format", "json"])
    data = json.loads(result.output)
    assert data["ok"] is True
    statuses = {c["name"]: (c["status"], c["ok"]) for c in data["checks"]}
    assert statuses == {
        "exists": ("pass", True),
        "live-version": ("pass", True),
        "version-source": ("pass", True),
        "service-logs": ("skipped", False),
    }
