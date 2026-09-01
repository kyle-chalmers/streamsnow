"""End-to-end: `streamsnow init` produces a working, configured, governed repo.

Runs with no Snowflake account and no network — proves a newcomer can scaffold
and that config drives the output + guardrails.
"""

from __future__ import annotations

import py_compile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from streamsnow.cli import app
from streamsnow.config import CONFIG_FILENAME, Config, ConfigError, load_config
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

    # Every governance hook the checks ship is wired into the generated pre-commit.
    hooks = (tmp_path / ".pre-commit-config.yaml").read_text()
    for hook in (
        "schema-refs",
        "security",
        "bind-predicates",
        "caching",
        "sql-tokens",
        "session-fallback",
        "page-imports",
        "artifacts",
    ):
        assert f"streamsnow-{hook}" in hooks, f"missing hook: {hook}"


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


def test_schema_refs_catches_quoted_and_whitespaced_refs():
    policy = SchemaPolicy(database="DB", schema_allow=("ANALYTICS",), schema_deny=("BRIDGE",))
    assert find_denied_refs('FROM "BI"."BRIDGE"."T"', policy)  # quoted identifiers
    assert find_denied_refs("FROM BI . BRIDGE . T", policy)  # whitespace around dots
    assert not find_denied_refs("FROM BI.ANALYTICS.T", policy)


def test_generated_repo_ships_ci_workflow(tmp_path):
    runner.invoke(
        app, ["init", "--config", str(EXAMPLE_CONFIG), "--dir", str(tmp_path), "--app", "x-y"]
    )
    assert (tmp_path / ".github/workflows/checks.yml").is_file()


def test_generated_snowflake_yml_parses_both_runtimes(tmp_path):
    base = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    # container
    scaffold(Config.from_dict(base), tmp_path / "c", "app-c")
    cyml = yaml.safe_load((tmp_path / "c/apps/app-c/snowflake.yml").read_text())
    entity = cyml["entities"]["app_c"]
    assert entity["runtime_name"]
    assert entity["compute_pool"]
    # warehouse
    wdata = dict(base)
    wdata["runtime"] = "warehouse"
    wdata["snowflake"]["objects"] = dict(base["snowflake"]["objects"])
    wdata["snowflake"]["objects"]["compute_pool"] = ""
    wdata["snowflake"]["objects"]["external_access_integration"] = ""
    scaffold(Config.from_dict(wdata), tmp_path / "w", "app-w")
    wyml = yaml.safe_load((tmp_path / "w/apps/app-w/snowflake.yml").read_text())
    assert "runtime_name" not in wyml["entities"]["app_w"]


def test_git_repository_config_scaffolds_git_deploy_workflow(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    data["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "DATA_APPS.BI_APPS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "DATA_APPS.BI_APPS.GITHUB_PAT_SECRET",
    }
    scaffold(Config.from_dict(data), tmp_path, "g-app")
    deploy = (tmp_path / ".github/workflows/deploy.yml").read_text()
    assert "snow git fetch" in deploy
    assert "stage copy" not in deploy


def test_brand_injection_rejected(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    data["brand"] = {"theme": {"primary": '#fff"; evil'}}
    with pytest.raises(ConfigError):
        scaffold(Config.from_dict(data), tmp_path, "b-app")


def test_doctor_fails_loudly_on_malformed_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / CONFIG_FILENAME).write_text("runtime: container\n")  # missing required sections
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code != 0
    assert "invalid" in result.output.lower()


def test_deploy_workflows_pin_verify_concurrency_and_dotfile_copy(tmp_path):
    """Regression pins for production deploy lessons.

    - concurrency serialization: concurrent CREATE OR REPLACE races version
      pointers; cancel-in-progress would silently drop commits.
    - dotfile copy: `snow stage copy --recursive` silently skips dotted dirs,
      so .streamlit/config.toml needs its own copy — and secrets never ship.
    - verify step: deploy success is not app health; verify-deploy must run.
    """
    # stage-copy variant
    scaffold(Config.from_dict(yaml.safe_load(EXAMPLE_CONFIG.read_text())), tmp_path / "s", "s-app")
    stage_deploy = (tmp_path / "s/.github/workflows/deploy.yml").read_text()
    assert "group: deploy-snowflake" in stage_deploy
    assert "cancel-in-progress: false" in stage_deploy
    assert ".streamlit/config.toml" in stage_deploy  # explicit dotfile copy loop
    assert "secrets.toml" not in stage_deploy  # secrets must never be staged
    assert "streamsnow verify-deploy" in stage_deploy
    assert '--sha "$GITHUB_SHA"' in stage_deploy

    # git-repository variant
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    data["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "DATA_APPS.BI_APPS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "DATA_APPS.BI_APPS.GITHUB_PAT_SECRET",
    }
    scaffold(Config.from_dict(data), tmp_path / "g", "g-app")
    git_deploy = (tmp_path / "g/.github/workflows/deploy.yml").read_text()
    assert "group: deploy-snowflake" in git_deploy
    assert "cancel-in-progress: false" in git_deploy
    assert "streamsnow verify-deploy" in git_deploy


def test_generated_precommit_enforces_sql_review_and_vulns(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    text = (tmp_path / ".pre-commit-config.yaml").read_text()
    assert "streamsnow sql-review check" in text
    assert "streamsnow check dependency-vulns --best-effort" in text
    assert "streamsnow check path-leaks" in text
    parsed = yaml.safe_load(text)  # stays valid YAML
    ids = [h["id"] for repo in parsed["repos"] for h in repo["hooks"]]
    assert "streamsnow-sql-review" in ids


def test_generated_ci_enforces_the_deterministic_gates(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    text = (tmp_path / ".github" / "workflows" / "checks.yml").read_text()
    assert "streamsnow check dependency-vulns" in text  # fail-closed: no --best-effort in CI
    assert "--best-effort" not in text
    assert "streamsnow sql-review check" in text
    assert "streamsnow check tombstones --base-ref origin/main" in text
    assert "fetch-depth: 0" in text  # the tombstones diff needs history
    yaml.safe_load(text)


def test_generated_deploy_workflows_reconcile_tombstones(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    stage_copy = (tmp_path / ".github" / "workflows" / "deploy.yml").read_text()
    assert "streamsnow check tombstones --drop-sql" in stage_copy
    yaml.safe_load(stage_copy)
    # git-repository deploy source renders the other template — same step.
    gitdata = dict(data)
    gitdata["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "DATA_APPS.BI_APPS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "DATA_APPS.BI_APPS.GITHUB_PAT_SECRET",
    }
    scaffold(Config.from_dict(gitdata), tmp_path / "g", "acme-sales-dashboard")
    git_deploy = (tmp_path / "g" / ".github" / "workflows" / "deploy.yml").read_text()
    assert "streamsnow check tombstones --drop-sql" in git_deploy
    yaml.safe_load(git_deploy)


def test_scaffolded_tombstones_registry_is_valid_and_user_owned(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    reg = tmp_path / "deploy" / "tombstones.yml"
    assert yaml.safe_load(reg.read_text()) == {"tombstones": []}
    # `streamsnow update` must never re-render the registry (it would wipe
    # user-appended tombstone entries).
    from streamsnow.scaffolder import GOVERNANCE_ITEMS

    assert "deploy/tombstones.yml" not in {i.output for i in GOVERNANCE_ITEMS}


def test_generated_workflows_pin_a_compatible_streamsnow(tmp_path):
    """The templates install a pinned range; it must always cover the version
    of streamsnow that generated them — a 0.7 bump that forgets the templates
    fails here, not in a consumer's CI."""
    from packaging.specifiers import SpecifierSet

    import streamsnow

    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    for wf in ("checks.yml", "deploy.yml"):
        text = (tmp_path / ".github" / "workflows" / wf).read_text()
        assert "uv tool install 'streamsnow" in text
        spec = text.split("uv tool install 'streamsnow")[1].split("'")[0]
        assert streamsnow.__version__ in SpecifierSet(spec), (
            f"{wf} pins streamsnow{spec}, which excludes this version "
            f"({streamsnow.__version__}) — update the template pin with the release"
        )
