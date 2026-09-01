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
