"""The configure wizard's UX contract: every question prefilled, commented-YAML output."""

from __future__ import annotations

from pathlib import Path

import typer
import yaml

from streamsnow import cli, probe
from streamsnow.cli import _connection_hint, _prompt_config, _render_config_yaml
from streamsnow.config import Config
from streamsnow.tools import doctor

# Captured at import, before tests/conftest.py stubs it per test.
_REAL_SNOW_CONNECTIONS = cli._snow_connections
_REAL_LIVE_PROBE = cli._live_probe


def _run_wizard(monkeypatch, prefill=None, directory=Path("acme-analytics")):
    """Drive the wizard accepting every default; return (config dict, prompts asked)."""
    asked: list[tuple[str, object]] = []

    def fake_prompt(text, default=None, **kwargs):
        asked.append((str(text), default))
        # The account locator is the one question with no default on a first run.
        return default if default is not None else "ab12345.us-east-1"

    monkeypatch.setattr(typer, "prompt", fake_prompt)
    return _prompt_config(prefill, directory), asked


def test_every_question_has_a_detected_or_default_answer(monkeypatch):
    """D8: no fixed question count. Every question arrives prefilled, so Enter through
    the wizard writes a complete, valid config. The account locator is the one value
    nothing can default on a first run; a re-run prefills it too."""
    cfg_dict, asked = _run_wizard(monkeypatch)
    no_default = [q for q, default in asked if default in (None, "")]
    assert all(q.startswith("Snowflake account") for q in no_default), no_default
    Config.from_dict(cfg_dict)
    _, again = _run_wizard(monkeypatch, prefill=cfg_dict)
    assert all(default not in (None, "") for _, default in again), again


def test_wizard_derives_project_identity_from_directory(monkeypatch):
    cfg_dict, _ = _run_wizard(monkeypatch, directory=Path("Acme Analytics"))
    assert cfg_dict["project"]["slug"] == "acme-analytics"
    assert cfg_dict["project"]["name"] == "Acme Analytics"
    assert cfg_dict["snowflake"]["connection_name"] == "acme-analytics"


def test_wizard_prefill_survives_for_unasked_values(monkeypatch):
    prefill = {
        "project": {"name": "Custom Name", "slug": "custom-slug"},
        "snowflake": {"roles": {"viewer_role": "MY_VIEWER"}},
        "governance": {"schema_deny": ["SECRET_SCHEMA"]},
    }
    cfg_dict, asked = _run_wizard(monkeypatch, prefill=prefill)
    assert all(
        default not in (None, "") for q, default in asked if not q.startswith("Snowflake account")
    )
    # Hand-edited values the wizard no longer asks about are preserved.
    assert cfg_dict["project"]["slug"] == "custom-slug"
    assert cfg_dict["snowflake"]["roles"]["viewer_role"] == "MY_VIEWER"
    assert cfg_dict["governance"]["schema_deny"] == ["SECRET_SCHEMA"]


def test_wizard_preserves_hand_edited_keys_it_never_asks_about(monkeypatch):
    # Keys Config supports but the wizard doesn't build — a rewrite must not drop them.
    prefill = {
        "project": {"agents_md_char_limit": 20000},
        "snowflake": {
            "objects": {"stage_name": "MY_STAGE", "container_python": "3.11"},
        },
        "governance": {"read_exceptions": ["ANALYTICS_DB.RAW.SANCTIONED_VIEW"]},
    }
    cfg_dict, _ = _run_wizard(monkeypatch, prefill=prefill)
    assert cfg_dict["project"]["agents_md_char_limit"] == 20000
    assert cfg_dict["snowflake"]["objects"]["stage_name"] == "MY_STAGE"
    assert cfg_dict["snowflake"]["objects"]["container_python"] == "3.11"
    assert cfg_dict["governance"]["read_exceptions"] == ["ANALYTICS_DB.RAW.SANCTIONED_VIEW"]
    text = _render_config_yaml(cfg_dict)
    assert yaml.safe_load(text) == cfg_dict
    Config.from_dict(cfg_dict)


def test_rendered_yaml_survives_values_longer_than_yaml_wrap_width(monkeypatch):
    # PyYAML wraps flow lists at ~80 cols by default; the renderer must not truncate.
    prefill = {
        "snowflake": {
            "objects": {"allowed_warehouses": [f"REPORTING_WAREHOUSE_{i:02d}" for i in range(8)]}
        }
    }
    cfg_dict, _ = _run_wizard(monkeypatch, prefill=prefill)
    text = _render_config_yaml(cfg_dict)
    assert yaml.safe_load(text) == cfg_dict


def test_slugify_directory_starting_with_digits(monkeypatch):
    cfg_dict, _ = _run_wizard(monkeypatch, directory=Path("2024-reports"))
    assert cfg_dict["project"]["slug"] == "reports"


def test_rendered_yaml_round_trips_and_carries_comments(monkeypatch):
    cfg_dict, _ = _run_wizard(monkeypatch)
    text = _render_config_yaml(cfg_dict)
    assert yaml.safe_load(text) == cfg_dict
    Config.from_dict(yaml.safe_load(text))
    # The defaulted values are self-documenting in the file.
    assert "#" in text
    assert "viewer" in text.lower()


def test_wizard_defaults_to_the_pre_provisioned_compute_pool(monkeypatch):
    """A first-time container user owns no compute pool. The old STREAMLIT_POOL default
    made deploy-setup --admin emit a CREATE COMPUTE POOL they had no reason to run (and
    often no privilege to); SYSTEM_COMPUTE_POOL_CPU exists in every account."""
    from streamsnow.deploy import generate_admin_sql

    cfg_dict, _ = _run_wizard(monkeypatch)
    assert cfg_dict["runtime"] == "container"
    assert cfg_dict["snowflake"]["objects"]["compute_pool"] == "SYSTEM_COMPUTE_POOL_CPU"
    sql = generate_admin_sql(Config.from_dict(cfg_dict))
    statements = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    assert "CREATE COMPUTE POOL" not in statements
    assert "GRANT USAGE ON COMPUTE POOL SYSTEM_COMPUTE_POOL_CPU TO ROLE" in statements
    # A pool the user already named survives a re-run of the wizard.
    prefill = {"snowflake": {"objects": {"compute_pool": "ACME_POOL"}}}
    cfg_dict, _ = _run_wizard(monkeypatch, prefill=prefill)
    assert cfg_dict["snowflake"]["objects"]["compute_pool"] == "ACME_POOL"


def test_wizard_defaults_are_streamsnow_branded(monkeypatch):
    """Default object names are StreamSnow's own, so an admin finds every database,
    warehouse, role, user and stage the bootstrap creates with LIKE 'STREAMSNOW%'
    (the DASHBOARDS schema lives inside STREAMSNOW_APPS; the PyPI integration keeps
    its descriptive name). The CI user derives from the deploy role."""
    import re

    from streamsnow.deploy import generate_admin_sql

    cfg_dict, _ = _run_wizard(monkeypatch)
    o, r = cfg_dict["snowflake"]["objects"], cfg_dict["snowflake"]["roles"]
    assert (o["app_database"], o["app_schema"]) == ("STREAMSNOW_APPS", "DASHBOARDS")
    assert (o["stage_database"], o["stage_schema"]) == ("STREAMSNOW_APPS", "DASHBOARDS")
    assert o["default_warehouse"] == "STREAMSNOW_WH"
    assert o["allowed_warehouses"] == ["STREAMSNOW_WH"]
    assert (r["ci_role"], r["viewer_role"]) == ("STREAMSNOW_DEPLOY_ROLE", "STREAMSNOW_VIEWER_ROLE")
    cfg = Config.from_dict(cfg_dict)
    assert cfg.snowflake.objects.stage_name == "STREAMSNOW_CODE_STAGE"  # loader default
    sql = generate_admin_sql(cfg)
    assert "CREATE USER IF NOT EXISTS STREAMSNOW_DEPLOY_USER" in sql
    created = re.findall(
        r"^CREATE (?:DATABASE|WAREHOUSE|ROLE|USER|STAGE) IF NOT EXISTS ([\w.$]+)", sql, re.M
    )
    assert len(created) == 6, created
    assert all(fqn.rsplit(".", 1)[-1].startswith("STREAMSNOW_") for fqn in created), created


class _Proc:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _fake_snow(monkeypatch, list_result, calls=None):
    """Route the wizard's detection through the real doctor parser over a faked
    subprocess. ``list_result`` is (returncode, stdout) or an exception to raise."""
    import json as _json

    monkeypatch.setattr(cli, "_snow_connections", _REAL_SNOW_CONNECTIONS)
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: f"/opt/acme/bin/{tool}")

    def fake_run(cmd, **_kwargs):
        if calls is not None:
            calls.append(list(cmd))
        if isinstance(list_result, Exception):
            raise list_result
        code, out = list_result
        return _Proc(code, out if isinstance(out, str) else _json.dumps(out))

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)


# The default connection's account is the wizard's answer ("ab12345.us-east-1" in
# _run_wizard) in another case: accounts compare case-insensitively.
_ROWS = [
    {"connection_name": "other", "is_default": False, "parameters": {"account": "zz99999"}},
    {
        "connection_name": "tutorial",
        "is_default": True,
        "parameters": {"account": "AB12345.US-EAST-1", "user": "u"},
    },
]


def test_wizard_defaults_connection_name_to_the_existing_default_connection(monkeypatch):
    """Someone who already has a working default snow connection (a prior tutorial) got
    connection_name = the folder slug, failed doctor's connection check, and was told
    to create a second --default connection. The existing default is the right answer
    when it opens the account they answered."""
    _fake_snow(monkeypatch, (0, _ROWS))
    cfg_dict, asked = _run_wizard(monkeypatch)
    assert cfg_dict["snowflake"]["connection_name"] == "tutorial"
    assert not any("connection" in q.lower() for q, _ in asked)  # detected, never asked
    cfg = Config.from_dict(cfg_dict)
    # ...so the doctor's connection check passes on the same rows.
    res = doctor.check_snow_connection(
        {"ok": True, "detail": {"connection_name": cfg.snowflake.connection_name}}, rows=_ROWS
    )
    assert res["ok"]
    # ...and the one-time step no longer tells them to add a default connection.
    hint = _connection_hint(cfg, _ROWS)
    assert "snow connection add" not in hint and "already your default" in hint


def _default_row(params: dict) -> list[dict]:
    return [{"connection_name": "tutorial", "is_default": True, "parameters": params}]


def test_wizard_ignores_a_default_connection_for_another_account(monkeypatch, capsys):
    """The default connection was adopted before the account was even asked, so a
    default left over from another account (a trial, a previous employer) wrote a
    config whose connection opens the wrong account. A mismatch falls back to the slug
    with a one-line note, and neither account value is printed."""
    _fake_snow(monkeypatch, (0, _default_row({"account": "zz99999.eu-west-1", "user": "u"})))
    cfg_dict, asked = _run_wizard(monkeypatch)
    assert cfg_dict["snowflake"]["connection_name"] == "acme-analytics"
    assert cfg_dict["snowflake"]["account"] == "ab12345.us-east-1"
    assert not any("connection" in q.lower() for q, _ in asked)  # detected, never asked
    out = capsys.readouterr().out
    assert "'tutorial' is for another account" in out
    assert "zz99999" not in out.lower() and "ab12345" not in out.lower()


def test_wizard_ignores_a_default_connection_that_names_no_account(monkeypatch, capsys):
    """No account parameter (or a non-string one) cannot be shown to match: slug."""
    for params in ({"user": "u"}, {"account": None}, {"account": ""}):
        _fake_snow(monkeypatch, (0, _default_row(params)))
        cfg_dict, _ = _run_wizard(monkeypatch)
        assert cfg_dict["snowflake"]["connection_name"] == "acme-analytics", params
        assert "'tutorial' names no account" in capsys.readouterr().out
    # No parameters mapping at all.
    _fake_snow(monkeypatch, (0, [{"connection_name": "tutorial", "is_default": True}]))
    cfg_dict, _ = _run_wizard(monkeypatch)
    assert cfg_dict["snowflake"]["connection_name"] == "acme-analytics"
    out = capsys.readouterr().out
    assert "'tutorial' names no account" in out and "ab12345" not in out.lower()


def test_wizard_connection_name_falls_back_to_the_slug(monkeypatch):
    no_default = [{"connection_name": "other", "is_default": False}]
    for result in (
        (0, no_default),  # connections, none of them the default
        (0, []),  # no connections at all
        (1, ""),  # snow on PATH but failing
        (0, "not json"),  # garbage output
        (0, {"not": "a list"}),  # wrong shape
        doctor.subprocess.TimeoutExpired(cmd="snow", timeout=15),  # hung cold start
        OSError("snow vanished"),
    ):
        _fake_snow(monkeypatch, result)
        cfg_dict, _ = _run_wizard(monkeypatch)
        assert cfg_dict["snowflake"]["connection_name"] == "acme-analytics", result
    # snow not installed at all.
    monkeypatch.setattr(cli, "_snow_connections", _REAL_SNOW_CONNECTIONS)
    monkeypatch.setattr(doctor.shutil, "which", lambda tool: None)
    cfg_dict, _ = _run_wizard(monkeypatch)
    assert cfg_dict["snowflake"]["connection_name"] == "acme-analytics"


def test_wizard_keeps_a_configured_connection_name_without_asking_snow(monkeypatch):
    calls: list[list[str]] = []
    _fake_snow(monkeypatch, (0, _ROWS), calls)
    prefill = {"snowflake": {"connection_name": "acme-prod"}}
    cfg_dict, _ = _run_wizard(monkeypatch, prefill=prefill)
    assert cfg_dict["snowflake"]["connection_name"] == "acme-prod"
    assert calls == []  # re-running configure never shells out for a value it has


def test_connection_hint_matches_what_snow_already_has():
    from streamsnow.config import load_config

    cfg = load_config(Path(__file__).resolve().parent.parent / "streamsnow.config.example.yaml")
    name = cfg.snowflake.connection_name
    absent = _connection_hint(cfg, None)
    assert f"snow connection add --connection-name {name}" in absent and "--default" in absent
    assert _connection_hint(cfg, [{"connection_name": "other", "is_default": True}]) == absent
    exists = _connection_hint(cfg, [{"connection_name": name, "is_default": False}])
    assert exists.startswith(f"snow connection set-default {name}")
    already = _connection_hint(cfg, [{"connection_name": name, "is_default": True}])
    assert "snow connection add" not in already and "already your default" in already


def test_wizard_checks_sources_live_and_prefills_imported_databases(monkeypatch, capsys):
    calls = []

    def fake_probe(connection, targets):
        calls.append((connection, list(targets)))
        return probe.ProbeReport(
            role="ANALYST",
            results=(
                probe.ProbeResult("ANALYTICS_DB.ANALYTICS", probe.VISIBLE, "ANALYST"),
                probe.ProbeResult(
                    "ANALYTICS_DB.REPORTING", probe.NOT_VISIBLE, "ANALYST", "not visible"
                ),
            ),
            imported_databases=("ANALYTICS_DB", "PARTNER_SHARE"),
        )

    monkeypatch.setattr(cli, "_live_probe", fake_probe)
    cfg_dict, _ = _run_wizard(monkeypatch)
    assert calls == [("acme-analytics", ["ANALYTICS_DB.ANALYTICS", "ANALYTICS_DB.REPORTING"])]
    # Only an imported database that holds a source (or app data) is recorded.
    assert cfg_dict["governance"]["imported_databases"] == ["ANALYTICS_DB"]
    Config.from_dict(cfg_dict)
    out = " ".join(capsys.readouterr().out.split())
    assert "role ANALYST" in out and "ANALYTICS_DB.REPORTING: not visible" in out


def test_wizard_without_a_probe_says_unverified_and_writes_no_imported_list(monkeypatch, capsys):
    cfg_dict, _ = _run_wizard(monkeypatch)  # conftest's stub: unverified
    assert "imported_databases" not in cfg_dict["governance"]
    assert "not checked live" in capsys.readouterr().out


def test_live_probe_only_runs_for_a_connection_this_machine_has(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("probed without a matching snow connection")

    monkeypatch.setattr(probe, "probe_schemas", boom)
    monkeypatch.setattr(cli, "_snow_connections", lambda: None)
    report = _REAL_LIVE_PROBE("acme", ["ANALYTICS_DB.REPORTING"])
    assert report.results[0].status == probe.UNVERIFIED and "acme" in report.error
    seen = []
    monkeypatch.setattr(cli, "_snow_connections", lambda: [{"connection_name": "acme"}])
    monkeypatch.setattr(
        probe,
        "probe_schemas",
        lambda c, t, **_kw: seen.append((c, t)) or probe.unverified(t, "x"),
    )
    _REAL_LIVE_PROBE("acme", ["ANALYTICS_DB.REPORTING"])
    assert seen == [("acme", ["ANALYTICS_DB.REPORTING"])]
