"""`streamsnow objects-sql`: the app-data DDL the deploy job applies, or nothing (#79)."""

from __future__ import annotations

from _app_data_fixtures import AD, dynamic_table, write_app, write_config
from typer.testing import CliRunner

from streamsnow.cli import app

runner = CliRunner()


def _run(cfg):
    return runner.invoke(app, ["objects-sql", "--config", str(cfg)])


def test_objects_sql_prints_the_plan(tmp_path):
    cfg = write_config(tmp_path)
    write_app(tmp_path, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    r = _run(cfg)
    assert r.exit_code == 0, r.output
    assert r.stdout.count(f"CREATE OR ALTER DYNAMIC TABLE {AD}.DAILY_REVENUE") == 1
    assert r.stdout.rstrip().endswith(";")
    assert r.stderr == ""


def test_objects_sql_prints_nothing_without_objects(tmp_path):
    cfg = write_config(tmp_path)
    write_app(tmp_path, "acme-sales", {})
    r = _run(cfg)
    assert r.exit_code == 0, r.output
    assert r.stdout == ""


def test_objects_sql_prints_nothing_when_any_file_fails(tmp_path):
    """One good file and one whose CREATE names another object: no SQL at all, so
    the deploy stops before Snowflake sees the good one either."""
    cfg = write_config(tmp_path)
    write_app(tmp_path, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    # The copy-paste case: the file and its index entry say LEDGER_DAILY, the CREATE
    # still names DAILY_REVENUE.
    write_app(tmp_path, "acme-finance", {"LEDGER_DAILY": dynamic_table("DAILY_REVENUE")})
    r = _run(cfg)
    assert r.exit_code == 1
    assert r.stdout == ""
    assert "objects-sql: apps/acme-finance/sql_review/app_specific_reporting_objects/" in r.stderr
    assert "printed no SQL" in r.stderr


def test_objects_sql_without_a_config_exits_2(tmp_path):
    r = _run(tmp_path / "missing.yaml")
    assert r.exit_code == 2
    assert r.stdout == ""
