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


# --------------------------------------------------------------------------- #
# Phase 2 external-review regressions (Codex pass)
# --------------------------------------------------------------------------- #


def test_with_cte_prefixed_write_refused() -> None:
    # A CTE prefix proves nothing about the terminal statement.
    assert sr._verify_read_only("WITH x AS (SELECT 1) DELETE FROM t;") != []
    assert sr._verify_read_only("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x;") != []
    # Legitimate shapes stay allowed: single, multi, column-list, nested parens.
    assert sr._verify_read_only("WITH x AS (SELECT 1) SELECT * FROM x;") == []
    assert (
        sr._verify_read_only(
            "WITH a (c) AS (SELECT 1), b AS (SELECT MAX(c) FROM (SELECT c FROM a)) SELECT * FROM b;"
        )
        == []
    )


def test_content_after_provenance_is_a_finding(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    f = _review_file(repo)
    f.write_text(f.read_text() + "DELETE FROM ANALYTICS_DB.REPORTING.VW_REVENUE_DAILY;\n")
    assert _check(repo) == 1
    assert "content after the provenance line" in capsys.readouterr().out


def test_second_provenance_line_is_a_finding(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    f = _review_file(repo)
    text = f.read_text()
    prov = [ln for ln in text.splitlines() if ln.startswith("-- Provenance: ")][0]
    f.write_text(text.replace("SET start_date", f"{prov}\nSET start_date"))
    assert _check(repo) == 1
    assert "multiple provenance lines" in capsys.readouterr().out


def test_crlf_conversion_reads_as_edit(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    f = _review_file(repo)
    f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))
    assert _check(repo) == 1


def test_transitive_module_edit_reads_as_drift(repo: Path) -> None:
    # data.py delegates to helper.py; only helper.py changes after generation.
    app = repo / "apps" / SLUG
    (app / "helper.py").write_text("def frag(region):\n    return f\"AND region = '{region}'\"\n")
    (app / "data.py").write_text(
        "from helper import frag\n\ndef region_filter_sql(region):\n    return frag(region)\n"
    )
    manifest = dict(MANIFEST)
    manifest["token_strategy"] = "manifest"
    manifest["modules"] = {"data": "data"}
    manifest["token_dispatchers"] = {
        "REGION_FILTER": {"call": "region_filter_sql", "args": ["West"]}
    }
    mp = app / "sql_review" / "manifests" / "revenue.json"
    mp.write_text(json.dumps(manifest))
    assert _generate(repo) == 0
    assert _check(repo) == 0
    (app / "helper.py").write_text("def frag(region):\n    return \"AND region = 'East'\"\n")
    assert _check(repo) == 1


def test_traversal_combo_name_rejected(repo: Path, capsys: pytest.CaptureFixture) -> None:
    manifest = dict(MANIFEST)
    manifest["combos"] = [
        {"name": "ok", "description": "x"},
        {"name": "../../../evil", "description": "y"},
    ]
    mp = repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json"
    mp.write_text(json.dumps(manifest))
    assert _generate(repo) == 2
    assert "combo name" in capsys.readouterr().err


def test_traversal_source_query_rejected(repo: Path, capsys: pytest.CaptureFixture) -> None:
    manifest = dict(MANIFEST)
    manifest["query_specs"] = {"revenue_daily": {"source_query": "../../secret"}}
    mp = repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json"
    mp.write_text(json.dumps(manifest))
    assert _generate(repo) == 2
    assert "bare query name" in capsys.readouterr().err


def test_orphaned_review_file_is_a_finding_and_generate_cleans_it(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    manifest = dict(MANIFEST)
    manifest["combos"] = [
        {"name": "west", "description": "w"},
        {"name": "east", "description": "e"},
    ]
    mp = repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json"
    mp.write_text(json.dumps(manifest))
    _generate(repo)
    # Drop the east combo: check flags the orphan; regenerate removes it.
    mp.write_text(json.dumps({**manifest, "combos": [{"name": "west", "description": "w"}]}))
    assert _check(repo) == 1
    assert "orphaned review file" in capsys.readouterr().out
    _generate(repo)
    assert not (repo / "apps" / SLUG / "sql_review" / "revenue.east.review.sql").exists()
    assert _check(repo) == 0


def test_unreadable_review_file_is_a_finding_not_a_traceback(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    _generate(repo)
    _review_file(repo).write_bytes(b"\xff\xfe invalid utf8 \xff")
    assert _check(repo) == 1
    assert "unreadable review file" in capsys.readouterr().out


def test_hand_added_write_statement_fails_even_with_forged_hashes(repo: Path) -> None:
    """Even if someone recomputes both digests over an edited file, the
    check-time allowlist re-verification refuses a write statement."""
    _generate(repo)
    f = _review_file(repo)
    text = f.read_text()
    body, _, _ = text.rpartition("-- Provenance:")
    evil_body = body + "DELETE FROM ANALYTICS_DB.REPORTING.VW_REVENUE_DAILY;\n"
    forged_output = sr._output_digest(evil_body + sr._FINAL_PROVENANCE_PLACEHOLDER + "\n")
    prov_line = [ln for ln in text.splitlines() if ln.startswith("-- Provenance: ")][0]
    forged_prov = prov_line[: prov_line.rfind("output=")] + f"output={forged_output}"
    f.write_text(evil_body + forged_prov + "\n")
    assert _check(repo) == 1


# --------------------------------------------------------------------------- #
# Phase 2 second-reviewer regressions
# --------------------------------------------------------------------------- #


def test_string_literal_paren_cannot_smuggle_a_cte_write() -> None:
    """The confirmed bypass: a literal containing ')SELECT' collapsed a raw
    paren counter and the DELETE read as a SELECT. Masked scanning closes it."""
    evil = "WITH x AS (SELECT ')SELECT' AS s FROM t) DELETE FROM x;"
    assert sr._verify_read_only(evil) != []
    # And the equivalent through generate: nothing is written.


def test_semicolon_inside_string_literal_is_legit(repo: Path) -> None:
    """Over-rejection fix: a ; or verb inside a literal must not split the
    statement into a fragment with a disallowed root."""
    ok = "SELECT 'a; DROP TABLE x' AS s FROM ANALYTICS_DB.REPORTING.VW_REVENUE_DAILY;"
    assert sr._verify_read_only(ok) == []
    q = repo / "apps" / SLUG / "queries" / "revenue_daily.sql"
    q.write_text(QUERY.replace("SUM(revenue)", "'a; DROP' || SUM(revenue)"))
    assert _generate(repo) == 0


def test_semicolon_in_block_comment_is_legit() -> None:
    assert sr._verify_read_only("SELECT 1 /* note; DROP TABLE x */ FROM t;") == []


def test_escaped_quotes_handled() -> None:
    assert sr._verify_read_only("SELECT 'it''s; fine' FROM t;") == []


def test_unterminated_literal_fails_closed() -> None:
    # Masking consumes to end; a WITH that can't be parsed is not allowed.
    assert sr._verify_read_only("WITH x AS (SELECT 'unterminated) DELETE FROM t;") != []


def test_manifest_feature_collision_is_detected(repo: Path, capsys: pytest.CaptureFixture) -> None:
    # Second manifest with the same feature: generate refuses, check flags.
    mdir = repo / "apps" / SLUG / "sql_review" / "manifests"
    (mdir / "revenue2.json").write_text(json.dumps(MANIFEST))
    assert _generate(repo) == 2
    assert "output collision" in capsys.readouterr().err
    assert _check(repo) == 1
    assert "output collision" in capsys.readouterr().out


def test_index_duplicate_markers_hard_error(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    readme = repo / "apps" / SLUG / "sql_review" / "README.md"
    sr.main(["index", SLUG, "--dir", str(repo)])
    text = readme.read_text()
    # A second (stray, reversed) marker pair in the narrative must hard-error,
    # never silently splice the wrong region.
    readme.write_text(f"{sr._README_TABLE_END}\n\nsomeone quoting the marker syntax\n\n{text}")
    code = sr.main(["index", SLUG, "--dir", str(repo)])
    assert code == 2
    assert "index markers" in capsys.readouterr().err


def test_index_unrelated_table_cannot_clobber_signoff(repo: Path) -> None:
    _generate(repo)
    readme = repo / "apps" / SLUG / "sql_review" / "README.md"
    sr.main(["index", SLUG, "--dir", str(repo)])
    text = readme.read_text()
    # Reviewer signs off inside the marked block…
    text = text.replace(
        "| `revenue_daily` | _(fill via /review-app --sql)_ | Overview | `revenue.review.sql` | no |",
        "| `revenue_daily` | ANALYTICS_DB.REPORTING.VW_REVENUE_DAILY | Overview | `revenue.review.sql` | yes |",
    )
    # …and an unrelated illustrative 5-column table appears in the narrative
    # BELOW the block, first cell colliding with the query name.
    text += "\n\nNarrative example (not the index):\n\n| `revenue_daily` | a | b | c | table |\n"
    readme.write_text(text)
    assert sr.main(["index", SLUG, "--dir", str(repo)]) == 0
    rebuilt = readme.read_text()
    # The sign-off survives; the narrative's bogus 'table' value did not win.
    assert "| yes |" in rebuilt.split(sr._README_TABLE_END)[0]
    assert "Narrative example (not the index):" in rebuilt


# --------------------------------------------------------------------------- #
# Metrics mode (0.6.1): per-visual authored blocks
# --------------------------------------------------------------------------- #

METRICS_MANIFEST = {
    "schema_version": 1,
    "feature": "overview_metrics",
    "app": SLUG,
    "mode": "metrics",
    "description": "Per-visual review blocks for the Overview page",
    "metrics": [
        {
            "name": "avg_daily_revenue",
            "page": "Overview",
            "title": "Avg daily revenue",
            "source": "sql_review/_metrics/avg_daily_revenue.sql",
            "params_doc": ":1 start_date, :2 end_date",
        },
        {
            "name": "revenue_trend",
            "page": "Overview",
            "title": "Revenue trend",
            "source": "queries/revenue_daily.sql",
            "notes": "1:1 with the app query",
        },
    ],
}


@pytest.fixture()
def metrics_repo(repo: Path) -> Path:
    app = repo / "apps" / SLUG
    mdir = app / "sql_review" / "_metrics"
    mdir.mkdir(parents=True)
    (mdir / "avg_daily_revenue.sql").write_text(
        "-- Query: avg_daily_revenue\n-- Feeds: Overview KPI\n"
        "SELECT AVG(revenue) FROM ANALYTICS_DB.REPORTING.VW_REVENUE_DAILY\n"
        "WHERE order_date BETWEEN :1 AND :2\n"
    )
    # The token-mode manifest from `repo` also claims revenue_daily; replace it
    # with the metrics manifest to keep this fixture single-manifest.
    (app / "sql_review" / "manifests" / "revenue.json").unlink()
    (app / "sql_review" / "manifests" / "overview_metrics.json").write_text(
        json.dumps(METRICS_MANIFEST, indent=2)
    )
    return repo


def test_metrics_mode_renders_per_visual_blocks(metrics_repo: Path) -> None:
    assert sr.main(["generate", SLUG, "--dir", str(metrics_repo)]) == 0
    out = metrics_repo / "apps" / SLUG / "sql_review" / "overview_metrics.review.sql"
    text = out.read_text()
    assert "DASHBOARD MAP" in text
    assert "-- avg_daily_revenue" in text and "-- revenue_trend" in text
    assert "[Overview] Avg daily revenue" in text
    sql_lines = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))
    assert ":1" not in sql_lines and "$start_date" in sql_lines
    assert "-- Provenance: schema=1" in text
    # Round-trips clean, incl. coverage: the queries/-sourced metric claims it.
    assert _check(metrics_repo) == 0


def test_metrics_source_edit_reads_as_drift(metrics_repo: Path) -> None:
    sr.main(["generate", SLUG, "--dir", str(metrics_repo)])
    src = metrics_repo / "apps" / SLUG / "sql_review" / "_metrics" / "avg_daily_revenue.sql"
    src.write_text(src.read_text().replace("AVG(revenue)", "MEDIAN(revenue)"))
    assert _check(metrics_repo) == 1


def test_metrics_traversal_source_rejected(metrics_repo: Path, capsys) -> None:
    manifest = json.loads(json.dumps(METRICS_MANIFEST))
    manifest["metrics"][0]["source"] = "../../secrets.toml"
    mp = metrics_repo / "apps" / SLUG / "sql_review" / "manifests" / "overview_metrics.json"
    mp.write_text(json.dumps(manifest))
    assert sr.main(["generate", SLUG, "--dir", str(metrics_repo)]) == 2
    assert "app-relative path under" in capsys.readouterr().err


def test_metrics_write_statement_refused(metrics_repo: Path, capsys) -> None:
    src = metrics_repo / "apps" / SLUG / "sql_review" / "_metrics" / "avg_daily_revenue.sql"
    src.write_text(src.read_text() + ";\nTRUNCATE TABLE ANALYTICS_DB.REPORTING.VW_REVENUE_DAILY")
    assert sr.main(["generate", SLUG, "--dir", str(metrics_repo)]) == 2
    assert "not allowed" in capsys.readouterr().err


def test_metrics_index_rows(metrics_repo: Path) -> None:
    sr.main(["generate", SLUG, "--dir", str(metrics_repo)])
    sr.main(["index", SLUG, "--dir", str(metrics_repo)])
    text = (metrics_repo / "apps" / SLUG / "sql_review" / "README.md").read_text()
    assert "| `avg_daily_revenue` |" in text
    assert "Overview > Revenue trend" in text


def test_metrics_symlink_source_refused(metrics_repo: Path, capsys) -> None:
    """The manifest regex constrains the lexical path; the resolver must
    refuse a symlink that would pull an out-of-app file into the render."""
    import os as _os

    app = metrics_repo / "apps" / SLUG
    outside = metrics_repo / "outside.sql"
    outside.write_text("SELECT 1\n")
    target = app / "sql_review" / "_metrics" / "avg_daily_revenue.sql"
    target.unlink()
    _os.symlink(outside, target)
    assert sr.main(["generate", SLUG, "--dir", str(metrics_repo)]) == 2
    assert "symlink" in capsys.readouterr().err
    # And check treats the symlinked source as drift, never a trusted read.
    assert _check(metrics_repo) == 1


# --------------------------------------------------------------------------- #
# SET-block honesty (regression: a header promising an editable review window
# over variables no section references makes a reviewer edit the window, rerun,
# get byte-identical numbers, and sign off believing the window was applied.)
# --------------------------------------------------------------------------- #


_SELF_ANCHORED = """-- Query: revenue_daily
-- Feeds: Overview page — daily revenue
-- Schemas: ANALYTICS.ORDERS
SELECT order_date, SUM(revenue) AS revenue
FROM ANALYTICS.ORDERS
WHERE order_date >= DATEADD('day', -7, CURRENT_DATE)
GROUP BY 1
"""


def test_no_set_block_when_queries_self_anchor(repo: Path) -> None:
    """A query taking no date binds must not get a SET block it ignores."""
    (repo / "apps" / SLUG / "queries" / "revenue_daily.sql").write_text(_SELF_ANCHORED)
    manifest = dict(MANIFEST)
    manifest["query_specs"] = {"revenue_daily": {"params_doc": "(none)"}}
    manifest["token_dispatchers"] = {}
    mdir = repo / "apps" / SLUG / "sql_review" / "manifests"
    (mdir / "revenue.json").write_text(json.dumps(manifest, indent=2))

    assert _generate(repo) == 0
    text = _review_file(repo).read_text()
    assert "SET start_date" not in text, "emitted a SET line nothing references"
    assert "SET end_date" not in text
    # And the header must not promise an editable window that does not exist.
    assert "edit the SET lines once to apply new values" not in text
    assert "No SET block" in text
    assert "bounds its own range" in text


def test_set_block_pruned_to_referenced_vars_only(repo: Path) -> None:
    """Half-used SET blocks emit only the half that is actually referenced."""
    q = (repo / "apps" / SLUG / "queries" / "revenue_daily.sql").read_text()
    # Drop the :2 (end_date) bind; keep :1.
    (repo / "apps" / SLUG / "queries" / "revenue_daily.sql").write_text(
        q.replace("AND order_date <= :2", "").replace(":2", ":1")
    )
    assert _generate(repo) == 0
    text = _review_file(repo).read_text()
    assert "SET start_date" in text
    assert "SET end_date" not in text, "end_date is unreferenced but was emitted"
    # A surviving variable means the editable-window promise is still accurate.
    assert "edit the SET lines once to apply new values" in text


def test_var_used_is_word_boundary_anchored() -> None:
    """`$start_date_cutoff` must not count as a use of `start_date`."""
    assert sr._var_used("start_date", "WHERE d >= $start_date")
    assert not sr._var_used("start_date", "WHERE d >= $start_date_cutoff")
    assert not sr._var_used("start_date", "WHERE d >= $startdate")


def test_var_used_is_case_insensitive_like_snowflake_identifiers() -> None:
    """`$START_DATE` is the SAME variable as `$start_date`.

    Matching case-sensitively would prune a SET line that is actually used,
    leaving a dangling `$START_DATE` that errors on paste — worse than the
    unused SET line the pruning exists to remove.
    """
    assert sr._var_used("start_date", "WHERE d >= $START_DATE")
    assert sr._var_used("start_date", "WHERE d >= $Start_Date")


def test_uppercase_reference_keeps_its_set_line(repo: Path) -> None:
    """End-to-end: an uppercase reference must not lose its SET line."""
    q = (repo / "apps" / SLUG / "queries" / "revenue_daily.sql").read_text()
    (repo / "apps" / SLUG / "queries" / "revenue_daily.sql").write_text(
        q.replace("BETWEEN :1 AND :2", "BETWEEN :1 AND :2").replace(
            "WHERE order_date", "WHERE order_date"
        )
    )
    assert _generate(repo) == 0
    rf = _review_file(repo)
    # Force the emitted body to reference the variable in upper case, as a
    # hand-authored metrics source legitimately might.
    rf.write_text(rf.read_text().replace("$start_date", "$START_DATE"))
    text = rf.read_text()
    body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("SET "))
    assert sr._var_used("start_date", body), "uppercase use went undetected"


def test_metrics_mode_also_prunes_unused_set_block(tmp_path: Path) -> None:
    """Metrics mode shares the pruning path — and keeps its dashboard map."""
    root = tmp_path / "repo"
    app = root / "apps" / SLUG
    (app / "queries").mkdir(parents=True)
    (app / "snowflake.yml").write_text("definition_version: 2\n")
    metrics_dir = app / "sql_review" / "_metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "revenue_card.sql").write_text(_SELF_ANCHORED)
    mdir = app / "sql_review" / "manifests"
    mdir.mkdir(parents=True)
    (mdir / "revenue.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "feature": "revenue",
                "app": SLUG,
                "mode": "metrics",
                "metrics": [
                    {
                        "name": "revenue_card",
                        "page": "Overview",
                        "title": "Revenue (7d)",
                        "source": "sql_review/_metrics/revenue_card.sql",
                    }
                ],
            },
            indent=2,
        )
    )
    assert sr.main(["generate", SLUG, "--dir", str(root)]) == 0
    text = (app / "sql_review" / "revenue.review.sql").read_text()
    assert "SET start_date" not in text
    assert "No SET block" in text
    assert "DASHBOARD MAP (in on-screen order)" in text
    assert "Overview > Revenue (7d)" in text


# --------------------------------------------------------------------------- #
# Read-only guard — quote-aware masking. Two bypasses of the same shape have
# now been found, so both quote styles are pinned with cases in BOTH
# directions: a bypass must be rejected, and legitimate quoting must not be.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql",
    [
        # Double-quoted DELIMITED IDENTIFIER hiding a `)` — Snowflake treats
        # "..." as an identifier, not a string, so it was initially unmasked:
        # the `)` closed the CTE scan early and the trailing SELECT read as the
        # terminal verb while Snowflake executed the DELETE.
        'WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM target;',
        # Single-quoted literal, the original bypass.
        "WITH x AS (SELECT ')SELECT' AS s FROM t) DELETE FROM x;",
        # Block comment hiding the same trick.
        "WITH x AS (SELECT 1 /* ) SELECT */ ) DELETE FROM t;",
    ],
)
def test_read_only_guard_rejects_quote_hidden_writes(sql: str) -> None:
    assert sr._verify_read_only(sql), f"guard accepted a write statement: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "weird col name" FROM ANALYTICS.ORDERS;',
        'SELECT 1 AS "quoted "" escaped" FROM ANALYTICS.ORDERS;',
        "WITH x AS (SELECT 1) SELECT * FROM x;",
        "SELECT 'a string with ) parens' FROM t;",
    ],
)
def test_read_only_guard_allows_legitimate_quoting(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"guard rejected valid read-only SQL: {sql}"


def test_generate_refuses_to_write_a_quote_hidden_write(repo: Path) -> None:
    """End-to-end: the guard runs before the file is written, not after."""
    q = repo / "apps" / SLUG / "queries" / "revenue_daily.sql"
    q.write_text(
        "-- Query: revenue_daily\n"
        "-- Feeds: Overview\n"
        "-- Schemas: ANALYTICS.ORDERS\n"
        'WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM ANALYTICS.ORDERS\n'
    )
    assert _generate(repo) != 0
    assert not _review_file(repo).exists(), "wrote a file containing a write statement"


# --------------------------------------------------------------------------- #
# Unbound binds. Provenance hashes prove a file matches its inputs; they do not
# prove it RUNS. A manifest declaring only :1/:2 for a query using :3 rendered
# seven live `AND col <= :3` predicates that passed every existing gate.
# --------------------------------------------------------------------------- #


def _query_with_third_bind() -> str:
    return (
        "-- Query: revenue_daily\n"
        "-- Feeds: Overview page — daily revenue\n"
        "-- Schemas: ANALYTICS.ORDERS\n"
        "-- Params: :1 start_date, :2 end_date, :3 cutoff_date\n"
        "SELECT order_date, SUM(revenue) AS revenue\n"
        "FROM ANALYTICS.ORDERS\n"
        "WHERE order_date BETWEEN :1 AND :2\n"
        "  AND load_date <= :3\n"
        "GROUP BY 1\n"
    )


def test_generate_refuses_unsubstituted_bind(repo: Path, capsys: pytest.CaptureFixture) -> None:
    """An undeclared :3 must fail generation, not ship an unrunnable file."""
    (repo / "apps" / SLUG / "queries" / "revenue_daily.sql").write_text(_query_with_third_bind())
    manifest = dict(MANIFEST)
    manifest["token_dispatchers"] = {}
    (repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json").write_text(
        json.dumps(manifest, indent=2)
    )
    assert _generate(repo) != 0
    assert "unsubstituted bind :3" in capsys.readouterr().err
    assert not _review_file(repo).exists()


def test_declaring_the_bind_makes_generation_succeed(repo: Path) -> None:
    """The remedy the error names must actually work."""
    (repo / "apps" / SLUG / "queries" / "revenue_daily.sql").write_text(_query_with_third_bind())
    manifest = dict(MANIFEST)
    manifest["token_dispatchers"] = {}
    manifest["set_block"] = {
        "start_date": "DATEADD('year', -1, CURRENT_DATE)",
        "end_date": "CURRENT_DATE",
        "cutoff_date": "CURRENT_DATE",
    }
    manifest["param_bindings"] = {"1": "$start_date", "2": "$end_date", "3": "$cutoff_date"}
    (repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json").write_text(
        json.dumps(manifest, indent=2)
    )
    assert _generate(repo) == 0
    text = _review_file(repo).read_text()
    assert "$cutoff_date" in text
    sql_lines = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("--"))
    assert ":3" not in sql_lines


def test_check_flags_a_committed_file_with_an_unbound_bind(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """check is import-free but must still audit the committed bytes."""
    assert _generate(repo) == 0
    rf = _review_file(repo)
    text = rf.read_text()
    # Simulate a hand-edit / a file generated before the guard existed, keeping
    # provenance intact so ONLY the byte-level audit can catch it.
    rf.write_text(text.replace("$end_date", ":3"))
    assert _check(repo) != 0
    assert "unsubstituted bind :3" in capsys.readouterr().out


def test_params_banner_comment_is_not_mistaken_for_an_unbound_bind(repo: Path) -> None:
    """`Params: :1 start_date` documentation lines must stay exempt."""
    assert _generate(repo) == 0
    text = _review_file(repo).read_text()
    assert "Params: :1 start_date" in text  # the banner survives
    assert _check(repo) == 0  # and does not trip the audit


# --------------------------------------------------------------------------- #
# CTE fragments. A shared-CTE file is inlined via a token and is not
# independently runnable, so it can never be "claimed" — yet it counted as an
# uncovered gap, making the coverage gate unsatisfiable for any repo that
# factors CTEs into their own files.
# --------------------------------------------------------------------------- #


def _add_fragment(repo: Path, declare: bool, *, reason: str = "inlined as {REGION_CTES}") -> None:
    (repo / "apps" / SLUG / "queries" / "_region_ctes.sql").write_text(
        "-- Query: _region_ctes\n"
        "-- Feeds: (fragment — inlined into other queries)\n"
        "-- Schemas: ANALYTICS.ORDERS\n"
        "SELECT 1 AS region\n"
    )
    manifest = dict(MANIFEST)
    if declare:
        manifest["fragments"] = [{"file": "_region_ctes.sql", "reason": reason}]
    (repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json").write_text(
        json.dumps(manifest, indent=2)
    )


def test_undeclared_fragment_still_fails_coverage(repo: Path) -> None:
    """Exemption must be explicit — a filename convention would be a hole."""
    _add_fragment(repo, declare=False)
    _generate(repo)
    assert "_region_ctes" in sr.coverage(repo / "apps" / SLUG)["uncovered"]
    assert _check(repo) != 0


def test_declared_fragment_is_exempt_from_coverage(repo: Path) -> None:
    _add_fragment(repo, declare=True)
    assert _generate(repo) == 0
    cov = sr.coverage(repo / "apps" / SLUG)
    assert "_region_ctes" not in cov["uncovered"]
    assert cov["fragments"] == ["_region_ctes"]
    assert _check(repo) == 0


def test_declared_fragment_reason_reaches_the_index(repo: Path) -> None:
    """The reason is the knowledge a naming convention would have lost."""
    _add_fragment(repo, declare=True, reason="produces REGION_CASE; inlined, never joined")
    _generate(repo)
    assert sr.main(["index", SLUG, "--dir", str(repo)]) == 0
    readme = (repo / "apps" / SLUG / "sql_review" / "README.md").read_text()
    assert "produces REGION_CASE; inlined, never joined" in readme
    assert "_region_ctes" in readme


def test_stale_fragment_declaration_is_a_finding(repo: Path, capsys: pytest.CaptureFixture) -> None:
    """A deleted fragment must not keep its exemption silently."""
    _add_fragment(repo, declare=True)
    _generate(repo)
    (repo / "apps" / SLUG / "queries" / "_region_ctes.sql").unlink()
    assert _check(repo) != 0
    assert "does not exist" in capsys.readouterr().out


def test_set_block_note_renders_above_the_set_lines(repo: Path) -> None:
    """Rationale for the defaults renders inline, not only in the manifest."""
    manifest = dict(MANIFEST)
    manifest["set_block_note"] = (
        "Bounds derive from this page's OWN freshness source, not the calendar: "
        "capping on another source asks for a day this view has no rows for."
    )
    (repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json").write_text(
        json.dumps(manifest, indent=2)
    )
    assert _generate(repo) == 0
    text = _review_file(repo).read_text()
    assert "Bounds derive from this page's OWN freshness source" in text
    note_at = text.index("Bounds derive")
    set_at = text.index("SET start_date")
    assert note_at < set_at, "note must precede the SET lines it explains"


# --------------------------------------------------------------------------- #
# Masking: every Snowflake quoting form. Four bypasses of the same shape have
# now been found (single-quote, double-quote, dollar-quote, backslash escape),
# so each is pinned in BOTH directions.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("WITH x AS (SELECT $$ ) SELECT y $$) DELETE FROM t;", "dollar-quoted constant"),
        ("WITH x AS (SELECT '\\') SELECT y') DELETE FROM t;", "backslash-escaped quote"),
        ('WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM t;', "delimited identifier"),
        ("WITH x AS (SELECT ')SELECT' AS s FROM t) DELETE FROM x;", "string literal"),
    ],
)
def test_every_quoting_form_is_masked_for_structure(sql: str, why: str) -> None:
    assert sr._verify_read_only(sql), f"bypass via {why}"


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("WITH x AS (SELECT $$ ) SELECT y $$) DELETE FROM t;", "dollar-quoted constant"),
        ("WITH x AS (SELECT '\\') SELECT y') DELETE FROM t;", "backslash-escaped quote"),
        ('WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM t;', "delimited identifier"),
        ("WITH x AS (SELECT ')SELECT' AS s FROM t) DELETE FROM x;", "string literal"),
    ],
)
def test_masking_itself_defeats_each_bypass(sql: str, why: str) -> None:
    """Exercise the MASKER, not just the aggregate verdict.

    `_verify_read_only` now also carries a write-verb tripwire, which would
    reject all of these even if masking regressed — so asserting only on the
    verdict would let a masking bug pass unnoticed. These assert on the thing
    the bypasses actually attacked: the terminal verb the CTE walker reads out
    of masked text.
    """
    masked = sr._mask_strings_and_comments(sql).strip().rstrip(";").strip()
    assert sr._with_terminal_verb(masked) != "SELECT", (
        f"masking still lets {why} pose as a terminal SELECT"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $$hello$$ AS greeting FROM ANALYTICS.ORDERS;",
        "SELECT 'it\\'s fine' FROM ANALYTICS.ORDERS;",
        # A delimited CTE name is legal Snowflake; refusing it would block
        # generating a legitimate audit file, which is also a defect.
        'WITH "cte name" AS (SELECT 1) SELECT * FROM "cte name";',
        'WITH RECURSIVE "r" AS (SELECT 1) SELECT * FROM "r";',
    ],
)
def test_masking_does_not_reject_legitimate_sql(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


def test_var_used_ignores_quoted_and_string_occurrences() -> None:
    """`SELECT "$start_date"` is an identifier, not a variable reference."""
    assert not sr._var_used("start_date", 'SELECT "$start_date" FROM t')
    assert not sr._var_used("start_date", "SELECT '$start_date' FROM t")
    assert sr._var_used("start_date", "WHERE d >= $start_date")


# --------------------------------------------------------------------------- #
# Fragment declarations suppress a coverage requirement, so a malformed one is
# an error, never silently ignored — and must never exempt a different file
# than it appears to name.
# --------------------------------------------------------------------------- #


_FRAG_BASE = {
    "schema_version": 1,
    "feature": "revenue",
    "app": SLUG,
    "pages": [{"name": "Overview", "queries": ["revenue_daily"]}],
    "query_specs": {"revenue_daily": {}},
}


@pytest.mark.parametrize(
    ("fragments", "why"),
    [
        (["_x.sql"], "plain string grants no exemption but looks like it does"),
        ([{"file": "sub/dir/_x.sql", "reason": "r"}], "path exempts a different file"),
        ([{"file": "../../_x.sql", "reason": "r"}], "traversal exempts a different file"),
        ([{"file": "_x.sql"}], "no reason recorded"),
        ([{"file": "_x.sql", "reason": "a"}, {"file": "_x.sql", "reason": "b"}], "duplicate"),
        ([{"file": "revenue_daily.sql", "reason": "r"}], "also claimed by a page"),
        ([{"file": "_x.txt", "reason": "r"}], "not a .sql file"),
        ({"file": "_x.sql"}, "not a list"),
    ],
)
def test_malformed_fragment_declaration_is_rejected(fragments, why: str) -> None:
    m = {**_FRAG_BASE, "fragments": fragments}
    assert [p for p in sr.validate_manifest(m) if "fragment" in p], f"accepted: {why}"


def test_wellformed_fragment_declaration_is_accepted() -> None:
    m = {**_FRAG_BASE, "fragments": [{"file": "_shared.sql", "reason": "inlined via a token"}]}
    assert [p for p in sr.validate_manifest(m) if "fragment" in p] == []


def test_path_shaped_fragment_cannot_exempt_a_real_query(repo: Path) -> None:
    """The traversal form must not silence coverage for `queries/_x.sql`."""
    (repo / "apps" / SLUG / "queries" / "_x.sql").write_text(
        "-- Query: _x\n-- Feeds: (fragment)\n-- Schemas: ANALYTICS.ORDERS\nSELECT 1\n"
    )
    manifest = dict(MANIFEST)
    manifest["fragments"] = [{"file": "../../_x.sql", "reason": "r"}]
    (repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json").write_text(
        json.dumps(manifest, indent=2)
    )
    cov = sr.coverage(repo / "apps" / SLUG)
    assert "_x" in cov["uncovered"], "traversal path silenced coverage"
    assert cov["fragments"] == []


# --------------------------------------------------------------------------- #
# Second-round review findings.
# --------------------------------------------------------------------------- #


def test_dollar_quote_masking_does_not_regress_the_double_quote_fix() -> None:
    """An odd `"` inside `$$…$$` must not blind the guard to later statements.

    Making `"` a delimiter without teaching the masker about `$$` turned a
    write that 0.6.1 CAUGHT into one that passed: the unbalanced quote masked
    to end-of-text and hid every following statement.
    """
    sql = 'SELECT $$5" pipe$$ AS a; DELETE FROM t;'
    assert sr._verify_read_only(sql), "dollar-quote/double-quote interaction regressed"
    # It must also not hide a surviving bind on a later line.
    sql2 = 'SELECT $$5" pipe$$ AS a;\nSELECT b FROM t WHERE d <= :3;\n'
    assert sr._verify_binds_bound(sql2), "unbalanced quote hid a live bind"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $$refreshes at 12:30 UTC$$ AS note FROM t;",
        "SELECT $$see http://host:8080/x$$ AS url FROM t;",
        # Snowflake semi-structured access with a numeric key is not a bind.
        "SELECT payload:1 FROM t;",
        "SELECT b:2 FROM t;",
        "SELECT 1::INT FROM t;",
    ],
)
def test_bind_check_does_not_falsely_refuse_valid_sql(sql: str) -> None:
    """A false positive here refuses to generate a legitimate audit file, and
    the remedy the message prescribes cannot fix it."""
    assert sr._verify_binds_bound(sql) == [], f"false positive on: {sql}"


def test_no_set_block_header_claims_only_what_it_verified() -> None:
    """The header must not assert HOW a section bounds itself.

    `_bind_note` never inspects the query, so claiming the sections
    self-anchor on CURRENT_DATE / DATE_TRUNC / DATEADD is unverified — and a
    body using hardcoded literal dates would make it false. That is the same
    species of misdescription the SET pruning exists to remove.
    """
    lines = " ".join(sr._bind_note(False))
    assert "bind param" in lines and "session" in lines
    for unverified in ("CURRENT_DATE", "DATE_TRUNC", "DATEADD"):
        assert unverified not in lines, f"header asserts unverified {unverified}"


def test_non_string_set_block_note_is_a_validation_error_not_a_traceback() -> None:
    m = {"schema_version": 1, "feature": "revenue", "app": SLUG, "set_block_note": ["a", "list"]}
    assert [p for p in sr.validate_manifest(m) if "set_block_note" in p]
    # And rendering must degrade rather than raise, if validation is bypassed.
    out = sr._set_block({"set_block": {"d": "CURRENT_DATE"}, "set_block_note": ["a"]}, "x $d")
    assert "SET d = CURRENT_DATE;" in out


def test_planted_write_in_a_committed_file_is_reported_once(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """One defect, one finding — duplicates bury the real one."""
    assert _generate(repo) == 0
    rf = _review_file(repo)
    rf.write_text(rf.read_text().replace("-- Provenance:", "DELETE FROM t;\n-- Provenance:", 1))
    assert _check(repo) != 0
    out = capsys.readouterr().out
    assert out.count("statement root 'DELETE' is not allowed") == 1, out


# --------------------------------------------------------------------------- #
# Write-verb tripwire. It fires on statement-START position, not on any token,
# because most of these verbs are NOT Snowflake reserved words — a blanket
# match refused legitimate read-only SQL.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1) DELETE FROM t;",
        'SELECT $$5" pipe$$ AS a; DELETE FROM t;',
        "COMMENT ON TABLE t IS 'x';",
        "UNDROP TABLE t;",
        "TRUNCATE TABLE t;",
        "GRANT SELECT ON t TO ROLE r;",
    ],
)
def test_tripwire_catches_writes_in_statement_start_position(sql: str) -> None:
    assert sr._verify_read_only(sql), f"write slipped through: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        # These verbs are not reserved words in Snowflake, so they are legal
        # bare column aliases. Rejecting them refuses a valid audit file.
        "SELECT 1 AS CALL FROM t;",
        "SELECT 1 AS COPY FROM t;",
        "SELECT 1 AS PUT FROM t;",
        "SELECT 1 AS REMOVE FROM t;",
        "SELECT 1 AS UNLOAD FROM t;",
        "SELECT 1 AS EXECUTE FROM t;",
        # Suffix/prefix names never match, thanks to the word boundary.
        "SELECT CALLBACK_TS, COMPUTED_PUT_RATIO, COPY_COUNT FROM t;",
        "SELECT LAST_VALUE(x) OVER (ORDER BY d) FROM t;",
        # Quoted identifiers are masked before the tripwire sees them.
        'SELECT "CREATE", "ALTER", "CALL" FROM t;',
        # The one legal write-shaped root, already validated by the allowlist.
        "SET start_date = CURRENT_DATE;",
    ],
)
def test_tripwire_does_not_refuse_legitimate_read_only_sql(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


@pytest.mark.parametrize(
    "pages",
    [None, ["a string"], [{"name": "P"}], [{"queries": None}]],
)
def test_malformed_pages_with_a_valid_fragment_reports_not_crashes(pages) -> None:
    """A shape error must not surface as a traceback just because a valid
    fragment declaration happened to be present."""
    m = {
        "schema_version": 1,
        "feature": "revenue",
        "app": SLUG,
        "pages": pages,
        "query_specs": {},
        "fragments": [{"file": "_shared.sql", "reason": "inlined"}],
    }
    problems = sr.validate_manifest(m)  # must not raise
    assert problems, "malformed pages reported no problem at all"


# --------------------------------------------------------------------------- #
# Final-audit findings. A mutation run showed that deleting the write-verb
# tripwire entirely caused ZERO test failures: every existing case asserted on
# `_verify_read_only`'s aggregate verdict, which the statement-root allowlist
# already satisfied. These bind to the second layer directly.
# --------------------------------------------------------------------------- #


def test_tripwire_fires_independently_of_the_allowlist() -> None:
    """Bind to the layer itself, so removing it fails a test.

    Each bypass is fed to the tripwire regexes directly. The allowlist cannot
    mask the result because it is not consulted here.
    """
    for sql in (
        "WITH x AS (SELECT 1) DELETE FROM t",
        'WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM t',
        "WITH x AS (SELECT $$ ) SELECT y $$) DELETE FROM t",
    ):
        masked = sr._mask_strings_and_comments(sql)
        assert sr._WRITE_VERB_AFTER_PAREN_RE.search(masked), f"tripwire blind to: {sql}"
    assert sr._WRITE_VERB_AT_START_RE.search("DELETE FROM t")
    assert sr._WRITE_VERB_AT_START_RE.search("COMMENT ON TABLE t IS 'x'")


def test_set_rooted_statement_does_not_escape_both_layers() -> None:
    """`SET x = (SELECT 1) DELETE FROM t` passed BOTH layers.

    The tripwire used `search` (first match only) and then skipped the whole
    statement on the SET exemption, while the allowlist's SET form is a
    prefix-only regex — so everything after the `=` was examined by neither.
    """
    assert sr._verify_read_only("SET x = (SELECT 1) DELETE FROM t;")


@pytest.mark.parametrize(
    "sql",
    [
        # Bare (un-AS'd, unquoted) column aliases after a `)`. Legal Snowflake:
        # these verbs are not reserved words. `comment` is a real
        # INFORMATION_SCHEMA.TABLES column that discovery queries select.
        "SELECT MAX(d) comment FROM ANALYTICS.ORDERS;",
        "SELECT LISTAGG(x, chr(44)) copy FROM ANALYTICS.ORDERS;",
        "SELECT IFF(a,b,c) merge FROM ANALYTICS.ORDERS;",
        "SELECT COUNT(*) call FROM ANALYTICS.ORDERS;",
    ],
)
def test_bare_alias_after_paren_is_not_a_write(sql: str) -> None:
    """Refusing to generate a legitimate audit file is its own defect."""
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


def test_referenced_but_undeclared_session_var_is_refused(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """param_bindings pointing at an undeclared variable must not ship.

    Pruning removed SET lines for declared-but-unreferenced variables; nothing
    checked that referenced variables were declared. The result rendered
    `WHERE d BETWEEN $window_start AND $window_end` with NO SET block, under a
    header stating no section references a session variable, and both generate
    and check reported success.
    """
    manifest = dict(MANIFEST)
    manifest["param_bindings"] = {"1": "$window_start", "2": "$window_end"}
    manifest["token_dispatchers"] = {"REGION_FILTER": {"literal": ""}}
    (repo / "apps" / SLUG / "sql_review" / "manifests" / "revenue.json").write_text(
        json.dumps(manifest, indent=2)
    )
    assert _generate(repo) != 0
    err = capsys.readouterr().err
    assert "referenced but never SET" in err
    assert not _review_file(repo).exists()


def test_check_flags_an_undeclared_session_var_in_committed_text(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    assert _generate(repo) == 0
    rf = _review_file(repo)
    rf.write_text(rf.read_text().replace("$start_date", "$window_start"))
    assert _check(repo) != 0
    assert "referenced but never SET" in capsys.readouterr().out


def test_query_claimed_in_one_manifest_and_a_fragment_in_another_conflicts(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """Per-manifest validation cannot see this; coverage must.

    Left alone the query was both claimed AND exempt, and the index emitted two
    contradictory rows for it - the fragment row reading `Verified: n/a` - so a
    reviewer could read a page-feeding query as out of scope.
    """
    app = repo / "apps" / SLUG
    (app / "queries" / "orders_daily.sql").write_text(
        "-- Query: orders_daily\n-- Feeds: Other\n-- Schemas: ANALYTICS.ORDERS\nSELECT 1\n"
    )
    md = app / "sql_review" / "manifests"
    (md / "other.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "feature": "other",
                "app": SLUG,
                "pages": [{"name": "Other", "queries": ["orders_daily"]}],
                "query_specs": {"orders_daily": {}},
                "fragments": [{"file": "revenue_daily.sql", "reason": "inlined"}],
            },
            indent=2,
        )
    )
    cov = sr.coverage(app)
    assert cov["fragments_conflicting"] == ["revenue_daily"]
    assert "revenue_daily" not in cov["fragments"], "honoured a contradictory exemption"
    assert _check(repo) != 0
    assert "declared a fragment in another" in capsys.readouterr().out


def test_provenance_is_independent_of_the_checkout_path(tmp_path: Path) -> None:
    """The same commit must hash identically wherever it is checked out.

    Module hashing filtered on the ABSOLUTE path's components, so a checkout
    under any dotted directory - a git worktree at `.claude/worktrees/<name>/`,
    which this project's own guidance recommends - skipped every app module.
    Provenance then differed between a worktree and a clean clone, and a
    contributor saw false DRIFT on files they had not touched.
    """
    manifest = {**MANIFEST, "token_strategy": "manifest", "modules": {"data": "data"}}
    manifest["token_dispatchers"] = {"REGION_FILTER": {"const_attr": "REGION_SQL"}}
    digests = []
    for parent in ("plain", ".dotted"):
        root = tmp_path / parent / "repo"
        app = root / "apps" / SLUG
        (app / "queries").mkdir(parents=True)
        (app / "snowflake.yml").write_text("definition_version: 2\n")
        (app / "queries" / "revenue_daily.sql").write_text(QUERY)
        (app / "data.py").write_text("REGION_SQL = \"AND region = 'West'\"\n")
        md = app / "sql_review" / "manifests"
        md.mkdir(parents=True)
        (md / "revenue.json").write_text(json.dumps(manifest, indent=2))
        digests.append(sr._inputs_digest(app, md / "revenue.json", manifest))
    assert digests[0] == digests[1], (
        f"provenance depends on checkout path: plain={digests[0]} dotted={digests[1]}"
    )
    # And it must actually hash the module, not silently skip it everywhere.
    root = tmp_path / "plain" / "repo" / "apps" / SLUG
    (root / "data.py").write_text("REGION_SQL = \"AND region = 'East'\"\n")
    assert (
        sr._inputs_digest(root, root / "sql_review" / "manifests" / "revenue.json", manifest)
        != (digests[0])
    ), "an app-module edit did not change the digest"


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1) MERGE INTO t USING s ON 1=1",
        "WITH x AS (SELECT 1) TRUNCATE TABLE t",
        "WITH x AS (SELECT 1) COMMENT ON TABLE t IS 1",
        "WITH x AS (SELECT 1) COPY INTO t FROM @s",
        "WITH x AS (SELECT 1) DELETE FROM t",
    ],
)
def test_after_paren_anchor_sees_non_reserved_commands_too(sql: str) -> None:
    """The tripwire must not be blind to the non-reserved write commands.

    Restricting the after-paren anchor to RESERVED words (to stop it refusing
    bare column aliases) left it blind to `) MERGE INTO t` and
    `) TRUNCATE TABLE t`. The allowlist catches those today, but this layer
    exists to hold when the walker is fooled, so omitting them traded away the
    exact coverage it is for. Matched in two-token command form instead.
    """
    masked = sr._mask_strings_and_comments(sql)
    assert sr._WRITE_VERB_AFTER_PAREN_RE.search(masked), f"tripwire blind to: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        # The same verbs as BARE aliases: followed by FROM, never INTO/TABLE/ON.
        "SELECT COUNT(*) merge FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) truncate FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) put FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) remove FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) comment FROM ANALYTICS.ORDERS;",
        "SELECT LISTAGG(x, chr(44)) copy FROM ANALYTICS.ORDERS;",
    ],
)
def test_two_token_commands_do_not_fire_on_bare_aliases(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        # A subquery aliased with a write-verb name, followed by a JOIN's ON.
        # `) comment ON a.id = ...` looks exactly like `COMMENT ON`, and this
        # was a real false positive: legal read-only SQL refused.
        "SELECT a.x FROM t a JOIN (SELECT 1 AS id) comment ON a.id = comment.id;",
        "SELECT a.x FROM t a JOIN (SELECT 1 AS id) copy ON a.id = copy.id;",
        "SELECT a.x FROM t a LEFT JOIN (SELECT 1 AS id) merge ON a.id = merge.id;",
        "SELECT * FROM (SELECT 1) remove;",
    ],
)
def test_join_alias_named_after_a_write_verb_is_allowed(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        "COMMENT ON TABLE t IS 'x';",
        "WITH x AS (SELECT 1) COMMENT ON TABLE t IS 1;",
        "WITH x AS (SELECT 1) COMMENT ON VIEW v IS 1;",
        "WITH x AS (SELECT 1) COMMENT ON COLUMN t.c IS 1;",
    ],
)
def test_real_comment_ddl_is_still_refused(sql: str) -> None:
    """Narrowing `COMMENT ON` must not lose the DDL it exists to catch."""
    assert sr._verify_read_only(sql), f"COMMENT DDL slipped through: {sql}"


# --------------------------------------------------------------------------- #
# `set_block` expressions. Requiring the statement to END at the first balanced
# paren group refused the canonical idiom — anchoring a window to a source's
# last loaded date, cast or adjusted. The rule is "no write verb in the
# expression", not "nothing after it".
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "expr",
    [
        "(SELECT MAX(load_date) FROM REPORTING.VW_ORDERS)::DATE",
        "(SELECT MAX(load_date) FROM REPORTING.VW_ORDERS) - 1",
        "(SELECT COUNT(*) FROM ANALYTICS.ORDERS) / 2",
        "(SELECT MAX(v) FROM ANALYTICS.ORDERS) || '-x'",
        "COALESCE((SELECT MAX(d) FROM ANALYTICS.ORDERS), CURRENT_DATE)",
        "(SELECT MAX(d) FROM ANALYTICS.ORDERS)",
        "CURRENT_DATE",
        "DATEADD('day', -30, CURRENT_DATE)",
        # An identifier that merely CONTAINS a verb must not trip the scan.
        "(SELECT MAX(create_date) FROM ANALYTICS.UPDATES)",
        "(SELECT MAX(d) FROM ANALYTICS.ORDERS WHERE action = 'DELETE')",
    ],
)
def test_legal_set_block_expressions_are_accepted(expr: str) -> None:
    assert sr._verify_read_only(f"SET end_date = {expr};") == [], f"refused: {expr}"


@pytest.mark.parametrize(
    "expr",
    [
        "(SELECT 1) DELETE FROM t",
        "(SELECT 1) CALL MY_PROC()",
        "(SELECT 1) UNLOAD TO @s",
        "(SELECT 1) UNSET y",
        "1 DROP TABLE t",
        # Wrapping it in outer parens hid the command inside the group.
        "((SELECT 1) CALL MY_PROC())",
        "((SELECT 1) DELETE FROM t)",
    ],
)
def test_command_smuggled_into_a_set_expression_is_refused(expr: str) -> None:
    assert sr._verify_read_only(f"SET x = {expr};"), f"smuggled command passed: {expr}"


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("WITH x AS (SELECT 1) TRUNCATE ANALYTICS.ORDERS", "TABLE is optional in Snowflake"),
        ("WITH x AS (SELECT 1) TRUNCATE TABLE t", "explicit TABLE"),
        ("WITH x AS (SELECT 1) UNDROP SCHEMA ANALYTICS", "UNDROP takes SCHEMA too"),
        ("WITH x AS (SELECT 1) EXECUTE TASK MY_TASK", "EXECUTE takes TASK too"),
        ("WITH x AS (SELECT 1) RM @MY_STAGE/f.csv", "RM is REMOVE's alias"),
        ("WITH x AS (SELECT 1) CALL SYSTEM$ABORT_SESSION(1)", "CALL"),
        ("WITH x AS (SELECT 1) UNLOAD TO @s", "UNLOAD"),
        ("WITH x AS (SELECT 1) UNSET my_var", "UNSET"),
    ],
)
def test_after_paren_anchor_covers_every_write_command_form(sql: str, why: str) -> None:
    masked = sr._mask_strings_and_comments(sql)
    assert sr._WRITE_VERB_AFTER_PAREN_RE.search(masked), f"tripwire blind to {why}: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        # Bare aliases named after write verbs, followed by a CLAUSE keyword.
        "SELECT COUNT(*) call FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) truncate FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) unset FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) execute FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) undrop FROM ANALYTICS.ORDERS;",
        "SELECT COUNT(*) call, 1 AS y FROM ANALYTICS.ORDERS;",
        "SELECT MAX(d) truncate WHERE 1=1;",
        # A widened pattern must not match a longer identifier.
        "SELECT f(x) copy INTO_TAB FROM ANALYTICS.ORDERS;",
    ],
)
def test_widened_patterns_do_not_refuse_bare_aliases(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


def test_symlinked_module_keeps_provenance_checkout_independent(tmp_path: Path) -> None:
    """Hashing a symlink's TARGET made provenance environment-dependent again."""
    manifest = {**MANIFEST, "token_strategy": "manifest", "modules": {"data": "data"}}
    manifest["token_dispatchers"] = {"REGION_FILTER": {"const_attr": "REGION_SQL"}}
    digests = []
    for i, payload in enumerate(("AND region = 'West'", "AND region = 'East'")):
        root = tmp_path / f"c{i}" / "repo"
        app = root / "apps" / SLUG
        (app / "queries").mkdir(parents=True)
        (app / "snowflake.yml").write_text("definition_version: 2\n")
        (app / "queries" / "revenue_daily.sql").write_text(QUERY)
        outside = tmp_path / f"c{i}" / "outside.py"
        outside.write_text(f'REGION_SQL = "{payload}"\n')
        # A RELATIVE link, which is what git stores and what a real repo has.
        (app / "data.py").symlink_to(Path("../../../outside.py"))
        md = app / "sql_review" / "manifests"
        md.mkdir(parents=True)
        (md / "revenue.json").write_text(json.dumps(manifest, indent=2))
        digests.append(sr._inputs_digest(app, md / "revenue.json", manifest))
    assert digests[0] == digests[1], (
        "provenance still depends on what the symlink target holds in this checkout"
    )


@pytest.mark.parametrize("value", ["", "   ", None, 5])
def test_empty_or_non_string_set_block_value_is_rejected(value) -> None:
    """`set_block: {"x": ""}` renders `SET x = ;`.

    That is invalid SQL which nonetheless looked like a definition to the
    session-variable check, so it satisfied its own guard. Rejected at
    validation, where the author can see it.
    """
    m = {
        "schema_version": 1,
        "feature": "revenue",
        "app": SLUG,
        "pages": [{"name": "Overview", "queries": ["revenue_daily"]}],
        "query_specs": {"revenue_daily": {}},
        "set_block": {"start_date": value},
    }
    assert [p for p in sr.validate_manifest(m) if "set_block" in p], f"accepted {value!r}"


def test_valid_set_block_values_are_accepted() -> None:
    m = {
        "schema_version": 1,
        "feature": "revenue",
        "app": SLUG,
        "pages": [{"name": "Overview", "queries": ["revenue_daily"]}],
        "query_specs": {"revenue_daily": {}},
        "set_block": {"start_date": "CURRENT_DATE", "end_date": "(SELECT MAX(d) FROM t)"},
    }
    assert [p for p in sr.validate_manifest(m) if "set_block" in p] == []


# --------------------------------------------------------------------------- #
# Round 8. The recurring failure here is a guard that exists but is not wired,
# or is pinned one level too shallow — so these bind to CALL PATHS, not to
# regex constants.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sql", "form"),
    [
        ("SELECT ' ;\nDELETE FROM prod.t;", "string literal"),
        ('SELECT " ;\nDELETE FROM prod.t;', "quoted identifier"),
        ("SELECT $$ ;\nDELETE FROM prod.t;", "dollar-quoted constant"),
        ("SELECT 1 /* ;\nDELETE FROM prod.t;", "block comment"),
    ],
)
def test_unterminated_quoting_fails_closed(sql: str, form: str) -> None:
    """An open quoting form masks to end-of-text, blinding EVERY guard after it.

    Reachable by ordinary error, not attack: a token literal containing an
    apostrophe (`AND last_name = 'O'Brien'`) is enough. Both generate and check
    reported success on a file whose header claimed no section referenced a
    session variable while a section referenced two undeclared ones.
    """
    problems = sr._verify_read_only(sql)
    assert problems, f"blind after an unterminated {form}"
    assert "unterminated" in problems[0]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'ok' FROM ANALYTICS.ORDERS;",
        'SELECT "col name" FROM ANALYTICS.ORDERS;',
        "SELECT $$body$$ FROM ANALYTICS.ORDERS;",
        "SELECT 1 /* note */ FROM ANALYTICS.ORDERS;",
        "SELECT 'it''s' FROM ANALYTICS.ORDERS;",
        "SELECT 'it\\'s' FROM ANALYTICS.ORDERS;",
    ],
)
def test_closed_quoting_is_not_flagged_as_unterminated(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


def test_tripwire_wiring_holds_when_the_walker_is_fooled(monkeypatch) -> None:
    """Bind to the CALL PATH, not the regex objects.

    The previous test asserted on `_WRITE_VERB_*_RE.search(...)` directly, so
    disabling the code that CONSULTS them left the suite green — the whole
    defence-in-depth layer could be deleted by a refactor. Simulating a fooled
    walker is the contract: the allowlist is satisfied, and the tripwire must
    still refuse.
    """
    monkeypatch.setattr(sr, "_with_terminal_verb", lambda stmt: "SELECT")
    assert sr._verify_read_only("WITH x AS (SELECT 1) DELETE FROM t;"), (
        "after-paren anchor is not wired into _verify_read_only"
    )
    # The START anchor is inherently redundant with the root allowlist: any
    # statement beginning with a write verb is already refused by its root. To
    # bind its WIRING, simulate a compromised allowlist by admitting the root,
    # so only the tripwire can refuse it.
    monkeypatch.setattr(sr, "ALLOWED_ROOTS", sr.ALLOWED_ROOTS | {"DELETE"})
    assert sr._verify_read_only("DELETE FROM t;"), "start anchor is not wired in"


@pytest.mark.parametrize(
    "alias",
    ["comment", "copy", "merge", "call", "truncate", "unset", "execute", "undrop", "put"],
)
def test_legal_alias_inside_a_set_expression_is_accepted(alias: str) -> None:
    """The same fragment is accepted after `)`; refusing it here contradicted
    the tool's own pinned behaviour and made the coverage gate unsatisfiable."""
    sql = f"SET end_date = (SELECT MAX(d) {alias} FROM ANALYTICS.ORDERS)::DATE;"
    assert sr._verify_read_only(sql) == [], f"refused legal alias {alias!r}"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT METADATA$FILENAME FROM @RAW_FILES/inbound/",
        "SELECT METADATA$FILE_ROW_NUMBER FROM @RAW_FILES/inbound/",
        "SELECT SYSTEM$TYPEOF(x) FROM ANALYTICS.ORDERS",
        "SELECT SYSTEM$CLUSTERING_INFORMATION('ANALYTICS.ORDERS')",
        "SELECT $1 FROM @RAW_FILES/inbound/",
    ],
)
def test_dollar_inside_an_identifier_is_not_a_session_variable(sql: str) -> None:
    assert sr._verify_session_vars_defined(sql) == [], f"false positive on: {sql}"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT MAX(d) call FETCH FIRST 1 ROWS ONLY;",
        "SELECT MAX(d) call MINUS SELECT 1;",
        "SELECT ROUND(d) truncate FETCH FIRST 1 ROWS ONLY;",
        "SELECT MAX(d) unset FETCH FIRST 1 ROWS ONLY;",
        "SELECT * FROM (SELECT a FROM t) call SAMPLE (10);",
        "SELECT * FROM (SELECT a FROM t) call TABLESAMPLE (10);",
    ],
)
def test_clause_keyword_list_covers_the_less_common_clauses(sql: str) -> None:
    assert sr._verify_read_only(sql) == [], f"false positive on: {sql}"


def test_multi_variable_set_form_is_recognised_both_halves() -> None:
    """Both halves of the `SET (a, b) = (...)` fix were uncovered."""
    assert sr._SET_STMT_RE.match("SET (start_date, end_date) = (1, 2)"), "regex half"
    body = "SET (start_date, end_date) = (1, 2);\nSELECT $start_date, $end_date FROM t;"
    assert sr._verify_session_vars_defined(body) == [], "definition half"


def test_duplicate_fragment_across_manifests_is_reported(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """A named CHANGELOG 'Fixed' item that had no test at all."""
    app = repo / "apps" / SLUG
    (app / "queries" / "_shared.sql").write_text(
        "-- Query: _shared\n-- Feeds: (fragment)\n-- Schemas: ANALYTICS.ORDERS\nSELECT 1\n"
    )
    md = app / "sql_review" / "manifests"
    frag = [{"file": "_shared.sql", "reason": "inlined"}]
    for name in ("a", "b"):
        (md / f"{name}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "feature": name,
                    "app": SLUG,
                    "pages": [{"name": "P", "queries": ["revenue_daily"]}],
                    "query_specs": {"revenue_daily": {}},
                    "fragments": frag,
                },
                indent=2,
            )
        )
    assert sr._duplicate_fragments(app) == ["_shared"]
    assert _check(repo) != 0
    assert "declared by more than one manifest" in capsys.readouterr().out


@pytest.mark.parametrize(
    "entry",
    [{"variable": "cap", "default": "100"}, {"name": "cap"}, {"name": "cap", "default": ""}, "cap"],
)
def test_malformed_set_vars_is_a_validation_error_not_a_traceback(entry) -> None:
    m = {
        "schema_version": 1,
        "feature": "revenue",
        "app": SLUG,
        "pages": [{"name": "Overview", "queries": ["revenue_daily"]}],
        "query_specs": {"revenue_daily": {}},
        "set_vars": [entry],
    }
    assert [p for p in sr.validate_manifest(m) if "set_vars" in p], f"accepted {entry!r}"
    # And rendering must degrade rather than raise if validation is bypassed.
    assert isinstance(
        sr._set_block({"set_block": {"d": "CURRENT_DATE"}, "set_vars": [entry]}, "$d"), str
    )
