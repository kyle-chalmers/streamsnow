"""`streamsnow objects-sql`: the app-data DDL the deploy job applies, or nothing (#79)."""

from __future__ import annotations

import sys

import pytest
from _app_data_fixtures import AD, dynamic_table, view, write_app, write_config
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
    lines = r.stderr.splitlines()
    findings = [ln for ln in lines if ln.startswith("objects-sql: apps/")]
    assert len(findings) >= 1
    assert lines[-1] == (
        f"objects-sql: {len(findings)} finding(s); printed no SQL, "
        "so the deploy stops before any change."
    )
    assert lines[:-1] == findings


def test_objects_sql_without_a_config_exits_2(tmp_path):
    r = _run(tmp_path / "missing.yaml")
    assert r.exit_code == 2
    assert r.stdout == ""


def test_objects_sql_with_only_advisories_prints_the_sql_and_nothing_on_stderr(tmp_path):
    """shared_logic with a single reader is an advisory: it never fails the verb and is
    never printed, so the deploy proceeds with a clean stderr."""
    cfg = write_config(tmp_path)
    write_app(
        tmp_path,
        "acme-sales",
        {
            "REGION_REVENUE": view(
                "REGION_REVENUE",
                "SELECT region, COUNT(*) AS n FROM ANALYTICS_DB.REPORTING.ORDERS GROUP BY region",
            )
        },
        reasons={"REGION_REVENUE": "shared_logic"},
    )
    r = _run(cfg)
    assert r.exit_code == 0, r.output
    assert f"CREATE OR REPLACE VIEW {AD}.REGION_REVENUE COPY GRANTS" in r.stdout
    assert r.stderr == ""


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_objects_sql_prints_nothing_for_a_symlinked_ddl_file(tmp_path):
    """The link's target name holds a newline and a DROP. Followed and rendered into the
    `-- ... from <path>` comment, it printed a live DROP VIEW (Codex review of #79)."""
    cfg = write_config(tmp_path)
    app = write_app(tmp_path, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    link = app / "sql_review" / "app_specific_reporting_objects" / f"{AD}.DAILY_REVENUE.sql"
    target = app / f"payload\nDROP VIEW {AD}.OTHER;--"
    target.write_text(link.read_text(encoding="utf-8"), encoding="utf-8")
    link.unlink()
    link.symlink_to(target)
    r = _run(cfg)
    assert r.exit_code == 1, r.output
    assert r.stdout == ""
    assert "symbolic link" in r.stderr
    assert "DROP VIEW" not in r.stderr
