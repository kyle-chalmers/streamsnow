"""Tests for streamsnow.tools.sql_review — the automatic audit-trail generator.

The load-bearing properties, each pinned here:

- generate → check round-trips clean; any input drift (template, manifest,
  app module) or a hand-edited output reads as a named failure;
- ``check`` is IMPORT-FREE — a consumer module that explodes on import must
  never be executed by the gate (only ``generate`` with token_strategy
  'manifest' may import, on a dev machine);
- coverage is a hard gate: an unclaimed queries/*.sql is a finding;
- the statement-root allowlist refuses to write anything but
  SELECT/WITH/SHOW/DESCRIBE/EXPLAIN + SET session vars.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from streamsnow.tools import sql_review as sr

SLUG = "acme-sales-dashboard"

QUERY = """-- Query: revenue_daily
-- Feeds: Overview page (revenue trend)
-- Schemas: ANALYTICS_DB.REPORTING
-- Params: :1 start_date, :2 end_date
-- Tokens: REGION_FILTER
SELECT order_date, SUM(revenue) AS revenue
FROM ANALYTICS_DB.REPORTING.VW_REVENUE_DAILY
WHERE order_date BETWEEN :1 AND :2 {REGION_FILTER}
GROUP BY order_date
ORDER BY order_date
"""

MANIFEST = {
    "schema_version": 1,
    "feature": "revenue",
    "app": SLUG,
    "description": "Revenue pages",
    "token_strategy": "static",
    "token_dispatchers": {"REGION_FILTER": {"literal": "AND region = 'West'"}},
    "combos": [{"name": "all-default", "description": "West region"}],
    "pages": [{"name": "Overview", "queries": ["revenue_daily"]}],
    "query_specs": {"revenue_daily": {"params_doc": ":1 start_date, :2 end_date"}},
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    app = root / "apps" / SLUG
    (app / "queries").mkdir(parents=True)
    (app / "snowflake.yml").write_text("definition_version: 2\n")
    (app / "queries" / "revenue_daily.sql").write_text(QUERY)
    mdir = app / "sql_review" / "manifests"
    mdir.mkdir(parents=True)
    (mdir / "revenue.json").write_text(json.dumps(MANIFEST, indent=2))
    return root


def _review_file(repo: Path) -> Path:
    return repo / "apps" / SLUG / "sql_review" / "revenue.review.sql"


def _generate(repo: Path) -> int:
    return sr.main(["generate", SLUG, "--dir", str(repo)])


def _check(repo: Path) -> int:
    return sr.main(["check", SLUG, "--dir", str(repo)])


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_generate_renders_paste_runnable_sql(repo: Path, capsys: pytest.CaptureFixture) -> None:
    assert _generate(repo) == 0
    text = _review_file(repo).read_text()
    # Tokens and binds fully substituted — nothing that errors on paste.
    # (The Params: banner COMMENT documents the original :N slots on purpose;
    # only executable lines must be bind-free.)
    sql_lines = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))
    assert "{REGION_FILTER}" not in text
    assert "AND region = 'West'" in text
    assert ":1" not in sql_lines and ":2" not in sql_lines
    assert "$start_date" in sql_lines and "$end_date" in sql_lines
    # SET block present; one section per query with the page banner.
    assert "SET start_date" in text
    assert "[Page: Overview] revenue_daily.sql" in text
    # Provenance stamped.
    assert "-- Provenance: schema=1 inputs=" in text


def test_double_colon_cast_survives_bind_substitution(repo: Path) -> None:
    q = repo / "apps" / SLUG / "queries" / "revenue_daily.sql"
    q.write_text(QUERY.replace("SUM(revenue)", "SUM(revenue)::NUMBER(18,2)"))
    _generate(repo)
    assert "::NUMBER(18,2)" in _review_file(repo).read_text()


def test_single_combo_drops_suffix_multi_combo_keeps_it(repo: Path) -> None:
    _generate(repo)
    assert _review_file(repo).exists()
    manifest = dict(MANIFEST)
    manifest["combos"] = [
        {"name": "west", "description": "w"},
        {"name": "east", "description": "e"},
    ]
    mp = repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json"
    mp.write_text(json.dumps(manifest))
    _generate(repo)
    rd = repo / "apps" / SLUG / "sql_review"
    assert (rd / "revenue.west.review.sql").exists()
    assert (rd / "revenue.east.review.sql").exists()


def test_unresolved_token_is_an_error(repo: Path, capsys: pytest.CaptureFixture) -> None:
    manifest = dict(MANIFEST)
    manifest["token_dispatchers"] = {}
    mp = repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json"
    mp.write_text(json.dumps(manifest))
    assert _generate(repo) == 2
    assert "no dispatcher" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Read-only allowlist
# --------------------------------------------------------------------------- #


def test_write_statement_refused(repo: Path, capsys: pytest.CaptureFixture) -> None:
    q = repo / "apps" / SLUG / "queries" / "revenue_daily.sql"
    q.write_text(QUERY + ";\nDELETE FROM ANALYTICS_DB.REPORTING.VW_REVENUE_DAILY")
    code = _generate(repo)
    assert code == 2
    assert "not allowed" in capsys.readouterr().err
    assert not _review_file(repo).exists()  # nothing written


def test_sneaky_statement_roots_refused(repo: Path, capsys: pytest.CaptureFixture) -> None:
    # Allowlist, not a write-verb denylist: an unanticipated root must fail.
    q = repo / "apps" / SLUG / "queries" / "revenue_daily.sql"
    q.write_text(QUERY + ";\nCALL some_procedure()")
    assert _generate(repo) == 2


def test_set_session_variable_allowed_but_arbitrary_set_not(repo: Path) -> None:
    assert sr._verify_read_only("SET start_date = CURRENT_DATE;\nSELECT 1;") == []
    assert sr._verify_read_only("GRANT ROLE admin TO USER x;") != []


# --------------------------------------------------------------------------- #
# check: drift + hand-edit + coverage (all import-free)
# --------------------------------------------------------------------------- #


def test_round_trip_clean(repo: Path) -> None:
    _generate(repo)
    assert _check(repo) == 0


def test_template_edit_reads_as_drift(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    q = repo / "apps" / SLUG / "queries" / "revenue_daily.sql"
    q.write_text(QUERY.replace("SUM(revenue)", "AVG(revenue)"))
    assert _check(repo) == 1
    assert "DRIFT" in capsys.readouterr().out


def test_manifest_edit_reads_as_drift(repo: Path) -> None:
    _generate(repo)
    mp = repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json"
    manifest = dict(MANIFEST)
    manifest["token_dispatchers"] = {"REGION_FILTER": {"literal": "AND region = 'East'"}}
    mp.write_text(json.dumps(manifest))
    assert _check(repo) == 1


def test_hand_edited_review_file_reads_as_edited(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    f = _review_file(repo)
    f.write_text(f.read_text().replace("'West'", "'North'"))
    assert _check(repo) == 1
    assert "edited by hand" in capsys.readouterr().out


def test_regenerating_on_a_later_day_is_not_drift(repo: Path) -> None:
    import re as _re

    _generate(repo)
    f = _review_file(repo)
    # Simulate a file generated on a different date: only the volatile
    # Generated line differs. The normalized output hash must not change,
    # and check must stay clean — otherwise the gate is a daily false alarm.
    text = f.read_text()
    aged = _re.sub(
        r"Generated: \d{4}-\d{2}-\d{2} by streamsnow sql-review",
        "Generated: 2020-01-01 by streamsnow sql-review",
        text,
    )
    assert aged != text
    f.write_text(aged)
    assert _check(repo) == 0


def test_uncovered_query_is_a_hard_finding(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    (repo / "apps" / SLUG / "queries" / "orders_by_channel.sql").write_text(
        "-- Query: orders_by_channel\n-- Feeds: Channels page\nSELECT 1\n"
    )
    assert _check(repo) == 1
    assert "not claimed by any sql_review manifest" in capsys.readouterr().out


def test_missing_review_file_is_a_finding(repo: Path, capsys: pytest.CaptureFixture) -> None:
    assert _check(repo) == 1
    assert "review file missing" in capsys.readouterr().out


def test_check_never_imports_app_code(repo: Path) -> None:
    """The malicious-import regression: a data module that explodes on import
    sits in an app whose manifest uses token_strategy 'manifest'. check must
    pass/fail on hashes alone without ever executing it."""
    app = repo / "apps" / SLUG
    (app / "data.py").write_text("raise SystemExit('check imported consumer code!')\n")
    manifest = dict(MANIFEST)
    manifest["token_strategy"] = "manifest"
    manifest["modules"] = {"data": "data"}
    manifest["token_dispatchers"] = {"REGION_FILTER": {"literal": "AND region = 'West'"}}
    mp = app / "sql_review" / "manifests" / "revenue.json"
    mp.write_text(json.dumps(manifest))
    # No review file yet → finding; the assertion is that this RETURNS (1),
    # rather than dying on the module's SystemExit.
    assert _check(repo) == 1


def test_check_all_apps_when_no_slug(repo: Path) -> None:
    _generate(repo)
    assert sr.main(["check", "--dir", str(repo)]) == 0


# --------------------------------------------------------------------------- #
# manifest token strategy (generate-only import)
# --------------------------------------------------------------------------- #


def test_manifest_strategy_calls_app_token_producers(repo: Path) -> None:
    app = repo / "apps" / SLUG
    (app / "data.py").write_text(
        "def region_filter_sql(region):\n"
        "    return '' if region == 'All' else f\"AND region = '{region}'\"\n"
    )
    manifest = dict(MANIFEST)
    manifest["token_strategy"] = "manifest"
    manifest["modules"] = {"data": "data"}
    manifest["token_dispatchers"] = {
        "REGION_FILTER": {"call": "region_filter_sql", "args": ["@combo.region"]}
    }
    manifest["combos"] = [{"name": "west", "description": "West only", "region": "West"}]
    mp = app / "sql_review" / "manifests" / "revenue.json"
    mp.write_text(json.dumps(manifest))
    assert _generate(repo) == 0
    text = (app / "sql_review" / "revenue.review.sql").read_text()
    assert "AND region = 'West'" in text
    # And an app-module edit AFTER generation reads as drift, import-free.
    (app / "data.py").write_text(
        "def region_filter_sql(region):\n    return \"AND region = 'East'\"\n"
    )
    assert _check(repo) == 1


# --------------------------------------------------------------------------- #
# discover + index
# --------------------------------------------------------------------------- #


def test_discover_proposes_static_skeletons(repo: Path, capsys: pytest.CaptureFixture) -> None:
    (repo / "apps" / SLUG / "queries" / "orders_by_channel.sql").write_text(
        "-- Query: orders_by_channel\n-- Feeds: Channels page\n"
        "-- Tokens: CHANNEL_FILTER\n"
        "SELECT channel, COUNT(*) FROM ANALYTICS_DB.REPORTING.VW_ORDERS "
        "WHERE 1=1 {CHANNEL_FILTER} GROUP BY channel\n"
    )
    code = sr.main(["discover", SLUG, "--dir", str(repo), "--write"])
    assert code == 1  # gaps existed
    out = json.loads(capsys.readouterr().out)
    assert out["coverage"]["uncovered"] == ["orders_by_channel"]
    proposed = out["proposed_manifests"][0]
    assert proposed["token_strategy"] == "static"
    assert "CHANNEL_FILTER" in proposed["token_dispatchers"]
    written = repo / "apps" / SLUG / "sql_review" / "manifests" / "orders_by_channel.json"
    assert written.exists()


def test_index_builds_table_and_preserves_narrative(repo: Path) -> None:
    _generate(repo)
    readme = repo / "apps" / SLUG / "sql_review" / "README.md"
    sr.main(["index", SLUG, "--dir", str(repo)])
    text = readme.read_text()
    assert "| `revenue_daily` |" in text
    # Human narrative + verified column survive a rebuild.
    text = text.replace(
        "| `revenue_daily` | _(fill via /review-app --sql)_ | Overview | `revenue.review.sql` | no |",
        "| `revenue_daily` | ANALYTICS_DB.REPORTING.VW_REVENUE_DAILY | Overview | `revenue.review.sql` | 2026-08-31 |",
    )
    readme.write_text(text + "\nHand-written lineage narrative.\n")
    sr.main(["index", SLUG, "--dir", str(repo)])
    rebuilt = readme.read_text()
    assert "ANALYTICS_DB.REPORTING.VW_REVENUE_DAILY" in rebuilt
    assert "2026-08-31" in rebuilt
    assert "Hand-written lineage narrative." in rebuilt


# --------------------------------------------------------------------------- #
# Scaffold integration: the audit trail exists from commit 1
# --------------------------------------------------------------------------- #


def test_scaffolded_app_ships_manifest_and_companion(tmp_path: Path) -> None:

    from typer.testing import CliRunner

    from streamsnow.cli import app as cli_app

    repo_root = Path(__file__).resolve().parent.parent
    result = CliRunner().invoke(
        cli_app,
        [
            "init",
            "--config",
            str(repo_root / "streamsnow.config.example.yaml"),
            "--dir",
            str(tmp_path),
            "--app",
            "acme-sales-dashboard",
        ],
    )
    assert result.exit_code == 0, result.output
    app_dir = tmp_path / "apps" / "acme-sales-dashboard"
    manifest = app_dir / "sql_review" / "manifests" / "example_metric.json"
    companion = app_dir / "sql_review" / "example_metric.review.sql"
    assert manifest.is_file()
    assert companion.is_file()
    assert "-- Provenance:" in companion.read_text()
    # And the fresh scaffold passes its own gate.
    assert sr.main(["check", "acme-sales-dashboard", "--dir", str(tmp_path)]) == 0
