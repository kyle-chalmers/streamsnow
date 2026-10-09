"""Config validation tests — the typed model + the injection-safety gate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from streamsnow.config import (
    CONFIG_SCHEMA_VERSION,
    V1_CONFIG_ERROR,
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
        "schema_version": 2,
        "runtime": "container",
        "project": {"name": "Acme Dashboards", "slug": "acme-dashboards"},
        "snowflake": {
            "account": "ab12345.us-east-1",
            "connection_name": "acme",
            "objects": {
                "app_database": "STREAMSNOW_APPS",
                "app_schema": "DASHBOARDS",
                "stage_database": "STREAMSNOW_APPS",
                "stage_schema": "DASHBOARDS",
                "default_warehouse": "STREAMSNOW_WH",
                "allowed_warehouses": ["STREAMSNOW_WH"],
                "compute_pool": "SYSTEM_COMPUTE_POOL_CPU",
                "external_access_integration": "PYPI_ACCESS_INTEGRATION",
            },
            "roles": {"ci_role": "STREAMSNOW_DEPLOY_ROLE", "viewer_role": "STREAMSNOW_VIEWER_ROLE"},
        },
        "governance": {
            "sources": ["ANALYTICS_DB.ANALYTICS", "ANALYTICS_DB.REPORTING"],
            "schema_deny": ["RAW", "STAGING", "BRIDGE"],
        },
        "deploy": {"source": "stage-copy"},
    }


def test_valid_container_config_loads():
    cfg = Config.from_dict(_base())
    assert cfg.runtime == "container"
    assert cfg.deploy.source == "stage-copy"
    assert cfg.snowflake.objects.compute_pool == "SYSTEM_COMPUTE_POOL_CPU"
    assert "ANALYTICS_DB.ANALYTICS" in cfg.governance.sources


def test_example_file_is_valid():
    data = yaml.safe_load(
        (REPO_ROOT / "streamsnow.config.example.yaml").read_text(encoding="utf-8")
    )
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
        "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
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


def test_empty_sources_rejected():
    d = _base()
    d["governance"]["sources"] = []
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


def test_invalid_yaml_error_names_the_position_not_the_content(tmp_path):
    # --config can point at a file holding a secret; the error must not quote it.
    from streamsnow.config import ConfigError, load_config

    bad = tmp_path / "x.yaml"
    bad.write_text("a: [unclosed SECRETVALUE\n", encoding="utf-8")
    with pytest.raises(ConfigError) as err:
        load_config(bad)
    assert "invalid YAML at line" in str(err.value)
    assert "SECRETVALUE" not in str(err.value)


def test_explicit_missing_config_path_is_a_config_error(tmp_path):
    """An explicit --config path that doesn't exist must raise the friendly
    ConfigError, not a raw FileNotFoundError traceback (seen live from
    `streamsnow update` run outside a configured repo)."""
    from streamsnow.config import load_config

    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_path / "streamsnow.config.yaml")


# --------------------------------------------------------------------------- #
# 0.7: sql_review policy + artifact_exclude are typed blocks, never tracebacks
# --------------------------------------------------------------------------- #
def test_sql_review_defaults_to_warn_and_accepts_fail():
    assert Config.from_dict(_base()).sql_review.coverage == "warn"
    d = _base()
    d["sql_review"] = {"coverage": "fail"}
    assert Config.from_dict(d).sql_review.coverage == "fail"


@pytest.mark.parametrize("bad", ["warn", ["warn"], 1, {"coverage": "maybe"}])
def test_malformed_sql_review_block_is_a_config_error(bad):
    d = _base()
    d["sql_review"] = bad
    with pytest.raises(ConfigError):
        Config.from_dict(d)


@pytest.mark.parametrize("bad", ["stage-copy", ["stage-copy"]])
def test_scalar_deploy_block_is_a_config_error(bad):
    d = _base()
    d["deploy"] = bad
    with pytest.raises(ConfigError):
        Config.from_dict(d)


def test_artifact_exclude_must_be_a_list():
    d = _base()
    d["deploy"]["artifact_exclude"] = ".streamlit/config.toml"
    with pytest.raises(ConfigError):
        Config.from_dict(d)


def test_v1_config_is_rejected_with_the_new_keys_named():
    d = _base()
    d["schema_version"] = 1
    d["governance"] = {"database": "ANALYTICS_DB", "schema_allow": ["ANALYTICS"]}
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(d)
    msg = str(exc.value)
    assert msg.startswith(V1_CONFIG_ERROR)
    assert "governance.sources" in msg and "governance.app_data" in msg
    assert "streamsnow configure" in msg and "--reconfigure" not in msg  # C10
    assert "(found governance.database, governance.schema_allow)" in msg


def test_retired_keys_in_a_v2_file_are_rejected_too():
    d = _base()
    d["governance"]["database"] = "ANALYTICS_DB"
    with pytest.raises(ConfigError, match="found governance.database"):
        Config.from_dict(d)


def test_app_data_defaults_into_the_app_database():
    cfg = Config.from_dict(_base())
    assert cfg.governance.app_data == "STREAMSNOW_APPS.STREAMSNOW_REPORTING"
    assert cfg.governance.boundary == "warn"
    assert cfg.governance.imported_databases == ()


def test_sources_are_uppercased_and_deduplicated():
    d = _base()
    d["governance"]["sources"] = [
        "analytics_db.reporting",
        "FINANCE_DB.MARTS",
        "ANALYTICS_DB.REPORTING",
    ]
    assert Config.from_dict(d).governance.sources == ("ANALYTICS_DB.REPORTING", "FINANCE_DB.MARTS")


@pytest.mark.parametrize("bad", ["REPORTING", "A.B.C", "A-B.C", "A.", "A.B;DROP", '"a".b'])
def test_a_source_must_be_exactly_database_dot_schema(bad):
    d = _base()
    d["governance"]["sources"] = [bad]
    with pytest.raises(ConfigError, match="DATABASE.SCHEMA"):
        Config.from_dict(d)


@pytest.mark.parametrize(
    ("deny", "ok"), [(["FINANCE.RAW"], True), (["RAW"], True), (["A.B.C"], False), (["A-B"], False)]
)
def test_deny_entries_are_bare_or_qualified(deny, ok):
    d = _base()
    d["governance"]["schema_deny"] = deny
    if ok:
        assert Config.from_dict(d).governance.schema_deny == tuple(deny)
    else:
        with pytest.raises(ConfigError):
            Config.from_dict(d)


@pytest.mark.parametrize("deny", [["REPORTING"], ["ANALYTICS_DB.REPORTING"]])
def test_a_denied_source_is_rejected(deny):
    d = _base()
    d["governance"]["schema_deny"] = deny
    with pytest.raises(ConfigError, match="both allowed and denied"):
        Config.from_dict(d)


def test_a_qualified_deny_in_another_database_is_no_conflict():
    d = _base()
    d["governance"]["schema_deny"] = ["FINANCE_DB.REPORTING"]
    Config.from_dict(d)


@pytest.mark.parametrize(
    ("app_data", "imported", "match"),
    [
        ("ANALYTICS_DB.REPORTING", [], "is also a source"),
        ("STREAMSNOW_APPS.RAW", [], "denied"),
        ("PARTNER_SHARE.APP", ["PARTNER_SHARE"], "shared database"),
        ("SNOWFLAKE_SAMPLE_DATA.APP", [], "shared database"),
    ],
)
def test_app_data_must_be_a_writable_non_source_schema(app_data, imported, match):
    d = _base()
    d["governance"]["app_data"] = app_data
    d["governance"]["imported_databases"] = imported
    with pytest.raises(ConfigError, match=match):
        Config.from_dict(d)


def test_boundary_is_warn_or_enforce():
    d = _base()
    d["governance"]["boundary"] = "enforce"
    assert Config.from_dict(d).governance.boundary == "enforce"
    d["governance"]["boundary"] = "strict"
    with pytest.raises(ConfigError, match="governance.boundary"):
        Config.from_dict(d)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("snowflake", "objects", "stage_database"), "OTHER_STAGE_DB\n"),
        (("snowflake", "objects", "app_database"), "STREAMSNOW_APPS\n"),
        (("snowflake", "objects", "default_warehouse"), "STREAMSNOW_WH\n"),
        (("snowflake", "roles", "ci_role"), "STREAMSNOW_DEPLOY_ROLE\n"),
        (("snowflake", "connection_name"), "acme\n"),
        (("snowflake", "objects", "container_python"), "3.11\n"),
        (("project", "slug"), "acme-dashboards\n"),
        (("governance", "schema_deny"), ["RAW\n"]),
        (("governance", "read_exceptions"), ["ANALYTICS_DB.RAW.EVENTS\n"]),
        (("deploy", "git_branch"), "main\n"),
        (("deploy", "git_repository_fqn"), "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO\n"),
    ],
)
def test_a_trailing_newline_in_a_rendered_value_is_rejected(path, value):
    """`$` matches before a final newline, so `OTHER_STAGE_DB\\n` passed and teardown's
    `-- The stage lives in OTHER_STAGE_DB\\n, ...` comment broke onto a live line (#79)."""
    d = _base()
    if path[0] == "deploy":
        d["deploy"] = {
            "source": "git-repository",
            "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
            "api_integration_name": "GITHUB_API_INTEGRATION",
            "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
        }
    node = d
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(d)
    assert path[-1] in str(exc.value)


@pytest.mark.parametrize("version", ["oops", None, [2], True])
def test_malformed_schema_version_is_a_config_error(version):
    data = _base()
    data["schema_version"] = version
    with pytest.raises(ConfigError, match="schema_version"):
        Config.from_dict(data)


@pytest.mark.parametrize("block", ["snowflake", "project", "governance"])
def test_scalar_required_block_is_a_config_error(block):
    data = _base()
    data[block] = "oops"
    with pytest.raises(ConfigError, match="must be a mapping"):
        Config.from_dict(data)


def test_malformed_config_exits_2_from_the_cli(tmp_path):
    from typer.testing import CliRunner

    from streamsnow.cli import app

    data = _base()
    data["schema_version"] = "two"
    cfg = tmp_path / "streamsnow.config.yaml"
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = CliRunner().invoke(app, ["deploy-sql", "acme-sales", "--config", str(cfg)])
    assert result.exit_code == 2
    assert "schema_version" in result.output
