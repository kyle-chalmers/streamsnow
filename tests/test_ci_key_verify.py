"""`streamsnow ci-key verify`: sign in as the CI user, check read-only, never echo a secret.

No Snowflake and no network: a fake ``snow`` (subprocess.run-shaped) answers each
probe from its SQL, and records argv, stdin and env so the tests can prove where
each secret went.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import re
import subprocess
from pathlib import Path

import pytest

from streamsnow import ci_key
from streamsnow.config import load_config
from streamsnow.deploy import expected_ci_grants, generate_admin_sql

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"

KEY = (
    "-----BEGIN "
    + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwSecretBody\n-----END "
    + "PRIVATE KEY-----\n"
)
KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwSecretBody"
ACCOUNT = "ab12345.us-east-1"
USER = "STREAMSNOW_DEPLOY_USER"
ROLE = "STREAMSNOW_DEPLOY_ROLE"
WAREHOUSE = "STREAMSNOW_WH"
VALUES = {
    "SNOWFLAKE_USER": USER,
    "SNOWFLAKE_WAREHOUSE": WAREHOUSE,
    "SNOWFLAKE_ROLE": ROLE,
    "SNOWFLAKE_ACCOUNT": ACCOUNT,
}
SECRETS = (KEY_BODY, ACCOUNT, "ab12345", USER)


def _secrets_dir(tmp_path: Path, *, skip: tuple[str, ...] = (), **overrides: str) -> Path:
    d = tmp_path / "ci"
    (d / "secrets").mkdir(parents=True)
    if ci_key.PRIVATE_KEY_SECRET not in skip:
        (d / "secrets" / ci_key.PRIVATE_KEY_SECRET).write_bytes(KEY.encode("utf-8"))
    for name, value in {**VALUES, **overrides}.items():
        if name not in skip:
            (d / "secrets" / name).write_text(value, encoding="utf-8")
    return d


def _cfg():
    return load_config(EXAMPLE)


def _grant_rows(cfg, *, drop: tuple[str, ...] = ()) -> list[dict]:
    """SHOW GRANTS TO ROLE rows for every grant the admin script gives the CI role."""
    rows = []
    for g in expected_ci_grants(cfg):
        if g.name in drop:
            continue
        rows.append(
            {
                "created_on": "2026-10-01 00:00:00",
                "privilege": g.privilege,
                "granted_on": g.granted_on.replace(" ", "_"),
                "name": g.name,
                "granted_to": "ROLE",
                "grantee_name": ROLE,
            }
        )
    return rows


class FakeSnow:
    """A subprocess.run stand-in for `snow sql`; fails probes whose SQL matches fail_on."""

    def __init__(self, cfg, *, fail_on: tuple[str, ...] = (), err: str | None = None, **answers):
        self.calls: list[dict] = []
        self.fail_on = fail_on
        self.err = err
        self.answers = {
            "CURRENT_ROLE": [{"ROLE": ROLE}],
            "USE WAREHOUSE": [{"status": "Statement executed successfully."}],
            "SHOW SCHEMAS": [{"name": "DASHBOARDS", "database_name": "STREAMSNOW_APPS"}],
            "SHOW GRANTS": _grant_rows(cfg),
            "LIMIT 0": [],
            **answers,
        }

    def __call__(self, argv, **kwargs):
        sql = kwargs.get("input") or ""
        self.calls.append({"argv": list(argv), **kwargs})
        for needle in self.fail_on:
            if needle in sql:
                # snow's error panel, echoing what a real failure can carry.
                err = (
                    "╭─ Error ─────────────────────────────╮\n"
                    f"│ 250001: Could not connect to {ACCOUNT}.snowflakecomputing.com │\n"
                    f"│ as user {USER}: JWT token is invalid ({KEY_BODY}) │\n"
                    "╰─────────────────────────────────────╯\n"
                )
                return subprocess.CompletedProcess(argv, 1, "", self.err or err)
        for needle, rows in self.answers.items():
            if needle in sql:
                return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")
        raise AssertionError(f"unexpected probe SQL: {sql!r}")


def _which(name):
    return f"/usr/bin/{name}"


def _cli(monkeypatch, fake, *args: str):
    from typer.testing import CliRunner

    from streamsnow.cli import app

    monkeypatch.setattr(ci_key, "verify", functools.partial(ci_key.verify, run=fake, which=_which))
    return CliRunner().invoke(app, ["ci-key", "verify", "--config", str(EXAMPLE), *args])


def _no_secret(text: str) -> None:
    for value in SECRETS:
        assert value not in text
        assert value.lower() not in text.lower()


# --- the env the probes sign in with ------------------------------------------------


def test_ci_env_mirrors_the_deploy_job_and_strips_inherited_snowflake_vars():
    payloads = {
        ci_key.PRIVATE_KEY_SECRET: KEY.encode(),
        **{k: v.encode() for k, v in VALUES.items()},
    }
    environ = {
        "PATH": "/usr/bin",
        "SNOWFLAKE_PASSWORD": "hunter2",
        "SNOWFLAKE_DEFAULT_CONNECTION_NAME": "mine",
        "SNOWFLAKE_CONNECTIONS_MINE_ROLE": "ACCOUNTADMIN",
        "snowflake_authenticator": "externalbrowser",
        "PRIVATE_KEY_PASSPHRASE": "pp",
    }
    env = ci_key.ci_env(payloads, environ)
    assert env["PATH"] == "/usr/bin"
    assert env["SNOWFLAKE_AUTHENTICATOR"] == "SNOWFLAKE_JWT"
    assert env["SNOWFLAKE_PRIVATE_KEY_RAW"] == KEY
    for name, value in VALUES.items():
        assert env[name] == value
    for gone in (
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_DEFAULT_CONNECTION_NAME",
        "SNOWFLAKE_CONNECTIONS_MINE_ROLE",
        "snowflake_authenticator",
        "PRIVATE_KEY_PASSPHRASE",
    ):
        assert gone not in env
    snowflake_vars = {k for k in env if k.upper().startswith("SNOWFLAKE_")}
    assert snowflake_vars == {*VALUES, ci_key.PRIVATE_KEY_SECRET, "SNOWFLAKE_AUTHENTICATOR"}


def test_secrets_go_only_in_env_never_argv_or_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "hunter2")
    monkeypatch.setenv("PRIVATE_KEY_PASSPHRASE", "pp")
    cfg = _cfg()
    fake = FakeSnow(cfg)
    result = ci_key.verify(
        _secrets_dir(tmp_path),
        cfg=cfg,
        obj="ANALYTICS_DB.ANALYTICS.ORDERS",
        run=fake,
        which=_which,
    )
    assert result.ok
    assert len(fake.calls) >= 5  # one sign-in per probe, as the brief requires
    for call in fake.calls:
        argv = call["argv"]
        assert argv[1:] == [
            "sql",
            "--stdin",
            "--format",
            "json",
            "--enable-templating",
            "NONE",
            "--temporary-connection",
        ]
        for value in SECRETS:
            assert all(value not in a for a in argv)
            assert value not in call["input"]
        env = call["env"]
        assert env["SNOWFLAKE_PRIVATE_KEY_RAW"] == KEY
        assert env["SNOWFLAKE_ACCOUNT"] == ACCOUNT
        assert env["SNOWFLAKE_USER"] == USER
        assert env["SNOWFLAKE_AUTHENTICATOR"] == "SNOWFLAKE_JWT"
        assert "SNOWFLAKE_PASSWORD" not in env
        assert "PRIVATE_KEY_PASSPHRASE" not in env
        assert call.get("shell") in (None, False)


def test_every_probe_passes_and_exits_0(tmp_path, monkeypatch):
    cfg = _cfg()
    fake = FakeSnow(cfg)
    r = _cli(
        monkeypatch,
        fake,
        "--dir",
        str(_secrets_dir(tmp_path)),
        "--object",
        "ANALYTICS_DB.ANALYTICS.ORDERS",
    )
    assert r.exit_code == 0, r.output
    assert "✗" not in r.output
    assert ROLE in r.output and WAREHOUSE in r.output
    assert "ANALYTICS_DB.ANALYTICS.ORDERS" in r.output
    _no_secret(r.output)


def test_json_output_shape_and_no_secret(tmp_path, monkeypatch):
    cfg = _cfg()
    fake = FakeSnow(cfg, fail_on=("USE WAREHOUSE",))
    r = _cli(monkeypatch, fake, "--dir", str(_secrets_dir(tmp_path)), "--format", "json")
    assert r.exit_code == 1
    data = json.loads(r.output)
    assert data["ok"] is False
    assert {p["probe"] for p in data["probes"]} >= {"role", "warehouse", "schema", "grant"}
    for p in data["probes"]:
        assert set(p) == {"probe", "object", "status", "detail"}
        assert p["status"] in {"pass", "fail", "skipped"}
    _no_secret(r.output)


def test_output_never_contains_the_key_account_or_user_on_failure(tmp_path, monkeypatch):
    cfg = _cfg()
    for fail in ("USE WAREHOUSE", "SHOW SCHEMAS", "SHOW GRANTS", "LIMIT 0", "CURRENT_ROLE"):
        fake = FakeSnow(cfg, fail_on=(fail,))
        for fmt in ("md", "json"):
            r = _cli(
                monkeypatch,
                fake,
                "--dir",
                str(_secrets_dir(tmp_path / fail.replace(" ", "_") / fmt)),
                "--object",
                "ANALYTICS_DB.ANALYTICS.ORDERS",
                "--format",
                fmt,
            )
            assert r.exit_code == 1, (fail, fmt, r.output)
            _no_secret(r.output)
            assert "JWT token is invalid" in r.output  # the useful part survives


def test_a_failed_probe_is_named_by_object(tmp_path, monkeypatch):
    cfg = _cfg()
    fake = FakeSnow(cfg, fail_on=("USE WAREHOUSE",))
    r = _cli(monkeypatch, fake, "--dir", str(_secrets_dir(tmp_path)))
    assert r.exit_code == 1
    line = next(ln for ln in r.output.splitlines() if "✗" in ln)
    assert "warehouse" in line and WAREHOUSE in line


def test_a_missing_grant_is_named(tmp_path, monkeypatch):
    cfg = _cfg()
    fake = FakeSnow(cfg, **{"SHOW GRANTS": _grant_rows(cfg, drop=("STREAMSNOW_WH",))})
    r = _cli(monkeypatch, fake, "--dir", str(_secrets_dir(tmp_path)))
    assert r.exit_code == 1
    failed = [ln for ln in r.output.splitlines() if "✗" in ln]
    assert len(failed) == 1
    assert "USAGE on WAREHOUSE STREAMSNOW_WH" in failed[0]


def test_ownership_satisfies_a_grant(tmp_path):
    cfg = _cfg()
    rows = _grant_rows(cfg, drop=("STREAMSNOW_WH",))
    rows.append({"privilege": "OWNERSHIP", "granted_on": "WAREHOUSE", "name": '"STREAMSNOW_WH"'})
    fake = FakeSnow(cfg, **{"SHOW GRANTS": rows})
    assert ci_key.verify(_secrets_dir(tmp_path), cfg=cfg, run=fake, which=_which).ok


def test_role_mismatch_fails_the_role_probe(tmp_path):
    cfg = _cfg()
    fake = FakeSnow(cfg, CURRENT_ROLE=[{"ROLE": "PUBLIC"}])
    result = ci_key.verify(_secrets_dir(tmp_path), cfg=cfg, run=fake, which=_which)
    role = next(p for p in result.probes if p.probe == "role")
    assert role.status == "fail" and role.object == ROLE and "PUBLIC" in role.detail


def test_missing_schema_fails(tmp_path):
    cfg = _cfg()
    fake = FakeSnow(cfg, **{"SHOW SCHEMAS": []})
    result = ci_key.verify(_secrets_dir(tmp_path), cfg=cfg, run=fake, which=_which)
    schema = next(p for p in result.probes if p.probe == "schema")
    assert schema.status == "fail" and schema.object == "STREAMSNOW_APPS.DASHBOARDS"


def test_a_failed_sign_in_stops_after_the_first_probe(tmp_path):
    cfg = _cfg()
    fake = FakeSnow(cfg, fail_on=("CURRENT_ROLE",))
    result = ci_key.verify(_secrets_dir(tmp_path), cfg=cfg, run=fake, which=_which)
    assert len(fake.calls) == 1
    assert not result.ok
    assert all(p.status == "skipped" for p in result.probes[1:])


def test_without_object_the_read_probe_is_skipped(tmp_path):
    cfg = _cfg()
    fake = FakeSnow(cfg)
    result = ci_key.verify(_secrets_dir(tmp_path), cfg=cfg, run=fake, which=_which)
    assert result.ok
    read = next(p for p in result.probes if p.probe == "select")
    assert read.status == "skipped" and "--object" in read.detail
    assert not any("LIMIT 0" in c["input"] for c in fake.calls)


def test_the_read_probe_runs_a_guarded_limit_0_select(tmp_path):
    cfg = _cfg()
    fake = FakeSnow(cfg)
    ci_key.verify(
        _secrets_dir(tmp_path), cfg=cfg, obj="analytics_db.reporting.daily", run=fake, which=_which
    )
    sql = [c["input"] for c in fake.calls if "LIMIT 0" in c["input"]]
    assert sql == ["SELECT * FROM analytics_db.reporting.daily LIMIT 0;\n"]


# --- exit 2: refused before any sign-in --------------------------------------------


@pytest.mark.parametrize("name", [*VALUES, ci_key.PRIVATE_KEY_SECRET])
def test_a_missing_secret_file_exits_2_before_signing_in(tmp_path, monkeypatch, name):
    cfg = _cfg()
    fake = FakeSnow(cfg)
    r = _cli(monkeypatch, fake, "--dir", str(_secrets_dir(tmp_path, skip=(name,))))
    assert r.exit_code == 2
    assert name in r.output
    assert fake.calls == []
    _no_secret(r.output)


@pytest.mark.parametrize(
    "obj",
    [
        "ANALYTICS_DB.RAW.ORDERS",  # a denied schema
        "ANALYTICS_DB.MARKETING.ORDERS",  # neither allowed nor denied
        "OTHER_DB.ANALYTICS.ORDERS",  # another database
        "ANALYTICS_DB.ANALYTICS",  # not DB.SCHEMA.OBJECT
        "ANALYTICS_DB.ANALYTICS.ORDERS; DROP TABLE X",
        "ANALYTICS_DB.ANALYTICS.ORDERS--",
    ],
)
def test_an_object_outside_the_allowlist_is_refused(tmp_path, monkeypatch, obj):
    cfg = _cfg()
    fake = FakeSnow(cfg)
    r = _cli(monkeypatch, fake, "--dir", str(_secrets_dir(tmp_path)), "--object", obj)
    assert r.exit_code == 2, r.output
    assert fake.calls == []


def test_a_secret_file_that_differs_from_the_config_exits_2(tmp_path, monkeypatch):
    cfg = _cfg()
    fake = FakeSnow(cfg)
    d = _secrets_dir(tmp_path, SNOWFLAKE_WAREHOUSE="OLD_WH")
    r = _cli(monkeypatch, fake, "--dir", str(d))
    assert r.exit_code == 2
    assert "SNOWFLAKE_WAREHOUSE" in r.output
    assert fake.calls == []


def test_snow_missing_exits_2(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from streamsnow.cli import app

    cfg = _cfg()
    fake = FakeSnow(cfg)
    monkeypatch.setattr(
        ci_key, "verify", functools.partial(ci_key.verify, run=fake, which=lambda name: None)
    )
    r = CliRunner().invoke(
        app,
        ["ci-key", "verify", "--config", str(EXAMPLE), "--dir", str(_secrets_dir(tmp_path))],
    )
    assert r.exit_code == 2
    assert "snow" in r.output
    assert fake.calls == []


def test_snow_not_found_at_run_time_exits_2(tmp_path, monkeypatch):
    def gone(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    r = _cli(monkeypatch, gone, "--dir", str(_secrets_dir(tmp_path)))
    assert r.exit_code == 2


def test_bad_format_exits_2(tmp_path, monkeypatch):
    fake = FakeSnow(_cfg())
    r = _cli(monkeypatch, fake, "--dir", str(_secrets_dir(tmp_path)), "--format", "yaml")
    assert r.exit_code == 2
    assert fake.calls == []


def test_help_states_the_trade_off():
    from typer.testing import CliRunner

    from streamsnow.cli import app

    r = CliRunner().invoke(app, ["ci-key", "verify", "--help"], env={"COLUMNS": "200"})
    text = " ".join(r.output.split()).lower()
    assert "production" in text
    assert "login history" in text
    assert "network policy" in text


# --- expected_ci_grants comes from the admin script itself ------------------------


_GRANT_TO_CI = re.compile(r"^GRANT (?P<priv>.+?) ON (?P<target>.+) TO ROLE (?P<role>\S+);$")


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"runtime": "warehouse"},
        {"governance_db": "SNOWFLAKE_SAMPLE_DATA"},
        {"source": "git-repository"},
        {"compute_pool": "ACME_POOL"},
    ],
)
def test_expected_ci_grants_match_the_admin_script_line_for_line(overrides):
    from streamsnow.deploy import with_source

    cfg = _cfg()
    if overrides.get("runtime"):
        cfg = dataclasses.replace(cfg, runtime=overrides["runtime"])
    if overrides.get("governance_db"):
        cfg = dataclasses.replace(
            cfg, governance=dataclasses.replace(cfg.governance, database=overrides["governance_db"])
        )
    if overrides.get("source"):
        cfg = with_source(cfg, overrides["source"], git_origin="https://github.com/acme/apps")
    if overrides.get("compute_pool"):
        objects = dataclasses.replace(cfg.snowflake.objects, compute_pool=overrides["compute_pool"])
        cfg = dataclasses.replace(
            cfg, snowflake=dataclasses.replace(cfg.snowflake, objects=objects)
        )
    ci = cfg.snowflake.roles.ci_role
    lines = set()
    for line in generate_admin_sql(cfg).splitlines():
        m = _GRANT_TO_CI.match(line)
        if m and m["role"] == ci and not m["target"].startswith(("ALL ", "FUTURE ")):
            lines.add(line)
    rendered = {
        f"GRANT {g.privilege} ON {g.granted_on} {g.name} TO ROLE {ci};"
        for g in expected_ci_grants(cfg)
    }
    assert rendered == lines
    assert lines  # every configuration grants the CI role something checkable


def test_expected_ci_grants_follow_a_new_admin_grant(monkeypatch):
    """Derived, not hand-copied: a grant added to the admin script shows up here."""
    from streamsnow import deploy

    cfg = _cfg()
    original = deploy.generate_admin_sql

    def extended(c, **kw):
        return original(c, **kw) + f"\nGRANT MONITOR ON WAREHOUSE X_WH TO ROLE {ROLE};"

    monkeypatch.setattr(deploy, "generate_admin_sql", extended)
    grants = deploy.expected_ci_grants(cfg)
    assert ("MONITOR", "WAREHOUSE", "X_WH") in {(g.privilege, g.granted_on, g.name) for g in grants}


# --- fix round 1: exit contract and fail-closed redaction ---------------------------


@pytest.mark.parametrize("name", [*VALUES, ci_key.PRIVATE_KEY_SECRET])
def test_a_non_utf8_secret_file_exits_2(tmp_path, monkeypatch, name):
    fake = FakeSnow(_cfg())
    d = _secrets_dir(tmp_path)
    (d / "secrets" / name).write_bytes(b"\xff\xfe not text")
    r = _cli(monkeypatch, fake, "--dir", str(d))
    assert r.exit_code == 2, r.output
    assert "UTF-8" in r.output and name in r.output
    assert "Traceback" not in r.output
    assert fake.calls == []


@pytest.mark.parametrize("exc", [PermissionError(13, "Permission denied"), OSError(8, "Exec")])
def test_snow_that_cannot_start_exits_2(tmp_path, monkeypatch, exc):
    def broken(argv, **kwargs):
        raise exc

    r = _cli(monkeypatch, broken, "--dir", str(_secrets_dir(tmp_path)))
    assert r.exit_code == 2, r.output
    assert "could not be started" in r.output
    assert "Traceback" not in r.output
    _no_secret(r.output)


def test_the_cli_never_prints_traceback_locals():
    from streamsnow.cli import app

    assert app.pretty_exceptions_show_locals is False


def test_env_sets_wide_columns_so_rich_does_not_wrap(tmp_path):
    cfg = _cfg()
    fake = FakeSnow(cfg)
    ci_key.verify(_secrets_dir(tmp_path), cfg=cfg, run=fake, which=_which)
    assert all(c["env"]["COLUMNS"] == "1000" for c in fake.calls)


LONG_ACCOUNT = "acme_org-acme_account_name_for_tests"


def _detail_for(tmp_path, err: str, account: str = LONG_ACCOUNT) -> str:
    cfg = _cfg()
    fake = FakeSnow(cfg, fail_on=("USE WAREHOUSE",), err=err)
    result = ci_key.verify(
        _secrets_dir(tmp_path, SNOWFLAKE_ACCOUNT=account), cfg=cfg, run=fake, which=_which
    )
    return next(p for p in result.probes if p.probe == "warehouse").detail


def _squashed(text: str) -> str:
    return re.sub(r"[\s│╭╮╰╯─]+", "", text).lower().replace("-", "_")


def test_an_account_wrapped_across_panel_lines_is_withheld(tmp_path):
    err = (
        "╭─ Error ──────────────────────────────────────────╮\n"
        "│ 250001: Could not connect to acme_org-acme_account_na │\n"
        "│ me_for_tests.snowflakecomputing.com: timed out     │\n"
        "╰──────────────────────────────────────────────────╯\n"
    )
    detail = _detail_for(tmp_path, err)
    assert detail == ci_key.WITHHELD
    assert _squashed(LONG_ACCOUNT) not in _squashed(detail)


def test_a_hyphenated_hostname_account_never_reaches_the_detail(tmp_path):
    err = "250001: Could not connect to acme-org-acme-account-name-for-tests.snowflakecomputing.com"
    detail = _detail_for(tmp_path, err)
    assert _squashed(LONG_ACCOUNT) not in _squashed(detail)
    assert "acme" not in detail.lower()


def test_a_wrapped_user_or_key_line_is_withheld(tmp_path):
    for err in (
        f"│ JWT token is invalid for user {USER[:9]} │\n│ {USER[9:]} │",
        f"│ bad key {KEY_BODY[:20]} │\n│ {KEY_BODY[20:]} │",
    ):
        assert _detail_for(tmp_path / str(len(err)), err) == ci_key.WITHHELD


def test_a_clean_error_is_kept(tmp_path):
    err = "002003 (02000): SQL compilation error: Warehouse 'STREAMSNOW_WH' does not exist."
    detail = _detail_for(tmp_path, err)
    assert "does not exist" in detail and "STREAMSNOW_WH" in detail


# --- fix round 2: truncation, NUL bytes, OSError text ------------------------------


def test_a_wrapped_account_before_the_600_char_cut_is_withheld(tmp_path):
    # _error_detail keeps the last 600 characters; the wrap point sits just
    # before that cut, so only a fragment of the account would survive it.
    err = (
        "│ 250001: Could not connect to acme_org-acme_account_na │\n"
        "│ me_for_tests.snowflakecomputing.com │\n" + "│ " + "x" * 560 + " │\n"
    )
    detail = _detail_for(tmp_path, err)
    assert detail == ci_key.WITHHELD
    assert "for_tests" not in detail and "me_for" not in detail


@pytest.mark.parametrize("name", [*VALUES, ci_key.PRIVATE_KEY_SECRET])
def test_a_secret_file_with_a_nul_byte_exits_2(tmp_path, monkeypatch, name):
    fake = FakeSnow(_cfg())
    d = _secrets_dir(tmp_path)
    original = (d / "secrets" / name).read_bytes()
    (d / "secrets" / name).write_bytes(original[:4] + b"\x00" + original[4:])
    r = _cli(monkeypatch, fake, "--dir", str(d))
    assert r.exit_code == 2, r.output
    assert "NUL" in r.output and name in r.output
    assert "Traceback" not in r.output
    assert fake.calls == []
    _no_secret(r.output)


def test_a_value_error_from_the_subprocess_exits_2(tmp_path, monkeypatch):
    def nul(argv, **kwargs):
        raise ValueError(f"embedded null byte in {ACCOUNT}")

    r = _cli(monkeypatch, nul, "--dir", str(_secrets_dir(tmp_path)))
    assert r.exit_code == 2, r.output
    assert "could not be started (ValueError)" in r.output
    _no_secret(r.output)


def test_an_os_error_without_strerror_shows_its_class_name(tmp_path, monkeypatch):
    def broken(argv, **kwargs):
        raise OSError(f"cannot exec with {ACCOUNT}")

    r = _cli(monkeypatch, broken, "--dir", str(_secrets_dir(tmp_path)))
    assert r.exit_code == 2, r.output
    assert "could not be started (OSError)" in r.output
    assert "None" not in r.output
    _no_secret(r.output)
