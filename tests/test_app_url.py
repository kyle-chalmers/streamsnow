"""Tests for ``streamsnow app-url``: the Snowsight link to a deployed app.

A fake ``snow`` runner answers the org/account query, so the tests pin the exact
URL, that the app name comes from the deploy's own FQN helper, that the named
connection reaches ``snow``, and the exit 2 paths (unknown slug, failed query).
No network and no ``snow`` binary.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from streamsnow import sf_exec
from streamsnow.app_url import app_url, snowsight_url
from streamsnow.cli import app
from streamsnow.config import load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"
URL = "https://app.snowflake.com/acme/acme_prod/#/streamlit-apps/STREAMSNOW_APPS.DASHBOARDS.ACME_SALES"


class FakeSnow:
    """Answers every session statement with one ok row, the last with org/account."""

    def __init__(self, row: dict | None = None, code: int = 0, err: str = "") -> None:
        self.row = row if row is not None else {"ORGANIZATION": "ACME", "ACCOUNT": "ACME_PROD"}
        self.code = code
        self.err = err
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
        self.calls.append((argv, stdin))
        stmts = [s for s in stdin.split("\n;\n") if s.strip()]
        results = [[{"status": "ok"}] for _ in stmts[:-1]] + [[self.row]]
        return self.code, json.dumps(results), self.err


def _repo(tmp_path: Path, slug: str = "acme-sales") -> Path:
    cfg = tmp_path / "streamsnow.config.yaml"
    cfg.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "apps" / slug).mkdir(parents=True)
    return cfg


def _invoke(monkeypatch, fake: FakeSnow, *args: str):
    monkeypatch.setattr(sf_exec, "_default_runner", fake)
    return CliRunner().invoke(app, ["app-url", *args])


def test_snowsight_url_is_the_documented_app_builder_form():
    assert snowsight_url("ACME", "ACME_PROD", "STREAMSNOW_APPS.DASHBOARDS.ACME_SALES") == URL


def test_app_url_uses_the_deploy_fqn(tmp_path):
    cfg = load_config(_repo(tmp_path))
    result = app_url(
        cfg, "acme-sales", lambda sql: [{"ORGANIZATION": "ACME", "ACCOUNT": "ACME_PROD"}]
    )
    assert result == {
        "app": "acme-sales",
        "fqn": "STREAMSNOW_APPS.DASHBOARDS.ACME_SALES",
        "url": URL,
    }


def test_app_url_upper_cases_a_lower_case_database_and_schema(tmp_path):
    """The deploy SQL names the database and schema unquoted, so Snowflake stores
    `analytics` as `ANALYTICS`; the URL must name the object as stored."""
    cfg_path = _repo(tmp_path)
    text = cfg_path.read_text(encoding="utf-8")
    text = text.replace('"STREAMSNOW_APPS"', '"acme_apps"').replace('"DASHBOARDS"', '"reporting"')
    cfg_path.write_text(text, encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.snowflake.objects.app_database == "acme_apps"
    result = app_url(
        cfg, "acme-sales", lambda sql: [{"ORGANIZATION": "ACME", "ACCOUNT": "ACME_PROD"}]
    )
    assert result["fqn"] == "ACME_APPS.REPORTING.ACME_SALES"
    assert result["url"] == (
        "https://app.snowflake.com/acme/acme_prod/#/streamlit-apps/ACME_APPS.REPORTING.ACME_SALES"
    )


def test_cli_prints_the_exact_url(tmp_path, monkeypatch):
    cfg = _repo(tmp_path)
    fake = FakeSnow()
    result = _invoke(monkeypatch, fake, "acme-sales", "--config", str(cfg))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == URL
    argv, stdin = fake.calls[0]
    assert "--connection=acme" in argv  # snowflake.connection_name from the config
    assert "CURRENT_ORGANIZATION_NAME()" in stdin and "CURRENT_ACCOUNT_NAME()" in stdin


def test_cli_connection_flag_overrides_the_config(tmp_path, monkeypatch):
    cfg = _repo(tmp_path)
    fake = FakeSnow()
    result = _invoke(monkeypatch, fake, "acme-sales", "--config", str(cfg), "--connection", "other")
    assert result.exit_code == 0, result.output
    assert "--connection=other" in fake.calls[0][0]


def test_cli_json_format(tmp_path, monkeypatch):
    cfg = _repo(tmp_path)
    result = _invoke(
        monkeypatch, FakeSnow(), "acme-sales", "--config", str(cfg), "--format", "json"
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["url"] == URL


def test_cli_unknown_slug_exits_2_without_calling_snow(tmp_path, monkeypatch):
    cfg = _repo(tmp_path)
    fake = FakeSnow()
    result = _invoke(monkeypatch, fake, "acme-nope", "--config", str(cfg))
    assert result.exit_code == 2
    assert "acme-nope" in result.output
    assert not fake.calls


def test_cli_invalid_slug_exits_2(tmp_path, monkeypatch):
    cfg = _repo(tmp_path)
    result = _invoke(monkeypatch, FakeSnow(), "Acme_Sales", "--config", str(cfg))
    assert result.exit_code == 2


def test_cli_query_failure_exits_2(tmp_path, monkeypatch):
    cfg = _repo(tmp_path)
    fake = FakeSnow(code=1, err="Connection acme is not configured")
    result = _invoke(monkeypatch, fake, "acme-sales", "--config", str(cfg))
    assert result.exit_code == 2
    assert "app.snowflake.com" not in result.output


def test_cli_empty_org_exits_2(tmp_path, monkeypatch):
    cfg = _repo(tmp_path)
    fake = FakeSnow(row={"ORGANIZATION": None, "ACCOUNT": "ACME_PROD"})
    result = _invoke(monkeypatch, fake, "acme-sales", "--config", str(cfg))
    assert result.exit_code == 2


def test_cli_missing_config_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _invoke(monkeypatch, FakeSnow(), "acme-sales", "--config", str(tmp_path / "nope.yaml"))
    assert result.exit_code == 2
