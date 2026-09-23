"""Deploy SQL generation — stage-copy + git-repository, both runtimes."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from streamsnow.config import Config
from streamsnow.deploy import (
    generate_admin_sql,
    generate_create_sql,
    generate_refresh_sql,
    generate_setup_sql,
    stage_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"


def _cfg(**overrides) -> Config:
    data = yaml.safe_load(EXAMPLE.read_text())
    for k, v in overrides.items():
        data[k] = v
    return Config.from_dict(data)


def test_stage_copy_container_create_sql():
    cfg = _cfg()  # example is container + stage-copy
    sql = generate_create_sql(cfg, "sales-overview", sha="abc1234")
    assert "CREATE OR REPLACE STREAMLIT DATA_APPS.BI_APPS.SALES_OVERVIEW" in sql
    assert (
        "FROM '@DATA_APPS.BI_APPS.STREAMLIT_CODE_STAGE/commits/abc1234/apps/sales-overview/'" in sql
    )
    assert "QUERY_WAREHOUSE = STREAMLIT_WH" in sql
    assert "RUNTIME_NAME = 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11'" in sql
    assert "COMPUTE_POOL = STREAMLIT_POOL" in sql
    assert "ADD LIVE VERSION FROM LAST" in sql
    assert (
        "GRANT USAGE ON STREAMLIT DATA_APPS.BI_APPS.SALES_OVERVIEW TO ROLE STREAMLIT_APP_ROLE"
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


def test_git_repository_create_and_refresh():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "DATA_APPS.BI_APPS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "DATA_APPS.BI_APPS.GITHUB_PAT_SECRET",
    }
    cfg = Config.from_dict(data)
    create = generate_create_sql(cfg, "sales-overview")
    assert "CREATE STREAMLIT IF NOT EXISTS" in create
    assert "FROM '@DATA_APPS.BI_APPS.STREAMLIT_REPO/branches/main/apps/sales-overview/'" in create
    refresh = generate_refresh_sql(cfg, "sales-overview")
    for verb in ("ABORT;", "PULL;", "COMMIT;", "ADD LIVE VERSION FROM LAST;"):
        assert verb in refresh


def test_setup_sql_per_source():
    stage = generate_setup_sql(_cfg())
    assert "CREATE STAGE IF NOT EXISTS DATA_APPS.BI_APPS.STREAMLIT_CODE_STAGE" in stage

    data = yaml.safe_load(EXAMPLE.read_text())
    data["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "DATA_APPS.BI_APPS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "DATA_APPS.BI_APPS.GITHUB_PAT_SECRET",
    }
    git = generate_setup_sql(Config.from_dict(data))
    assert "CREATE API INTEGRATION IF NOT EXISTS GITHUB_API_INTEGRATION" in git
    assert "CREATE GIT REPOSITORY IF NOT EXISTS DATA_APPS.BI_APPS.STREAMLIT_REPO" in git
    assert "GRANT READ ON GIT REPOSITORY" in git


def test_stage_path():
    assert stage_path(_cfg()) == "@DATA_APPS.BI_APPS.STREAMLIT_CODE_STAGE"


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
    assert "CREATE DATABASE IF NOT EXISTS DATA_APPS;" in sysadmin
    assert "CREATE SCHEMA IF NOT EXISTS DATA_APPS.BI_APPS;" in sysadmin
    assert "CREATE WAREHOUSE IF NOT EXISTS STREAMLIT_WH" in sysadmin
    for opt in ("WAREHOUSE_SIZE = XSMALL", "AUTO_SUSPEND = 60", "INITIALLY_SUSPENDED = TRUE"):
        assert opt in sysadmin
    useradmin = _stmts(sec["USERADMIN"])
    assert "CREATE ROLE IF NOT EXISTS STREAMLIT_CI_ROLE;" in useradmin
    assert "CREATE ROLE IF NOT EXISTS STREAMLIT_APP_ROLE;" in useradmin
    assert "TYPE = SERVICE" in useradmin
    assert "RSA_PUBLIC_KEY = '<paste public key>'" in useradmin
    sec_admin = _stmts(sec["SECURITYADMIN"])
    for grant in (
        "GRANT ROLE STREAMLIT_CI_ROLE TO ROLE SYSADMIN;",
        "GRANT ROLE STREAMLIT_APP_ROLE TO ROLE SYSADMIN;",
        "GRANT USAGE ON DATABASE DATA_APPS TO ROLE STREAMLIT_CI_ROLE;",
        "GRANT USAGE ON DATABASE DATA_APPS TO ROLE STREAMLIT_APP_ROLE;",
        "GRANT USAGE ON SCHEMA DATA_APPS.BI_APPS TO ROLE STREAMLIT_CI_ROLE;",
        "GRANT USAGE ON SCHEMA DATA_APPS.BI_APPS TO ROLE STREAMLIT_APP_ROLE;",
        "GRANT USAGE ON WAREHOUSE STREAMLIT_WH TO ROLE STREAMLIT_CI_ROLE;",
        "GRANT USAGE ON WAREHOUSE STREAMLIT_WH TO ROLE STREAMLIT_APP_ROLE;",
        "GRANT CREATE STREAMLIT ON SCHEMA DATA_APPS.BI_APPS TO ROLE STREAMLIT_CI_ROLE;",
        "GRANT CREATE STAGE ON SCHEMA DATA_APPS.BI_APPS TO ROLE STREAMLIT_CI_ROLE;",
        # governance database: a normal database gets USAGE + SELECT per allowed schema
        "GRANT USAGE ON DATABASE ANALYTICS_DB TO ROLE STREAMLIT_CI_ROLE;",
        "GRANT USAGE ON SCHEMA ANALYTICS_DB.ANALYTICS TO ROLE STREAMLIT_CI_ROLE;",
        "GRANT SELECT ON ALL TABLES IN SCHEMA ANALYTICS_DB.REPORTING TO ROLE STREAMLIT_CI_ROLE;",
        "GRANT SELECT ON FUTURE VIEWS IN SCHEMA ANALYTICS_DB.ANALYTICS TO ROLE STREAMLIT_CI_ROLE;",
    ):
        assert grant in sec_admin, grant
    # Denied schemas never receive a grant.
    assert "ANALYTICS_DB.RAW" not in _stmts(sql)
    assert "IMPORTED PRIVILEGES" in sql  # the shared-database alternative is explained
    assert "IMPORTED PRIVILEGES" not in _stmts(sql)
    # The stage itself is created by the CI role that will own it.
    ci = _stmts(sec["STREAMLIT_CI_ROLE"])
    assert "CREATE STAGE IF NOT EXISTS DATA_APPS.BI_APPS.STREAMLIT_CODE_STAGE" in ci


def test_admin_sql_container_objects_custom_pool():
    sql = generate_admin_sql(_cfg())  # example pool is STREAMLIT_POOL
    acct = _stmts(_sections(sql)["ACCOUNTADMIN"])
    assert "CREATE EXTERNAL ACCESS INTEGRATION PYPI_ACCESS_INTEGRATION" in acct
    assert "snowflake.external_access.pypi_rule" in acct
    assert "GRANT USAGE ON INTEGRATION PYPI_ACCESS_INTEGRATION TO ROLE STREAMLIT_CI_ROLE;" in acct
    assert "CREATE COMPUTE POOL IF NOT EXISTS STREAMLIT_POOL" in acct
    assert "GRANT USAGE ON COMPUTE POOL STREAMLIT_POOL TO ROLE STREAMLIT_CI_ROLE;" in acct


def test_admin_sql_system_pool_is_never_created():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["snowflake"]["objects"]["compute_pool"] = "SYSTEM_COMPUTE_POOL_CPU"
    sql = generate_admin_sql(Config.from_dict(data))
    assert "CREATE COMPUTE POOL" not in _stmts(sql)
    assert "pre-provisioned" in sql
    assert "GRANT USAGE ON COMPUTE POOL SYSTEM_COMPUTE_POOL_CPU TO ROLE STREAMLIT_CI_ROLE;" in sql
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
        "GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE STREAMLIT_CI_ROLE;"
        in _stmts(_sections(sql)["ACCOUNTADMIN"])
    )
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA SNOWFLAKE_SAMPLE_DATA" not in stmts


def test_admin_sql_git_repository_source():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "DATA_APPS.BI_APPS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "DATA_APPS.BI_APPS.GITHUB_PAT_SECRET",
    }
    sql = generate_admin_sql(Config.from_dict(data))
    sec = _sections(sql)
    acct = _stmts(sec["ACCOUNTADMIN"])
    assert "CREATE API INTEGRATION IF NOT EXISTS GITHUB_API_INTEGRATION" in acct
    assert "GRANT USAGE ON INTEGRATION GITHUB_API_INTEGRATION TO ROLE STREAMLIT_CI_ROLE;" in acct
    grants = _stmts(sec["SECURITYADMIN"])
    assert "GRANT CREATE SECRET ON SCHEMA DATA_APPS.BI_APPS TO ROLE STREAMLIT_CI_ROLE;" in grants
    assert (
        "GRANT CREATE GIT REPOSITORY ON SCHEMA DATA_APPS.BI_APPS TO ROLE STREAMLIT_CI_ROLE;"
        in grants
    )
    ci = _stmts(sec["STREAMLIT_CI_ROLE"])
    assert "CREATE SECRET IF NOT EXISTS DATA_APPS.BI_APPS.GITHUB_PAT_SECRET" in ci
    assert "CREATE GIT REPOSITORY IF NOT EXISTS DATA_APPS.BI_APPS.STREAMLIT_REPO" in ci
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


def test_admin_sql_eai_uses_valid_create_syntax():
    # Snowflake's CREATE EXTERNAL ACCESS INTEGRATION has no IF NOT EXISTS clause.
    stmts = _stmts(generate_admin_sql(_cfg()))
    assert "EXTERNAL ACCESS INTEGRATION IF NOT EXISTS" not in stmts
    assert "CREATE EXTERNAL ACCESS INTEGRATION PYPI_ACCESS_INTEGRATION" in stmts


def test_admin_sql_viewer_role_gets_no_data_grants_by_default():
    # Apps run with owner's rights, so viewers need USAGE on the app, not SELECT on data.
    sql = generate_admin_sql(_cfg())
    stmts = _stmts(sql)
    assert "TO ROLE STREAMLIT_APP_ROLE;" in stmts  # app database/schema/warehouse usage stays
    for line in stmts.splitlines():
        if "STREAMLIT_APP_ROLE" in line:
            assert "ANALYTICS_DB" not in line, line
            assert "SELECT" not in line and "IMPORTED" not in line, line
    # The viewer data grants are still offered, commented, as an explicit opt-in.
    assert (
        "--   GRANT SELECT ON ALL TABLES IN SCHEMA ANALYTICS_DB.ANALYTICS TO ROLE STREAMLIT_APP_ROLE;"
        in sql
    )


def test_admin_sql_shared_database_imported_privileges_ci_role_only():
    data = yaml.safe_load(EXAMPLE.read_text())
    data["governance"]["database"] = "SNOWFLAKE_SAMPLE_DATA"
    sql = generate_admin_sql(Config.from_dict(data))
    stmts = _stmts(sql)
    assert (
        "GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE STREAMLIT_CI_ROLE;"
        in stmts
    )
    assert (
        "IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE STREAMLIT_APP_ROLE"
        not in stmts
    )
