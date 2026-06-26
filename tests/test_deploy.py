"""Deploy SQL generation — stage-copy + git-repository, both runtimes."""

from __future__ import annotations

from pathlib import Path

import yaml

from streamsnow.config import Config
from streamsnow.deploy import (
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
    sql = generate_create_sql(cfg, "sales-overview", sha="abc123")
    assert "CREATE OR REPLACE STREAMLIT DATA_APPS.BI_APPS.SALES_OVERVIEW" in sql
    assert (
        "FROM '@DATA_APPS.BI_APPS.STREAMLIT_CODE_STAGE/commits/abc123/apps/sales-overview/'" in sql
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
    sql = generate_create_sql(Config.from_dict(data), "ops", sha="def456")
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
