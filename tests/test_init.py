"""End-to-end: `streamsnow init` produces a working, configured, governed repo.

Runs with no Snowflake account and no network — proves a newcomer can scaffold
and that config drives the output + guardrails.
"""

from __future__ import annotations

import py_compile
from pathlib import Path

import yaml
from typer.testing import CliRunner

from streamsnow.cli import app
from streamsnow.config import CONFIG_FILENAME, Config, load_config
from streamsnow.policy import SchemaPolicy
from streamsnow.scaffolder import scaffold
from streamsnow.tools.check_schema_refs import check_paths, find_denied_refs

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "streamsnow.config.example.yaml"
runner = CliRunner()


def _compile_py(root: Path) -> None:
    for py in root.rglob("*.py"):
        py_compile.compile(str(py), doraise=True)


def test_init_container_scaffolds_a_working_repo(tmp_path):
    result = runner.invoke(
        app,
        [
            "init",
            "--config",
            str(EXAMPLE_CONFIG),
            "--dir",
            str(tmp_path),
            "--app",
            "sales-overview",
        ],
    )
    assert result.exit_code == 0, result.output

    # Core files exist.
    for rel in (
        CONFIG_FILENAME,
        "AGENTS.md",
        "CLAUDE.md",
        ".pre-commit-config.yaml",
        "apps/sales-overview/streamlit_app.py",
        "apps/sales-overview/snowflake.yml",
        "apps/sales-overview/pyproject.toml",
        "apps/sales-overview/branding.py",
        "apps/sales-overview/sql_loader.py",
        "apps/sales-overview/.streamlit/config.toml",
        "apps/sales-overview/.streamlit/secrets.toml.example",
        "apps/sales-overview/queries/example_metric.sql",
        "apps/sales-overview/pages/overview.py",
    ):
        assert (tmp_path / rel).is_file(), f"missing {rel}"

    # Container runtime: pyproject yes, environment.yml no.
    assert not (tmp_path / "apps/sales-overview/environment.yml").exists()

    # Generated config re-validates.
    cfg = load_config(tmp_path / CONFIG_FILENAME)
    assert cfg.runtime == "container"

    # Config DROVE the governance doc.
    agents = (tmp_path / "AGENTS.md").read_text()
    assert "ANALYTICS_DB" in agents
    assert "ANALYTICS" in agents and "REPORTING" in agents
    assert "BRIDGE" in agents  # denied schema documented

    # Generated Python is valid.
    _compile_py(tmp_path / "apps")

    # Container connection pattern present.
    overview = (tmp_path / "apps/sales-overview/pages/overview.py").read_text()
    assert 'st.connection("snowflake")' in overview

    # The example app passes its own schema-refs guardrail.
    policy = SchemaPolicy.from_governance(cfg.governance)
    report = check_paths(list((tmp_path / "apps").rglob("*")), policy)
    assert report["ok"], report["findings"]


def test_init_warehouse_runtime(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    data["runtime"] = "warehouse"
    data["snowflake"]["objects"]["compute_pool"] = ""
    data["snowflake"]["objects"]["external_access_integration"] = ""
    cfg = Config.from_dict(data)

    scaffold(cfg, tmp_path, "ops-monitor")

    assert (tmp_path / "apps/ops-monitor/environment.yml").is_file()
    assert not (tmp_path / "apps/ops-monitor/pyproject.toml").exists()
    overview = (tmp_path / "apps/ops-monitor/pages/overview.py").read_text()
    assert "get_active_session" in overview
    _compile_py(tmp_path / "apps")


def test_schema_refs_guardrail_blocks_denied_schema():
    policy = SchemaPolicy(
        database="ANALYTICS_DB", schema_allow=("ANALYTICS",), schema_deny=("RAW", "BRIDGE")
    )
    # denied
    assert find_denied_refs("SELECT * FROM RAW.events", policy)
    assert find_denied_refs("FROM mydb.BRIDGE.t", policy)
    # allowed
    assert not find_denied_refs("FROM ANALYTICS_DB.ANALYTICS.sales", policy)
    # commented-out denied ref is ignored
    assert not find_denied_refs("-- FROM RAW.events", policy)


def test_init_refuses_to_clobber_without_force(tmp_path):
    args = ["init", "--config", str(EXAMPLE_CONFIG), "--dir", str(tmp_path), "--app", "a-b"]
    assert runner.invoke(app, args).exit_code == 0
    # second run with --config (import) onto an existing config should refuse
    assert runner.invoke(app, args).exit_code != 0


def test_configure_writes_config_without_scaffolding(tmp_path):
    result = runner.invoke(
        app, ["configure", "--dir", str(tmp_path), "--config", str(EXAMPLE_CONFIG)]
    )
    assert result.exit_code == 0, result.output
    cfg = load_config(tmp_path / CONFIG_FILENAME)
    assert cfg.snowflake.connection_name == "acme"
    # configure sets up the environment only — it does NOT scaffold apps
    assert not (tmp_path / "apps").exists()
    # and it surfaces the one-time connection command
    assert "snow connection add" in result.output


def test_init_reuses_existing_config_for_multiple_apps(tmp_path):
    # 1) configure the Snowflake environment once
    assert (
        runner.invoke(
            app, ["configure", "--dir", str(tmp_path), "--config", str(EXAMPLE_CONFIG)]
        ).exit_code
        == 0
    )
    # 2) init reuses that config (no --config) and scaffolds the first app
    assert runner.invoke(app, ["init", "--dir", str(tmp_path), "--app", "first-app"]).exit_code == 0
    # 3) init again reuses the same config and adds a second app (no clobber error)
    assert (
        runner.invoke(app, ["init", "--dir", str(tmp_path), "--app", "second-app"]).exit_code == 0
    )
    assert (tmp_path / "apps/first-app/streamlit_app.py").is_file()
    assert (tmp_path / "apps/second-app/streamlit_app.py").is_file()
