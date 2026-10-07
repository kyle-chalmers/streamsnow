"""The non-interactive answer flags on `init` and `configure`.

`/onboard` investigates the account read-only, proposes the wizard
answers, and passes the confirmed ones as flags. The flags feed the same
`_prompt_config` defaults and prefill logic as the interactive wizard, so the
written file must be identical for identical answers, and no prompt may fire
when every answer is supplied.
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
    "sources": "ACME_ANALYTICS.MARTS,ACME_ANALYTICS.REPORTING",
    "app_data": "STREAMSNOW_APPS.STREAMSNOW_REPORTING",
    "deploy_source": "stage-copy",
}
WIZARD_INPUT = "\n".join(
    [
        ANSWERS["runtime"],
        ANSWERS["account"],
        ANSWERS["sources"],
        ANSWERS["app_data"],
        ANSWERS["deploy_source"],
    ]
)
FLAGS = [
    "--runtime",
    ANSWERS["runtime"],
    "--account",
    ANSWERS["account"],
    "--sources",
    ANSWERS["sources"],
    "--app-data",
    ANSWERS["app_data"],
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
    assert (flag_dir / CONFIG_FILENAME).read_text(encoding="utf-8") == (
        wiz_dir / CONFIG_FILENAME
    ).read_text(encoding="utf-8")


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
        "governance": {"sources": ["OLD_DB.MARTS"], "read_exceptions": ["OLD_DB.RAW.SANCTIONED"]},
        "snowflake": {"objects": {"stage_name": "MY_STAGE"}},
    }
    cfg = _prompt_config(prefill, Path("x"), dict(ANSWERS))
    assert cfg["runtime"] == "warehouse"
    assert cfg["governance"]["sources"] == ["ACME_ANALYTICS.MARTS", "ACME_ANALYTICS.REPORTING"]
    assert cfg["governance"]["read_exceptions"] == ["OLD_DB.RAW.SANCTIONED"]
    assert cfg["snowflake"]["objects"]["stage_name"] == "MY_STAGE"


def test_partial_flags_prompt_only_for_the_rest(monkeypatch):
    asked: list[str] = []

    def fake_prompt(text, default=None, **kwargs):
        asked.append(str(text))
        return default if default is not None else "ab12345.us-east-1"

    monkeypatch.setattr(typer, "prompt", fake_prompt)
    cfg = _prompt_config(
        None, Path("x"), {"runtime": "warehouse", "sources": "ACME_ANALYTICS.MARTS"}
    )
    assert len(asked) == 3
    assert not any(q.startswith(("Runtime", "Sources")) for q in asked)
    assert cfg["runtime"] == "warehouse"
    assert cfg["governance"]["sources"] == ["ACME_ANALYTICS.MARTS"]


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
    written = yaml.safe_load((tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
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
        (["--sources", " , "], "--sources"),
        (["--sources", "MARTS"], "DATABASE.SCHEMA"),
        (["--app-data", "REPORTING"], "--app-data"),
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
    before = (tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8")
    r = runner.invoke(
        app, ["init", "--dir", str(tmp_path), "--no-starter-app", "--sources", "OTHER_DB.MARTS"]
    )
    assert r.exit_code == 2, r.output
    assert "--reconfigure" in r.output
    assert (tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8") == before
    # With --reconfigure the flags apply (the last --sources wins).
    r = runner.invoke(
        app,
        [
            "init",
            "--dir",
            str(tmp_path),
            "--no-starter-app",
            "--reconfigure",
            *FLAGS,
            "--sources",
            "OTHER_DB.MARTS",
        ],
    )
    assert r.exit_code == 0, r.output
    after = yaml.safe_load((tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert after["governance"]["sources"] == ["OTHER_DB.MARTS"]
    assert after["governance"]["app_data"] == ANSWERS["app_data"]


def test_flags_and_config_import_are_exclusive(tmp_path, monkeypatch):
    example = Path(__file__).resolve().parent.parent / "streamsnow.config.example.yaml"
    _no_prompts(monkeypatch)
    for verb in ("init", "configure"):
        r = runner.invoke(
            app, [verb, "--dir", str(tmp_path / verb), "--config", str(example), *FLAGS]
        )
        assert r.exit_code == 2, r.output
        assert "--config" in r.output


@pytest.mark.parametrize("bad", ["", "  ", "not a locator!"])
def test_bad_account_flag_exits_2_without_prompting(tmp_path, monkeypatch, bad):
    _no_prompts(monkeypatch)
    flags = [f for f in FLAGS if f not in ("--account", ANSWERS["account"])]
    r = runner.invoke(app, ["configure", "--dir", str(tmp_path), *flags, "--account", bad])
    assert r.exit_code == 2, r.output
    assert "--account" in r.output
    assert not (tmp_path / CONFIG_FILENAME).exists()


def test_allowed_schema_conflicting_with_the_default_deny_list_exits_2(tmp_path, monkeypatch):
    # --sources alone still meets the RAW,STAGING default deny list.
    _no_prompts(monkeypatch)
    flags = [f for f in FLAGS if f not in ("--sources", ANSWERS["sources"])]
    r = runner.invoke(
        app,
        [
            "configure",
            "--dir",
            str(tmp_path),
            *flags,
            "--sources",
            "ACME_ANALYTICS.RAW,ACME_ANALYTICS.MARTS",
        ],
    )
    assert r.exit_code == 2, r.output
    assert "both allowed and denied" in r.output
    assert "RAW" in r.output
    assert not (tmp_path / CONFIG_FILENAME).exists()


def test_deny_flag_conflicting_with_the_prefilled_allow_list_exits_2(tmp_path, monkeypatch):
    _no_prompts(monkeypatch)
    assert runner.invoke(app, ["configure", "--dir", str(tmp_path), *FLAGS]).exit_code == 0
    before = (tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8")
    # --sources omitted: its prompt is answered with the prefilled default (Enter).
    monkeypatch.setattr(typer, "prompt", lambda text, default=None, **kw: default)
    r = runner.invoke(
        app,
        ["configure", "--dir", str(tmp_path), *FLAGS[:4], *FLAGS[6:], "--deny-schemas", "MARTS"],
    )
    assert r.exit_code == 2, r.output
    assert "both allowed and denied" in r.output
    assert (tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8") == before


def _v1_file(tmp_path: Path) -> Path:
    example = Path(__file__).resolve().parent.parent / "streamsnow.config.example.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    data["schema_version"] = 1
    data["governance"] = {
        "database": "ACME_ANALYTICS",
        "schema_allow": ["MARTS", "REPORTING"],
        "schema_deny": ["RAW"],
        "read_exceptions": ["ACME_ANALYTICS.RAW.CALENDAR_DIM"],
    }
    path = tmp_path / CONFIG_FILENAME
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_configure_rewrites_a_v1_file_as_v2(tmp_path, monkeypatch):
    _v1_file(tmp_path)
    monkeypatch.setattr(typer, "prompt", lambda text, default=None, **kw: default)
    r = runner.invoke(app, ["configure", "--dir", str(tmp_path)])
    assert r.exit_code == 0, r.output
    written = yaml.safe_load((tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
    gov = written["governance"]
    assert written["schema_version"] == 2
    assert "database" not in gov and "schema_allow" not in gov
    assert gov["sources"] == ["ACME_ANALYTICS.MARTS", "ACME_ANALYTICS.REPORTING"]
    assert gov["read_exceptions"] == ["ACME_ANALYTICS.RAW.CALENDAR_DIM"]
    Config.from_dict(written)


def test_init_on_a_v1_file_names_configure(tmp_path, monkeypatch):
    path = _v1_file(tmp_path)
    before = path.read_text(encoding="utf-8")
    _no_prompts(monkeypatch)
    r = runner.invoke(app, ["init", "--dir", str(tmp_path), "--no-starter-app"])
    assert r.exit_code == 2, r.output
    flat = " ".join(r.output.split())
    assert "schema_version 1" in flat and "streamsnow configure" in flat
    assert path.read_text(encoding="utf-8") == before


def test_setup_skill_probes_are_shell_safe():
    """`-q "<query>"` would let the shell eat the "name" quotes and expand $1."""
    text = (Path(__file__).resolve().parent.parent / "skills/onboard/setup.md").read_text(
        encoding="utf-8"
    )
    assert '-q "<query>"' not in text
    assert "-q '<query>'" in text
