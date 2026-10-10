"""Tests for metric summaries: ``summary:`` in index.yaml, ``--N_key_summary`` sections.

A summary is a SELECT over ``detail``, the app query's result, returning
exactly the values a visual shows. What is pinned here:

- the detail section is untouched: an index without ``summary:`` renders byte
  for byte as before, and adding one changes only the header's metrics line and
  appends a section;
- the summary section is one read-only statement: the params CTE, the query's
  own CTEs, its final SELECT as ``detail``, then the summary, merged into one
  ``WITH`` whether the query or the summary starts with one;
- ``check`` refuses a summary that is not a single read-only SELECT reading
  ``detail``, lints it at its index.yaml line, and keeps provenance honest;
- ``probe``/``run`` measure summary sections as ``NN#ns``; ``bench`` ignores them;
  ``compare`` holds the visual to the summary when the metric has one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import test_sql_review as base
import test_sql_review_compare as ct
import test_sql_review_live as lt
import yaml

from streamsnow.tools import sql_review as sr
from streamsnow.tools import sql_review_index as sri
from streamsnow.tools import sql_review_live as live

SLUG = base.SLUG

KPI_SUMMARY = """-- The Revenue tile, rounded to whole dollars as shown
SELECT ROUND(SUM(revenue), 0) AS revenue_shown
FROM detail
"""

TREND_SUMMARY = """-- The trend chart: weekly totals, as the chart buckets them
WITH
-- One row per week
weekly AS (
    SELECT
        DATE_TRUNC('WEEK', order_date) AS week_start,
        SUM(revenue) AS revenue
    FROM detail
    GROUP BY week_start
)

SELECT
    week_start,
    revenue
FROM weekly
ORDER BY week_start
"""

repo = base.repo  # the shared tmp_path app fixture


def _with_summaries(**by_key: object) -> dict:
    data = base._index()
    for page in data["pages"]:
        for m in page["metrics"]:
            if m["key"] in by_key:
                m["summary"] = by_key[m["key"]]
    return data


def _sections(text: str) -> dict[str, str]:
    """Tag (without ``--``) -> the section's SQL; the description line above a
    summary tag is not part of the section before it."""
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.split("\n")[sr.HEADER_LINES :]:
        if line.startswith("-- Provenance:"):
            break
        if re.fullmatch(r"--\d+_[a-z0-9_]+", line):
            while current and (not current[-1].strip() or current[-1].startswith("--")):
                current.pop()
            current = out.setdefault(line[2:], [])
            continue
        if current is not None:
            current.append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


class _BlockDumper(yaml.SafeDumper):
    """Multi-line strings as ``|`` blocks, the way a person writes a summary."""


_BlockDumper.add_representer(
    str,
    lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|" if "\n" in v else None),
)


def _write_block_index(repo: Path, data: dict) -> None:
    path = base._app(repo) / "sql_review" / "index.yaml"
    path.write_text(yaml.dump(data, Dumper=_BlockDumper, sort_keys=False), encoding="utf-8")


def _index_findings(repo: Path) -> list[str]:
    return [f["detail"] for f in sri.load_index(base._app(repo)).findings]


def _indented(sql: str) -> str:
    return "\n".join("    " + ln if ln else ln for ln in sql.split("\n"))


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_without_a_summary_nothing_changes(repo: Path) -> None:
    base._generate(repo)
    before = base._page(repo).read_text(encoding="utf-8")
    base._write_index(repo, _with_summaries(total_revenue=KPI_SUMMARY))
    base._generate(repo)
    base._write_index(repo, base._index())
    base._generate(repo)
    assert base._page(repo).read_text(encoding="utf-8") == before
    assert "summary" not in before


def test_adding_a_summary_keeps_the_detail_section_byte_identical(repo: Path) -> None:
    base._generate(repo)
    before = base._page(repo).read_text(encoding="utf-8")
    base._write_index(repo, _with_summaries(total_revenue=KPI_SUMMARY))
    assert base._generate(repo) == 0
    after = base._page(repo).read_text(encoding="utf-8")
    old, new = _sections(before), _sections(after)
    assert list(new) == ["1_total_revenue", "1_total_revenue_summary", "2_revenue_trend"]
    assert new["1_total_revenue"] == old["1_total_revenue"]
    assert new["2_revenue_trend"] == old["2_revenue_trend"]
    lines = after.split("\n")
    assert lines[5] == "-- Metrics: 1 total_revenue (+summary), 2 revenue_trend"
    assert lines[8] == "--1_total_revenue"  # the first tag stays on line 9
    # Detail, one blank line, the description, the tag, then the SQL.
    assert (
        ";\n\n-- The Revenue tile, rounded to whole dollars as shown\n"
        "--1_total_revenue_summary\nWITH detail AS (\n    WITH params AS (\n"
    ) in after


def test_the_detail_cte_holds_the_detail_section_verbatim(repo: Path) -> None:
    base._write_index(repo, _with_summaries(total_revenue=KPI_SUMMARY))
    base._generate(repo)
    sections = _sections(base._page(repo).read_text(encoding="utf-8"))
    detail = sections["1_total_revenue"].removesuffix(";")
    summary = sections["1_total_revenue_summary"]
    assert summary == (
        f"WITH detail AS (\n{_indented(detail)}\n)\n\n"
        "SELECT ROUND(SUM(revenue), 0) AS revenue_shown\nFROM detail;"
    )
    # Tokens and binds as the detail section has them.
    assert "AND region = 'West'" in summary
    assert "BETWEEN (SELECT start_date FROM params) AND (SELECT end_date FROM params)" in summary
    assert sr._verify_read_only(summary) == [] and sr._verify_binds_bound(summary) == []


def test_a_with_in_the_summary_continues_the_cte_list(repo: Path) -> None:
    base._write_index(repo, _with_summaries(revenue_trend=TREND_SUMMARY))
    base._generate(repo)
    sections = _sections(base._page(repo).read_text(encoding="utf-8"))
    detail = sections["2_revenue_trend"].removesuffix(";")
    summary = sections["2_revenue_trend_summary"]
    assert summary.startswith(f"WITH detail AS (\n{_indented(detail)}\n),\n\n-- One row per week\n")
    assert "\nweekly AS (\n" in summary and summary.endswith("ORDER BY week_start;")
    assert sr._verify_read_only(summary) == []


def test_a_leading_comma_continues_the_cte_list_too(repo: Path) -> None:
    summary = (
        "-- The tile, from a helper CTE\n"
        ", totals AS (\n    SELECT SUM(revenue) AS r FROM detail\n)\n\nSELECT r FROM totals\n"
    )
    base._write_index(repo, _with_summaries(total_revenue=summary))
    assert base._generate(repo) == 0
    section = _sections(base._page(repo).read_text(encoding="utf-8"))["1_total_revenue_summary"]
    assert "\n),\n\ntotals AS (\n" in section and section.endswith("FROM totals;")


def test_a_comment_above_the_first_summary_cte_is_kept(repo: Path) -> None:
    summary = (
        "-- The tile, from a helper CTE\n"
        "-- Totals of the detail rows\n"
        ", totals AS (\n    SELECT SUM(revenue) AS r FROM detail\n)\n\nSELECT r FROM totals\n"
    )
    base._write_index(repo, _with_summaries(total_revenue=summary))
    assert base._generate(repo) == 0
    section = _sections(base._page(repo).read_text(encoding="utf-8"))["1_total_revenue_summary"]
    assert "\n),\n\n-- Totals of the detail rows\ntotals AS (\n" in section


def test_a_trailing_comment_does_not_swallow_the_semicolon(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    summary = "-- The tile\nSELECT SUM(revenue) AS revenue_shown\nFROM detail  -- one row\n"
    base._write_index(repo, _with_summaries(total_revenue=summary))
    assert base._generate(repo) == 0
    sections = _sections(base._page(repo).read_text(encoding="utf-8"))
    assert sections["1_total_revenue_summary"].endswith("-- one row\n;")
    assert sections["2_revenue_trend"].startswith("WITH params AS (")
    assert base._check(repo) == 0, capsys.readouterr().out


def test_lint_lines_survive_a_blank_line_and_key_order(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    data = _with_summaries(total_revenue="-- The tile\n\nselect SUM(revenue) AS r\nFROM detail\n")
    metric = data["pages"][0]["metrics"][0]
    data["pages"][0]["metrics"][0] = {"summary": metric.pop("summary"), **metric}  # before key:
    data["pages"][0]["metrics"][1]["summary"] = TREND_SUMMARY
    _write_block_index(repo, data)
    base._generate(repo)
    index_text = (base._app(repo) / "sql_review" / "index.yaml").read_text(encoding="utf-8")
    want = next(i for i, ln in enumerate(index_text.splitlines(), 1) if "select SUM" in ln)
    lint = [f for f in base._findings(repo, capsys) if "CP01" in f["detail"]]
    assert [f["line"] for f in lint] == [want], lint


def test_summary_without_a_review_window(repo: Path) -> None:
    data = _with_summaries(orders_by_region="-- Orders in all\nSELECT SUM(n) AS orders FROM detail")
    data.pop("review_window")
    for page in data["pages"]:
        for m in page["metrics"]:
            m["binds"] = {k: "'2026-01-01'" for k in m.get("binds", {})}
    base._write_index(repo, data)
    assert base._generate(repo) == 0
    section = _sections(base._page(repo, "02_regions.sql").read_text(encoding="utf-8"))[
        "1_orders_by_region_summary"
    ]
    assert section.startswith("WITH detail AS (\n    SELECT\n")
    assert "params" not in section and "order_date >= '2026-01-01'" in section


def test_a_trailing_semicolon_and_non_ascii_literals(repo: Path) -> None:
    summary = (
        "-- The tile with its markers\n"
        "SELECT\n    IFF(SUM(revenue) > 0, '▲', '') AS grew,\n    '★' AS star\nFROM detail;\n"
    )
    base._write_index(repo, _with_summaries(total_revenue=summary))
    assert base._generate(repo) == 0
    raw = base._page(repo).read_bytes().decode("utf-8")
    section = _sections(raw)["1_total_revenue_summary"]
    assert "'▲'" in section and "'★'" in section and section.count(";") == 1
    assert base._check(repo) == 0


def test_a_multi_line_literal_in_the_query_is_not_reindented(repo: Path) -> None:
    q = base._app(repo) / "queries" / "revenue.sql"
    q.write_text(
        q.read_text(encoding="utf-8").replace(
            "SELECT SUM(revenue) AS revenue", "SELECT SUM(revenue) AS revenue, 'a\nb' AS note"
        ),
        encoding="utf-8",
    )
    base._write_index(repo, _with_summaries(total_revenue=KPI_SUMMARY))
    assert base._generate(repo) == 0
    section = _sections(base._page(repo).read_text(encoding="utf-8"))["1_total_revenue_summary"]
    assert "'a\nb' AS note" in section  # not 'a\n    b'


def test_round_trip_is_clean_and_the_readme_names_summaries(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    base._write_index(repo, _with_summaries(total_revenue=KPI_SUMMARY, revenue_trend=TREND_SUMMARY))
    assert base._generate(repo) == 0
    assert base._check(repo) == 0, capsys.readouterr().out
    readme = (base._app(repo) / "sql_review" / "README.md").read_text(encoding="utf-8")
    assert "| `01_overview.sql` | 1 | `total_revenue` + summary |" in readme
    assert "| `02_regions.sql` | 1 | `orders_by_region` |" in readme


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
def test_editing_a_summary_reads_as_drift(repo: Path, capsys: pytest.CaptureFixture) -> None:
    base._write_index(repo, _with_summaries(total_revenue=KPI_SUMMARY))
    base._generate(repo)
    base._write_index(repo, _with_summaries(total_revenue=KPI_SUMMARY.replace("0) AS", "2) AS")))
    assert any("DRIFT" in f["detail"] for f in base._findings(repo, capsys))


def test_hand_editing_a_summary_section_reads_as_edited(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    base._write_index(repo, _with_summaries(total_revenue=KPI_SUMMARY))
    base._generate(repo)
    page = base._page(repo)
    page.write_text(
        page.read_text(encoding="utf-8").replace("revenue_shown", "revenue_typed"),
        encoding="utf-8",
    )
    assert any("edited by hand" in f["detail"] for f in base._findings(repo, capsys))


def test_a_hand_added_write_in_a_summary_section_is_refused(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    base._write_index(repo, _with_summaries(total_revenue=KPI_SUMMARY))
    base._generate(repo)
    page = base._page(repo)
    text = page.read_text(encoding="utf-8").replace(
        "FROM detail;", "FROM detail;\nDELETE FROM ANALYTICS_DB.REPORTING.ORDERS;"
    )
    page.write_text(text, encoding="utf-8")
    assert sr.KIND_READONLY in base._kinds(repo, capsys)


# --------------------------------------------------------------------------- #
# check: what a summary may be
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("summary", "detail"),
    [
        ("-- d\nDELETE FROM detail", "must be a SELECT"),
        ("-- d\nSELECT 1 AS a FROM detail; SELECT 2 AS b FROM detail", "more than one statement"),
        ("-- d\nSELECT 1 AS a", "never reads detail"),
        ("-- d\nSELECT 'detail' AS a FROM t", "never reads detail"),
        ("-- d\nWITH d AS (SELECT * FROM detail) DELETE FROM t", "must be a SELECT"),
        ("-- d\nSELECT * FROM detail WHERE region = {REGION}", "takes no tokens"),
        ("-- d\nSELECT * FROM detail WHERE n > :1", "takes no binds"),
        ("-- d\nWITH detail AS (SELECT 1 AS x) SELECT x FROM detail", "CTE named detail"),
        ("-- d\nSELECT * FROM detail, params", "reads params, which lives inside detail"),
        ("-- d\nSELECT * FROM detail WHERE x = 'it''s", "unterminated"),
        ("SELECT SUM(revenue) AS r FROM detail", "needs a one-line description first"),
        ("-- " + "x" * 100 + "\nSELECT * FROM detail", "100 characters or fewer"),
        ("--\nSELECT * FROM detail", "100 characters or fewer"),
        ("--2_revenue_trend\nSELECT * FROM detail", "100 characters or fewer"),
        ("-- d\nSELECT * FROM detail\n--2_revenue_trend\n", "shaped like a section tag"),
        ("-- d\nWITH detail (a) AS (SELECT 1) SELECT a FROM detail", "CTE named detail"),
        ("-- Provenance: schema=2\nSELECT * FROM detail", "100 characters or fewer"),
        ("-- only a description", "no SQL after its description"),
        ("   ", "must be a SQL SELECT"),
        (["SELECT 1"], "must be a SQL SELECT"),
    ],
)
def test_an_invalid_summary_is_an_index_finding(repo: Path, summary: object, detail: str) -> None:
    base._write_index(repo, _with_summaries(total_revenue=summary))
    found = _index_findings(repo)
    assert any(detail in f and "total_revenue" in f for f in found), found


def test_a_summary_may_define_its_own_params_cte(repo: Path) -> None:
    summary = (
        "-- The tile against a fixed target\n"
        "WITH\n-- The target\nparams AS (SELECT 100 AS target)\n\n"
        "SELECT SUM(revenue) - MAX(target) AS gap FROM detail, params\n"
    )
    base._write_index(repo, _with_summaries(total_revenue=summary))
    assert not any("total_revenue" in f for f in _index_findings(repo))


def test_a_query_with_its_own_detail_cte_still_takes_a_summary(repo: Path) -> None:
    trend = base._app(repo) / "queries" / "trend.sql"
    trend.write_text(trend.read_text(encoding="utf-8").replace("daily", "detail"), encoding="utf-8")
    base._write_index(repo, _with_summaries(revenue_trend=TREND_SUMMARY))
    assert not any("revenue_trend" in f for f in _index_findings(repo))
    assert base._generate(repo) == 0  # the query's detail is scoped inside the outer one


def test_a_summary_needs_a_select_query(repo: Path) -> None:
    q = base._app(repo) / "queries" / "by_region.sql"
    q.write_text("-- Query: by_region\nSHOW TABLES IN SCHEMA ANALYTICS_DB.APP\n", encoding="utf-8")
    data = _with_summaries(orders_by_region="-- d\nSELECT * FROM detail")
    data["pages"][1]["metrics"][0].pop("binds")
    base._write_index(repo, data)
    assert any("is not a SELECT or WITH statement" in f for f in _index_findings(repo))


def test_generate_refuses_an_invalid_summary(repo: Path, capsys: pytest.CaptureFixture) -> None:
    base._write_index(repo, _with_summaries(total_revenue="-- d\nDROP TABLE detail"))
    capsys.readouterr()
    assert base._generate(repo) == 2
    assert "summary" in capsys.readouterr().err
    assert not base._page(repo).exists()


def test_a_summary_is_linted_at_its_index_line(repo: Path, capsys: pytest.CaptureFixture) -> None:
    summary = "-- The tile\nselect SUM(revenue) AS r\nFROM detail\n"
    _write_block_index(repo, _with_summaries(total_revenue=summary))
    base._generate(repo)
    index_text = (base._app(repo) / "sql_review" / "index.yaml").read_text(encoding="utf-8")
    want = next(i for i, ln in enumerate(index_text.splitlines(), 1) if "select SUM" in ln)
    lint = [f for f in base._findings(repo, capsys) if f["kind"] == sr.KIND_LINT]
    assert lint and all(f["file"].endswith("sql_review/index.yaml") for f in lint)
    assert any(f["line"] == want and "CP01" in f["detail"] for f in lint), lint


def test_a_summary_cte_needs_its_comment(repo: Path, capsys: pytest.CaptureFixture) -> None:
    summary = "-- The tile\nWITH\nt AS (\n    SELECT SUM(revenue) AS r FROM detail\n)\n\nSELECT r FROM t\n"
    base._write_index(repo, _with_summaries(total_revenue=summary))
    base._generate(repo)
    found = base._findings(repo, capsys)
    assert any(
        f["kind"] == sr.KIND_COMMENTS and "summary of 'total_revenue'" in f["detail"] for f in found
    )


# --------------------------------------------------------------------------- #
# Live: probe, run, bench, log
# --------------------------------------------------------------------------- #
class SummarySnow(lt.FakeSnow):
    def __init__(self) -> None:
        super().__init__()
        # Later keys win: a summary section also holds its query's text.
        self.columns["FROM detail"] = [("REVENUE_SHOWN", "NUMBER(38,0)"), ("PRIOR", "NUMBER(38,0)")]
        self.measures["FROM detail"] = {
            "__ROWS": "1",
            "__HASH": "444",
            "__C1_N": "1",
            "__C1_SUM": "12346",
            "__C2_N": "1",
            "__C2_SUM": "10000",
        }


@pytest.fixture()
def live_repo(tmp_path: Path) -> Path:
    root = lt._make_repo(tmp_path)
    (root / "streamsnow.config.yaml").write_text(lt.CONFIG, encoding="utf-8")
    index = root / "apps" / SLUG / "sql_review" / "index.yaml"
    index.write_text(yaml.safe_dump(_with_summaries(total_revenue=KPI_SUMMARY)), encoding="utf-8")
    assert sr.main(["generate", SLUG, "--dir", str(root)]) == 0
    return root


def test_page_sections_include_summaries_after_their_detail(live_repo: Path) -> None:
    app, index = live.load_app(live_repo, SLUG)
    sections = live.page_sections(live_repo, app, index, "01")
    assert [s.ref for s in sections] == ["01#1", "01#1s", "01#2"]
    summary = sections[1]
    assert summary.summary and summary.metric.key == "total_revenue"
    assert summary.base()["summary"] is True and "summary" not in sections[0].base()
    assert "FROM detail" in summary.sql and not summary.sql.endswith(";")


def test_run_measures_the_summary_section(live_repo: Path, capsys: pytest.CaptureFixture) -> None:
    assert lt._live(live_repo, SummarySnow(), "run") == 0
    by_id = {r["id"]: r for r in lt._out(capsys)["results"]}
    assert set(by_id) == {"run:01#1", "run:01#1s", "run:01#2", "run:02#1"}
    s = by_id["run:01#1s"]
    assert (s["n"], s["key"], s["summary"]) == (1, "total_revenue", True)
    assert s["totals"] == {"REVENUE_SHOWN": "12346", "PRIOR": "10000"}
    assert "summary" not in by_id["run:01#1"]


def test_probe_compiles_the_summary_section(live_repo: Path, capsys: pytest.CaptureFixture) -> None:
    lt._live(live_repo, SummarySnow(), "probe")
    by_id = {r["id"]: r for r in lt._out(capsys)["results"]}
    assert by_id["probe:01#1s"]["status"] == "pass"
    assert [c["name"] for c in by_id["probe:01#1s"]["columns"]] == ["REVENUE_SHOWN", "PRIOR"]


def test_bench_measures_the_detail_never_the_summary(
    live_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    lt._live(live_repo, SummarySnow(), "bench", "--metric", "01#1")
    out = lt._out(capsys)
    assert out["results"][0]["id"].startswith("bench:01#1:")
    assert out["results"][0]["key"] == "total_revenue"


def test_a_missing_summary_section_is_refused(
    live_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, index = live.load_app(live_repo, SLUG)
    original = live.split_page

    def without_summary(text: str) -> dict:
        return {k: v for k, v in original(text).items() if not k[1].endswith("_summary")}

    monkeypatch.setattr(live, "split_page", without_summary)
    with pytest.raises(live.ToolError, match="no section --1_total_revenue_summary"):
        live.page_sections(live_repo, app, index, "01")


def test_log_shows_the_summary_row(
    live_repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live, "_today", lambda: "2026-10-05")
    fake = SummarySnow()
    lt._live(live_repo, fake, "probe")
    run_id = lt._out(capsys)["run_id"]
    lt._live(live_repo, fake, "run", "--run", run_id)
    capsys.readouterr()
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"findings": []}), encoding="utf-8")
    lt._live(live_repo, fake, "log", "--run", run_id, "--findings", str(findings))
    log = (live_repo / lt._out(capsys)["log"]).read_text(encoding="utf-8")
    assert "| 1 `total_revenue` | pass | 1 | REVENUE = 12345.67 | n/a | — |" in log
    assert (
        "| 1 `total_revenue` summary | pass | 1 | REVENUE_SHOWN = 12346, PRIOR = 10000 | — | — |"
        in log
    )


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #
@pytest.fixture()
def summary_run_dir(live_repo: Path, capsys: pytest.CaptureFixture) -> Path:
    assert lt._live(live_repo, SummarySnow(), "run") == 0
    capsys.readouterr()
    return lt._run_dir(live_repo)


def test_compare_holds_the_visual_to_the_summary(
    summary_run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    # The tile shows the summary's first column; the detail total would not match.
    ct._capture(summary_run_dir, "overview", "total_revenue", "text", headline={"value": "$12,346"})
    rc, out = ct._compare(summary_run_dir, capsys)
    r = ct._by_id(out)["compare:01#1"]
    assert (r["status"], r["rule"], r["against"]) == ("match", "first_column", "summary")
    assert rc == 0
    assert ct._by_id(out)["compare:01#2"]["against"] == "detail"
    assert not any(r["id"].endswith("s") for r in out["results"])  # one verdict per metric


def test_compare_against_the_summary_catches_a_wrong_number(
    summary_run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    ct._capture(summary_run_dir, "overview", "total_revenue", "text", headline={"value": "$10,000"})
    rc, out = ct._compare(summary_run_dir, capsys)
    assert ct._by_id(out)["compare:01#1"]["status"] == "mismatch"
    assert rc == 1


def test_compare_falls_back_to_the_detail_for_a_run_without_the_summary(
    summary_run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    path = summary_run_dir / "run-01.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["results"] = [r for r in data["results"] if not r.get("summary")]
    path.write_text(json.dumps(data), encoding="utf-8")
    ct._capture(
        summary_run_dir, "overview", "total_revenue", "text", headline={"value": "$12,345.67"}
    )
    _, out = ct._compare(summary_run_dir, capsys)
    r = ct._by_id(out)["compare:01#1"]
    assert (r["status"], r["against"]) == ("match", "detail")
    assert any("predates the summary" in w for w in out["warnings"])
