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


def test_fresh_scaffold_passes_its_own_validate_gate(tmp_path):
    """A repo straight out of `streamsnow init` (including the auto-generated
    sql_review companion) must pass `validate-app` — the accuracy audit caught
    check-artifacts demanding the .review.sql be declared deployable."""
    result = runner.invoke(
        app,
        [
            "init",
            "--config",
            str(EXAMPLE_CONFIG),
            "--dir",
            str(tmp_path),
            "--app",
            "acme-sales-dashboard",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["validate-app", "acme-sales-dashboard", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output


def test_generated_python_is_format_clean_under_ruff_defaults(tmp_path):
    """A consumer repo has no [tool.ruff] of its own, so its pre-commit `ruff format`
    runs at the default line length. A template written at this repo's wider limit
    made the very first commit in a fresh scaffold fail on `ruff format` (seen in
    the 0.7 fresh-user run). `--isolated` reproduces the consumer's view."""
    import shutil
    import subprocess

    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff not on PATH")
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    proc = subprocess.run(
        [ruff, "format", "--check", "--isolated", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _scaffold_runtime(tmp_path: Path, runtime: str) -> Path:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    if runtime == "warehouse":
        data["runtime"] = "warehouse"
        data["snowflake"]["objects"]["compute_pool"] = ""
        data["snowflake"]["objects"]["external_access_integration"] = ""
    root = tmp_path / runtime
    scaffold(Config.from_dict(data), root, "acme-sales-dashboard")
    return root


def test_generated_ci_pins_the_same_ruff_as_pre_commit(tmp_path):
    """An unpinned `uv tool install ruff` in CI picked up a newer ruff whose
    default rule set failed the untouched scaffold on its first push, while the
    pinned pre-commit hook passed it locally. One version, rendered into both."""
    import re

    from streamsnow.scaffolder import RUFF_VERSION

    root = _scaffold_runtime(tmp_path, "container")
    precommit = yaml.safe_load((root / ".pre-commit-config.yaml").read_text())
    ruff_repo = next(r for r in precommit["repos"] if "ruff-pre-commit" in r["repo"])
    assert ruff_repo["rev"] == f"v{RUFF_VERSION}"
    ci = (root / ".github/workflows/checks.yml").read_text()
    installs = re.findall(r"uv tool install (\S+)", ci)
    ruff_installs = [i for i in installs if i.strip("'\"").startswith("ruff")]
    assert ruff_installs == [f"ruff=={RUFF_VERSION}"], installs


@pytest.mark.parametrize("runtime", ["container", "warehouse"])
@pytest.mark.parametrize(
    "extra",
    [
        [],
        # Rules newer ruff releases enable by default; the templates must not
        # depend on which default set the consumer's ruff happens to ship.
        ["--extend-select", "I,C4,BLE,RUF100"],
    ],
)
def test_generated_python_is_lint_clean_under_ruff(tmp_path, runtime, extra):
    import shutil
    import subprocess

    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff not on PATH")
    root = _scaffold_runtime(tmp_path, runtime)
    proc = subprocess.run(
        [ruff, "check", "--isolated", "--no-cache", *extra, "apps/"],
        capture_output=True,
        text=True,
        check=False,
        cwd=root,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --------------------------------------------------------------------------- #
# 0.7.1: the plugin setup path must write the governed repo, not just config
# --------------------------------------------------------------------------- #
_REPO_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".gitignore",
    ".pre-commit-config.yaml",
    ".github/workflows/checks.yml",
    ".github/workflows/deploy.yml",
    "README.md",
    "deploy/tombstones.yml",
)


def test_init_no_starter_app_writes_repo_files_without_an_app(tmp_path):
    """`/start-app --setup` used to run only `configure`, and `/start-app` then
    ran `new`, which writes app files only: a repo with no .gitignore (so a
    secrets.toml could be committed), no hooks, no CI. `init --no-starter-app`
    is the setup verb that writes the governed repo and nothing app-shaped."""
    result = runner.invoke(
        app, ["init", "--config", str(EXAMPLE_CONFIG), "--dir", str(tmp_path), "--no-starter-app"]
    )
    assert result.exit_code == 0, result.output
    for rel in _REPO_FILES:
        assert (tmp_path / rel).is_file(), f"missing {rel}"
    assert not (tmp_path / "apps").exists()
    readme = (tmp_path / "README.md").read_text()
    assert "example-dashboard" not in readme
    assert "apps//" not in readme and "| |" not in readme
    assert "streamsnow new" in readme
    assert "secrets.toml" in (tmp_path / ".gitignore").read_text()
    assert "streamsnow new <domain> <function>" in result.output


def test_init_no_starter_app_reuses_an_existing_config(tmp_path):
    """The setup skill runs `configure` first on some paths; init must reuse it."""
    assert (
        runner.invoke(
            app, ["configure", "--dir", str(tmp_path), "--config", str(EXAMPLE_CONFIG)]
        ).exit_code
        == 0
    )
    before = (tmp_path / CONFIG_FILENAME).read_text()
    result = runner.invoke(app, ["init", "--dir", str(tmp_path), "--no-starter-app"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / CONFIG_FILENAME).read_text() == before
    assert (tmp_path / ".gitignore").is_file()
    # Re-running is idempotent: existing repo files are left alone.
    (tmp_path / "README.md").write_text("# mine\n")
    assert runner.invoke(app, ["init", "--dir", str(tmp_path), "--no-starter-app"]).exit_code == 0
    assert (tmp_path / "README.md").read_text() == "# mine\n"


def test_new_warns_when_repo_governance_files_are_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert (
        runner.invoke(
            app, ["configure", "--dir", str(tmp_path), "--config", str(EXAMPLE_CONFIG)]
        ).exit_code
        == 0
    )
    result = runner.invoke(app, ["new", "sales", "order-trends"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "apps/sales-order-trends/streamlit_app.py").is_file()
    out = " ".join(result.output.split())
    assert "streamsnow init --no-starter-app" in out
    assert ".gitignore" in out and ".pre-commit-config.yaml" in out

    # Once the repo files exist, `new` is quiet about them.
    assert runner.invoke(app, ["init", "--dir", str(tmp_path), "--no-starter-app"]).exit_code == 0
    result = runner.invoke(app, ["new", "sales", "region-mix"])
    assert result.exit_code == 0, result.output
    assert "--no-starter-app" not in result.output


def test_init_next_block_puts_the_plugin_first(tmp_path):
    """Docs Path B installs the plugin first; the Next: block agrees."""
    result = runner.invoke(
        app, ["init", "--config", str(EXAMPLE_CONFIG), "--dir", str(tmp_path), "--app", "a-b"]
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.index("/plugin marketplace add") < out.index("snow connection add")
    # B8: the starter query is a placeholder until repointed.
    assert "YOUR_TABLE" in out


def test_setup_skill_writes_repo_files_on_a_repo_without_apps():
    setup = (REPO_ROOT / "skills/start-app/setup.md").read_text()
    assert "streamsnow init --no-starter-app" in setup
    skill = (REPO_ROOT / "skills/start-app/SKILL.md").read_text()
    assert "init --no-starter-app" in skill


def test_validate_app_warns_but_passes_on_the_scaffold_placeholder_query(tmp_path):
    """The starter query reads YOUR_TABLE: it validated clean, then CI deployed an
    app that cannot run. A WARN (not a FAIL: the fresh scaffold must still pass
    its own gate) names the file until the query is repointed."""
    import json as _json

    args = ["init", "--config", str(EXAMPLE_CONFIG), "--dir", str(tmp_path), "--app", "a-b"]
    assert runner.invoke(app, args).exit_code == 0
    result = runner.invoke(app, ["validate-app", "a-b", "--dir", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output)
    check = next(c for c in payload["checks"] if c["name"] == "placeholders")
    assert check["ok"] is True
    assert any("queries/example_metric.sql" in str(w) for w in check["warnings"])

    md = runner.invoke(app, ["validate-app", "a-b", "--dir", str(tmp_path)])
    assert md.exit_code == 0
    assert "YOUR_TABLE" in md.output and "PASS" in md.output

    q = tmp_path / "apps/a-b/queries/example_metric.sql"
    q.write_text(q.read_text().replace("YOUR_TABLE  -- TODO: replace YOUR_TABLE", "ORDERS"))
    payload = _json.loads(
        runner.invoke(
            app, ["validate-app", "a-b", "--dir", str(tmp_path), "--format", "json"]
        ).output
    )
    check = next(c for c in payload["checks"] if c["name"] == "placeholders")
    assert check["warnings"] == []


def test_next_block_explains_preview_role_grants():
    from streamsnow.cli import PREVIEW_ROLE_NOTE

    assert "no data grants" in PREVIEW_ROLE_NOTE
    assert "CI role" in PREVIEW_ROLE_NOTE


def _warehouse_cfg() -> Config:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    data["runtime"] = "warehouse"
    data["snowflake"]["objects"]["compute_pool"] = ""
    data["snowflake"]["objects"]["external_access_integration"] = ""
    return Config.from_dict(data)


def test_warehouse_scaffold_pins_newest_supported_streamlit(tmp_path):
    from streamsnow.scaffolder import WAREHOUSE_STREAMLIT_PIN

    scaffold(_warehouse_cfg(), tmp_path, "sales-overview")
    env = (tmp_path / "apps/sales-overview/environment.yml").read_text()
    assert f"streamlit={WAREHOUSE_STREAMLIT_PIN}" in env
    assert WAREHOUSE_STREAMLIT_PIN == "1.52.2"
    assert "cosmetic" not in env


def test_warehouse_scaffold_ships_dated_osv_allowlist(tmp_path):
    import datetime as dt
    import json

    from streamsnow.tools import check_dependency_vulns as cdv

    scaffold(_warehouse_cfg(), tmp_path, "sales-overview")
    entries = json.loads((tmp_path / "osv_allowlist.json").read_text())
    ids = {e["id"] for e in entries}
    assert ids == {
        "GHSA-7p48-42j8-8846",
        "PYSEC-2026-2285",
        "GHSA-vqwp-45wm-r9r5",
        "PYSEC-2026-212",
    }
    for e in entries:
        assert e["package"] == "streamlit"
        assert len(e["reason"]) > 40
    active, expired = cdv.load_allowlist(
        tmp_path / "osv_allowlist.json", today=dt.date(2026, 9, 23)
    )
    assert len(active) == 4 and expired == []
    # The allowlist expires so the gate fires again and forces a re-check.
    _, later = cdv.load_allowlist(tmp_path / "osv_allowlist.json", today=dt.date(2027, 1, 1))
    assert len(later) == 4


def test_warehouse_scaffold_passes_the_vuln_gate_with_the_known_advisories(tmp_path, monkeypatch):
    from streamsnow.scaffolder import WAREHOUSE_STREAMLIT_PIN
    from streamsnow.tools import check_dependency_vulns as cdv

    scaffold(_warehouse_cfg(), tmp_path, "sales-overview")
    known = ["GHSA-7p48-42j8-8846", "PYSEC-2026-2285", "GHSA-vqwp-45wm-r9r5", "PYSEC-2026-212"]

    def fake(pins):
        return [known if (n, v) == ("streamlit", WAREHOUSE_STREAMLIT_PIN) else [] for n, v in pins]

    monkeypatch.setattr(cdv, "query_osv", fake)
    monkeypatch.chdir(tmp_path)
    assert cdv.main(["apps"]) == 0


def test_container_scaffold_has_no_osv_allowlist(tmp_path):
    scaffold(load_config(EXAMPLE_CONFIG), tmp_path, "sales-overview")
    assert not (tmp_path / "osv_allowlist.json").exists()


def test_update_never_rewrites_the_osv_allowlist():
    from streamsnow.scaffolder import GOVERNANCE_ITEMS

    assert all(i.output != "osv_allowlist.json" for i in GOVERNANCE_ITEMS)
