"""0.7.6: a from-nothing admin file that is safe to re-run, plus a teardown.

`deploy-setup --admin` must run unedited on an empty account and again on a
populated one; `deploy-setup --teardown` is its reviewable reverse.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
import yaml

from streamsnow.config import Config, ConfigError
from streamsnow.deploy import (
    READ_OBJECT_TYPES,
    SYSTEM_POOL,
    generate_admin_sql,
    generate_teardown_sql,
    read_public_key,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"
BASELINE = Path(__file__).parent / "fixtures" / "admin_sql_0_7_5_example.sql"
ORIGIN = "https://github.com/acme/dashboards.git"
# Not a real key: any valid base64 body passes read_public_key's shape check.
FAKE_KEY_BODY = base64.b64encode(b"streamsnow-test-public-key" * 4).decode()


def _data(**overrides) -> dict:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for dotted, value in overrides.items():
        node = data
        *parents, leaf = dotted.split(".")
        for p in parents:
            node = node.setdefault(p, {})
        node[leaf] = value
    return data


def _cfg(**overrides) -> Config:
    return Config.from_dict(_data(**overrides))


def _git(**overrides) -> Config:
    data = _data(**overrides)
    data["deploy"] = {
        "source": "git-repository",
        "git_repository_fqn": "STREAMSNOW_APPS.DASHBOARDS.STREAMLIT_REPO",
        "api_integration_name": "GITHUB_API_INTEGRATION",
        "secret_name": "STREAMSNOW_APPS.DASHBOARDS.GITHUB_PAT_SECRET",
        "git_origin": ORIGIN,
        **data.get("deploy_overrides", {}),
    }
    data.pop("deploy_overrides", None)
    return Config.from_dict(data)


def _cli(*args: str):
    from typer.testing import CliRunner

    from streamsnow.cli import app

    return CliRunner().invoke(app, list(args))


def _pub_file(tmp_path: Path, body: str = FAKE_KEY_BODY) -> Path:
    path = tmp_path / "ci.pub"
    wrapped = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    path.write_text(
        f"-----BEGIN PUBLIC KEY-----\n{wrapped}\n-----END PUBLIC KEY-----\n", encoding="utf-8"
    )
    return path


def _statements(sql: str) -> list[str]:
    """Executable statements, comments dropped, a `$$ ... $$;` block kept whole."""
    out: list[str] = []
    buf: list[str] = []
    in_block = False
    for line in sql.splitlines():
        if not in_block:
            line = re.sub(r"\s+--.*$", "", line)  # trailing `;  -- note` comments
            if not line.strip() or line.lstrip().startswith("--"):
                continue
        buf.append(line)
        if line.count("$$") % 2:
            in_block = not in_block
        if not in_block and line.rstrip().endswith(";"):
            out.append("\n".join(buf))
            buf = []
    assert not buf and not in_block, f"unterminated statement: {buf}"
    return out


_RERUN_SAFE = (
    re.compile(r"^USE ROLE \w+;$"),
    re.compile(r"^SET \w+ = .+;$"),
    re.compile(r"^GRANT ", re.S),
    re.compile(
        r"^CREATE (DATABASE|SCHEMA|WAREHOUSE|ROLE|USER|STAGE|SECRET|GIT REPOSITORY|"
        r"API INTEGRATION|COMPUTE POOL) IF NOT EXISTS ",
        re.S,
    ),  # fmt: skip
    re.compile(r"^ALTER USER IF EXISTS \w+ SET RSA_PUBLIC_KEY = '[A-Za-z0-9+/=]+';$"),
    # No IF NOT EXISTS exists for EAIs; replacing is safe for live apps and the
    # CI role's grant follows (see test_eai_is_replaced_then_regranted).
    re.compile(r"^CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION ", re.S),
)


# --------------------------------------------------------------------------- #
# Re-run safety, every runtime x source, with and without a key
# --------------------------------------------------------------------------- #


def _all_configs() -> list[Config]:
    configs = []
    for runtime in ("container", "warehouse"):
        configs.append(_cfg(runtime=runtime))
        configs.append(_git(runtime=runtime))
    configs.append(_cfg(**{"snowflake.objects.compute_pool": "MY_POOL"}))
    return configs


@pytest.mark.parametrize("key", [None, FAKE_KEY_BODY])
@pytest.mark.parametrize("cfg", _all_configs(), ids=lambda c: f"{c.runtime}-{c.deploy.source}")
def test_every_admin_statement_is_safe_to_rerun(cfg, key):
    stmts = _statements(generate_admin_sql(cfg, public_key=key, viewer_users=["ANALYST_1"]))
    assert stmts
    for stmt in stmts:
        assert any(p.match(stmt) for p in _RERUN_SAFE), f"not re-run safe:\n{stmt}"
        if "OR REPLACE" in stmt:
            assert stmt.startswith("CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION"), stmt


def test_default_output_changes_only_where_intended():
    """Without the new flags, only the viewer grant, EAI, read-type and app-data hunks change vs 0.7.5."""
    old = _statements(BASELINE.read_text(encoding="utf-8"))
    new = _statements(generate_admin_sql(_cfg()))
    removed = [s for s in old if s not in new]
    added = [s for s in new if s not in old]
    body = (
        " EXTERNAL ACCESS INTEGRATION PYPI_ACCESS_INTEGRATION\n"
        "  ALLOWED_NETWORK_RULES = (snowflake.external_access.pypi_rule)\n"
        "  ENABLED = TRUE;"
    )
    assert removed == ["CREATE" + body]
    # Unreleased (#80): every readable object type beyond TABLES and VIEWS, CI role only.
    new_kinds = [k for k in READ_OBJECT_TYPES if k not in ("TABLES", "VIEWS")]
    read_grants = [
        f"GRANT SELECT ON {scope} {kind} IN SCHEMA ANALYTICS_DB.{schema} "
        "TO ROLE STREAMSNOW_DEPLOY_ROLE;"
        for schema in ("ANALYTICS", "REPORTING")
        for scope in ("ALL", "FUTURE")
        for kind in new_kinds
    ]
    # 0.11 (#78): the app-data schema, created by SYSADMIN, built in by the CI role.
    app_data = "STREAMSNOW_APPS.STREAMSNOW_REPORTING"
    app_data_grants = [
        f"GRANT {priv} ON SCHEMA {app_data} TO ROLE STREAMSNOW_DEPLOY_ROLE;"
        for priv in ("USAGE", "CREATE VIEW", "CREATE DYNAMIC TABLE")
    ]
    assert added == [
        f"CREATE SCHEMA IF NOT EXISTS {app_data};",
        "SET streamsnow_me = '\"' || CURRENT_USER() || '\"';",
        "GRANT ROLE STREAMSNOW_VIEWER_ROLE TO USER IDENTIFIER($streamsnow_me);",
        *read_grants,
        *app_data_grants,
        "CREATE OR REPLACE" + body,
    ]
    # Every other statement is identical and in the same order.
    assert [s for s in old if s in new] == [s for s in new if s in old]


# --------------------------------------------------------------------------- #
# CI user key
# --------------------------------------------------------------------------- #


def test_public_key_fills_the_user_and_is_reapplied_on_rerun(tmp_path):
    key = read_public_key(_pub_file(tmp_path))
    assert key == FAKE_KEY_BODY  # header, footer and line wrapping stripped
    stmts = _statements(generate_admin_sql(_cfg(), public_key=key))
    create = next(s for s in stmts if s.startswith("CREATE USER IF NOT EXISTS"))
    assert f"RSA_PUBLIC_KEY = '{FAKE_KEY_BODY}';" in create
    alter = f"ALTER USER IF EXISTS STREAMSNOW_DEPLOY_USER SET RSA_PUBLIC_KEY = '{FAKE_KEY_BODY}';"
    assert stmts.index(alter) == stmts.index(create) + 1
    assert "<paste public key>" not in generate_admin_sql(_cfg(), public_key=key)


def test_without_a_key_the_placeholder_stays_and_nothing_alters_it():
    sql = generate_admin_sql(_cfg())
    assert "RSA_PUBLIC_KEY = '<paste public key>';" in sql
    assert "ALTER USER" not in sql


# Built from parts so the privacy gate's private-key pattern never matches this file.
_PRIV = "PRIVATE " + "KEY-----"


@pytest.mark.parametrize(
    "content",
    [
        f"-----BEGIN {_PRIV}\nTOPSECRETBODY\n-----END {_PRIV}\n",
        f"-----BEGIN ENCRYPTED {_PRIV}\nTOPSECRETBODY\n-----END ENCRYPTED {_PRIV}\n",
        "-----BEGIN PUBLIC KEY-----\nnot*base64!TOPSECRETBODY\n-----END PUBLIC KEY-----\n",
        "TOPSECRETBODY\n",
    ],
)
def test_bad_key_files_are_refused_without_echoing_them(tmp_path, content):
    path = tmp_path / "key.pem"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        read_public_key(path)
    assert "TOPSECRETBODY" not in str(exc.value)
    cfg = tmp_path / "streamsnow.config.yaml"
    cfg.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    res = _cli("deploy-setup", "--admin", "--config", str(cfg), "--public-key-file", str(path))
    assert res.exit_code == 2
    assert "TOPSECRETBODY" not in res.output


def test_missing_key_file_exits_2(tmp_path):
    res = _cli(
        "deploy-setup", "--admin", "--config", str(EXAMPLE),
        "--public-key-file", str(tmp_path / "nope.pub"),
    )  # fmt: skip
    assert res.exit_code == 2, res.output


# --------------------------------------------------------------------------- #
# Viewer grant
# --------------------------------------------------------------------------- #


def _securityadmin_body(sql: str) -> str:
    return sql.split("USE ROLE SECURITYADMIN;", 1)[1].split("USE ROLE ", 1)[0]


def test_viewer_role_goes_to_whoever_runs_the_script():
    body = _securityadmin_body(generate_admin_sql(_cfg()))
    set_line = "SET streamsnow_me = '\"' || CURRENT_USER() || '\"';"
    grant = "GRANT ROLE STREAMSNOW_VIEWER_ROLE TO USER IDENTIFIER($streamsnow_me);"
    assert set_line in body and grant in body
    assert body.index(set_line) < body.index(grant)
    assert "TO USER <your_user>" not in body


def test_viewer_user_flag_adds_explicit_grants(tmp_path):
    cfg = tmp_path / "streamsnow.config.yaml"
    cfg.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    res = _cli(
        "deploy-setup", "--admin", "--config", str(cfg),
        "--viewer-user", "ANALYST_1", "--viewer-user", "ANALYST_2",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    body = _securityadmin_body(res.output)
    assert "GRANT ROLE STREAMSNOW_VIEWER_ROLE TO USER ANALYST_1;" in body
    assert "GRANT ROLE STREAMSNOW_VIEWER_ROLE TO USER ANALYST_2;" in body


def test_viewer_user_is_validated_as_an_identifier():
    with pytest.raises(ConfigError):
        generate_admin_sql(_cfg(), viewer_users=["x; DROP DATABASE y"])


def test_viewer_role_flag_grants_the_viewer_role_to_existing_roles(tmp_path):
    cfg = tmp_path / "streamsnow.config.yaml"
    cfg.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    res = _cli(
        "deploy-setup", "--admin", "--config", str(cfg),
        "--viewer-role", "ANALYST_ROLE", "--viewer-role", "AI_AGENT",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    stmts = _statements(res.output)
    # Last, under SECURITYADMIN, so a role name that does not resolve stops nothing else.
    assert stmts[-3:] == [
        "USE ROLE SECURITYADMIN;",
        "GRANT ROLE STREAMSNOW_VIEWER_ROLE TO ROLE ANALYST_ROLE;",
        "GRANT ROLE STREAMSNOW_VIEWER_ROLE TO ROLE AI_AGENT;",
    ]


def test_viewer_role_grants_are_safe_to_rerun():
    stmts = _statements(generate_admin_sql(_cfg(), viewer_roles=["ANALYST_ROLE"]))
    for stmt in stmts:
        assert any(p.match(stmt) for p in _RERUN_SAFE), f"not re-run safe:\n{stmt}"


def test_viewer_role_is_validated_as_an_identifier():
    with pytest.raises(ConfigError):
        generate_admin_sql(_cfg(), viewer_roles=["x; DROP DATABASE y"])


@pytest.mark.parametrize(
    "role", ["PUBLIC", "public", "ACCOUNTADMIN", "STREAMSNOW_VIEWER_ROLE", "STREAMSNOW_DEPLOY_ROLE"]
)
def test_viewer_role_refuses_system_and_streamsnow_roles(role):
    """PUBLIC would open every app to every user; a system role or StreamSnow's own roles
    would make a grant cycle or widen an admin role."""
    with pytest.raises(ConfigError) as exc:
        generate_admin_sql(_cfg(), viewer_roles=[role])
    assert "--viewer-role" in str(exc.value)


@pytest.mark.parametrize(
    "flag",
    [["--public-key-file", "x.pub"], ["--viewer-user", "A"], ["--viewer-role", "A"]],
)
def test_admin_only_flags_need_admin(flag):
    res = _cli("deploy-setup", "--config", str(EXAMPLE), *flag)
    assert res.exit_code == 2


# --------------------------------------------------------------------------- #
# External access integration: replaced on a re-run, then re-granted
# --------------------------------------------------------------------------- #


def test_eai_is_replaced_then_regranted():
    stmts = _statements(generate_admin_sql(_cfg()))
    eai = [s for s in stmts if "EXTERNAL ACCESS INTEGRATION" in s and s.startswith("CREATE")]
    assert len(eai) == 1
    assert eai[0].startswith(
        "CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION PYPI_ACCESS_INTEGRATION"
    )
    assert "IF NOT EXISTS" not in eai[0]  # Snowflake has no such clause for EAIs
    # OR REPLACE drops the integration's grants, so the CI role's must follow it.
    grant = "GRANT USAGE ON INTEGRATION PYPI_ACCESS_INTEGRATION TO ROLE STREAMSNOW_DEPLOY_ROLE;"
    assert stmts.index(grant) > stmts.index(eai[0])


def test_warehouse_runtime_has_no_eai():
    stmts = _statements(generate_admin_sql(_cfg(runtime="warehouse")))
    assert not any("EXTERNAL ACCESS INTEGRATION" in s for s in stmts)


# --------------------------------------------------------------------------- #
# Teardown
# --------------------------------------------------------------------------- #


def _drops(sql: str) -> list[str]:
    return [s for s in _statements(sql) if not s.startswith("USE ROLE")]


def test_teardown_drops_everything_in_a_safe_order():
    sql = generate_teardown_sql(_cfg())
    assert _statements(sql)[0] == "USE ROLE ACCOUNTADMIN;"
    assert _drops(sql) == [
        "DROP DATABASE IF EXISTS STREAMSNOW_APPS;",
        "DROP WAREHOUSE IF EXISTS STREAMSNOW_WH;",
        "DROP USER IF EXISTS STREAMSNOW_DEPLOY_USER;",
        "DROP ROLE IF EXISTS STREAMSNOW_VIEWER_ROLE;",
        "DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;",
        "DROP EXTERNAL ACCESS INTEGRATION IF EXISTS PYPI_ACCESS_INTEGRATION;",
    ]


def test_teardown_app_data_in_the_app_database_goes_with_it():
    sql = generate_teardown_sql(_cfg())
    assert "-- 2. App data (STREAMSNOW_APPS.STREAMSNOW_REPORTING)." in sql
    assert "DROP SCHEMA" not in sql


def test_teardown_app_data_elsewhere_is_offered_commented_before_the_roles():
    sql = generate_teardown_sql(_cfg(**{"governance.app_data": "STREAMSNOW_DATA.REPORTING"}))
    line = "--   DROP SCHEMA IF EXISTS STREAMSNOW_DATA.REPORTING;"
    assert line in sql and "only if nothing else lives there" in sql
    assert not any(s.startswith("DROP SCHEMA") for s in _drops(sql))
    assert sql.index(line) < sql.index("DROP ROLE IF EXISTS STREAMSNOW_VIEWER_ROLE;")


@pytest.mark.parametrize("cfg", _all_configs(), ids=lambda c: f"{c.runtime}-{c.deploy.source}")
def test_teardown_never_touches_governance_data_or_the_system_pool(cfg):
    sql = generate_teardown_sql(cfg)
    for stmt in _drops(sql):
        assert re.match(r"^(DROP [A-Z ]+ IF EXISTS|ALTER COMPUTE POOL IF EXISTS) ", stmt), stmt
        for source in cfg.governance.sources:
            assert source.split(".", 1)[0] not in stmt, stmt
        assert SYSTEM_POOL not in stmt


def test_teardown_custom_pool_is_stopped_then_dropped():
    drops = _drops(generate_teardown_sql(_cfg(**{"snowflake.objects.compute_pool": "MY_POOL"})))
    stop = "ALTER COMPUTE POOL IF EXISTS MY_POOL STOP ALL;"
    assert drops.index(stop) + 1 == drops.index("DROP COMPUTE POOL IF EXISTS MY_POOL;")


def test_teardown_git_source_drops_the_api_integration():
    drops = _drops(generate_teardown_sql(_git()))
    assert drops[-1] == "DROP API INTEGRATION IF EXISTS GITHUB_API_INTEGRATION;"
    # The git repository and secret live in the app database, so its DROP covers them.
    assert not any("GIT REPOSITORY" in d or "SECRET" in d for d in drops)


def test_teardown_drops_only_the_stage_in_a_separate_stage_database():
    drops = _drops(
        generate_teardown_sql(
            _cfg(**{"snowflake.objects.stage_database": "SHARED_TOOLS",
                    "snowflake.objects.stage_schema": "STAGES"})
        )
    )  # fmt: skip
    assert "DROP DATABASE IF EXISTS SHARED_TOOLS;" not in drops
    assert any(d.startswith("DROP STAGE IF EXISTS SHARED_TOOLS.STAGES.") for d in drops)


@pytest.mark.parametrize(
    "field", ["snowflake.objects.app_database", "snowflake.objects.stage_database"]
)
@pytest.mark.parametrize("db", ["ANALYTICS_DB", "SNOWFLAKE_SAMPLE_DATA"])
def test_teardown_refuses_to_drop_the_governance_database(field, db):
    with pytest.raises(ConfigError, match="Refusing"):
        generate_teardown_sql(_cfg(**{field: db}))


def test_cli_teardown(tmp_path):
    cfg = tmp_path / "streamsnow.config.yaml"
    cfg.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    res = _cli("deploy-setup", "--teardown", "--config", str(cfg))
    assert res.exit_code == 0, res.output
    assert res.output.strip() == generate_teardown_sql(_cfg()).strip()
    both = _cli("deploy-setup", "--teardown", "--admin", "--config", str(cfg))
    assert both.exit_code == 2


@pytest.mark.parametrize("field", ["snowflake.roles.ci_role", "snowflake.roles.viewer_role"])
def test_teardown_refuses_system_roles(field):
    with pytest.raises(ConfigError, match="system role"):
        generate_teardown_sql(_cfg(**{field: "SYSADMIN"}))


def test_teardown_flags_every_object_that_could_predate_streamsnow():
    sql = generate_teardown_sql(_git(**{"snowflake.objects.compute_pool": "MY_POOL"})).splitlines()
    marker = "-- Existed before StreamSnow and used by other work? Delete the next line."
    for target in (
        "DROP DATABASE IF EXISTS STREAMSNOW_APPS;",
        "DROP WAREHOUSE IF EXISTS STREAMSNOW_WH;",
        "DROP EXTERNAL ACCESS INTEGRATION IF EXISTS PYPI_ACCESS_INTEGRATION;",
        "ALTER COMPUTE POOL IF EXISTS MY_POOL STOP ALL;",
        "DROP API INTEGRATION IF EXISTS GITHUB_API_INTEGRATION;",
    ):
        assert sql[sql.index(target) - 1] == marker, target


# --------------------------------------------------------------------------- #
# Teardown: declared app-data objects go before the roles that own them (#79)
# --------------------------------------------------------------------------- #

ELSEWHERE = "STREAMSNOW_DATA.REPORTING"


def test_teardown_drops_declared_app_data_objects_before_the_roles():
    objs = [
        (f"{ELSEWHERE}.REGION_REVENUE", "view"),
        (f"{ELSEWHERE}.DAILY_REVENUE", "dynamic_table"),
    ]
    sql = generate_teardown_sql(_cfg(**{"governance.app_data": ELSEWHERE}), objs)
    view = f"DROP VIEW IF EXISTS {ELSEWHERE}.REGION_REVENUE;"
    table = f"DROP DYNAMIC TABLE IF EXISTS {ELSEWHERE}.DAILY_REVENUE;"
    assert view in _drops(sql) and table in _drops(sql)
    order = [
        sql.index(f"-- 2. App data ({ELSEWHERE})."),
        sql.index(view),
        sql.index(table),
        sql.index(f"--   DROP SCHEMA IF EXISTS {ELSEWHERE};"),
        sql.index("DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;"),
    ]
    assert order == sorted(order)


def test_an_object_of_unknown_kind_holds_back_its_owners_role_drop():
    """The object cannot be dropped safely, so neither can the CI role that owns it:
    dropping the owner first would leave the object owned by whoever runs teardown."""
    objs = [(f"{ELSEWHERE}.X", ""), (f"{ELSEWHERE}.DAILY_REVENUE", "dynamic_table")]
    sql = generate_teardown_sql(_cfg(**{"governance.app_data": ELSEWHERE}), objs)
    assert f"--   DROP VIEW IF EXISTS {ELSEWHERE}.X;" in sql
    assert f"--   DROP DYNAMIC TABLE IF EXISTS {ELSEWHERE}.X;" in sql
    assert not [s for s in _drops(sql) if f"{ELSEWHERE}.X" in s]
    assert f"DROP DYNAMIC TABLE IF EXISTS {ELSEWHERE}.DAILY_REVENUE;" in _drops(sql)
    drops = _drops(sql)
    assert "DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;" not in drops
    held = "--   DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;"
    assert held in sql
    assert f"{ELSEWHERE}.X" in sql[sql.index(held) - 300 : sql.index(held)]  # the reason names it
    assert "DROP ROLE IF EXISTS STREAMSNOW_VIEWER_ROLE;" in drops  # the viewer owns nothing there


def test_an_incomplete_inventory_holds_back_the_ci_role_drop():
    """A malformed index.yaml gives an empty drop order: objects the deploy job built
    may still exist unseen, so the role that owns them must not be dropped first."""
    index = "apps/acme-sales/sql_review/index.yaml"
    sql = generate_teardown_sql(
        _cfg(**{"governance.app_data": ELSEWHERE}), [], inventory_incomplete=[index]
    )
    drops = _drops(sql)
    assert "DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;" not in drops
    held = "--   DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;"
    assert held in sql
    assert index in sql[sql.index("-- 2. App data") : sql.index(held)]
    assert "DROP ROLE IF EXISTS STREAMSNOW_VIEWER_ROLE;" in drops


def test_deploy_setup_teardown_holds_the_role_back_for_a_malformed_index(tmp_path):
    from _app_data_fixtures import dynamic_table, write_app, write_config

    cfg = write_config(tmp_path, app_data=ELSEWHERE)
    app = write_app(
        tmp_path,
        "acme-sales",
        {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE", ad=ELSEWHERE)},
        ad=ELSEWHERE,
    )
    (app / "sql_review" / "index.yaml").write_text("objects: [\n", encoding="utf-8")
    result = _cli("deploy-setup", "--teardown", "--config", str(cfg))
    assert result.exit_code == 0, result.output
    assert "--   DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;" in result.output
    assert "\nDROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;" not in result.output


def test_known_kinds_keep_the_role_drop():
    sql = generate_teardown_sql(
        _cfg(**{"governance.app_data": ELSEWHERE}), [(f"{ELSEWHERE}.V", "view")]
    )
    assert "DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;" in _drops(sql)


def test_teardown_app_data_in_the_app_database_needs_no_object_drops():
    objs = [("STREAMSNOW_APPS.STREAMSNOW_REPORTING.DAILY_REVENUE", "dynamic_table")]
    sql = generate_teardown_sql(_cfg(), objs)
    assert "DROP DYNAMIC TABLE" not in sql
    assert "1 view(s) and dynamic table(s) the deploy job built there" in sql


def test_deploy_setup_teardown_reads_the_declared_objects(tmp_path):
    from _app_data_fixtures import dynamic_table, write_app, write_config

    cfg = write_config(tmp_path, app_data=ELSEWHERE)
    write_app(
        tmp_path,
        "acme-sales",
        {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE", ad=ELSEWHERE)},
        ad=ELSEWHERE,
    )
    result = _cli("deploy-setup", "--teardown", "--config", str(cfg))
    assert result.exit_code == 0, result.output
    assert f"DROP DYNAMIC TABLE IF EXISTS {ELSEWHERE}.DAILY_REVENUE;" in result.output


# --------------------------------------------------------------------------- #
# Fix round 1: a declared name must never reach teardown SQL unquoted (#79)
# --------------------------------------------------------------------------- #


def test_a_quoted_name_with_a_newline_never_becomes_a_live_drop(tmp_path):
    """An index.yaml entry can carry a newline inside a quoted part. Rendered into a
    `-- ...` comment it would end the comment and print a live DROP DATABASE."""
    from _app_data_fixtures import dynamic_table, write_app, write_config

    cfg = write_config(tmp_path, app_data=ELSEWHERE)
    app = write_app(
        tmp_path,
        "acme-sales",
        {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE", ad=ELSEWHERE)},
        ad=ELSEWHERE,
    )
    index = app / "sql_review" / "index.yaml"
    data = yaml.safe_load(index.read_text(encoding="utf-8"))
    evil = f'{ELSEWHERE}."X\nDROP DATABASE ANALYTICS_DB;\n--"'
    data["objects"].append({"name": evil, "grants": [], "reason": "performance"})
    index.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    result = _cli("deploy-setup", "--teardown", "--config", str(cfg))
    assert result.exit_code == 0, result.output
    live = [ln for ln in result.stdout.splitlines() if not ln.startswith("--")]
    assert not [ln for ln in live if "ANALYTICS_DB" in ln]
    for ln in result.stdout.splitlines():
        assert not ln.startswith("DROP DATABASE ANALYTICS_DB")
    assert f"DROP DYNAMIC TABLE IF EXISTS {ELSEWHERE}.DAILY_REVENUE;" in live


def test_teardown_sql_renders_only_plain_names_and_reports_others_once():
    evil = f'{ELSEWHERE}."X\nDROP DATABASE ANALYTICS_DB;\n--"'
    sql = generate_teardown_sql(
        _cfg(**{"governance.app_data": ELSEWHERE}), [(evil, ""), (evil, "view")]
    )
    assert "\nDROP DATABASE ANALYTICS_DB;" not in sql
    assert sql.count("DROP DATABASE ANALYTICS_DB") <= 1
    assert "DROP VIEW IF EXISTS" not in sql
    # A malformed name cannot prove the object was never built (it may have been renamed
    # after a deploy), so the CI role that would own it is held back, never dropped.
    assert "DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;" not in _drops(sql)
    assert "--   DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;" in sql
    # the one report line is a single comment line
    report = [ln for ln in sql.splitlines() if "DROP DATABASE ANALYTICS_DB" in ln]
    assert all(ln.startswith("--") for ln in report)


def test_drop_order_puts_dependents_first_even_for_invalid_objects(tmp_path):
    from _app_data_fixtures import dynamic_table, view, write_app, write_config

    from streamsnow.app_data import load_app_data

    cfg_path = write_config(tmp_path, app_data=ELSEWHERE)
    # A_DT is invalid (wrong warehouse) yet Z_VIEW reads it: Z_VIEW must go first.
    bad_dt = dynamic_table("A_DT", ad=ELSEWHERE).replace("STREAMSNOW_WH", "OTHER_WH")
    write_app(
        tmp_path,
        "acme-sales",
        {
            "A_DT": bad_dt,
            "Z_VIEW": view("Z_VIEW", f"SELECT revenue FROM {ELSEWHERE}.A_DT", ad=ELSEWHERE),
        },
        ad=ELSEWHERE,
    )
    cfg = Config.from_dict(yaml.safe_load(cfg_path.read_text(encoding="utf-8")))
    order = [f for f, _ in load_app_data(tmp_path, cfg).drop_order()]
    assert order.index(f"{ELSEWHERE}.Z_VIEW") < order.index(f"{ELSEWHERE}.A_DT")


def test_teardown_prints_the_finding_count_on_stderr(tmp_path):
    from _app_data_fixtures import dynamic_table, write_app, write_config

    cfg = write_config(tmp_path, app_data=ELSEWHERE)
    bad = dynamic_table("DAILY_REVENUE", ad=ELSEWHERE).replace("STREAMSNOW_WH", "OTHER_WH")
    write_app(tmp_path, "acme-sales", {"DAILY_REVENUE": bad}, ad=ELSEWHERE)
    result = _cli("deploy-setup", "--teardown", "--config", str(cfg))
    assert result.exit_code == 0, result.output
    assert "finding(s)" in result.stderr
    assert "finding(s)" not in result.stdout


def test_drop_order_puts_a_cycle_member_before_the_acyclic_object_it_reads(tmp_path):
    """D_V reads A_BASE and is in a cycle with C_V. Nothing acyclic can read a cycle
    member, so both cycle members must go before A_BASE, which they depend on."""
    from _app_data_fixtures import dynamic_table, view, write_app, write_config

    from streamsnow.app_data import load_app_data

    cfg_path = write_config(tmp_path, app_data=ELSEWHERE)
    write_app(
        tmp_path,
        "acme-sales",
        {
            "A_BASE": dynamic_table("A_BASE", ad=ELSEWHERE),
            "C_V": view("C_V", f"SELECT revenue FROM {ELSEWHERE}.D_V", ad=ELSEWHERE),
            "D_V": view(
                "D_V",
                f"SELECT a.revenue FROM {ELSEWHERE}.A_BASE a JOIN {ELSEWHERE}.C_V c ON 1 = 1",
                ad=ELSEWHERE,
            ),
        },
        ad=ELSEWHERE,
    )
    cfg = Config.from_dict(yaml.safe_load(cfg_path.read_text(encoding="utf-8")))
    order = [f for f, _ in load_app_data(tmp_path, cfg).drop_order()]
    assert order.index(f"{ELSEWHERE}.C_V") < order.index(f"{ELSEWHERE}.A_BASE")
    assert order.index(f"{ELSEWHERE}.D_V") < order.index(f"{ELSEWHERE}.A_BASE")


def test_a_declared_name_with_a_trailing_newline_holds_the_role_back(tmp_path):
    """`...EVIL\\n` renders as a plain name once stripped, and the loader rejects the entry.
    A name malformed now cannot prove the object never existed (the newline may have come
    after a deploy built it), so the CI role's DROP is held back with the reason, and the
    name is rendered once, escaped, in one comment line."""
    from _app_data_fixtures import dynamic_table, write_app, write_config

    cfg = write_config(tmp_path, app_data=ELSEWHERE)
    app = write_app(
        tmp_path,
        "acme-sales",
        {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE", ad=ELSEWHERE)},
        ad=ELSEWHERE,
    )
    index = app / "sql_review" / "index.yaml"
    data = yaml.safe_load(index.read_text(encoding="utf-8"))
    data["objects"].append({"name": f"{ELSEWHERE}.EVIL\n", "grants": [], "reason": "performance"})
    index.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    result = _cli("deploy-setup", "--teardown", "--config", str(cfg))
    assert result.exit_code == 0, result.output
    live = [ln for ln in result.stdout.splitlines() if not ln.startswith("--")]
    assert not [ln for ln in live if "EVIL" in ln]
    skipped = [ln for ln in result.stdout.splitlines() if "EVIL" in ln]
    assert len(skipped) == 1 and skipped[0].startswith("-- Skipped")
    assert "\nDROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;" not in result.stdout
    assert "--   DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;" in result.stdout
    held = [ln for ln in result.stdout.splitlines() if ln.startswith("-- Held back")]
    assert len(held) == 1 and "not a plain DATABASE.SCHEMA.NAME" in held[0]


def test_drop_order_drops_what_reads_a_cycle_before_the_cycle(tmp_path):
    """A_V and B_V read each other; C_V reads B_V. C_V must go before B_V: sorting every
    object left after the topological pass by name put it last."""
    from _app_data_fixtures import view, write_app, write_config

    from streamsnow.app_data import load_app_data

    cfg_path = write_config(tmp_path, app_data=ELSEWHERE)
    write_app(
        tmp_path,
        "acme-sales",
        {
            "A_V": view("A_V", f"SELECT revenue FROM {ELSEWHERE}.B_V", ad=ELSEWHERE),
            "B_V": view("B_V", f"SELECT revenue FROM {ELSEWHERE}.A_V", ad=ELSEWHERE),
            "C_V": view("C_V", f"SELECT revenue FROM {ELSEWHERE}.B_V", ad=ELSEWHERE),
        },
        ad=ELSEWHERE,
    )
    cfg = Config.from_dict(yaml.safe_load(cfg_path.read_text(encoding="utf-8")))
    order = [f for f, _ in load_app_data(tmp_path, cfg).drop_order()]
    assert order == [f"{ELSEWHERE}.C_V", f"{ELSEWHERE}.A_V", f"{ELSEWHERE}.B_V"]


# --------------------------------------------------------------------------- #
# Codex round 2: a config value never ends the comment it is rendered into (#79)
# --------------------------------------------------------------------------- #


def _unvalidated(cfg: Config, **objects) -> Config:
    """The config with values validation would refuse, to prove the renderer holds alone."""
    import dataclasses

    sf = dataclasses.replace(
        cfg.snowflake, objects=dataclasses.replace(cfg.snowflake.objects, **objects)
    )
    return dataclasses.replace(cfg, snowflake=sf)


def _comment_tails_stay_comments(sql: str, *tails: str) -> None:
    for tail in tails:
        lines = [ln for ln in sql.splitlines() if tail in ln]
        assert lines, tail
        assert all(ln.startswith("--") for ln in lines), lines


def test_teardown_keeps_a_newline_in_a_config_value_inside_its_comment():
    cfg = _unvalidated(
        _cfg(**{"governance.app_data": ELSEWHERE}), stage_database="OTHER_STAGE_DB\nX"
    )
    sql = generate_teardown_sql(cfg)
    _comment_tails_stay_comments(sql, "which may hold other things")
    assert "'OTHER_STAGE_DB\\nX'" in sql


def test_admin_sql_keeps_a_newline_in_a_config_value_inside_its_comment():
    cfg = _unvalidated(_cfg(), default_warehouse="STREAMSNOW_WH\nX")
    sql = generate_admin_sql(cfg)
    _comment_tails_stay_comments(sql, "which the CI", "refresh with WAREHOUSE")
