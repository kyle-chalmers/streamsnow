"""End-to-end: `streamsnow init` produces a working, configured, governed repo.

Runs with no Snowflake account and no network — proves a newcomer can scaffold
and that config drives the output + guardrails.
"""

from __future__ import annotations

import py_compile
import re
import subprocess
import sys
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
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "ANALYTICS_DB" in agents
    assert "ANALYTICS" in agents and "REPORTING" in agents
    assert "BRIDGE" in agents  # denied schema documented

    # Generated Python is valid.
    _compile_py(tmp_path / "apps")

    # Container connection pattern present.
    overview = (tmp_path / "apps/sales-overview/pages/overview.py").read_text(encoding="utf-8")
    assert 'st.connection("snowflake")' in overview

    # The example app passes its own schema-refs guardrail.
    policy = SchemaPolicy.from_governance(cfg.governance)
    report = check_paths(list((tmp_path / "apps").rglob("*")), policy)
    assert report["ok"], report["findings"]

    # Every governance hook the checks ship is wired into the generated pre-commit.
    hooks = (tmp_path / ".pre-commit-config.yaml").read_text(encoding="utf-8")
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
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["runtime"] = "warehouse"
    data["snowflake"]["objects"]["compute_pool"] = ""
    data["snowflake"]["objects"]["external_access_integration"] = ""
    cfg = Config.from_dict(data)

    scaffold(cfg, tmp_path, "ops-monitor")

    assert (tmp_path / "apps/ops-monitor/environment.yml").is_file()
    assert not (tmp_path / "apps/ops-monitor/pyproject.toml").exists()
    overview = (tmp_path / "apps/ops-monitor/pages/overview.py").read_text(encoding="utf-8")
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
    base = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    # container
    scaffold(Config.from_dict(base), tmp_path / "c", "app-c")
    cyml = yaml.safe_load((tmp_path / "c/apps/app-c/snowflake.yml").read_text(encoding="utf-8"))
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
    wyml = yaml.safe_load((tmp_path / "w/apps/app-w/snowflake.yml").read_text(encoding="utf-8"))
    assert "runtime_name" not in wyml["entities"]["app_w"]


def test_git_repository_config_scaffolds_git_deploy_workflow(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
    }
    scaffold(Config.from_dict(data), tmp_path, "g-app")
    deploy = (tmp_path / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "snow git fetch" in deploy
    assert "stage copy" not in deploy
    # Hardened in 0.7.4: no refresh step that hid failures behind `|| true`,
    # and verify-deploy proves the live version came from this commit.
    assert "|| true" not in deploy
    assert "--refresh" not in deploy
    assert "EXPERIMENTAL" not in deploy
    assert 'verify-deploy "$(basename "$d")" --sha "$GITHUB_SHA"' in deploy


def test_brand_injection_rejected(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["brand"] = {"theme": {"primary": '#fff"; evil'}}
    with pytest.raises(ConfigError):
        scaffold(Config.from_dict(data), tmp_path, "b-app")


def test_doctor_fails_loudly_on_malformed_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / CONFIG_FILENAME).write_text(
        "runtime: container\n", encoding="utf-8"
    )  # missing required sections
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
    scaffold(
        Config.from_dict(yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))),
        tmp_path / "s",
        "s-app",
    )
    stage_deploy = (tmp_path / "s/.github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "group: deploy-snowflake" in stage_deploy
    assert "cancel-in-progress: false" in stage_deploy
    assert ".streamlit/config.toml" in stage_deploy  # explicit dotfile copy loop
    assert "secrets.toml" not in stage_deploy  # secrets must never be staged
    assert "streamsnow verify-deploy" in stage_deploy
    assert '--sha "$GITHUB_SHA"' in stage_deploy

    # git-repository variant
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
    }
    scaffold(Config.from_dict(data), tmp_path / "g", "g-app")
    git_deploy = (tmp_path / "g/.github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "group: deploy-snowflake" in git_deploy
    assert "cancel-in-progress: false" in git_deploy
    assert "streamsnow verify-deploy" in git_deploy


# A `snow` command word: not the tail of `streamsnow`, not inside a path or a
# flag. The call runs to the end of its shell command (`&&`, `||`, `;`, `|`).
_SNOW_CALL = re.compile(r"(?<![\w./-])snow\s[^\n;&|]*")


def _render_deploy_workflows(tmp_path: Path) -> dict[str, dict]:
    stage = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    git = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    git["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
    }
    out = {}
    for name, data in (("stage-copy", stage), ("git-repository", git)):
        scaffold(Config.from_dict(data), tmp_path / name, "acme-sales-dashboard")
        text = (tmp_path / name / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
        out[name] = yaml.safe_load(text)
    return out


def _run_scripts(workflow: dict) -> list[str]:
    return [s["run"] for job in workflow["jobs"].values() for s in job["steps"] if "run" in s]


def test_every_snow_call_in_the_deploy_workflows_uses_a_temporary_connection(tmp_path):
    """CI has only SNOWFLAKE_* env vars and no config.toml. Snowflake CLI 3.27
    fails any call without --temporary-connection there ("Connection default
    is not configured"), so a single call missing it fails the first deploy."""
    expected = {"stage-copy": {"sql", "stage copy"}, "git-repository": {"sql", "git fetch"}}
    for source, workflow in _render_deploy_workflows(tmp_path).items():
        calls = [m.group(0) for run in _run_scripts(workflow) for m in _SNOW_CALL.finditer(run)]
        # `snow sql`, `snow stage copy`, `snow git fetch`: proves the scan found every call.
        seen = {"sql" if c.split()[1] == "sql" else " ".join(c.split()[1:3]) for c in calls}
        assert seen == expected[source], (source, calls)
        missing = [c for c in calls if "--temporary-connection" not in c.split()]
        assert not missing, (source, missing)


def test_deploy_workflows_run_verify_deploy_with_a_temporary_connection(tmp_path):
    for source, workflow in _render_deploy_workflows(tmp_path).items():
        lines = [
            line
            for run in _run_scripts(workflow)
            for line in run.splitlines()
            if "streamsnow verify-deploy" in line
        ]
        assert lines, source
        assert all("--temporary-connection" in line.split() for line in lines), (source, lines)


def test_deploy_workflows_hand_snow_the_key_passphrase_by_the_name_it_reads(tmp_path):
    """snow reads an encrypted key's passphrase from PRIVATE_KEY_PASSPHRASE;
    a SNOWFLAKE_PRIVATE_KEY_PASSPHRASE variable is ignored, which would fail
    an encrypted-key deploy. The repo secret keeps its documented name."""
    for source, workflow in _render_deploy_workflows(tmp_path).items():
        env = workflow["jobs"]["deploy"]["env"]
        assert env["PRIVATE_KEY_PASSPHRASE"] == "${{ secrets.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE }}"
        assert "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE" not in env, source


_APP_STEPS = {
    "stage-copy": ("Deploy changed apps (stage-copy)", "Verify deploy health"),
    "git-repository": ("Deploy changed apps (git-repository)", "Verify deploy health"),
}


def test_deploy_workflows_guard_against_a_repo_with_no_app_directories(tmp_path):
    """A repo with deploy secrets and no app yet failed its deploy: `snow stage
    copy "apps/"` exits 2 with "No data", and an unmatched `for d in apps/*/`
    runs once with the literal pattern. Every step that walks apps/ must use
    nullglob and stop with an explanation when there is nothing to walk."""
    for source, workflow in _render_deploy_workflows(tmp_path).items():
        steps = {s.get("name"): s.get("run") for s in workflow["jobs"]["deploy"]["steps"]}
        for name in _APP_STEPS[source]:
            run = steps[name]
            assert "shopt -s nullglob" in run, (source, name)
            assert "apps=(apps/*/)" in run, (source, name)
            assert 'if [ "${#apps[@]}" -eq 0 ]; then' in run, (source, name)
            assert "nothing to" in run and "exit 0" in run, (source, name)
            # The guard comes before any snow or streamsnow call in the step.
            guard = run.index("exit 0")
            assert "snow " not in run[:guard], (source, name)
            # Loops walk the collected array, never the bare glob.
            assert "for d in apps/*/" not in run, (source, name)


def _init_repo(tmp_path: Path, *, source: str, app_slug: str | None) -> Path:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    if source == "git-repository":
        data["deploy"] = {
            "source": "git-repository",
            "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
            "api_integration_name": "GITHUB_API_INTEGRATION",
            "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
        }
    cfg_file = tmp_path / "input.config.yaml"
    cfg_file.write_text(yaml.safe_dump(data), encoding="utf-8")
    repo = tmp_path / "repo"
    argv = ["init", "--config", str(cfg_file), "--dir", str(repo)]
    argv += ["--app", app_slug] if app_slug else ["--no-starter-app"]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    return repo


# The generated workflows run on ubuntu runners in consumer repos; executing their
# bash steps on Windows would test Git Bash quirks, not anything users run.
_workflow_bash = pytest.mark.skipif(sys.platform == "win32", reason="workflow steps run on Linux")


def _run_with_stub_snow(
    repo: Path, script: str, bin_dir: Path
) -> tuple[subprocess.CompletedProcess, str]:
    """Run one workflow step's shell under bash with `snow` stubbed on PATH. The
    stub logs its arguments and, like the real CLI, fails `stage copy` from a
    source that does not exist with "No data" (exit 2)."""
    import os

    bin_dir.mkdir(exist_ok=True)
    log = bin_dir / "snow.log"
    log.write_text("", encoding="utf-8")
    stub = bin_dir / "snow"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$SNOW_LOG"\n'
        'if [ "$1" = stage ] && [ ! -e "$4" ]; then echo "No data" >&2; exit 2; fi\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "SNOW_LOG": str(log),
        "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
    }
    proc = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc, log.read_text(encoding="utf-8")


@_workflow_bash
@pytest.mark.parametrize("source", ["stage-copy", "git-repository"])
def test_fresh_no_starter_repo_deploy_workflow_no_ops_without_calling_snow(tmp_path, source):
    """Run the deploy and verify steps, exactly as written, under bash in an
    `init --no-starter-app` repo (no apps/ directory) with `snow` stubbed:
    both exit 0, say why, and never reach snow."""
    import shutil

    if shutil.which("bash") is None:
        pytest.skip("needs bash")
    repo = _init_repo(tmp_path, source=source, app_slug=None)
    assert not (repo / "apps").exists()
    workflow = yaml.safe_load((repo / ".github/workflows/deploy.yml").read_text(encoding="utf-8"))
    steps = {s.get("name"): s.get("run") for s in workflow["jobs"]["deploy"]["steps"]}
    for name in _APP_STEPS[source]:
        proc, calls = _run_with_stub_snow(repo, steps[name], tmp_path / "bin")
        assert proc.returncode == 0, (name, proc.stdout, proc.stderr)
        assert "No app directories under apps/ yet" in proc.stdout, (name, proc.stdout)
        assert calls == "", (name, calls)
    # An apps/ holding no app directory (a stray file) is still nothing to deploy.
    (repo / "apps").mkdir()
    (repo / "apps" / "README.md").write_text("apps go here\n", encoding="utf-8")
    proc, calls = _run_with_stub_snow(repo, steps[_APP_STEPS[source][0]], tmp_path / "bin")
    assert proc.returncode == 0 and calls == "", (proc.stdout, proc.stderr, calls)


@_workflow_bash
def test_stage_copy_deploy_step_still_copies_when_an_app_exists(tmp_path):
    """Control for the no-apps test: with an app present the same step reaches
    `snow stage copy`, so an empty stub log there means the guard fired."""
    import shutil

    if shutil.which("bash") is None or shutil.which("streamsnow") is None:
        pytest.skip("needs bash and streamsnow on PATH (uv run pytest provides both)")
    repo = _init_repo(tmp_path, source="stage-copy", app_slug="acme-sales")
    workflow = yaml.safe_load((repo / ".github/workflows/deploy.yml").read_text(encoding="utf-8"))
    steps = {s.get("name"): s.get("run") for s in workflow["jobs"]["deploy"]["steps"]}
    proc, calls = _run_with_stub_snow(
        repo, steps["Deploy changed apps (stage-copy)"], tmp_path / "bin"
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    copies = [c for c in calls.splitlines() if c.startswith("stage copy")]
    assert any(" apps/ " in f" {c} " for c in copies), calls
    assert any("apps/acme-sales/.streamlit/config.toml" in c for c in copies), calls
    assert any(c.startswith("sql") and "/tmp/ss-acme-sales.sql" in c for c in calls.splitlines())


def test_generated_precommit_enforces_sql_review_and_vulns(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    text = (tmp_path / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "streamsnow sql-review check" in text
    assert "streamsnow check dependency-vulns --best-effort" in text
    assert "streamsnow check path-leaks" in text
    parsed = yaml.safe_load(text)  # stays valid YAML
    ids = [h["id"] for repo in parsed["repos"] for h in repo["hooks"]]
    assert "streamsnow-sql-review" in ids


def test_generated_ci_enforces_the_deterministic_gates(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    text = (tmp_path / ".github" / "workflows" / "checks.yml").read_text(encoding="utf-8")
    assert "streamsnow check dependency-vulns" in text  # fail-closed: no --best-effort in CI
    assert "--best-effort" not in text
    assert "streamsnow sql-review check" in text
    assert "streamsnow check tombstones --base-ref origin/main" in text
    assert "fetch-depth: 0" in text  # the tombstones diff needs history
    yaml.safe_load(text)


def test_generated_deploy_workflows_reconcile_tombstones(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    stage_copy = (tmp_path / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "streamsnow check tombstones --drop-sql" in stage_copy
    yaml.safe_load(stage_copy)
    # git-repository deploy source renders the other template — same step.
    gitdata = dict(data)
    gitdata["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
    }
    scaffold(Config.from_dict(gitdata), tmp_path / "g", "acme-sales-dashboard")
    git_deploy = (tmp_path / "g" / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    assert "streamsnow check tombstones --drop-sql" in git_deploy
    yaml.safe_load(git_deploy)


def test_scaffolded_tombstones_registry_is_valid_and_user_owned(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    reg = tmp_path / "deploy" / "tombstones.yml"
    assert yaml.safe_load(reg.read_text(encoding="utf-8")) == {"tombstones": []}
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

    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    for wf in ("checks.yml", "deploy.yml"):
        text = (tmp_path / ".github" / "workflows" / wf).read_text(encoding="utf-8")
        assert "uv tool install 'streamsnow" in text
        spec = text.split("uv tool install 'streamsnow")[1].split("'")[0]
        assert streamsnow.__version__ in SpecifierSet(spec), (
            f"{wf} pins streamsnow{spec}, which excludes this version "
            f"({streamsnow.__version__}) — update the template pin with the release"
        )


def _repoint_starter(app_dir: Path) -> None:
    """Replace YOUR_TABLE in all three starter files. Not enough on its own: the page's
    hard-coded sample metric and chart are still on screen."""
    for rel in (
        "queries/example_metric.sql",
        "sql_review/index.yaml",
        "pages/overview.py",
    ):
        f = app_dir / rel
        f.write_text(
            f.read_text(encoding="utf-8").replace("YOUR_TABLE", "ORDERS"), encoding="utf-8"
        )


# The starter page's sample block, from its marker through the sample chart.
_STARTER_SAMPLE_BLOCK = re.compile(
    r'# STREAMSNOW_STARTER_PLACEHOLDER.*?st\.plotly_chart\(fig, width="stretch"\)\n',
    re.S,
)


def _finish_starter(app_dir: Path) -> None:
    """The CLI-only path's step 4: repoint the starter trio at a real table AND render
    the query's results in place of the page's sample metric and chart."""
    _repoint_starter(app_dir)
    page = app_dir / "pages/overview.py"
    text, n = _STARTER_SAMPLE_BLOCK.subn(
        'df = load_example("2024-01-01", "2024-12-31")\n'
        'branded_metric("Rows", review_value("example_metric", f"{int(df[\'N\'].sum()):,}"))\n'
        'fig = px.bar(df, x="DT", y="N", color_discrete_sequence=BRAND_CHART_COLORS)\n'
        'st.plotly_chart(fig, width="stretch")\n',
        page.read_text(encoding="utf-8"),
    )
    assert n == 1, "starter page sample block not found"
    page.write_text(text, encoding="utf-8")


def test_fresh_scaffold_fails_only_on_its_placeholders(tmp_path):
    """A repo straight out of `streamsnow init` (including the auto-generated
    sql_review page file) is structurally whole: every check but `placeholders`
    passes (the accuracy audit once caught check-artifacts demanding the generated
    review SQL be declared deployable). `placeholders` FAILS until the starter trio is replaced,
    so the example app can never ship; once it is, the gate passes."""
    import json as _json

    from streamsnow.tools import sql_review

    args = ["init", "--config", str(EXAMPLE_CONFIG), "--dir", str(tmp_path)]
    result = runner.invoke(app, [*args, "--app", "acme-sales-dashboard"])
    assert result.exit_code == 0, result.output
    validate = ["validate-app", "acme-sales-dashboard", "--dir", str(tmp_path)]
    result = runner.invoke(app, [*validate, "--format", "json"])
    assert result.exit_code == 1, result.output
    failing = [c["name"] for c in _json.loads(result.output)["checks"] if not c["ok"]]
    assert failing == ["placeholders"]

    # Replacing YOUR_TABLE alone leaves the page's sample numbers: still a FAIL.
    _repoint_starter(tmp_path / "apps/acme-sales-dashboard")
    assert sql_review.main(["generate", "acme-sales-dashboard", "--dir", str(tmp_path)]) == 0
    result = runner.invoke(app, [*validate, "--format", "json"])
    assert result.exit_code == 1, result.output
    failing = [c["name"] for c in _json.loads(result.output)["checks"] if not c["ok"]]
    assert failing == ["placeholders"]

    _finish_starter(tmp_path / "apps/acme-sales-dashboard")
    assert sql_review.main(["generate", "acme-sales-dashboard", "--dir", str(tmp_path)]) == 0
    result = runner.invoke(app, validate)
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
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    proc = subprocess.run(
        [ruff, "format", "--check", "--isolated", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _scaffold_runtime(tmp_path: Path, runtime: str) -> Path:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
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
    precommit = yaml.safe_load((root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    ruff_repo = next(r for r in precommit["repos"] if "ruff-pre-commit" in r["repo"])
    assert ruff_repo["rev"] == f"v{RUFF_VERSION}"
    ci = (root / ".github/workflows/checks.yml").read_text(encoding="utf-8")
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
        encoding="utf-8",
        errors="replace",
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
    """The setup skill used to run only `configure`, and `/build-app` then
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
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "example-dashboard" not in readme
    assert "apps//" not in readme and "| |" not in readme
    assert "streamsnow new" in readme
    assert "secrets.toml" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "streamsnow new <domain> <function>" in result.output


def test_init_no_starter_app_reuses_an_existing_config(tmp_path):
    """The setup skill runs `configure` first on some paths; init must reuse it."""
    assert (
        runner.invoke(
            app, ["configure", "--dir", str(tmp_path), "--config", str(EXAMPLE_CONFIG)]
        ).exit_code
        == 0
    )
    before = (tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8")
    result = runner.invoke(app, ["init", "--dir", str(tmp_path), "--no-starter-app"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8") == before
    assert (tmp_path / ".gitignore").is_file()
    # Re-running is idempotent: existing repo files are left alone.
    (tmp_path / "README.md").write_text("# mine\n", encoding="utf-8")
    assert runner.invoke(app, ["init", "--dir", str(tmp_path), "--no-starter-app"]).exit_code == 0
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# mine\n"


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
    # /build-app runs this right after scaffolding (skills/build-app/scaffold.md).
    assert "Install the app's packages for local preview:" in out
    assert "apps/sales-order-trends" in out.split("for local preview:")[1]

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
    assert out.index("claude plugin marketplace add --scope project") < out.index(
        "snow connection add"
    )
    assert "claude plugin install --scope project streamsnow@streamsnow" in out
    # B8: the starter query is a placeholder until repointed.
    assert "YOUR_TABLE" in out


def test_setup_skill_writes_repo_files_on_a_repo_without_apps():
    setup = (REPO_ROOT / "skills/onboard/setup.md").read_text(encoding="utf-8")
    assert "streamsnow init --no-starter-app" in setup
    skill = (REPO_ROOT / "skills/onboard/SKILL.md").read_text(encoding="utf-8")
    assert "init --no-starter-app" in skill


def test_validate_app_fails_on_the_scaffold_placeholders(tmp_path):
    """B8, tightened: the starter query reads YOUR_TABLE. As a warning it validated
    clean and CI deployed an app that cannot run. It is a FAIL now, and it names every
    file of the starter trio: repointing the query alone still leaves the review window
    in sql_review/index.yaml and the page's sample numbers. The page's sample metric and chart are
    their own finding, so replacing YOUR_TABLE everywhere does not clear the gate either."""
    import json as _json

    args = ["init", "--config", str(EXAMPLE_CONFIG), "--dir", str(tmp_path), "--app", "a-b"]
    assert runner.invoke(app, args).exit_code == 0
    validate = ["validate-app", "a-b", "--dir", str(tmp_path)]

    def placeholder_findings() -> list[tuple[str, str]]:
        out = runner.invoke(app, [*validate, "--format", "json"]).output
        check = next(c for c in _json.loads(out)["checks"] if c["name"] == "placeholders")
        assert check["ok"] is (not check["findings"])
        kinds = {"scaffold placeholder YOUR_TABLE": "token", "starter page sample": "sample"}
        return [
            (f["file"], next(k for p, k in kinds.items() if f["detail"].startswith(p)))
            for f in check["findings"]
        ]

    assert placeholder_findings() == [
        ("pages/overview.py", "token"),
        ("pages/overview.py", "sample"),
        ("queries/example_metric.sql", "token"),
        ("sql_review/index.yaml", "token"),
    ]
    md = runner.invoke(app, validate)
    assert md.exit_code == 1
    assert "YOUR_TABLE" in md.output and "STREAMSNOW_STARTER_PLACEHOLDER" in md.output
    assert "FAIL" in md.output

    q = tmp_path / "apps/a-b/queries/example_metric.sql"
    q.write_text(
        q.read_text(encoding="utf-8").replace("YOUR_TABLE  -- TODO: replace YOUR_TABLE", "ORDERS"),
        encoding="utf-8",
    )
    assert ("queries/example_metric.sql", "token") not in placeholder_findings()
    assert runner.invoke(app, validate).exit_code == 1  # index.yaml + page still placeholders

    # Every YOUR_TABLE gone, the sample metric and chart still on the page: FAIL.
    _repoint_starter(tmp_path / "apps/a-b")
    assert placeholder_findings() == [("pages/overview.py", "sample")]
    assert runner.invoke(app, validate).exit_code == 1

    # Deleting only the marker comment does not dodge it: the sample values match too,
    # which is also how a page scaffolded before the marker existed is caught.
    page = tmp_path / "apps/a-b/pages/overview.py"
    marked = page.read_text(encoding="utf-8")
    lines = marked.splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines) if "STREAMSNOW_STARTER_PLACEHOLDER" in ln)
    del lines[start : start + 3]
    page.write_text("".join(lines), encoding="utf-8")
    assert "STREAMSNOW_STARTER_PLACEHOLDER" not in page.read_text(encoding="utf-8")
    assert placeholder_findings() == [("pages/overview.py", "sample")]

    page.write_text(marked, encoding="utf-8")
    _finish_starter(tmp_path / "apps/a-b")
    assert placeholder_findings() == []


def test_build_app_replacing_the_starter_trio_passes_validate(tmp_path, monkeypatch):
    """The /build-app end state (pages.md § Replace the starter trio): `new`, then the
    first real page (with its review_value marker), its query and its index.yaml entry
    replace all three starter files. The app must validate clean, which proves the
    documented replacement leaves nothing dangling (nav entry, artifacts, sql_review)."""
    import json as _json

    from streamsnow.tools import sql_review

    monkeypatch.chdir(tmp_path)
    init = ["init", "--no-starter-app", "--config", str(EXAMPLE_CONFIG), "--dir", str(tmp_path)]
    assert runner.invoke(app, init).exit_code == 0
    result = runner.invoke(app, ["new", "sales", "trends"])
    assert result.exit_code == 0, result.output
    assert "placeholders" in result.output and "YOUR_TABLE" in result.output
    a = tmp_path / "apps/sales-trends"
    assert (a / "sql_review/01_overview.sql").is_file()
    assert runner.invoke(app, ["validate-app", "sales-trends"]).exit_code == 1

    for rel in ("pages/overview.py", "queries/example_metric.sql"):
        (a / rel).unlink()
    (a / "queries/daily_sales.sql").write_text(
        "-- Query: daily_sales\n-- Feeds: Sales trend\n-- Schemas: ANALYTICS_DB.ANALYTICS\n"
        "-- Params: :1 start_date, :2 end_date\n"
        "SELECT\n    sold_date,\n    SUM(net_paid) AS net_paid\n"
        "FROM ANALYTICS_DB.ANALYTICS.STORE_SALES\n"
        "WHERE sold_date BETWEEN :1 AND :2\nGROUP BY sold_date\n",
        encoding="utf-8",
    )
    (a / "pages/sales_trend.py").write_text(
        '"""Sales trend."""\n\nimport streamlit as st\nfrom review import review_value\n'
        "from sql_loader import load_sql\n\n\n"
        "@st.cache_data(ttl=1800)\n"
        "def load_daily(start: str, end: str):\n"
        '    sql = load_sql("daily_sales")\n'
        '    return st.connection("snowflake").query(sql, params=[start, end], ttl=0)\n\n\n'
        'st.title("Sales trend")\n'
        'st.line_chart(review_value("net_paid_by_day", load_daily("2024-01-01", "2024-12-31")))\n',
        encoding="utf-8",
    )
    entry = a / "streamlit_app.py"
    entry.write_text(
        entry.read_text(encoding="utf-8").replace(
            'st.Page("pages/overview.py", title="Overview"',
            'st.Page("pages/sales_trend.py", title="Sales trend"',
        ),
        encoding="utf-8",
    )
    table = "ANALYTICS_DB.ANALYTICS.STORE_SALES"
    (a / "sql_review/index.yaml").write_text(
        "schema_version: 2\n"
        "app: sales-trends\n"
        "review_window:\n"
        f"  start_date: \"(SELECT DATEADD('year', -1, MAX(sold_date)) FROM {table})::DATE\"\n"
        f'  end_date: "(SELECT MAX(sold_date) FROM {table})::DATE"\n'
        "pages:\n"
        "  - path: pages/sales_trend.py\n"
        "    metrics:\n"
        "      - key: net_paid_by_day\n"
        "        query: queries/daily_sales.sql\n"
        '        binds: {"1": params.start_date, "2": params.end_date}\n'
        f"        reads: [{table}]\n"
        # The scaffold's About page stays in the nav, so it stays in the index.
        "  - path: pages/about.py\n"
        "    metrics: []\n",
        encoding="utf-8",
    )
    assert sql_review.main(["generate", "sales-trends", "--dir", str(tmp_path)]) == 0
    assert not (a / "sql_review/01_overview.sql").exists()  # generate removed the stale page
    assert (a / "sql_review/01_sales_trend.sql").is_file()
    result = runner.invoke(app, ["validate-app", "sales-trends", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert not [w for c in _json.loads(result.output)["checks"] for w in c.get("warnings", [])]

    # pages.md's documented check for this end state is validate-app. It once said
    # `grep -rn YOUR_TABLE apps/<slug>` must print nothing, but the app's own AGENTS.md
    # names the token in its instructions, so this correct app failed that check.
    assert "YOUR_TABLE" in (a / "AGENTS.md").read_text(encoding="utf-8")
    pages = (REPO_ROOT / "skills/build-app/pages.md").read_text(encoding="utf-8")
    assert "grep -rn YOUR_TABLE" not in pages
    assert "Then `streamsnow validate-app <slug>` must PASS" in pages


@_workflow_bash
def test_fresh_no_starter_repo_passes_its_own_checks_workflow(tmp_path):
    """`init --no-starter-app` (the /onboard setup path) writes no apps/ directory,
    and git cannot carry an empty one. The generated checks.yml then failed on its
    first push: `ruff check apps/` errors on a missing path, and `check tombstones`
    refuses a missing apps dir. Run every checks.yml step that needs no network,
    exactly as written, under bash in a fresh repo with origin/main set."""
    import shutil
    import subprocess

    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("needs bash and git")
    if shutil.which("ruff") is None or shutil.which("streamsnow") is None:
        pytest.skip("needs ruff and streamsnow on PATH (uv run pytest provides both)")
    init = ["init", "--no-starter-app", "--config", str(EXAMPLE_CONFIG), "--dir", str(tmp_path)]
    assert runner.invoke(app, init).exit_code == 0
    assert not (tmp_path / "apps").exists()
    git = ["git", "-c", "user.name=Acme", "-c", "user.email=ci@example.com"]
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
        ["update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run([*git, *cmd], cwd=tmp_path, check=True, capture_output=True)
    workflow = yaml.safe_load(
        (tmp_path / ".github/workflows/checks.yml").read_text(encoding="utf-8")
    )
    steps = {s.get("name"): s.get("run") for s in workflow["jobs"]["checks"]["steps"]}
    offline = [
        "Lint",
        "Governance gate (validate every app)",
        "SQL-review audit trail (fresh + complete)",
        "Tombstones (no abandoned deployed objects)",
    ]
    for name in offline:
        proc = subprocess.run(
            ["bash", "-eo", "pipefail", "-c", steps[name]],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.returncode == 0, (name, proc.stdout, proc.stderr)
    # The tombstones guard only skips while apps/ is absent on BOTH sides: once an
    # app exists the real check runs.
    assert "git ls-tree -d origin/main apps" in steps["Tombstones (no abandoned deployed objects)"]


def test_next_block_explains_preview_role_grants():
    from streamsnow.cli import PREVIEW_ROLE_NOTE

    assert "no data grants" in PREVIEW_ROLE_NOTE
    assert "CI role" in PREVIEW_ROLE_NOTE


def _warehouse_cfg() -> Config:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["runtime"] = "warehouse"
    data["snowflake"]["objects"]["compute_pool"] = ""
    data["snowflake"]["objects"]["external_access_integration"] = ""
    return Config.from_dict(data)


def test_warehouse_scaffold_pins_newest_supported_streamlit(tmp_path):
    from streamsnow.scaffolder import WAREHOUSE_STREAMLIT_PIN

    scaffold(_warehouse_cfg(), tmp_path, "sales-overview")
    env = (tmp_path / "apps/sales-overview/environment.yml").read_text(encoding="utf-8")
    assert f"streamlit={WAREHOUSE_STREAMLIT_PIN}" in env
    assert WAREHOUSE_STREAMLIT_PIN == "1.52.2"
    assert "cosmetic" not in env


def test_warehouse_scaffold_ships_dated_osv_allowlist(tmp_path):
    import datetime as dt
    import json

    from streamsnow.tools import check_dependency_vulns as cdv

    scaffold(_warehouse_cfg(), tmp_path, "sales-overview")
    entries = json.loads((tmp_path / "osv_allowlist.json").read_text(encoding="utf-8"))
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


def test_update_adds_missing_osv_allowlist_to_existing_warehouse_repo(tmp_path):
    import json

    cfg = _warehouse_cfg()
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["runtime"] = "warehouse"
    data["snowflake"]["objects"]["compute_pool"] = ""
    data["snowflake"]["objects"]["external_access_integration"] = ""
    (tmp_path / CONFIG_FILENAME).write_text(yaml.safe_dump(data), encoding="utf-8")
    scaffold(cfg, tmp_path, "sales-overview")
    (tmp_path / "osv_allowlist.json").unlink()  # a repo scaffolded before 0.7.1

    dry = runner.invoke(app, ["update", "--dir", str(tmp_path)])
    assert dry.exit_code == 0, dry.output
    assert "osv_allowlist.json" in dry.output
    assert not (tmp_path / "osv_allowlist.json").exists()

    res = runner.invoke(app, ["update", "--dir", str(tmp_path), "--apply"])
    assert res.exit_code == 0, res.output
    assert len(json.loads((tmp_path / "osv_allowlist.json").read_text(encoding="utf-8"))) == 4


def test_update_never_overwrites_an_existing_osv_allowlist(tmp_path):
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["runtime"] = "warehouse"
    data["snowflake"]["objects"]["compute_pool"] = ""
    data["snowflake"]["objects"]["external_access_integration"] = ""
    (tmp_path / CONFIG_FILENAME).write_text(yaml.safe_dump(data), encoding="utf-8")
    (tmp_path / "osv_allowlist.json").write_text("[]\n", encoding="utf-8")  # the user's own entries
    res = runner.invoke(app, ["update", "--dir", str(tmp_path), "--apply"])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "osv_allowlist.json").read_text(encoding="utf-8") == "[]\n"


def test_container_pyproject_is_dependencies_only(tmp_path):
    """The documented local setup is `uv pip install -e apps/<slug>`. With no package
    list, setuptools' flat-layout discovery found pages/, queries/ and sql_review/ and
    refused to build ("Multiple top-level packages discovered"), so the first preview
    of every container app failed at install. An explicit empty package list turns
    discovery off: the install resolves the dependencies and builds nothing."""
    import tomllib

    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    app_dir = tmp_path / "apps/acme-sales-dashboard"
    pyproject = tomllib.loads((app_dir / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["setuptools"]["packages"] == []
    # The directories that tripped discovery are all present in a fresh scaffold.
    assert all((app_dir / d).is_dir() for d in ("pages", "queries", "sql_review"))


def test_generated_gitignore_ignores_internal_notes(tmp_path):
    """.internal/ is where local-only working notes live (handoffs, evidence, drafts).
    A generated repo must never commit it."""
    import shutil
    import subprocess

    init = ["init", "--no-starter-app", "--config", str(EXAMPLE_CONFIG), "--dir", str(tmp_path)]
    assert runner.invoke(app, init).exit_code == 0
    assert ".internal/" in (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    proc = subprocess.run(
        ["git", "check-ignore", "-q", ".internal/notes.md"], cwd=tmp_path, check=False
    )
    assert proc.returncode == 0  # ignored
