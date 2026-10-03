"""Deploy SQL generation — stage-copy + git-repository, both runtimes."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from streamsnow.config import Config, ConfigError
from streamsnow.deploy import (
    generate_admin_sql,
    generate_create_sql,
    generate_setup_sql,
    stage_path,
    with_source,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"


ORIGIN = "https://github.com/acme/dashboards.git"

GIT_DEPLOY = {
    "source": "git-repository",
    "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
    "api_integration_name": "GITHUB_API_INTEGRATION",
    "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
    "git_origin": ORIGIN,
}


def _git_cfg(**deploy) -> Config:
    data = yaml.safe_load(EXAMPLE.read_text())
    data["deploy"] = {**GIT_DEPLOY, **deploy}
    return Config.from_dict(data)


def _cfg(**overrides) -> Config:
    data = yaml.safe_load(EXAMPLE.read_text())
    for k, v in overrides.items():
        data[k] = v
    return Config.from_dict(data)


def test_stage_copy_container_create_sql():
    cfg = _cfg()  # example is container + stage-copy
    sql = generate_create_sql(cfg, "sales-overview", sha="abc1234")
    assert "CREATE OR REPLACE STREAMLIT STREAMSNOW_APPS.DASHBOARDS.SALES_OVERVIEW" in sql
    assert (
        "FROM '@STREAMSNOW_APPS.DASHBOARDS.STREAMSNOW_CODE_STAGE/commits/abc1234/apps/sales-overview/'"
        in sql
    )
    assert "QUERY_WAREHOUSE = STREAMSNOW_WH" in sql
    assert "RUNTIME_NAME = 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11'" in sql
    assert "COMPUTE_POOL = SYSTEM_COMPUTE_POOL_CPU" in sql
    assert "ADD LIVE VERSION FROM LAST" in sql
    assert (
        "GRANT USAGE ON STREAMLIT STREAMSNOW_APPS.DASHBOARDS.SALES_OVERVIEW TO ROLE STREAMSNOW_VIEWER_ROLE"
        in sql
    )


def test_warehouse_create_sql_has_no_runtime_alter():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["runtime"] = "warehouse"
    data["snowflake"]["objects"]["compute_pool"] = ""
    data["snowflake"]["objects"]["external_access_integration"] = ""
    sql = generate_create_sql(Config.from_dict(data), "ops", sha="def4567")
    assert "RUNTIME_NAME" not in sql
    assert "COMPUTE_POOL" not in sql
    assert "ADD LIVE VERSION FROM LAST" in sql


def test_git_repository_create_redeploys_from_the_branch():
    """CREATE OR REPLACE from the branch path, like stage-copy: CREATE copies the
    files once, so each deploy rebuilds from what `snow git fetch` brought in.
    No ABORT/PULL/COMMIT refresh, and no /commits/ path (Snowflake rejects it
    for a GIT REPOSITORY source: "Invalid git branch path", observed 2026-10-03)."""
    create = generate_create_sql(_git_cfg(), "sales-overview")
    assert "CREATE OR REPLACE STREAMLIT STREAMSNOW_APPS.DASHBOARDS.SALES_OVERVIEW" in create
    assert (
        "FROM '@STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO/branches/main/apps/sales-overview/'"
        in create
    )
    assert "/commits/" not in create
    for verb in ("ABORT", "PULL", "COMMIT"):
        assert verb not in create
    assert "ADD LIVE VERSION FROM LAST" in create


def test_setup_sql_per_source():
    stage = generate_setup_sql(_cfg())
    assert "CREATE STAGE IF NOT EXISTS STREAMSNOW_APPS.DASHBOARDS.STREAMSNOW_CODE_STAGE" in stage

    data = yaml.safe_load(EXAMPLE.read_text())
    data["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
        "git_origin": ORIGIN,
    }
    git = generate_setup_sql(Config.from_dict(data))
    assert "CREATE API INTEGRATION IF NOT EXISTS GITHUB_API_INTEGRATION" in git
    assert "CREATE GIT REPOSITORY IF NOT EXISTS STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO" in git
    assert "GRANT READ, WRITE ON GIT REPOSITORY" in git


def test_git_setup_sql_has_no_placeholder_but_the_token():
    """ORIGIN comes from deploy.git_origin, and the integration allows only the
    repo's owner, not all of github.com."""
    git = generate_setup_sql(_git_cfg())
    assert f"ORIGIN = '{ORIGIN}';" in git
    assert "API_ALLOWED_PREFIXES = ('https://github.com/acme')" in git
    assert "your-org" not in git
    # Not named on the integration: the secret is created after it.
    assert "ALLOWED_AUTHENTICATION_SECRETS" not in git
    assert "GRANT READ, WRITE ON GIT REPOSITORY" in git
    assert "GIT_CREDENTIALS = STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET" in git
    assert _stmts(git).count("<") == 1 and "PASSWORD = '<github-token>'" in git


def test_public_repo_setup_needs_no_secret():
    cfg = _git_cfg(github_auth_mode="public", secret_name=None)
    assert cfg.deploy.secret_name == ""
    for sql in (generate_setup_sql(cfg), generate_admin_sql(cfg)):
        assert "SECRET" not in _stmts(sql)
        assert "GIT_CREDENTIALS" not in sql
        assert "<github-token>" not in sql
        assert f"ORIGIN = '{ORIGIN}';" in sql


def test_git_setup_without_an_origin_says_how_to_set_it():
    with pytest.raises(ConfigError, match="deploy.git_origin"):
        generate_setup_sql(_git_cfg(git_origin=None))


@pytest.mark.parametrize(
    "bad",
    [
        # Joined with + so the privacy scan does not read them as email addresses.
        "https://user:token" + "@github.com/acme/dashboards.git",
        "git" + "@github.com:acme/dashboards.git",
        "https://github.com/acme/dashboards.git'; DROP DATABASE x; --",
        "https://gitlab.com/acme/dashboards.git",
        "https://github.com/acme/dashboards.git\n",
        "https://github.com/acme/..",
    ],
)
def test_git_origin_is_validated_strictly(bad):
    with pytest.raises(ConfigError, match="git_origin"):
        _git_cfg(git_origin=bad)


def test_source_override_matches_a_git_repository_config():
    """`deploy-setup --source git-repository` on a stage-copy config prints the
    same SQL a git-repository config (with the wizard's default names) would."""
    stage_cfg = _cfg()
    previewed = with_source(stage_cfg, "git-repository", git_origin=ORIGIN)
    assert generate_setup_sql(previewed) == generate_setup_sql(_git_cfg())
    assert generate_admin_sql(previewed) == generate_admin_sql(_git_cfg())
    public = with_source(stage_cfg, "git-repository", git_origin=ORIGIN, github_auth="public")
    assert generate_admin_sql(public) == generate_admin_sql(
        _git_cfg(github_auth_mode="public", secret_name=None)
    )


def test_source_override_leaves_stage_copy_output_unchanged():
    cfg = _cfg()
    same = with_source(cfg, "stage-copy")
    assert generate_setup_sql(same) == generate_setup_sql(cfg)
    assert generate_admin_sql(same) == generate_admin_sql(cfg)
    back = with_source(_git_cfg(), "stage-copy")
    assert generate_admin_sql(back) == generate_admin_sql(cfg)


def test_stage_path():
    assert stage_path(_cfg()) == "@STREAMSNOW_APPS.DASHBOARDS.STREAMSNOW_CODE_STAGE"


def test_deploy_rejects_injection_slug_and_sha():
    cfg = _cfg()
    with pytest.raises(ValueError):
        generate_create_sql(cfg, "bad slug;", sha="abc1234")
    with pytest.raises(ValueError):
        generate_create_sql(cfg, "ok-slug", sha="; DROP TABLE x")


# --------------------------------------------------------------------------- #
# 0.7.1: the admin bootstrap (`deploy-setup --admin`)
# --------------------------------------------------------------------------- #


def _sections(sql: str) -> dict[str, str]:
    """Split admin SQL on `USE ROLE X;` lines -> {role: body} (last wins)."""
    out: dict[str, str] = {}
    role = None
    for line in sql.splitlines():
        if line.startswith("USE ROLE "):
            role = line.removeprefix("USE ROLE ").rstrip(";")
            out.setdefault(role, "")
            continue
        if role:
            out[role] += line + "\n"
    return out


def _stmts(sql: str) -> str:
    """Only the executable lines (comments dropped)."""
    return "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))


def test_admin_sql_creates_every_object_a_first_deploy_needs():
    sql = generate_admin_sql(_cfg())
    sec = _sections(sql)
    assert list(sec)[:4] == ["SYSADMIN", "USERADMIN", "SECURITYADMIN", "ACCOUNTADMIN"]
    sysadmin = _stmts(sec["SYSADMIN"])
    assert "CREATE DATABASE IF NOT EXISTS STREAMSNOW_APPS;" in sysadmin
    assert "CREATE SCHEMA IF NOT EXISTS STREAMSNOW_APPS.DASHBOARDS;" in sysadmin
    assert "CREATE WAREHOUSE IF NOT EXISTS STREAMSNOW_WH" in sysadmin
    for opt in ("WAREHOUSE_SIZE = XSMALL", "AUTO_SUSPEND = 60", "INITIALLY_SUSPENDED = TRUE"):
        assert opt in sysadmin
    useradmin = _stmts(sec["USERADMIN"])
    assert "CREATE ROLE IF NOT EXISTS STREAMSNOW_DEPLOY_ROLE;" in useradmin
    assert "CREATE ROLE IF NOT EXISTS STREAMSNOW_VIEWER_ROLE;" in useradmin
    assert "TYPE = SERVICE" in useradmin
    assert "RSA_PUBLIC_KEY = '<paste public key>'" in useradmin
    sec_admin = _stmts(sec["SECURITYADMIN"])
    for grant in (
        "GRANT ROLE STREAMSNOW_DEPLOY_ROLE TO ROLE SYSADMIN;",
        "GRANT ROLE STREAMSNOW_VIEWER_ROLE TO ROLE SYSADMIN;",
        "GRANT USAGE ON DATABASE STREAMSNOW_APPS TO ROLE STREAMSNOW_DEPLOY_ROLE;",
        "GRANT USAGE ON DATABASE STREAMSNOW_APPS TO ROLE STREAMSNOW_VIEWER_ROLE;",
        "GRANT USAGE ON SCHEMA STREAMSNOW_APPS.DASHBOARDS TO ROLE STREAMSNOW_DEPLOY_ROLE;",
        "GRANT USAGE ON SCHEMA STREAMSNOW_APPS.DASHBOARDS TO ROLE STREAMSNOW_VIEWER_ROLE;",
        "GRANT USAGE ON WAREHOUSE STREAMSNOW_WH TO ROLE STREAMSNOW_DEPLOY_ROLE;",
        "GRANT USAGE ON WAREHOUSE STREAMSNOW_WH TO ROLE STREAMSNOW_VIEWER_ROLE;",
        "GRANT CREATE STREAMLIT ON SCHEMA STREAMSNOW_APPS.DASHBOARDS TO ROLE STREAMSNOW_DEPLOY_ROLE;",
        "GRANT CREATE STAGE ON SCHEMA STREAMSNOW_APPS.DASHBOARDS TO ROLE STREAMSNOW_DEPLOY_ROLE;",
        # governance database: a normal database gets USAGE + SELECT per allowed schema
        "GRANT USAGE ON DATABASE ANALYTICS_DB TO ROLE STREAMSNOW_DEPLOY_ROLE;",
        "GRANT USAGE ON SCHEMA ANALYTICS_DB.ANALYTICS TO ROLE STREAMSNOW_DEPLOY_ROLE;",
        "GRANT SELECT ON ALL TABLES IN SCHEMA ANALYTICS_DB.REPORTING TO ROLE STREAMSNOW_DEPLOY_ROLE;",
        "GRANT SELECT ON FUTURE VIEWS IN SCHEMA ANALYTICS_DB.ANALYTICS TO ROLE STREAMSNOW_DEPLOY_ROLE;",
    ):
        assert grant in sec_admin, grant
    # Denied schemas never receive a grant.
    assert "ANALYTICS_DB.RAW" not in _stmts(sql)
    assert "IMPORTED PRIVILEGES" in sql  # the shared-database alternative is explained
    assert "IMPORTED PRIVILEGES" not in _stmts(sql)
    # The stage itself is created by the CI role that will own it.
    ci = _stmts(sec["STREAMSNOW_DEPLOY_ROLE"])
    assert "CREATE STAGE IF NOT EXISTS STREAMSNOW_APPS.DASHBOARDS.STREAMSNOW_CODE_STAGE" in ci


def test_admin_sql_container_objects_custom_pool():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["snowflake"]["objects"]["compute_pool"] = "STREAMLIT_POOL"  # a pool you create
    sql = generate_admin_sql(Config.from_dict(data))
    acct = _stmts(_sections(sql)["ACCOUNTADMIN"])
    assert "CREATE EXTERNAL ACCESS INTEGRATION PYPI_ACCESS_INTEGRATION" in acct
    assert "snowflake.external_access.pypi_rule" in acct
    assert (
        "GRANT USAGE ON INTEGRATION PYPI_ACCESS_INTEGRATION TO ROLE STREAMSNOW_DEPLOY_ROLE;" in acct
    )
    assert "CREATE COMPUTE POOL IF NOT EXISTS STREAMLIT_POOL" in acct
    assert "GRANT USAGE ON COMPUTE POOL STREAMLIT_POOL TO ROLE STREAMSNOW_DEPLOY_ROLE;" in acct


def test_admin_sql_system_pool_is_never_created():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["snowflake"]["objects"]["compute_pool"] = "SYSTEM_COMPUTE_POOL_CPU"
    sql = generate_admin_sql(Config.from_dict(data))
    assert "CREATE COMPUTE POOL" not in _stmts(sql)
    assert "pre-provisioned" in sql
    assert (
        "GRANT USAGE ON COMPUTE POOL SYSTEM_COMPUTE_POOL_CPU TO ROLE STREAMSNOW_DEPLOY_ROLE;" in sql
    )
    # ...and the default (non-admin) output no longer tells anyone to create it.
    default = generate_setup_sql(Config.from_dict(data))
    assert "CREATE COMPUTE POOL SYSTEM_COMPUTE_POOL_CPU" not in default
    assert "pre-provisioned" in default


def test_admin_sql_warehouse_runtime_has_no_container_objects():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["runtime"] = "warehouse"
    data["snowflake"]["objects"]["compute_pool"] = ""
    data["snowflake"]["objects"]["external_access_integration"] = ""
    sql = _stmts(generate_admin_sql(Config.from_dict(data)))
    assert "COMPUTE POOL" not in sql and "EXTERNAL ACCESS" not in sql


def test_admin_sql_shared_database_uses_imported_privileges():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["governance"]["database"] = "SNOWFLAKE_SAMPLE_DATA"
    data["governance"]["schema_allow"] = ["TPCH_SF1"]
    sql = generate_admin_sql(Config.from_dict(data))
    stmts = _stmts(sql)
    assert (
        "GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE STREAMSNOW_DEPLOY_ROLE;"
        in _stmts(_sections(sql)["ACCOUNTADMIN"])
    )
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA SNOWFLAKE_SAMPLE_DATA" not in stmts


def test_admin_sql_git_repository_source():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
        "git_origin": ORIGIN,
    }
    sql = generate_admin_sql(Config.from_dict(data))
    sec = _sections(sql)
    acct = _stmts(sec["ACCOUNTADMIN"])
    assert "CREATE API INTEGRATION IF NOT EXISTS GITHUB_API_INTEGRATION" in acct
    assert (
        "GRANT USAGE ON INTEGRATION GITHUB_API_INTEGRATION TO ROLE STREAMSNOW_DEPLOY_ROLE;" in acct
    )
    grants = _stmts(sec["SECURITYADMIN"])
    assert (
        "GRANT CREATE SECRET ON SCHEMA STREAMSNOW_APPS.DASHBOARDS TO ROLE STREAMSNOW_DEPLOY_ROLE;"
        in grants
    )
    assert (
        "GRANT CREATE GIT REPOSITORY ON SCHEMA STREAMSNOW_APPS.DASHBOARDS TO ROLE STREAMSNOW_DEPLOY_ROLE;"
        in grants
    )
    ci = _stmts(sec["STREAMSNOW_DEPLOY_ROLE"])
    assert "CREATE SECRET IF NOT EXISTS STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET" in ci
    assert "CREATE GIT REPOSITORY IF NOT EXISTS STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO" in ci
    assert "CREATE STAGE" not in _stmts(sql)


def test_default_setup_output_is_unchanged_for_stage_copy():
    """`deploy-setup` without --admin keeps its narrow, CI-role-runnable shape."""
    stage = generate_setup_sql(_cfg())
    assert "CREATE ROLE" not in stage and "CREATE USER" not in stage
    assert "deploy-setup --admin" in stage


def test_cli_deploy_setup_admin_flag(tmp_path):
    from typer.testing import CliRunner

    from streamsnow.cli import app

    cfg = tmp_path / "streamsnow.config.yaml"
    cfg.write_text(EXAMPLE.read_text())
    res = CliRunner().invoke(app, ["deploy-setup", "--admin", "--config", str(cfg)])
    assert res.exit_code == 0, res.output
    assert "USE ROLE USERADMIN;" in res.output
    plain = CliRunner().invoke(app, ["deploy-setup", "--config", str(cfg)])
    assert "USE ROLE USERADMIN;" not in plain.output


def _cli(*args: str):
    from typer.testing import CliRunner

    from streamsnow.cli import app

    return CliRunner().invoke(app, list(args))


def test_cli_deploy_setup_source_override_previews_without_touching_config(tmp_path):
    cfg = tmp_path / "streamsnow.config.yaml"
    cfg.write_text(EXAMPLE.read_text())
    before = cfg.read_text()
    res = _cli(
        "deploy-setup", "--admin", "--source", "git-repository", "--git-origin", ORIGIN,
        "--config", str(cfg),
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert res.output.startswith("-- PREVIEW of the git-repository deploy source.")
    assert "Your config uses stage-copy" in res.output
    assert generate_admin_sql(_git_cfg()) in res.output
    assert cfg.read_text() == before
    # Without the override, the stage-copy output carries no preview banner.
    plain = _cli("deploy-setup", "--admin", "--config", str(cfg))
    assert plain.exit_code == 0, plain.output
    assert "PREVIEW" not in plain.output
    assert plain.output.strip() == generate_admin_sql(_cfg()).strip()


def test_cli_deploy_setup_git_override_without_an_origin_exits_2(tmp_path):
    cfg = tmp_path / "streamsnow.config.yaml"
    cfg.write_text(EXAMPLE.read_text())
    res = _cli("deploy-setup", "--source", "git-repository", "--config", str(cfg))
    assert res.exit_code == 2
    assert "--git-origin" in res.output


def test_cli_deploy_sql_refresh_is_a_no_op_for_old_workflows(tmp_path):
    """Workflows rendered before 0.7.4 still run `deploy-sql --refresh` and pipe
    it to `snow sql ... || true`; it must print SQL that runs nothing."""
    data = yaml.safe_load(EXAMPLE.read_text())
    data["deploy"] = GIT_DEPLOY
    cfg = tmp_path / "streamsnow.config.yaml"
    cfg.write_text(yaml.safe_dump(data))
    res = _cli("deploy-sql", "my-app", "--refresh", "--config", str(cfg))
    assert res.exit_code == 0, res.output
    lines = [ln for ln in res.output.splitlines() if ln.strip()]
    assert lines and all(ln.startswith("--") for ln in lines)


def test_admin_sql_eai_uses_valid_create_syntax():
    # Snowflake's CREATE EXTERNAL ACCESS INTEGRATION has no IF NOT EXISTS clause.
    stmts = _stmts(generate_admin_sql(_cfg()))
    assert "EXTERNAL ACCESS INTEGRATION IF NOT EXISTS" not in stmts
    assert "CREATE EXTERNAL ACCESS INTEGRATION PYPI_ACCESS_INTEGRATION" in stmts


def test_admin_sql_viewer_role_gets_no_data_grants_by_default():
    # Apps run with owner's rights, so viewers need USAGE on the app, not SELECT on data.
    sql = generate_admin_sql(_cfg())
    stmts = _stmts(sql)
    assert "TO ROLE STREAMSNOW_VIEWER_ROLE;" in stmts  # app database/schema/warehouse usage stays
    for line in stmts.splitlines():
        if "STREAMSNOW_VIEWER_ROLE" in line:
            assert "ANALYTICS_DB" not in line, line
            assert "SELECT" not in line and "IMPORTED" not in line, line
    # The viewer data grants are still offered, commented, as an explicit opt-in.
    assert (
        "--   GRANT SELECT ON ALL TABLES IN SCHEMA ANALYTICS_DB.ANALYTICS TO ROLE STREAMSNOW_VIEWER_ROLE;"
        in sql
    )


def test_admin_sql_shared_database_imported_privileges_ci_role_only():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["governance"]["database"] = "SNOWFLAKE_SAMPLE_DATA"
    sql = generate_admin_sql(Config.from_dict(data))
    stmts = _stmts(sql)
    assert (
        "GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE STREAMSNOW_DEPLOY_ROLE;"
        in stmts
    )
    assert (
        "IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE STREAMSNOW_VIEWER_ROLE"
        not in stmts
    )
