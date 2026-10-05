"""Tests for streamsnow.tools.sql_review: page files from index.yaml, and the gate.

The load-bearing properties, each pinned here:

- the page file format: an 8-line header, so the first ``--N_key`` tag is line 9
  and its SQL line 10; one self-contained section per metric, each starting with
  its own ``params`` CTE and ending in exactly one ``;``;
- generate → check round-trips clean; an edited query or index, a hand-edited
  page file, a stale README table or ``Used by`` line reads as a named finding;
- ``check`` is IMPORT-FREE: a page module that explodes on import is never run;
- markers, the DDL folder, lint and comment rules, coverage and the coverage
  policy, each with its finding ``kind``;
- nothing that is not read-only SQL is ever written.

The read-only guard itself is tested in test_sql_review_guard.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from streamsnow.tools import sql_review as sr

SLUG = "acme-sales"

NAV = """import streamlit as st

nav = st.navigation(
    [
        st.Page("pages/overview.py", title="Overview", default=True),
        st.Page("pages/regions.py", title="Regions"),
    ]
)
nav.run()
"""

REVENUE = """-- Query: revenue
-- Feeds: Overview (revenue tile)
-- Params: :1 start_date, :2 end_date
SELECT SUM(revenue) AS revenue
FROM ANALYTICS_DB.REPORTING.ORDERS
WHERE order_date BETWEEN :1 AND :2 {REGION_FILTER}
"""

TREND = """-- Query: trend
-- Params: :1 start_date
WITH
-- Revenue per day inside the review window.
daily AS (
    SELECT
        order_date,
        SUM(revenue) AS revenue
    FROM ANALYTICS_DB.REPORTING.ORDERS
    WHERE order_date >= :1
    GROUP BY order_date
)

SELECT
    order_date,
    revenue
FROM daily
ORDER BY order_date
"""

BY_REGION = """-- Query: by_region
SELECT
    region,
    COUNT(*) AS n
FROM ANALYTICS_DB.APP.REGION_ROLLUP
WHERE order_date >= :start_date
GROUP BY region
"""

OVERVIEW = """import streamlit as st
from review import review_value

st.metric("Revenue", review_value("total_revenue", 1))
st.line_chart(review_value("revenue_trend", []))
"""

REGIONS = """import streamlit as st
from review import review_value

st.bar_chart(review_value("orders_by_region", []))
"""

DDL = """-- Object: ANALYTICS_DB.APP.REGION_ROLLUP
-- Purpose: Orders per region and day, so the Regions page does not scan ORDERS.
-- Used by: 02_regions.sql #1 orders_by_region
-- Grants: ROLE_APP_READER
-- Applied by a human. Claude deploys only when explicitly asked.
CREATE OR REPLACE VIEW ANALYTICS_DB.APP.REGION_ROLLUP AS
SELECT region, order_date, COUNT(*) AS n FROM ANALYTICS_DB.REPORTING.ORDERS GROUP BY 1, 2;
"""

ROLLUP = "ANALYTICS_DB.APP.REGION_ROLLUP"


def _index() -> dict:
    return {
        "schema_version": 2,
        "app": SLUG,
        "review_window": {
            "start_date": "DATEADD(DAY, -30, CURRENT_DATE())",
            "end_date": "CURRENT_DATE()",
        },
        "pages": [
            {
                "path": "pages/overview.py",
                "metrics": [
                    {
                        "key": "total_revenue",
                        "query": "queries/revenue.sql",
                        "tokens": {"REGION_FILTER": "AND region = 'West'"},
                        "binds": {"1": "params.start_date", "2": "params.end_date"},
                        "reads": ["ANALYTICS_DB.REPORTING.ORDERS"],
                    },
                    {
                        "key": "revenue_trend",
                        "query": "queries/trend.sql",
                        "binds": {"1": "params.start_date"},
                    },
                ],
            },
            {
                "path": "pages/regions.py",
                "metrics": [
                    {
                        "key": "orders_by_region",
                        "query": "queries/by_region.sql",
                        "binds": {"start_date": "params.start_date"},
                        "reads": [ROLLUP],
                    }
                ],
            },
        ],
        "objects": [{"name": ROLLUP, "grants": ["ROLE_APP_READER"]}],
    }


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    a = root / "apps" / SLUG
    for d in ("queries", "pages", f"sql_review/{sr.OBJECTS_DIR}"):
        (a / d).mkdir(parents=True)
    (a / "snowflake.yml").write_text("definition_version: 2\n", encoding="utf-8")
    (a / "streamlit_app.py").write_text(NAV, encoding="utf-8")
    for name, text in (("revenue", REVENUE), ("trend", TREND), ("by_region", BY_REGION)):
        (a / "queries" / f"{name}.sql").write_text(text, encoding="utf-8")
    (a / "pages" / "overview.py").write_text(OVERVIEW, encoding="utf-8")
    (a / "pages" / "regions.py").write_text(REGIONS, encoding="utf-8")
    (a / "sql_review" / sr.OBJECTS_DIR / f"{ROLLUP}.sql").write_text(DDL, encoding="utf-8")
    _write_index(root, _index())
    return root


def _app(repo: Path) -> Path:
    return repo / "apps" / SLUG


def _write_index(repo: Path, data: dict) -> None:
    (_app(repo) / "sql_review" / "index.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _page(repo: Path, name: str = "01_overview.sql") -> Path:
    return _app(repo) / "sql_review" / name


def _generate(repo: Path) -> int:
    return sr.main(["generate", SLUG, "--dir", str(repo)])


def _check(repo: Path, *extra: str) -> int:
    return sr.main(["check", SLUG, "--dir", str(repo), *extra])


def _findings(repo: Path, capsys: pytest.CaptureFixture) -> list[dict]:
    capsys.readouterr()
    _check(repo, "--format", "json")
    out = json.loads(capsys.readouterr().out)
    return out["findings"] + out["warnings"]


def _kinds(repo: Path, capsys: pytest.CaptureFixture) -> set[str]:
    return {f["kind"] for f in _findings(repo, capsys)}


def _sections(text: str) -> list[list[str]]:
    """Each section's lines, tag line first; provenance and header excluded."""
    lines = text.split("\n")[sr.HEADER_LINES :]
    out: list[list[str]] = []
    for line in lines:
        if re.match(r"^--\d+_[a-z0-9_]+$", line):
            out.append([line])
        elif out and not line.startswith("-- Provenance:"):
            out[-1].append(line)
    return [[ln for ln in s if ln.strip()] for s in out]


# --------------------------------------------------------------------------- #
# Page file format
# --------------------------------------------------------------------------- #
def test_round_trip_clean(repo: Path, capsys: pytest.CaptureFixture) -> None:
    assert _generate(repo) == 0
    assert _check(repo) == 0
    assert "sql-review: clean" in capsys.readouterr().out


def test_first_tag_is_line_9_and_its_sql_line_10(repo: Path) -> None:
    _generate(repo)
    lines = _page(repo).read_text(encoding="utf-8").split("\n")
    assert lines[0] == "-- Page: Overview (pages/overview.py)"
    assert lines[5] == "-- Metrics: 1 total_revenue, 2 revenue_trend"
    assert lines[8] == "--1_total_revenue"
    assert lines[9] == "WITH params AS ("
    assert all(len(ln) <= sr.MAX_LINE for ln in lines[: sr.HEADER_LINES])


def test_each_section_is_self_contained_with_one_semicolon(repo: Path) -> None:
    _generate(repo)
    text = _page(repo).read_text(encoding="utf-8")
    sections = _sections(text)
    assert [s[0] for s in sections] == ["--1_total_revenue", "--2_revenue_trend"]
    for section in sections:
        assert section[1] == "WITH params AS ("
        body = "\n".join(section[1:])
        assert body.endswith(";") and ";" not in body[:-1]  # none in comments either
        assert sr._verify_read_only(body) == [] and sr._verify_binds_bound(body) == []
    # Exactly one blank line between sections, none between a tag and its SQL.
    assert "\n\n--2_revenue_trend\nWITH params AS (" in text
    assert "\n\n\n" not in text


def test_tokens_and_binds_are_substituted(repo: Path) -> None:
    _generate(repo)
    text = _page(repo).read_text(encoding="utf-8")
    assert "AND region = 'West'" in text and "{REGION_FILTER}" not in text
    assert "BETWEEN (SELECT start_date FROM params) AND (SELECT end_date FROM params)" in text
    regions = _page(repo, "02_regions.sql").read_text(encoding="utf-8")
    assert "order_date >= (SELECT start_date FROM params)" in regions
    assert not re.search(r"(?<![:\w]):(\d+|start_date)\b", sr._mask_strings_and_comments(text))


def test_a_leading_with_merges_and_keeps_the_cte_comment_above_its_cte(repo: Path) -> None:
    _generate(repo)
    trend = _sections(_page(repo).read_text(encoding="utf-8"))[1]
    text = "\n".join(trend)
    assert text.count("WITH") == 1  # merged, not nested
    i = trend.index("daily AS (")
    assert trend[i - 1] == "-- Revenue per day inside the review window."
    assert trend[i - 2] == "),"


def test_header_fields_are_dropped_but_a_purpose_comment_is_kept(repo: Path) -> None:
    q = _app(repo) / "queries" / "revenue.sql"
    q.write_text(
        REVENUE.replace("SELECT SUM", "-- Booked revenue, refunds excluded.\nSELECT SUM"),
        encoding="utf-8",
    )
    _generate(repo)
    section = _sections(_page(repo).read_text(encoding="utf-8"))[0]
    assert "-- Booked revenue, refunds excluded." in section
    assert not any(ln.startswith(("-- Query:", "-- Params:")) for ln in section)
    assert section[section.index("-- Booked revenue, refunds excluded.") + 1].startswith("SELECT")


def test_double_colon_cast_and_semi_structured_access_survive(repo: Path) -> None:
    q = _app(repo) / "queries" / "revenue.sql"
    q.write_text(
        REVENUE.replace("SUM(revenue)", "SUM(revenue)::NUMBER(18, 2), MAX(payload:region)"),
        encoding="utf-8",
    )
    assert _generate(repo) == 0
    text = _page(repo).read_text(encoding="utf-8")
    assert "::NUMBER(18, 2)" in text and "payload:region" in text


def test_reordering_metrics_renumbers_tags(repo: Path) -> None:
    data = _index()
    data["pages"][0]["metrics"].reverse()
    _write_index(repo, data)
    _generate(repo)
    tags = [s[0] for s in _sections(_page(repo).read_text(encoding="utf-8"))]
    assert tags == ["--1_revenue_trend", "--2_total_revenue"]


def test_reordering_the_nav_renames_files_and_cleans_up(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    _generate(repo)
    entry = _app(repo) / "streamlit_app.py"
    lines = entry.read_text(encoding="utf-8").splitlines()
    lines[4], lines[5] = lines[5], lines[4]
    entry.write_text("\n".join(lines) + "\n", encoding="utf-8")
    findings = _findings(repo, capsys)
    orphans = [f["file"] for f in findings if "orphaned" in f["detail"]]
    assert sorted(Path(f).name for f in orphans) == ["01_overview.sql", "02_regions.sql"]
    assert _generate(repo) == 0
    names = sorted(p.name for p in sr._generated_page_files(_app(repo)))
    assert names == ["01_regions.sql", "02_overview.sql"]
    assert _check(repo) == 0


def test_a_page_without_metrics_gets_no_file(repo: Path, capsys: pytest.CaptureFixture) -> None:
    data = _index()
    data["pages"][1]["metrics"] = []
    data["objects"] = []
    _write_index(repo, data)
    (_app(repo) / "sql_review" / sr.OBJECTS_DIR / f"{ROLLUP}.sql").unlink()
    (_app(repo) / "pages" / "regions.py").write_text("import streamlit as st\n", encoding="utf-8")
    (_app(repo) / "queries" / "by_region.sql").unlink()
    assert _generate(repo) == 0
    assert not _page(repo, "02_regions.sql").exists()
    assert _check(repo) == 0


def test_generate_writes_lf_and_is_idempotent(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    assert b"\r\n" not in _page(repo).read_bytes()
    before = _page(repo).read_bytes()
    capsys.readouterr()
    assert _generate(repo) == 0
    assert "is up to date" in capsys.readouterr().out
    assert _page(repo).read_bytes() == before


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("query", "message"),
    [
        (
            "WITH x AS (SELECT 1 AS a)\nDELETE FROM ANALYTICS_DB.REPORTING.ORDERS WHERE d >= :1",
            "refusing to write",
        ),
        ("SELECT 1 AS a WHERE d >= :1;\nCALL some_procedure()", "more than one statement"),
        (
            "WITH params AS (SELECT 1 AS a)\nSELECT a FROM params WHERE d >= :1",
            "CTE named params",
        ),
    ],
)
def test_generate_refuses_unsafe_or_ambiguous_queries(
    repo: Path, capsys: pytest.CaptureFixture, query: str, message: str
) -> None:
    (_app(repo) / "queries" / "trend.sql").write_text(query, encoding="utf-8")
    capsys.readouterr()
    assert _generate(repo) == 2
    assert message in capsys.readouterr().err
    assert not _page(repo).exists()  # nothing half-written


def test_generate_refuses_an_invalid_index(repo: Path, capsys: pytest.CaptureFixture) -> None:
    data = _index()
    data["pages"][0]["metrics"][0]["binds"].pop("2")
    _write_index(repo, data)
    capsys.readouterr()
    assert _generate(repo) == 2
    assert "uses :2 but binds" in capsys.readouterr().err


def test_generate_without_an_index_is_a_tool_error(repo: Path) -> None:
    (_app(repo) / "sql_review" / "index.yaml").unlink()
    assert _generate(repo) == 2


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
def test_query_edit_reads_as_drift(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    q = _app(repo) / "queries" / "revenue.sql"
    q.write_text(REVENUE.replace("SUM(revenue)", "SUM(net_revenue)"), encoding="utf-8")
    assert _check(repo) == 1
    assert "DRIFT" in capsys.readouterr().out


def test_index_edit_reads_as_drift(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    data = _index()
    data["review_window"]["start_date"] = "DATEADD(DAY, -7, CURRENT_DATE())"
    _write_index(repo, data)
    assert _check(repo) == 1
    assert "DRIFT" in capsys.readouterr().out


def test_sqlfluff_config_edit_reads_as_drift(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    (repo / ".sqlfluff").write_text(
        sr_lint_default().replace("max_line_length = 100", "max_line_length = 120"),
        encoding="utf-8",
    )
    assert _check(repo) == 1
    assert "DRIFT" in capsys.readouterr().out


def sr_lint_default() -> str:
    from streamsnow.tools import sql_review_lint

    return sql_review_lint.DEFAULT_CONFIG.read_text(encoding="utf-8")


def test_hand_edited_page_file_reads_as_edited(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    page = _page(repo)
    page.write_text(page.read_text(encoding="utf-8").replace("'West'", "'East'"), encoding="utf-8")
    assert _check(repo) == 1
    assert "edited by hand" in capsys.readouterr().out


def test_hand_added_write_fails_even_with_forged_hashes(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """A committer who re-stamps both hashes still cannot land a write."""
    _generate(repo)
    page = _page(repo)
    text = page.read_text(encoding="utf-8")
    body = text[: text.index("-- Provenance:")].rstrip() + "\nDELETE FROM T.S.X;\n"
    index = sr.sri.load_index(_app(repo))
    p = index.numbered_pages[0]
    page.write_text(
        sr._stamp_provenance(body, sr._inputs_digest(_app(repo), index, p, sr_lint_default())),
        encoding="utf-8",
    )
    findings = _findings(repo, capsys)
    assert any(f["kind"] == "readonly" for f in findings)


@pytest.mark.parametrize(
    ("tail", "problem"),
    [
        ("SELECT 1;\n", "content after the provenance line"),
        ("-- Provenance: schema=2 inputs=0000000000000000 output=0000000000000000\n", "multiple"),
    ],
)
def test_content_after_provenance_is_a_finding(
    repo: Path, capsys: pytest.CaptureFixture, tail: str, problem: str
) -> None:
    _generate(repo)
    page = _page(repo)
    page.write_text(page.read_text(encoding="utf-8") + tail, encoding="utf-8")
    assert _check(repo) == 1
    assert problem in capsys.readouterr().out


def test_missing_page_file_is_a_finding(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    _page(repo).unlink()
    assert _check(repo) == 1
    assert "page file missing" in capsys.readouterr().out


def test_crlf_checkout_reads_clean(repo: Path) -> None:
    """Git for Windows (autocrlf) checks text out with CRLF: not drift, not an edit."""
    _generate(repo)
    a = _app(repo)
    for path in [
        *a.rglob("*.sql"),
        a / "sql_review" / "index.yaml",
        a / "sql_review" / "README.md",
    ]:
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert _check(repo) == 0


def test_lone_carriage_return_still_reads_as_edit(repo: Path) -> None:
    _generate(repo)
    page = _page(repo)
    page.write_bytes(page.read_bytes().replace(b"revenue", b"reven\rue", 1))
    assert _check(repo) == 1


def test_provenance_is_independent_of_the_checkout_path(tmp_path: Path) -> None:
    import shutil

    hashes = []
    for name in ("a", ".claude/worktrees/b"):
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        root = tmp_path / "src"
        if not root.exists():
            root.mkdir()
            repo_fixture(root)
        shutil.copytree(root, dest)
        assert sr.main(["generate", SLUG, "--dir", str(dest)]) == 0
        text = (dest / "apps" / SLUG / "sql_review" / "01_overview.sql").read_text(encoding="utf-8")
        hashes.append(text.splitlines()[-1])
    assert hashes[0] == hashes[1]


def repo_fixture(root: Path) -> None:
    a = root / "apps" / SLUG
    for d in ("queries", "pages", f"sql_review/{sr.OBJECTS_DIR}"):
        (a / d).mkdir(parents=True)
    (a / "snowflake.yml").write_text("definition_version: 2\n", encoding="utf-8")
    (a / "streamlit_app.py").write_text(NAV, encoding="utf-8")
    for name, text in (("revenue", REVENUE), ("trend", TREND), ("by_region", BY_REGION)):
        (a / "queries" / f"{name}.sql").write_text(text, encoding="utf-8")
    (a / "pages" / "overview.py").write_text(OVERVIEW, encoding="utf-8")
    (a / "pages" / "regions.py").write_text(REGIONS, encoding="utf-8")
    (a / "sql_review" / sr.OBJECTS_DIR / f"{ROLLUP}.sql").write_text(DDL, encoding="utf-8")
    (a / "sql_review" / "index.yaml").write_text(yaml.safe_dump(_index()), encoding="utf-8")


def test_check_never_imports_app_code(repo: Path) -> None:
    """A page module that explodes on import must never run: markers are read by AST."""
    page = _app(repo) / "pages" / "overview.py"
    page.write_text("raise SystemExit('imported!')\n" + OVERVIEW, encoding="utf-8")
    (_app(repo) / "review.py").write_text("raise SystemExit('imported!')\n", encoding="utf-8")
    _generate(repo)
    assert _check(repo) == 0


# --------------------------------------------------------------------------- #
# README and folder docs
# --------------------------------------------------------------------------- #
def test_readme_tables_are_generated_and_the_narrative_kept(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    _generate(repo)
    readme = _app(repo) / "sql_review" / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "| `01_overview.sql` | 2 | `revenue_trend` | `queries/trend.sql` | — |" in text
    assert f"| `{sr.OBJECTS_DIR}/{ROLLUP}.sql` | 02_regions.sql #1 orders_by_region |" in text
    readme.write_text("Team notes stay.\n\n" + text, encoding="utf-8")
    data = _index()
    data["pages"][0]["metrics"].reverse()
    _write_index(repo, data)
    _generate(repo)
    text = readme.read_text(encoding="utf-8")
    assert text.startswith("Team notes stay.")
    assert "| `01_overview.sql` | 1 | `revenue_trend` |" in text


def test_stale_readme_is_a_finding(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    readme = _app(repo) / "sql_review" / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("| 2 |", "| 9 |"), encoding="utf-8"
    )
    assert _check(repo) == 1
    assert "README.md tables are stale" in capsys.readouterr().out


def test_duplicate_readme_markers_stop_generate(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    readme = _app(repo) / "sql_review" / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") * 2, encoding="utf-8")
    capsys.readouterr()
    assert _generate(repo) == 2
    assert "index markers" in capsys.readouterr().err


def test_folder_agents_file_is_refreshed_above_its_marker_only(repo: Path) -> None:
    _generate(repo)
    rdir = _app(repo) / "sql_review"
    assert (rdir / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    agents = rdir / "AGENTS.md"
    text = agents.read_text(encoding="utf-8")
    assert sr.AGENTS_MARKER in text and "line 9" in text
    head, tail = text.split(sr.AGENTS_MARKER)
    agents.write_text(
        "stale rules\n" + sr.AGENTS_MARKER + tail + "My team's note.\n", encoding="utf-8"
    )
    _generate(repo)
    refreshed = agents.read_text(encoding="utf-8")
    assert refreshed.startswith(head) and refreshed.endswith("My team's note.\n")


# --------------------------------------------------------------------------- #
# review_value markers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("page", "detail"),
    [
        (OVERVIEW.replace('review_value("revenue_trend", [])', "[]"), "has no review_value"),
        (OVERVIEW + 'st.write(review_value("stray_key", 0))\n', "has no metric 'stray_key'"),
        (OVERVIEW + 'st.write(review_value("total_revenue", 0))\n', "appears 2 times"),
        (OVERVIEW + "st.write(review_value(name, 0))\n", "string literal key"),
        ("def broken(:\n", "cannot parse the page"),
    ],
)
def test_marker_mismatches_are_findings(
    repo: Path, capsys: pytest.CaptureFixture, page: str, detail: str
) -> None:
    _generate(repo)
    (_app(repo) / "pages" / "overview.py").write_text(page, encoding="utf-8")
    findings = _findings(repo, capsys)
    assert any(f["kind"] == "marker" and detail in f["detail"] for f in findings), findings


# --------------------------------------------------------------------------- #
# app_specific_reporting_objects/
# --------------------------------------------------------------------------- #
def _ddl(repo: Path) -> Path:
    return _app(repo) / "sql_review" / sr.OBJECTS_DIR / f"{ROLLUP}.sql"


def test_generate_rewrites_only_the_used_by_line(repo: Path) -> None:
    _ddl(repo).write_text(
        DDL.replace("02_regions.sql #1 orders_by_region", "stale"), encoding="utf-8"
    )
    _generate(repo)
    assert _ddl(repo).read_text(encoding="utf-8") == DDL


@pytest.mark.parametrize(
    ("mutate", "detail"),
    [
        (lambda r: _ddl(r).unlink(), "has no DDL file"),
        (
            lambda r: _ddl(r).write_text(
                DDL.replace("-- Grants: ROLE_APP_READER\n", ""), encoding="utf-8"
            ),
            "header is missing -- Grants:",
        ),
        (
            lambda r: _ddl(r).write_text(
                DDL.replace("ROLE_APP_READER", "PUBLIC"), encoding="utf-8"
            ),
            "index.yaml lists 'ROLE_APP_READER'",
        ),
        (
            lambda r: _ddl(r).write_text(
                DDL.replace("-- Object: ANALYTICS_DB", "-- Object: OTHER_DB"), encoding="utf-8"
            ),
            "-- Object: names",
        ),
        (
            lambda r: _ddl(r).write_text(
                DDL.replace("#1 orders_by_region", "#9 nope"), encoding="utf-8"
            ),
            "-- Used by: is stale",
        ),
        (
            lambda r: (_ddl(r).parent / "ANALYTICS_DB.APP.UNUSED.sql").write_text(
                DDL.replace("REGION_ROLLUP", "UNUSED"), encoding="utf-8"
            ),
            "not listed under objects:",
        ),
        (
            lambda r: (_ddl(r).parent / "not-a-name.sql").write_text(DDL, encoding="utf-8"),
            "<DATABASE>.<SCHEMA>.<OBJECT>.sql",
        ),
    ],
)
def test_object_folder_rules(
    repo: Path, capsys: pytest.CaptureFixture, mutate, detail: str
) -> None:
    _generate(repo)
    mutate(repo)
    findings = _findings(repo, capsys)
    assert any(f["kind"] == "objects" and detail in f["detail"] for f in findings), findings


def test_an_object_no_metric_reads_is_a_finding(repo: Path, capsys: pytest.CaptureFixture) -> None:
    data = _index()
    data["pages"][1]["metrics"][0]["reads"] = []
    _write_index(repo, data)
    _generate(repo)
    findings = _findings(repo, capsys)
    assert any("no metric reads" in f["detail"] for f in findings), findings


def test_ddl_is_exempt_from_the_read_only_guard(repo: Path) -> None:
    """CREATE OR REPLACE VIEW in the DDL folder is the point of the folder."""
    _generate(repo)
    assert "CREATE OR REPLACE VIEW" in _ddl(repo).read_text(encoding="utf-8")
    assert _check(repo) == 0


# --------------------------------------------------------------------------- #
# Lint and comment rules
# --------------------------------------------------------------------------- #
def test_query_lint_findings_name_the_rule_and_line(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    q = _app(repo) / "queries" / "by_region.sql"
    q.write_text(BY_REGION.replace("COUNT(*) AS n", "count(*) as n"), encoding="utf-8")
    _generate(repo)
    lint = [f for f in _findings(repo, capsys) if f["kind"] == "lint"]
    assert lint and all(f["file"].endswith("queries/by_region.sql") for f in lint)
    assert {f["line"] for f in lint} == {4}
    assert any(f["detail"].startswith("CP01") for f in lint)


def test_generate_fixes_layout_and_case_in_page_sections(repo: Path) -> None:
    q = _app(repo) / "queries" / "by_region.sql"
    q.write_text(BY_REGION.replace("COUNT(*) AS n", "count(*) as n"), encoding="utf-8")
    _generate(repo)
    assert "COUNT(*) AS n" in _page(repo, "02_regions.sql").read_text(encoding="utf-8")


def test_lint_files_limits_lint_but_not_the_other_checks(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    q = _app(repo) / "queries" / "by_region.sql"
    q.write_text(BY_REGION.replace("COUNT(*) AS n", "count(*) as n"), encoding="utf-8")
    _generate(repo)
    _page(repo).write_text("edited\n", encoding="utf-8")
    other = str(_app(repo) / "queries" / "revenue.sql")
    capsys.readouterr()
    assert _check(repo, "--format", "json", "--lint-files", other) == 1
    out = json.loads(capsys.readouterr().out)
    kinds = {f["kind"] for f in out["findings"]}
    assert "lint" not in kinds and "provenance" in kinds
    assert _check(repo, "--lint-files", str(q)) == 1


def test_a_section_that_does_not_parse_is_a_lint_finding(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    data = _index()
    data["pages"][0]["metrics"][0]["tokens"]["REGION_FILTER"] = "AND AND region"
    _write_index(repo, data)
    assert _generate(repo) == 0
    findings = _findings(repo, capsys)
    assert any(
        f["kind"] == "lint" and f["file"].endswith("01_overview.sql") and "parse" in f["detail"]
        for f in findings
    ), findings


def test_fragments_are_not_linted(repo: Path, capsys: pytest.CaptureFixture) -> None:
    (_app(repo) / "queries" / "_ctes.sql").write_text("x AS (SELECT 1)\n", encoding="utf-8")
    data = _index()
    data["fragments"] = [{"file": "queries/_ctes.sql", "reason": "inlined via {CTES}"}]
    _write_index(repo, data)
    _generate(repo)
    assert _check(repo) == 0


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        (TREND.replace("-- Revenue per day inside the review window.\n", ""), "CTE daily needs"),
        (
            TREND.replace(
                "WITH\n-- Revenue per day inside the review window.\ndaily", "WITH daily"
            ),
            "CTE daily needs",
        ),
        (TREND.replace("ORDER BY", "-- " + "x" * 100 + "\nORDER BY"), "keep each comment"),
    ],
)
def test_comment_rules(repo: Path, capsys: pytest.CaptureFixture, query: str, detail: str) -> None:
    (_app(repo) / "queries" / "trend.sql").write_text(query, encoding="utf-8")
    _generate(repo)
    findings = _findings(repo, capsys)
    assert any(f["kind"] == "comments" and detail in f["detail"] for f in findings), findings


def test_a_header_field_above_a_cte_is_not_its_comment(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    query = "-- Query: trend\n-- Params: :1\nWITH daily AS (SELECT 1 AS a WHERE b >= :1)\n\nSELECT a FROM daily\n"
    (_app(repo) / "queries" / "trend.sql").write_text(query, encoding="utf-8")
    _generate(repo)
    assert any("CTE daily needs" in f["detail"] for f in _findings(repo, capsys))


def test_heavy_commenting_is_advisory_only(repo: Path, capsys: pytest.CaptureFixture) -> None:
    q = _app(repo) / "queries" / "by_region.sql"
    q.write_text(BY_REGION.replace("GROUP BY", "-- a\n-- b\n-- c\nGROUP BY"), encoding="utf-8")
    _generate(repo)
    findings = _findings(repo, capsys)
    assert [f["kind"] for f in findings] == ["advisory"]
    assert _check(repo) == 0


# --------------------------------------------------------------------------- #
# Coverage, policy, old format
# --------------------------------------------------------------------------- #
def _set_policy(repo: Path, policy: str) -> None:
    from tests.test_init import EXAMPLE_CONFIG

    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["sql_review"] = {"coverage": policy}
    (repo / "streamsnow.config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_an_unused_query_is_coverage_and_its_severity_follows_the_policy(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    (_app(repo) / "queries" / "orphan.sql").write_text("SELECT 1 AS a\n", encoding="utf-8")
    _generate(repo)
    findings = _findings(repo, capsys)
    assert [f["kind"] for f in findings] == ["coverage"]
    assert _check(repo) == 0  # default policy: warn
    _set_policy(repo, "fail")
    assert _check(repo) == 1


def test_check_json_carries_kind_and_policy(repo: Path, capsys: pytest.CaptureFixture) -> None:
    (_app(repo) / "queries" / "orphan.sql").write_text("SELECT 1 AS a\n", encoding="utf-8")
    _generate(repo)
    capsys.readouterr()
    _check(repo, "--format", "json")
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["coverage_policy"] == "warn"
    assert out["warnings"][0]["kind"] == "coverage"


def test_app_without_an_index_is_coverage_only(repo: Path, capsys: pytest.CaptureFixture) -> None:
    (_app(repo) / "sql_review" / "index.yaml").unlink()
    (_app(repo) / "queries" / "by_region.sql").write_text("select bad sql", encoding="utf-8")
    assert [f["kind"] for f in _findings(repo, capsys)] == ["coverage"]  # no lint, no markers
    assert _check(repo) == 0


def test_old_format_without_an_index_is_a_hard_index_finding(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    (_app(repo) / "sql_review" / "index.yaml").unlink()
    (_app(repo) / "sql_review" / "manifests").mkdir()
    findings = _findings(repo, capsys)
    assert [f["kind"] for f in findings] == ["index"]
    assert "removed sql_review format" in findings[0]["detail"]
    assert _check(repo) == 1


def test_generate_removes_old_review_files_and_check_names_manifests(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    rdir = _app(repo) / "sql_review"
    (rdir / "revenue.review.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (rdir / "manifests").mkdir()
    assert _generate(repo) == 0
    assert not (rdir / "revenue.review.sql").exists()
    findings = _findings(repo, capsys)
    assert any("delete manifests/" in f["detail"] for f in findings)


def test_check_all_apps_when_no_slug(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _generate(repo)
    _page(repo).unlink()
    assert sr.main(["check", "--dir", str(repo)]) == 1


# --------------------------------------------------------------------------- #
# The scaffold
# --------------------------------------------------------------------------- #
def test_scaffolded_app_is_clean_straight_after_init(tmp_path: Path) -> None:
    """`init` generates the starter page file, so check passes with no manual step."""
    from streamsnow.config import Config
    from streamsnow.scaffolder import scaffold
    from tests.test_init import EXAMPLE_CONFIG

    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    scaffold(Config.from_dict(data), tmp_path, "acme-sales-dashboard")
    assert sr.main(["generate", "acme-sales-dashboard", "--dir", str(tmp_path)]) == 0
    assert sr.main(["check", "acme-sales-dashboard", "--dir", str(tmp_path)]) == 0
    page = tmp_path / "apps/acme-sales-dashboard/sql_review/01_overview.sql"
    lines = page.read_text(encoding="utf-8").split("\n")
    assert lines[8] == "--1_example_metric" and lines[9] == "WITH params AS ("


def test_skeleton_app_with_no_metrics_is_clean(tmp_path: Path) -> None:
    """A page listed with `metrics: []` is not a provenance failure: no file is due."""
    from streamsnow.config import Config
    from streamsnow.scaffolder import scaffold
    from tests.test_init import EXAMPLE_CONFIG

    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    scaffold(Config.from_dict(data), tmp_path, "skel-app")
    a = tmp_path / "apps/skel-app"
    index = yaml.safe_load((a / "sql_review/index.yaml").read_text(encoding="utf-8"))
    index["pages"][0]["metrics"] = []
    (a / "sql_review/index.yaml").write_text(yaml.safe_dump(index), encoding="utf-8")
    (a / "queries/example_metric.sql").unlink()
    page = a / "pages/overview.py"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            'review_value("example_metric", "1,234")', '"1,234"'
        ),
        encoding="utf-8",
    )
    assert sr.main(["generate", "skel-app", "--dir", str(tmp_path)]) == 0
    assert sr.main(["check", "skel-app", "--dir", str(tmp_path)]) == 0
    assert not sr._generated_page_files(a)
