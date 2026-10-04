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
    data = yaml.safe_load(EXAMPLE.read_text())
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
    path.write_text(f"-----BEGIN PUBLIC KEY-----\n{wrapped}\n-----END PUBLIC KEY-----\n")
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
    re.compile(r"^EXECUTE IMMEDIATE \$\$\nBEGIN\n  CREATE EXTERNAL ACCESS INTEGRATION ", re.S),
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
        assert "OR REPLACE" not in stmt, stmt


def test_default_output_changes_only_where_intended():
    """Without the new flags, only the viewer grant and EAI hunks change vs 0.7.5."""
    old = _statements(BASELINE.read_text())
    new = _statements(generate_admin_sql(_cfg()))
    removed = [s for s in old if s not in new]
    added = [s for s in new if s not in old]
    assert removed == [
        "CREATE EXTERNAL ACCESS INTEGRATION PYPI_ACCESS_INTEGRATION\n"
        "  ALLOWED_NETWORK_RULES = (snowflake.external_access.pypi_rule)\n"
        "  ENABLED = TRUE;"
    ]
    assert added[:2] == [
        "SET streamsnow_me = '\"' || CURRENT_USER() || '\"';",
        "GRANT ROLE STREAMSNOW_VIEWER_ROLE TO USER IDENTIFIER($streamsnow_me);",
    ]
    assert len(added) == 3 and added[2].startswith("EXECUTE IMMEDIATE $$")
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
    path.write_text(content)
    with pytest.raises(ConfigError) as exc:
        read_public_key(path)
    assert "TOPSECRETBODY" not in str(exc.value)
    cfg = tmp_path / "streamsnow.config.yaml"
    cfg.write_text(EXAMPLE.read_text())
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
    cfg.write_text(EXAMPLE.read_text())
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


@pytest.mark.parametrize("flag", [["--public-key-file", "x.pub"], ["--viewer-user", "A"]])
def test_admin_only_flags_need_admin(flag):
    res = _cli("deploy-setup", "--config", str(EXAMPLE), *flag)
    assert res.exit_code == 2


# --------------------------------------------------------------------------- #
# External access integration: created once, never replaced
# --------------------------------------------------------------------------- #


def test_eai_create_is_guarded_and_still_granted():
    stmts = _statements(generate_admin_sql(_cfg()))
    block = next(s for s in stmts if s.startswith("EXECUTE IMMEDIATE $$"))
    assert "CREATE EXTERNAL ACCESS INTEGRATION PYPI_ACCESS_INTEGRATION" in block
    assert "IF NOT EXISTS" not in block  # Snowflake has no such clause for EAIs
    assert "WHEN STATEMENT_ERROR THEN" in block
    assert "IF (SQLCODE = 2002 OR SQLSTATE = '42710') THEN RETURN 'already exists';" in block
    assert "ELSE RAISE;" in block
    bare = [s for s in stmts if s.startswith("CREATE EXTERNAL ACCESS INTEGRATION")]
    assert bare == []
    grant = "GRANT USAGE ON INTEGRATION PYPI_ACCESS_INTEGRATION TO ROLE STREAMSNOW_DEPLOY_ROLE;"
    assert stmts.index(grant) > stmts.index(block)


def test_warehouse_runtime_has_no_eai_block():
    assert "EXECUTE IMMEDIATE" not in generate_admin_sql(_cfg(runtime="warehouse"))


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


@pytest.mark.parametrize("cfg", _all_configs(), ids=lambda c: f"{c.runtime}-{c.deploy.source}")
def test_teardown_never_touches_governance_data_or_the_system_pool(cfg):
    sql = generate_teardown_sql(cfg)
    for stmt in _drops(sql):
        assert re.match(r"^(DROP [A-Z ]+ IF EXISTS|ALTER COMPUTE POOL IF EXISTS) ", stmt), stmt
        assert cfg.governance.database not in stmt
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
    cfg.write_text(EXAMPLE.read_text())
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
