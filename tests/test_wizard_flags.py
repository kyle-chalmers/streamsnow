"""The non-interactive answer flags on `init` and `configure`.

`/start-app --setup` investigates the account read-only, proposes the five wizard
answers, and passes the confirmed ones as flags. The flags feed the same
`_prompt_config` defaults and prefill logic as the interactive wizard, so the
written file must be identical for identical answers, and no prompt may fire
when all five are supplied.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
import yaml
from typer.testing import CliRunner

from streamsnow import cli
from streamsnow.cli import _prompt_config, app
from streamsnow.config import CONFIG_FILENAME, Config

runner = CliRunner()

# One set of answers, as the wizard would be typed and as flags would pass it.
ANSWERS = {
    "runtime": "warehouse",
    "account": "ab12345.us-east-1",
    "database": "ACME_ANALYTICS",
    "schemas": "MARTS,REPORTING",
    "deploy_source": "stage-copy",
}
WIZARD_INPUT = "\n".join(
    [
        ANSWERS["runtime"],
        ANSWERS["account"],
        ANSWERS["database"],
        ANSWERS["schemas"],
        ANSWERS["deploy_source"],
    ]
)
FLAGS = [
    "--runtime",
    ANSWERS["runtime"],
    "--account",
    ANSWERS["account"],
    "--database",
    ANSWERS["database"],
    "--schemas",
    ANSWERS["schemas"],
    "--deploy-source",
    ANSWERS["deploy_source"],
]


def _no_prompts(monkeypatch):
    def boom(text, *args, **kwargs):
        raise AssertionError(f"prompted although every answer was a flag: {text!r}")

    monkeypatch.setattr(typer, "prompt", boom)


def _same_named_dirs(tmp_path: Path) -> tuple[Path, Path]:
    # The project slug derives from the directory name, so both runs need the same one.
    a, b = tmp_path / "wizard" / "acme-analytics", tmp_path / "flags" / "acme-analytics"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    return a, b


def test_flags_build_the_same_config_dict_as_the_wizard(monkeypatch):
    typed = iter(WIZARD_INPUT.splitlines())
    monkeypatch.setattr(typer, "prompt", lambda text, default=None, **kw: next(typed))
    from_wizard = _prompt_config(None, Path("acme-analytics"))

    _no_prompts(monkeypatch)
    from_flags = _prompt_config(None, Path("acme-analytics"), dict(ANSWERS))
    assert from_flags == from_wizard
    Config.from_dict(from_flags)


@pytest.mark.parametrize("verb", ["init", "configure"])
def test_flags_write_the_same_file_as_the_wizard(tmp_path, monkeypatch, verb):
    wiz_dir, flag_dir = _same_named_dirs(tmp_path)
    extra = ["--no-starter-app"] if verb == "init" else []
    r = runner.invoke(app, [verb, "--dir", str(wiz_dir), *extra], input=WIZARD_INPUT + "\n")
    assert r.exit_code == 0, r.output

    _no_prompts(monkeypatch)
    r = runner.invoke(app, [verb, "--dir", str(flag_dir), *extra, *FLAGS])
    assert r.exit_code == 0, r.output
    assert (flag_dir / CONFIG_FILENAME).read_text() == (wiz_dir / CONFIG_FILENAME).read_text()


def test_init_with_flags_writes_the_governed_repo_files(tmp_path, monkeypatch):
    _no_prompts(monkeypatch)
    r = runner.invoke(app, ["init", "--dir", str(tmp_path), "--no-starter-app", *FLAGS])
    assert r.exit_code == 0, r.output
    for rel in ("AGENTS.md", ".gitignore", ".pre-commit-config.yaml"):
        assert (tmp_path / rel).exists(), rel
    assert not (tmp_path / "apps").exists()


def test_deny_schemas_flag_replaces_the_raw_staging_default(monkeypatch):
    _no_prompts(monkeypatch)
    cfg = _prompt_config(None, Path("x"), {**ANSWERS, "deny_schemas": "LANDING, STG_SALESFORCE"})
    assert cfg["governance"]["schema_deny"] == ["LANDING", "STG_SALESFORCE"]
    # An empty value means "deny nothing", not "use the default".
    cfg = _prompt_config(None, Path("x"), {**ANSWERS, "deny_schemas": ""})
    assert cfg["governance"]["schema_deny"] == []
    # Without the flag the existing default stands.
    cfg = _prompt_config(None, Path("x"), dict(ANSWERS))
    assert cfg["governance"]["schema_deny"] == ["RAW", "STAGING"]


def test_flags_override_prefill_and_keep_hand_edits(monkeypatch):
    _no_prompts(monkeypatch)
    prefill = {
        "runtime": "container",
        "governance": {"database": "OLD_DB", "read_exceptions": ["OLD_DB.RAW.SANCTIONED"]},
        "snowflake": {"objects": {"stage_name": "MY_STAGE"}},
    }
    cfg = _prompt_config(prefill, Path("x"), dict(ANSWERS))
    assert cfg["runtime"] == "warehouse"
    assert cfg["governance"]["database"] == "ACME_ANALYTICS"
    assert cfg["governance"]["read_exceptions"] == ["OLD_DB.RAW.SANCTIONED"]
    assert cfg["snowflake"]["objects"]["stage_name"] == "MY_STAGE"


def test_partial_flags_prompt_only_for_the_rest(monkeypatch):
    asked: list[str] = []

    def fake_prompt(text, default=None, **kwargs):
        asked.append(str(text))
        return default if default is not None else "ab12345.us-east-1"

    monkeypatch.setattr(typer, "prompt", fake_prompt)
    cfg = _prompt_config(None, Path("x"), {"runtime": "warehouse", "database": "ACME_ANALYTICS"})
    assert len(asked) == 3
    assert not any(q.startswith(("Runtime", "Database")) for q in asked)
    assert cfg["runtime"] == "warehouse"
    assert cfg["governance"]["database"] == "ACME_ANALYTICS"


def _connections(monkeypatch, rows):
    monkeypatch.setattr(cli, "_snow_connections", lambda: rows)


def test_connection_flag_reads_the_account_without_printing_it(tmp_path, monkeypatch):
    locator = "zq98765.eu-west-1"
    _connections(
        monkeypatch,
        [
            {
                "connection_name": "acme",
                "is_default": True,
                "parameters": {"account": f"{locator}.snowflakecomputing.com"},
            }
        ],
    )
    _no_prompts(monkeypatch)
    flags = [f for f in FLAGS if f not in ("--account", ANSWERS["account"])]
    r = runner.invoke(app, ["configure", "--dir", str(tmp_path), "--connection", "acme", *flags])
    assert r.exit_code == 0, r.output
    written = yaml.safe_load((tmp_path / CONFIG_FILENAME).read_text())
    # Stored as the locator (the hostname suffix would double up in the connector).
    assert written["snowflake"]["account"] == locator
    assert written["snowflake"]["connection_name"] == "acme"
    assert locator not in r.output


@pytest.mark.parametrize(
    ("rows", "expect"),
    [
        (None, "could not read"),
        ([{"connection_name": "other", "parameters": {"account": "zz1.us"}}], "no snow connection"),
        ([{"connection_name": "acme", "parameters": {}}], "names no account"),
    ],
)
def test_connection_flag_fails_plainly_when_it_cannot_derive(tmp_path, monkeypatch, rows, expect):
    _connections(monkeypatch, rows)
    _no_prompts(monkeypatch)
    flags = [f for f in FLAGS if f not in ("--account", ANSWERS["account"])]
    r = runner.invoke(app, ["configure", "--dir", str(tmp_path), "--connection", "acme", *flags])
    assert r.exit_code == 2, r.output
    assert expect in r.output
    assert "zz1.us" not in r.output
    assert not (tmp_path / CONFIG_FILENAME).exists()


@pytest.mark.parametrize(
    ("extra", "expect"),
    [
        (["--runtime", "lambda"], "--runtime"),
        (["--deploy-source", "ftp"], "--deploy-source"),
        (["--schemas", " , "], "--schemas"),
        (["--deny-schemas", "REPORTING"], "both allowed and denied"),
        (["--connection", "acme"], "--account or --connection"),
    ],
)
def test_bad_flag_values_exit_2_before_writing(tmp_path, monkeypatch, extra, expect):
    _no_prompts(monkeypatch)
    r = runner.invoke(app, ["configure", "--dir", str(tmp_path), *FLAGS, *extra])
    assert r.exit_code == 2, r.output
    assert expect in r.output
    assert not (tmp_path / CONFIG_FILENAME).exists()


def test_flags_never_silently_ignored_on_an_existing_config(tmp_path, monkeypatch):
    _no_prompts(monkeypatch)
    assert (
        runner.invoke(app, ["init", "--dir", str(tmp_path), "--no-starter-app", *FLAGS]).exit_code
        == 0
    )
    before = (tmp_path / CONFIG_FILENAME).read_text()
    r = runner.invoke(
        app, ["init", "--dir", str(tmp_path), "--no-starter-app", "--database", "OTHER_DB"]
    )
    assert r.exit_code == 2, r.output
    assert "--reconfigure" in r.output
    assert (tmp_path / CONFIG_FILENAME).read_text() == before
    # With --reconfigure the flags apply (the last --database wins).
    r = runner.invoke(
        app,
        [
            "init",
            "--dir",
            str(tmp_path),
            "--no-starter-app",
            "--reconfigure",
            *FLAGS,
            "--database",
            "OTHER_DB",
        ],
    )
    assert r.exit_code == 0, r.output
    after = yaml.safe_load((tmp_path / CONFIG_FILENAME).read_text())
    assert after["governance"]["database"] == "OTHER_DB"
    assert after["governance"]["schema_allow"] == ["MARTS", "REPORTING"]


def test_flags_and_config_import_are_exclusive(tmp_path, monkeypatch):
    example = Path(__file__).resolve().parent.parent / "streamsnow.config.example.yaml"
    _no_prompts(monkeypatch)
    for verb in ("init", "configure"):
        r = runner.invoke(
            app, [verb, "--dir", str(tmp_path / verb), "--config", str(example), *FLAGS]
        )
        assert r.exit_code == 2, r.output
        assert "--config" in r.output
