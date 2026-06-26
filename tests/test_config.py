"""Config validation tests — the typed model + the injection-safety gate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from streamsnow.config import (
    CONFIG_SCHEMA_VERSION,
    Config,
    ConfigError,
    normalize_account,
    quote_ident,
    quote_sql_literal,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _base() -> dict:
    """A valid container + stage-copy config dict, ready to mutate per test."""
    return {
        "schema_version": 1,
        "runtime": "container",
        "project": {"name": "Acme Dashboards", "slug": "acme-dashboards"},
        "snowflake": {
            "account": "ab12345.us-east-1",
            "connection_name": "acme",
            "objects": {
                "app_database": "DATA_APPS",
                "app_schema": "BI_APPS",
                "stage_database": "DATA_APPS",
                "stage_schema": "BI_APPS",
                "default_warehouse": "STREAMLIT_WH",
                "allowed_warehouses": ["STREAMLIT_WH"],
                "compute_pool": "STREAMLIT_POOL",
                "external_access_integration": "PYPI_ACCESS_INTEGRATION",
            },
            "roles": {"ci_role": "STREAMLIT_CI_ROLE", "viewer_role": "STREAMLIT_APP_ROLE"},
        },
        "governance": {
            "database": "ANALYTICS_DB",
            "schema_allow": ["ANALYTICS", "REPORTING"],
            "schema_deny": ["RAW", "STAGING", "BRIDGE"],
        },
        "deploy": {"source": "stage-copy"},
    }


def test_valid_container_config_loads():
    cfg = Config.from_dict(_base())
    assert cfg.runtime == "container"
    assert cfg.deploy.source == "stage-copy"
    assert cfg.snowflake.objects.compute_pool == "STREAMLIT_POOL"
    assert "ANALYTICS" in cfg.governance.schema_allow


def test_example_file_is_valid():
    data = yaml.safe_load((REPO_ROOT / "streamsnow.config.example.yaml").read_text())
    cfg = Config.from_dict(data)
    assert cfg.project.slug == "acme-dashboards"


def test_account_normalization_strips_hostname_and_scheme():
    assert (
        normalize_account("https://ab12345.us-east-1.snowflakecomputing.com") == "ab12345.us-east-1"
    )
    assert normalize_account("ab12345.us-east-1") == "ab12345.us-east-1"


@pytest.mark.parametrize(
    "bad", ["RAW; DROP TABLE x", "ANALYTICS'", "has space", "1starts_with_digit", "a-b"]
)
def test_injection_or_malformed_identifier_rejected(bad):
    d = _base()
    d["governance"]["schema_deny"] = [bad]
    with pytest.raises(ConfigError):
        Config.from_dict(d)


def test_container_requires_compute_pool_and_eai():
    d = _base()
    d["snowflake"]["objects"]["compute_pool"] = ""
    with pytest.raises(ConfigError):
        Config.from_dict(d)


def test_warehouse_runtime_ok_without_pool():
    d = _base()
    d["runtime"] = "warehouse"
    d["snowflake"]["objects"]["compute_pool"] = ""
    d["snowflake"]["objects"]["external_access_integration"] = ""
    cfg = Config.from_dict(d)
    assert cfg.runtime == "warehouse"


def test_git_repository_deploy_requires_fields():
    d = _base()
    d["deploy"] = {"source": "git-repository"}  # missing repo fqn etc.
    with pytest.raises(ConfigError):
        Config.from_dict(d)


def test_git_repository_deploy_valid():
    d = _base()
    d["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "DATA_APPS.BI_APPS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "DATA_APPS.BI_APPS.GITHUB_PAT_SECRET",
    }
    cfg = Config.from_dict(d)
    assert cfg.deploy.source == "git-repository"
    assert cfg.deploy.git_branch == "main"


def test_bad_runtime_rejected():
    d = _base()
    d["runtime"] = "kubernetes"
    with pytest.raises(ConfigError):
        Config.from_dict(d)


def test_schema_version_newer_than_supported_rejected():
    d = _base()
    d["schema_version"] = CONFIG_SCHEMA_VERSION + 1
    with pytest.raises(ConfigError):
        Config.from_dict(d)


def test_missing_required_section_rejected():
    d = _base()
    del d["snowflake"]
    with pytest.raises(ConfigError):
        Config.from_dict(d)


def test_quote_helpers():
    assert quote_ident("ANALYTICS") == "ANALYTICS"
    assert quote_ident('weird"name') == '"weird""name"'
    assert quote_sql_literal("O'Brien") == "'O''Brien'"


@pytest.mark.parametrize(
    "path,bad",
    [
        (("snowflake", "account"), 'foo"; DROP'),
        (("snowflake", "connection_name"), "bad name;"),
        (("snowflake", "objects", "default_warehouse"), "WH; DROP"),
        (("snowflake", "roles", "ci_role"), "ROLE'"),
        (("snowflake", "objects", "runtime_name"), "bad runtime!"),
        (("snowflake", "objects", "container_python"), "3.11; rm -rf"),
    ],
)
def test_injection_rejected_across_all_rendered_fields(path, bad):
    d = _base()
    node = d
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = bad
    with pytest.raises(ConfigError):
        Config.from_dict(d)


def test_empty_schema_allow_rejected():
    d = _base()
    d["governance"]["schema_allow"] = []
    with pytest.raises(ConfigError):
        Config.from_dict(d)


def test_read_exceptions_must_be_fqn():
    d = _base()
    d["governance"]["read_exceptions"] = ["not a fqn!"]
    with pytest.raises(ConfigError):
        Config.from_dict(d)


def test_account_trailing_junk_after_hostname_is_stripped():
    # Everything after the snowflakecomputing.com host is dropped, yielding a
    # clean locator — metacharacters there do not survive normalization.
    assert (
        normalize_account("https://ab12345.us-east-1.snowflakecomputing.com/$(whoami)")
        == "ab12345.us-east-1"
    )


def test_bare_account_with_metacharacters_rejected():
    d = _base()
    d["snowflake"]["account"] = "ab123$(whoami)"
    with pytest.raises(ConfigError):
        Config.from_dict(d)
