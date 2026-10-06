"""Tests for the live SQL review: ``sql-review probe | run | bench | log``.

No Snowflake: a fake ``snow`` runner reads the script the executor would send
and answers each statement from canned rows. What is pinned here:

- every result carries a stable id (``probe:01#1``, ``probe:<FQN>``,
  ``run:01#1``, ``bench:01#1:before``) and lands in the run directory, which
  ignores itself in git;
- only aggregates travel: the measure statement is a ``COUNT(*)``/``HASH_AGG``/
  ``SUM`` wrapper, and no data row is ever written;
- stale page files, the scaffold placeholder and denied schemas stop the run
  before anything is sent;
- one failing section does not sink the others;
- ``log`` refuses uncited or unknown evidence, withholds small-group totals,
  writes the fixed sections with a blank sign-off, and links the README.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import test_sql_review as base
import yaml

from streamsnow.tools import sql_review as sr
from streamsnow.tools import sql_review_live as live

SLUG = base.SLUG
ROLLUP = base.ROLLUP

CONFIG = """\
schema_version: 1
runtime: warehouse
project:
  name: "Acme Sales"
  slug: "acme-sales"
snowflake:
  account: "ab12345.us-east-1"
  connection_name: "acme"
  objects:
    app_database: "STREAMSNOW_APPS"
    app_schema: "DASHBOARDS"
    stage_database: "STREAMSNOW_APPS"
    stage_schema: "DASHBOARDS"
    default_warehouse: "STREAMSNOW_WH"
  roles:
    ci_role: "STREAMSNOW_DEPLOY_ROLE"
    viewer_role: "STREAMSNOW_VIEWER_ROLE"
governance:
  database: "ANALYTICS_DB"
  schema_allow: ["REPORTING", "APP"]
  schema_deny: ["RAW"]
"""

LIVE_VIEW_DDL = (
    "create or replace view ANALYTICS_DB.APP.REGION_ROLLUP as\n"
    "select region, order_date, count(*) as n from ANALYTICS_DB.REPORTING.ORDERS group by 1, 2;"
)


def qid(n: int) -> str:
    """A query id shaped like Snowflake's."""
    return f"01c00000-0000-0000-0000-{n:012x}"


class FakeSnow:
    """Answers ``snow sql --stdin --format json`` the way snow 3.x prints it.

    ``columns`` and ``measures`` are keyed by a substring of the section SQL
    (the query's table or a distinctive token). ``fail`` lists substrings
    whose statement makes the whole call fail, like a Snowflake error.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.columns = {
            "SUM(revenue) AS revenue\nFROM ANALYTICS_DB.REPORTING.ORDERS": [
                ("REVENUE", "NUMBER(38,2)")
            ],
            "FROM daily": [("ORDER_DATE", "DATE"), ("REVENUE", "NUMBER(38,2)")],
            "REGION_ROLLUP": [("REGION", "VARCHAR(16777216)"), ("N", "NUMBER(18,0)")],
        }
        self.measures = {
            "SUM(revenue) AS revenue\nFROM ANALYTICS_DB.REPORTING.ORDERS": {
                "__ROWS": "1",
                "__HASH": "111",
                "__C1_N": "1",
                "__C1_SUM": "12345.67",
            },
            "FROM daily": {"__ROWS": "30", "__HASH": "222", "__C2_N": "30", "__C2_SUM": "9999.00"},
            "REGION_ROLLUP": {"__ROWS": "4", "__HASH": "333", "__C2_N": "4", "__C2_SUM": "120"},
        }
        self.show = {"ORDERS": "TABLE", "REGION_ROLLUP": "VIEW"}
        self.grants = {"REGION_ROLLUP": ["ROLE_APP_READER", "STREAMSNOW_DEPLOY_ROLE"]}
        self.ddl = {"REGION_ROLLUP": LIVE_VIEW_DDL}
        self.fail: list[str] = []
        self.qid = 0
        self.last_describe = ""

    def __call__(self, argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
        self.calls.append((argv, stdin))
        stmts = [s.strip() for s in stdin.split("\n;\n") if s.strip()]
        out: list[list[dict]] = []
        for stmt in stmts:
            if any(f in stmt for f in self.fail):
                return (
                    1,
                    "",
                    "╭─ Error ─╮\n│ 002003 (42S02): SQL compilation error: does not exist │\n╰─╯",
                )
            out.append(self.answer(stmt))
        return 0, json.dumps(out), ""

    def _key(self, stmt: str, table: dict) -> str:
        return next(k for k in reversed(table) if k in stmt)  # later keys win

    def answer(self, stmt: str) -> list[dict]:
        if stmt.startswith(("ALTER SESSION", "USE ")):
            return [{"status": "Statement executed successfully."}]
        if stmt.startswith("SELECT CURRENT_ROLE()"):
            return [
                {
                    "ROLE": "STREAMSNOW_DEPLOY_ROLE",
                    "WAREHOUSE": "STREAMSNOW_WH",
                    "SECONDARY_ROLES": '{"roles":"","value":""}',
                }
            ]
        if stmt.endswith("WHERE 1 = 0"):
            self.last_describe = stmt
            return []
        if stmt == "DESCRIBE RESULT LAST_QUERY_ID()":
            cols = self.columns[self._key(self.last_describe, self.columns)]
            return [{"name": n, "type": t, "kind": "COLUMN"} for n, t in cols]
        if stmt.startswith('SELECT\n    COUNT(*) AS "__ROWS"'):
            self.qid += 1
            row = dict(self.measures[self._key(stmt, self.measures)])
            if "SUM(" not in stmt:
                row = {k: v for k, v in row.items() if not k.endswith(("_SUM", "_N"))}
            return [row]
        if stmt.startswith("SELECT LAST_QUERY_ID()"):
            return [{"QUERY_ID": qid(self.qid)}]
        if "QUERY_HISTORY_BY_USER" in stmt:
            wanted = re.findall(r"'([0-9a-f-]{36})'", stmt)
            return [
                {"QUERY_ID": q, "TOTAL_ELAPSED_TIME": str(100 * (i + 1)), "BYTES_SCANNED": "2048"}
                for i, q in enumerate(wanted)
            ]
        if "GET_QUERY_OPERATOR_STATS" in stmt:
            return [{"PARTITIONS_SCANNED": "3", "PARTITIONS_TOTAL": "10"}]
        if stmt.startswith("SHOW OBJECTS LIKE"):
            name = re.search(r"LIKE '([^']*)'", stmt).group(1)
            kind = self.show.get(name)
            return [{"name": name, "kind": kind, "is_dynamic": "N"}] if kind else []
        if stmt.startswith("SHOW GRANTS ON"):
            name = stmt.rsplit(".", 1)[-1]
            return [
                {"privilege": "SELECT", "grantee_name": g, "granted_to": "ROLE"}
                for g in self.grants.get(name, [])
            ]
        if stmt.startswith("SELECT GET_DDL("):
            name = re.search(r"\.([A-Z_]+)'\)", stmt).group(1)
            return [{"DDL": self.ddl[name]}]
        raise AssertionError(f"unexpected statement: {stmt[:80]}")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path)
    (root / "streamsnow.config.yaml").write_text(CONFIG, encoding="utf-8")
    assert sr.main(["generate", SLUG, "--dir", str(root)]) == 0
    return root


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    a = root / "apps" / SLUG
    for d in ("queries", "pages", f"sql_review/{sr.OBJECTS_DIR}"):
        (a / d).mkdir(parents=True)
    (a / "snowflake.yml").write_text("definition_version: 2\n", encoding="utf-8")
    (a / "streamlit_app.py").write_text(base.NAV, encoding="utf-8")
    for name, text in (
        ("revenue", base.REVENUE),
        ("trend", base.TREND),
        ("by_region", base.BY_REGION),
    ):
        (a / "queries" / f"{name}.sql").write_text(text, encoding="utf-8")
    (a / "pages" / "overview.py").write_text(base.OVERVIEW, encoding="utf-8")
    (a / "pages" / "regions.py").write_text(base.REGIONS, encoding="utf-8")
    (a / "sql_review" / sr.OBJECTS_DIR / f"{ROLLUP}.sql").write_text(base.DDL, encoding="utf-8")
    (a / "sql_review" / "index.yaml").write_text(yaml.safe_dump(base._index()), encoding="utf-8")
    return root


def _live(repo: Path, fake: FakeSnow, *argv: str) -> int:
    args = sr._build_parser().parse_args([*argv[:1], SLUG, "--dir", str(repo), *argv[1:]])
    return live.dispatch(args, runner=fake)


def _out(capsys: pytest.CaptureFixture) -> dict:
    return json.loads(capsys.readouterr().out)


def _run_dir(repo: Path) -> Path:
    runs = sorted((repo / live.RUNS_DIR / SLUG).iterdir())
    return runs[-1]


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def test_run_reports_aggregates_with_stable_ids(repo: Path, capsys: pytest.CaptureFixture) -> None:
    fake = FakeSnow()
    assert _live(repo, fake, "run") == 0
    out = _out(capsys)
    by_id = {r["id"]: r for r in out["results"]}
    assert set(by_id) == {"run:01#1", "run:01#2", "run:02#1"}
    rev = by_id["run:01#1"]
    assert (rev["rows"], rev["totals"], rev["hash"]) == (1, {"REVENUE": "12345.67"}, "111")
    assert rev["key"] == "total_revenue" and rev["page_file"] == "01_overview.sql"
    assert rev["query_id"] and rev["elapsed_ms"] is not None
    assert by_id["run:01#2"]["totals"] == {"REVENUE": "9999.00"}  # DATE is not a total
    # Three logins for the whole app: compile + columns, the measures, their timings.
    assert len(fake.calls) == 3
    assert "USE_CACHED_RESULT = FALSE" in fake.calls[1][1]  # a cached rerun reads as instant


def test_run_records_distinct_counts_of_key_columns(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    fake = FakeSnow()
    fake.measures["REGION_ROLLUP"]["__C1_DISTINCT"] = "4"
    assert _live(repo, fake, "run") == 0
    by_id = {r["id"]: r for r in _out(capsys)["results"]}
    assert by_id["run:02#1"]["distinct"] == {"REGION": 4}
    assert 'COUNT(DISTINCT $1) AS "__C1_DISTINCT"' in fake.calls[1][1]
    assert "COUNT(DISTINCT $2)" not in fake.calls[1][1]  # N is a number: a total, not a key


def test_run_output_lists_every_file_it_wrote(repo: Path, capsys: pytest.CaptureFixture) -> None:
    assert _live(repo, FakeSnow(), "run") == 0
    out = _out(capsys)
    run_dir = _run_dir(repo)
    assert out["files"] == [
        (run_dir / "run-01.json").relative_to(repo).as_posix(),
        (run_dir / "run-02.json").relative_to(repo).as_posix(),
    ]
    assert all((repo / f).is_file() for f in out["files"])


def test_run_files_are_per_page_and_ignored_by_git(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    _live(repo, FakeSnow(), "run")
    run_dir = _run_dir(repo)
    assert {p.name for p in run_dir.iterdir()} == {"meta.json", "run-01.json", "run-02.json"}
    assert (repo / live.RUNS_DIR / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["session"]["role"] == "STREAMSNOW_DEPLOY_ROLE"
    assert meta["connection"] == "acme"
    assert live._RUN_ID_RE.match(run_dir.name)


def test_only_aggregate_statements_reach_snowflake(repo: Path) -> None:
    fake = FakeSnow()
    _live(repo, fake, "run")
    sent = "\n".join(stdin for _, stdin in fake.calls)
    reviewed = [
        s.strip()
        for s in sent.split("\n;\n")
        if s.strip() and not s.strip().startswith(("ALTER SESSION", "USE "))
    ]
    for stmt in reviewed:
        assert (
            stmt.endswith("WHERE 1 = 0")
            or stmt.startswith(('SELECT\n    COUNT(*) AS "__ROWS"', "DESCRIBE RESULT"))
            or stmt.startswith(("SELECT LAST_QUERY_ID()", "SELECT CURRENT_ROLE()"))
            or "QUERY_HISTORY_BY_USER" in stmt
        ), stmt[:80]


def test_the_session_is_pinned(repo: Path) -> None:
    fake = FakeSnow()
    _live(repo, fake, "run")
    argv, stdin = fake.calls[0]
    assert argv[:2] == ["snow", "sql"] and "--stdin" in argv
    assert "--connection=acme" in argv
    assert argv[argv.index("--enable-templating") + 1] == "NONE"
    head = stdin.split("\n;\n")[:5]
    assert head == [
        "ALTER SESSION SET QUERY_TAG = 'streamsnow:sql-review:acme-sales'",
        "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 120",
        "USE ROLE STREAMSNOW_DEPLOY_ROLE",
        "USE SECONDARY ROLES NONE",
        "USE WAREHOUSE STREAMSNOW_WH",
    ]


def test_role_flag_overrides_the_ci_role(repo: Path) -> None:
    fake = FakeSnow()
    _live(repo, fake, "run", "--role", "ACME_ANALYST", "--timeout", "30")
    stdin = fake.calls[0][1]
    assert "USE ROLE ACME_ANALYST" in stdin and "STATEMENT_TIMEOUT_IN_SECONDS = 30" in stdin


def test_one_failing_section_does_not_sink_the_others(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    fake = FakeSnow()
    fake.fail = ["REGION_ROLLUP"]
    assert _live(repo, fake, "run") == 1
    by_id = {r["id"]: r for r in _out(capsys)["results"]}
    assert by_id["run:02#1"]["status"] == "fail"
    assert "does not exist" in by_id["run:02#1"]["detail"]
    assert by_id["run:01#1"]["status"] == "pass" and by_id["run:01#2"]["status"] == "pass"


def test_page_filter_runs_one_page(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _live(repo, FakeSnow(), "run", "--page", "02")
    assert [r["id"] for r in _out(capsys)["results"]] == ["run:02#1"]


def test_slow_sections_are_flagged(repo: Path, capsys: pytest.CaptureFixture) -> None:
    _live(repo, FakeSnow(), "run", "--slow-s", "0")
    assert all(r["slow"] for r in _out(capsys)["results"])


def test_a_stale_page_file_is_refused_before_anything_is_sent(repo: Path) -> None:
    q = repo / "apps" / SLUG / "queries" / "revenue.sql"
    q.write_text(q.read_text(encoding="utf-8") + "-- edited\n", encoding="utf-8")
    fake = FakeSnow()
    with pytest.raises(live.ToolError, match="do not match what the app runs"):
        _live(repo, fake, "run")
    assert fake.calls == []


def test_the_scaffold_placeholder_is_refused(repo: Path) -> None:
    idx = repo / "apps" / SLUG / "sql_review" / "index.yaml"
    idx.write_text(idx.read_text(encoding="utf-8") + "# YOUR_TABLE\n", encoding="utf-8")
    with pytest.raises(live.ToolError, match="YOUR_TABLE"):
        _live(repo, FakeSnow(), "run")


def test_a_denied_schema_in_a_section_is_never_sent(repo: Path) -> None:
    cfg = repo / "streamsnow.config.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace('["RAW"]', '["REPORTING"]'), encoding="utf-8"
    )
    fake = FakeSnow()
    with pytest.raises(live.ToolError, match="denied schema"):
        _live(repo, fake, "run")
    assert fake.calls == []


def test_no_connection_is_a_tool_error(repo: Path) -> None:
    (repo / "streamsnow.config.yaml").unlink()
    with pytest.raises(live.ToolError, match="no Snowflake connection"):
        _live(repo, FakeSnow(), "run")


def test_main_maps_tool_errors_to_exit_2(repo: Path, capsys: pytest.CaptureFixture) -> None:
    (repo / "streamsnow.config.yaml").unlink()
    assert sr.main(["run", SLUG, "--dir", str(repo)]) == 2
    assert "no Snowflake connection" in capsys.readouterr().err


def test_runs_share_a_directory_with_run_id(repo: Path, capsys: pytest.CaptureFixture) -> None:
    fake = FakeSnow()
    _live(repo, fake, "probe")
    run_id = _out(capsys)["run_id"]
    _live(repo, fake, "run", "--run", run_id, "--page", "01")
    assert _out(capsys)["run_id"] == run_id
    assert (_run_dir(repo) / "run-01.json").is_file()
    assert len(list((repo / live.RUNS_DIR / SLUG).iterdir())) == 1


@pytest.mark.parametrize("bad", ["../../etc", "latest/..", "2026-10-05", "x" * 8])
def test_run_ids_are_validated(repo: Path, bad: str) -> None:
    with pytest.raises(live.ToolError, match="not a run id"):
        _live(repo, FakeSnow(), "run", "--run", bad)


# --------------------------------------------------------------------------- #
# SQL shapes
# --------------------------------------------------------------------------- #
def test_measure_uses_positions_and_a_name_sorted_hash() -> None:
    cols = [
        live.Column(1, "B", "NUMBER(38,0)"),
        live.Column(2, "A", "VARCHAR"),
        live.Column(3, "B", "FLOAT"),
    ]
    sql = live.measure_sql("SELECT 1 -- trailing comment", cols)
    assert "TO_VARCHAR(HASH_AGG($2, $1, $3))" in sql
    assert 'TO_VARCHAR(SUM($1)) AS "__C1_SUM"' in sql and "SUM($3)" in sql and "SUM($2)" not in sql
    assert sql.endswith("SELECT 1 -- trailing comment\n)")  # the paren survives the comment
    parsed = live.parse_measure(
        {"__ROWS": "2", "__HASH": "9", "__C1_SUM": "5", "__C3_SUM": None}, cols
    )
    assert parsed["totals"] == {"B": "5", "B#2": None}
    assert parsed["float_columns"] == ["B"]
    assert "DISTINCT" not in sql and "distinct" not in parsed


def test_measure_counts_distinct_values_of_key_shaped_columns() -> None:
    """compare trusts a grouped frame only if it keeps every distinct key: the
    run counts them for text, date, time and boolean columns (never a value)."""
    cols = [
        live.Column(1, "REGION", "VARCHAR(16777216)"),
        live.Column(2, "REVENUE", "NUMBER(38,2)"),
        live.Column(3, "DAY", "DATE"),
        live.Column(4, "PAYLOAD", "VARIANT"),
        live.Column(5, "REGION", "TEXT"),
    ]
    sql = live.measure_sql("SELECT 1", cols, distinct=True)
    assert 'COUNT(DISTINCT $1) AS "__C1_DISTINCT"' in sql
    assert 'COUNT(DISTINCT $3) AS "__C3_DISTINCT"' in sql
    assert "DISTINCT $2" not in sql and "DISTINCT $4" not in sql
    row = {"__ROWS": "72", "__C1_DISTINCT": "6", "__C3_DISTINCT": "12", "__C5_DISTINCT": "2"}
    assert live.parse_measure(row, cols)["distinct"] == {"REGION": 6, "DAY": 12, "REGION#2": 2}


def test_split_page_reads_each_tagged_section(repo: Path) -> None:
    text = (repo / "apps" / SLUG / "sql_review" / "01_overview.sql").read_text(encoding="utf-8")
    chunks = live.split_page(text)
    assert set(chunks) == {(1, "total_revenue"), (2, "revenue_trend")}
    assert all(not v.rstrip().endswith(";") for v in chunks.values())
    assert chunks[(1, "total_revenue")].startswith("WITH params AS (")


def test_ddl_drift_ignores_case_whitespace_and_comments_but_not_literals() -> None:
    committed = base.DDL
    assert live.ddl_drift(committed, LIVE_VIEW_DDL)["drift"] is False
    changed = LIVE_VIEW_DDL.replace("group by 1, 2", "where region = 'west' group by 1, 2")
    result = live.ddl_drift(committed, changed)
    assert result["drift"] is True and "'west'" in result["diff"]
    assert live.ddl_drift(committed, LIVE_VIEW_DDL.upper())["drift"] is False


def test_fqn_parts_cannot_break_out_of_a_literal() -> None:
    assert live._lit("it's\\") == "'it''s\\\\'"
    with pytest.raises(live.ToolError):
        live.split_fqn('DB.SCH."x;DROP"')
    assert live.split_fqn('DB.SCH."Mixed Case"') == ("DB", "SCH", '"Mixed Case"')


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def test_probe_checks_sections_objects_grants_and_drift(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    fake = FakeSnow()
    assert _live(repo, fake, "probe") == 0
    by_id = {r["id"]: r for r in _out(capsys)["results"]}
    assert by_id["probe:01#1"]["columns"] == [{"name": "REVENUE", "type": "NUMBER(38,2)"}]
    rollup = by_id[f"probe:{ROLLUP}"]
    assert rollup["exists"] is True and rollup["kind"] == "VIEW"
    assert rollup["grants"]["status"] == "pass"
    assert rollup["drift"] == {"status": "pass", "drift": False, "diff": ""}
    orders = by_id["probe:ANALYTICS_DB.REPORTING.ORDERS"]
    assert orders["exists"] is True and "drift" not in orders
    assert len(fake.calls) == 2


def test_probe_reports_a_missing_object_and_a_drifted_view(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    fake = FakeSnow()
    fake.show.pop("ORDERS")
    fake.ddl["REGION_ROLLUP"] = LIVE_VIEW_DDL.replace("count(*)", "count(distinct region)")
    assert _live(repo, fake, "probe") == 1
    by_id = {r["id"]: r for r in _out(capsys)["results"]}
    orders = by_id["probe:ANALYTICS_DB.REPORTING.ORDERS"]
    assert orders["exists"] is False and "STREAMSNOW_DEPLOY_ROLE" in orders["detail"]
    assert by_id[f"probe:{ROLLUP}"]["drift"]["drift"] is True


def test_probe_missing_direct_grant_is_a_warning_not_a_failure(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    fake = FakeSnow()
    fake.grants["REGION_ROLLUP"] = ["ROLE_APP_READER"]
    assert _live(repo, fake, "probe") == 0
    grants = {r["id"]: r for r in _out(capsys)["results"]}[f"probe:{ROLLUP}"]["grants"]
    assert grants["status"] == "warn" and grants["missing_direct"] == ["STREAMSNOW_DEPLOY_ROLE"]


def test_probe_a_grant_to_public_reaches_every_role(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    """Every role inherits PUBLIC: shared sample data is readable through it alone."""
    fake = FakeSnow()
    fake.grants["REGION_ROLLUP"] = ["PUBLIC"]
    assert _live(repo, fake, "probe") == 0
    grants = {r["id"]: r for r in _out(capsys)["results"]}[f"probe:{ROLLUP}"]["grants"]
    assert grants["status"] == "pass"
    assert grants["via_public"] is True


# --------------------------------------------------------------------------- #
# bench
# --------------------------------------------------------------------------- #
def test_bench_compares_a_rewrite_with_the_cache_off(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    candidate = tmp_path / "candidate.sql"
    candidate.write_text(base.BY_REGION.replace("COUNT(*) AS n", "COUNT(1) AS n"), encoding="utf-8")
    fake = FakeSnow()
    assert _live(repo, fake, "bench", "--metric", "02#1", "--sql-file", str(candidate)) == 0
    out = _out(capsys)
    before, after = out["results"]
    assert (before["id"], after["id"]) == ("bench:02#1:before", "bench:02#1:after")
    assert after["equivalent"] is True and after["runs"] == 3
    assert before["partitions_scanned"] == 3 and before["partitions_total"] == 10
    assert before["median_elapsed_ms"] is not None
    timed = fake.calls[-2][1]  # the last call reads timings
    assert "USE_CACHED_RESULT = FALSE" in timed
    assert (_run_dir(repo) / "bench-02-1.json").is_file()


def test_bench_flags_a_rewrite_that_changes_the_result(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    candidate = tmp_path / "candidate.sql"
    candidate.write_text(
        base.BY_REGION.replace("GROUP BY region", "AND n > 1 GROUP BY region"),
        encoding="utf-8",
    )
    fake = FakeSnow()
    fake.measures["n > 1"] = {"__ROWS": "3", "__HASH": "444"}
    assert _live(repo, fake, "bench", "--metric", "02#1", "--sql-file", str(candidate)) == 1
    after = _out(capsys)["results"][1]
    assert after["equivalent"] is False
    assert any("row count" in n for n in after["equivalence_notes"])


def test_bench_refuses_a_multi_statement_candidate(repo: Path, tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.sql"
    candidate.write_text("SELECT 1;\nSELECT 2", encoding="utf-8")
    with pytest.raises(sr.ToolError, match="more than one statement"):
        _live(repo, FakeSnow(), "bench", "--metric", "02#1", "--sql-file", str(candidate))


# --------------------------------------------------------------------------- #
# log
# --------------------------------------------------------------------------- #
def _full_run(repo: Path, capsys: pytest.CaptureFixture) -> str:
    fake = FakeSnow()
    _live(repo, fake, "probe")
    run_id = _out(capsys)["run_id"]
    _live(repo, fake, "run", "--run", run_id)
    capsys.readouterr()
    return run_id


def _findings_file(tmp_path: Path, findings: list[dict]) -> Path:
    path = tmp_path / "findings.json"
    path.write_text(json.dumps({"findings": findings}), encoding="utf-8")
    return path


GOOD = {
    "id": "F1",
    "severity": "major",
    "page": "02",
    "metric": "orders_by_region",
    "object": None,
    "claim": "The region rollup counts orders twice when a region is renamed.",
    "evidence": ["run:02#1", f"probe:{ROLLUP}"],
    "suggested_fix": "Group on region_id, not the region name.",
}


def test_log_writes_the_fixed_sections_and_a_blank_sign_off(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "_today", lambda: "2026-10-05")
    run_id = _full_run(repo, capsys)
    code = _live(
        repo,
        FakeSnow(),
        "log",
        "--run",
        run_id,
        "--findings",
        str(_findings_file(tmp_path, [GOOD])),
    )
    assert code == 0
    out = _out(capsys)
    assert out["counts"] == {"blocker": 0, "major": 1, "minor": 0}
    log = (repo / out["log"]).read_text(encoding="utf-8")
    assert re.search(r"review_log/2026-10-05_[0-9a-f]{7}\.md$", out["log"])
    for needed in (
        "| Reviewed commit |",
        "| Connection | `acme` |",
        "| Role | `STREAMSNOW_DEPLOY_ROLE` (secondary roles: none) |",
        "| Warehouse | `STREAMSNOW_WH` |",
        "| Metric | SQL status | Rows | Headline | Screen match | Findings |",
        "| 1 `total_revenue` | pass | 1 | REVENUE = 12345.67 | n/a | — |",
        "| 1 `orders_by_region` | pass | 4 | withheld (4 rows: a small-group breakdown) | n/a | F1 |",
        "| 2 `revenue_trend` | pass | 30 | REVENUE = 9999.00 | n/a | — |",
        "### Major",
        "Evidence: `run:02#1`",
        "Reviewer:\nDate:\nDecision: approve / changes needed",
    ):
        assert needed in log, needed
    readme = (repo / "apps" / SLUG / "sql_review" / "README.md").read_text(encoding="utf-8")
    assert "No live review logged yet." not in readme
    assert re.search(r"\[2026-10-05 at `[0-9a-f]{7}`\]\(review_log/2026-10-05_", readme)
    # check still passes: the latest-review block is not part of the index tables.
    assert sr.main(["check", SLUG, "--dir", str(repo)]) == 0


def _compare_json(repo: Path, run_id: str, statuses: dict[str, str]) -> Path:
    """A compare.json as `sql-review compare` writes it, for the current run files."""
    run_dir = repo / live.RUNS_DIR / SLUG / run_id
    data = {
        "verb": "compare",
        "run_id": run_id,
        "run_digests": {pg: live.run_digest(run_dir / f"run-{pg}.json") for pg in ("01", "02")},
        "results": [
            {
                "id": f"compare:{ref}",
                "page": ref[:2],
                "n": int(ref[3:]),
                "status": status,
                "diffs": ["REVENUE: screen 7777777.77 vs run 12345.67 (tolerance 61.73)"],
            }
            for ref, status in statuses.items()
        ],
    }
    path = run_dir / "compare.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_log_fills_screen_match_from_compare_with_status_words_only(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    run_id = _full_run(repo, capsys)
    _compare_json(repo, run_id, {"01#1": "mismatch", "01#2": "unsupported", "02#1": "match"})
    found = _findings_file(tmp_path, [GOOD])
    assert (
        _live(repo, FakeSnow(), "log", "--run", run_id, "--findings", str(found), "--dry-run") == 0
    )
    out = _out(capsys)
    log = out["log"]
    assert "| 1 `total_revenue` | pass | 1 | REVENUE = 12345.67 | mismatch | — |" in log
    assert "| 2 `revenue_trend` | pass | 30 | REVENUE = 9999.00 | unsupported | — |" in log
    assert "| withheld (4 rows: a small-group breakdown) | match | F1 |" in log
    assert "displayed rounding" in log
    assert "7777777" not in log  # a comparison's numbers stay in the local evidence
    # A mismatch is a candidate finding, not a finding: log names it, never logs it.
    assert out["screen_mismatches_uncited"] == ["compare:01#1"]
    assert "### Blocker\n\nNone." in log


def test_log_marks_a_compare_stale_after_a_page_rerun(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    run_id = _full_run(repo, capsys)
    _compare_json(repo, run_id, {"01#1": "match", "02#1": "mismatch"})
    _live(repo, FakeSnow(), "run", "--run", run_id, "--page", "02")
    run_file = repo / live.RUNS_DIR / SLUG / run_id / "run-02.json"
    run_file.write_text(run_file.read_text(encoding="utf-8") + " ", encoding="utf-8")
    capsys.readouterr()
    found = _findings_file(tmp_path, [])
    assert (
        _live(repo, FakeSnow(), "log", "--run", run_id, "--findings", str(found), "--dry-run") == 0
    )
    out = _out(capsys)
    log = out["log"]
    assert "| REVENUE = 12345.67 | match | — |" in log
    assert "| stale | — |" in log
    # A stale mismatch is no longer a fact about this run: nothing to cite.
    assert out["screen_mismatches_uncited"] == []


def test_log_cites_compare_ids_as_evidence(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    run_id = _full_run(repo, capsys)
    _compare_json(repo, run_id, {"01#1": "mismatch"})
    finding = {
        **GOOD,
        "page": "01",
        "metric": "total_revenue",
        "evidence": ["compare:01#1", "run:01#1"],
    }
    found = _findings_file(tmp_path, [finding])
    assert (
        _live(repo, FakeSnow(), "log", "--run", run_id, "--findings", str(found), "--dry-run") == 0
    )
    assert _out(capsys)["screen_mismatches_uncited"] == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"evidence": []}, "non-empty list"),
        ({"evidence": ["run:09#9"]}, "not a result in this run"),
        ({"severity": "critical"}, "severity must be one of"),
        ({"metric": "nope"}, "not a metric on page"),
        ({"page": "2"}, "two-digit page number"),
        ({"colour": "red"}, "unknown keys"),
        ({"object": "not a name"}, "DATABASE.SCHEMA.OBJECT"),
    ],
)
def test_log_refuses_findings_it_cannot_verify(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture, change: dict, problem: str
) -> None:
    run_id = _full_run(repo, capsys)
    bad = {**GOOD, **change}
    code = _live(
        repo, FakeSnow(), "log", "--run", run_id, "--findings", str(_findings_file(tmp_path, [bad]))
    )
    assert code == 1
    out = _out(capsys)
    assert out["ok"] is False and any(problem in p for p in out["problems"]), out
    assert not (repo / "apps" / SLUG / "sql_review" / live.REVIEW_LOG_DIR).exists()


def test_log_refuses_a_path_or_email_leak(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    run_id = _full_run(repo, capsys)
    # Built at runtime so this file itself passes the privacy gate.
    email = "jane.doe" + "@" + "acme-corp.io"
    leaky = {**GOOD, "claim": f"Ask {email} about it."}
    assert (
        _live(
            repo,
            FakeSnow(),
            "log",
            "--run",
            run_id,
            "--findings",
            str(_findings_file(tmp_path, [leaky])),
        )
        == 1
    )
    problems = " ".join(_out(capsys)["problems"])
    assert "email address" in problems


def test_log_rerun_overwrites_its_own_file_and_a_new_run_gets_a_suffix(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "_today", lambda: "2026-10-05")
    findings = str(_findings_file(tmp_path, []))
    run1 = _full_run(repo, capsys)
    _live(repo, FakeSnow(), "log", "--run", run1, "--findings", findings)
    first = _out(capsys)["log"]
    _live(repo, FakeSnow(), "log", "--run", run1, "--findings", findings)
    assert _out(capsys)["log"] == first
    monkeypatch.setattr(live, "_now", lambda: live._dt.datetime(2030, 1, 1, tzinfo=live._dt.UTC))
    run2 = _full_run(repo, capsys)
    _live(repo, FakeSnow(), "log", "--run", run2, "--findings", findings)
    assert _out(capsys)["log"].endswith("_2.md")


def test_log_without_a_run_step_is_a_tool_error(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _live(repo, FakeSnow(), "probe")
    run_id = _out(capsys)["run_id"]
    with pytest.raises(live.ToolError, match="no `run` results"):
        _live(
            repo,
            FakeSnow(),
            "log",
            "--run",
            run_id,
            "--findings",
            str(_findings_file(tmp_path, [])),
        )


@pytest.mark.parametrize(
    ("entry", "shown"),
    [
        (
            {"rows": 1, "totals": {"X": "5"}, "columns": [{"name": "X", "type": "NUMBER(38,0)"}]},
            "X = 5",
        ),
        (
            {
                "rows": 1,
                "totals": {"X": "5"},
                "columns": [
                    {"name": "NAME", "type": "VARCHAR"},
                    {"name": "X", "type": "NUMBER(38,0)"},
                ],
            },
            "withheld",
        ),
        ({"rows": 3, "totals": {"X": "5"}, "columns": []}, "withheld"),
        ({"rows": 10, "totals": {"X": "5"}, "columns": []}, "X = 5"),
        ({"rows": 0, "totals": {"X": None}, "columns": []}, "no rows"),
    ],
)
def test_headline_never_shows_a_small_group(entry: dict, shown: str) -> None:
    assert shown in live.headline(entry)


def test_log_dry_run_validates_and_writes_nothing(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    run_id = _full_run(repo, capsys)
    findings = str(_findings_file(tmp_path, [GOOD]))
    assert _live(repo, FakeSnow(), "log", "--run", run_id, "--findings", findings, "--dry-run") == 0
    out = _out(capsys)
    assert out["dry_run"] is True and "## Sign-off" in out["log"]
    assert not (repo / "apps" / SLUG / "sql_review" / live.REVIEW_LOG_DIR).exists()
    readme = (repo / "apps" / SLUG / "sql_review" / "README.md").read_text(encoding="utf-8")
    assert "No live review logged yet." in readme


# --------------------------------------------------------------------------- #
# Review fixes: nothing ran, history, overflow, evidence files, privacy
# --------------------------------------------------------------------------- #
class NoConnection(FakeSnow):
    def __call__(self, argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
        self.calls.append((argv, stdin))
        return 1, "", "Error: Connection acme is not configured"


@pytest.mark.parametrize("verb", ["probe", "run"])
def test_a_connection_that_never_logs_in_is_exit_2_after_one_call(repo: Path, verb: str) -> None:
    fake = NoConnection()
    with pytest.raises(live.ToolError, match="not configured"):
        _live(repo, fake, verb)
    assert len(fake.calls) == 1  # no per-section retries, no login storm


class RoleRefused(FakeSnow):
    def answer(self, stmt: str) -> list[dict]:
        if stmt.startswith("USE ROLE"):
            raise RuntimeError
        return super().answer(stmt)

    def __call__(self, argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
        self.calls.append((argv, stdin))
        return 1, "", "003013 (42501): Requested role is not assigned to the executing user."


def test_a_role_the_user_does_not_hold_is_exit_2(repo: Path) -> None:
    fake = RoleRefused()
    with pytest.raises(live.ToolError, match="pass --role"):
        _live(repo, fake, "run")
    assert len(fake.calls) == 2  # the batch, then the session alone; never every section


class WarehouseInvisible(FakeSnow):
    """The session's ``USE WAREHOUSE`` fails the way a role that cannot see it does."""

    def __call__(self, argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
        self.calls.append((argv, stdin))
        if "USE WAREHOUSE STREAMSNOW_WH" in stdin:
            return (
                1,
                "",
                "╭─ Error ─╮\n│ Error 002043 (02000): 01c00000-0000-0000-0000-000000000001: "
                "SQL compilation error: Object does not exist, or operation cannot be "
                "performed. │\n╰─╯",
            )
        return super().__call__(argv, stdin, timeout)


@pytest.mark.parametrize("verb", ["probe", "run"])
def test_a_session_setup_failure_names_the_statements_and_the_flags(repo: Path, verb: str) -> None:
    fake = WarehouseInvisible()
    with pytest.raises(live.ToolError) as err:
        _live(repo, fake, verb, "--role", "ACME_AGENT")
    msg = str(err.value)
    assert "session setup failed" in msg
    assert "USE ROLE ACME_AGENT" in msg and "USE WAREHOUSE STREAMSNOW_WH" in msg
    assert "--role" in msg and "--warehouse" in msg
    assert "002043" in msg  # the Snowflake detail is kept
    assert len(fake.calls) == 2  # the batch, then the session alone; never every section


def test_passing_a_visible_warehouse_clears_the_session_failure(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    fake = WarehouseInvisible()
    assert _live(repo, fake, "run", "--warehouse", "ACME_AGENT_WH") == 0
    assert "USE WAREHOUSE ACME_AGENT_WH" in fake.calls[0][1]


class NoHistory(FakeSnow):
    def answer(self, stmt: str) -> list[dict]:
        if "QUERY_HISTORY_BY_USER" in stmt:
            raise RuntimeError
        return super().answer(stmt)

    def __call__(self, argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
        if "QUERY_HISTORY_BY_USER" in stdin:
            self.calls.append((argv, stdin))
            return 1, "", "002003 (02000): SQL compilation error: does not exist or not authorized."
        return super().__call__(argv, stdin, timeout)


def test_unreadable_history_never_reruns_the_measures(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    fake = NoHistory()
    assert _live(repo, fake, "run") == 0
    out = _out(capsys)
    assert all(r["status"] == "pass" and r["elapsed_ms"] is None for r in out["results"])
    assert any("query history" in w for w in out["warnings"])
    measures = [c for c in fake.calls if '"__ROWS"' in c[1]]
    assert len(measures) == 1


class Overflow(FakeSnow):
    def __call__(self, argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
        if "SUM($2)" in stdin and "REGION_ROLLUP" in stdin:
            self.calls.append((argv, stdin))
            return 1, "", "100046 (22003): Number out of representable range"
        return super().__call__(argv, stdin, timeout)


def test_a_sum_overflow_keeps_the_count_and_says_totals_are_missing(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    assert _live(repo, Overflow(), "run") == 0
    entry = {r["id"]: r for r in _out(capsys)["results"]}["run:02#1"]
    assert entry["rows"] == 4 and entry["totals"] is None and "overflowed" in entry["totals_detail"]
    assert "not computed" in live.headline(entry)


class DistinctRefused(FakeSnow):
    """Only the distinct-count measure fails: a timeout the extra aggregates
    pushed over, or a column type COUNT(DISTINCT) rejects."""

    def __call__(self, argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
        if "COUNT(DISTINCT" in stdin and "REGION_ROLLUP" in stdin:
            self.calls.append((argv, stdin))
            return 1, "", "000630 (57014): Statement reached its statement or warehouse timeout"
        return super().__call__(argv, stdin, timeout)


def test_a_failed_distinct_measure_keeps_the_totals(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    fake = DistinctRefused()
    assert _live(repo, fake, "run") == 0
    entry = {r["id"]: r for r in _out(capsys)["results"]}["run:02#1"]
    assert entry["status"] == "pass"
    assert entry["rows"] == 4 and entry["totals"], entry
    assert "totals_detail" not in entry
    assert "distinct" not in entry
    retries = [
        c[1]
        for c in fake.calls
        if '"__ROWS"' in c[1] and "REGION_ROLLUP" in c[1] and "COUNT(DISTINCT" not in c[1]
    ]
    assert len(retries) == 1 and "SUM($2)" in retries[0]  # the pre-distinct query


def test_agent_written_files_cannot_mint_evidence(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    run_id = _full_run(repo, capsys)
    run_dir = repo / live.RUNS_DIR / SLUG / run_id
    (run_dir / "findings-page-01.json").write_text("[]", encoding="utf-8")
    (run_dir / "verdict-page-01.json").write_text(
        json.dumps({"results": [{"id": "run:01#9"}]}), encoding="utf-8"
    )
    forged = {**GOOD, "evidence": ["run:01#9"]}
    code = _live(
        repo,
        FakeSnow(),
        "log",
        "--run",
        run_id,
        "--findings",
        str(_findings_file(tmp_path, [forged])),
    )
    assert code == 1
    assert any("not a result in this run" in p for p in _out(capsys)["problems"])


@pytest.mark.parametrize("change", [{"metric": ["a"]}, {"page": ["02"]}, {"metric": {"k": 1}}])
def test_log_reports_wrongly_typed_fields_instead_of_crashing(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture, change: dict
) -> None:
    run_id = _full_run(repo, capsys)
    bad = {**GOOD, **change}
    path = str(_findings_file(tmp_path, [bad]))
    assert _live(repo, FakeSnow(), "log", "--run", run_id, "--findings", path, "--dry-run") == 1
    assert _out(capsys)["ok"] is False


def test_latest_skips_a_run_that_produced_nothing(
    repo: Path, capsys: pytest.CaptureFixture
) -> None:
    run_id = _full_run(repo, capsys)
    with pytest.raises(live.ToolError):
        _live(repo, NoConnection(), "probe")  # leaves an empty run behind
    app = repo / "apps" / SLUG
    assert live.resolve_run(repo, app, "latest", None).name == run_id


def test_ddl_drift_ignores_spacing_around_operators_and_a_leading_use() -> None:
    committed = "USE ROLE X;\n" + base.DDL.replace("COUNT(*) AS n", "COUNT(*)+0 AS n")
    live_ddl = LIVE_VIEW_DDL.replace("count(*) as n", "count( * ) + 0 as n")
    assert live.ddl_drift(committed, live_ddl)["drift"] is False


def test_error_text_never_carries_a_cell_value() -> None:
    from streamsnow import sf_exec as sx

    detail = sx._error_detail(
        "100038 (22018): Numeric value 'Jane Doe' is not recognized; "
        "Object 'ANALYTICS_DB.REPORTING.ORDERS' does not exist; invalid identifier 'REVENUE'",
        "",
    )
    assert "Jane" not in detail and "'…'" in detail
    assert "'ANALYTICS_DB.REPORTING.ORDERS'" in detail and "'REVENUE'" in detail


@pytest.mark.parametrize("value", ["O''Brien", "a''b''c", "back\\'slash"])
def test_error_masking_keeps_escaped_quotes_inside_one_value(value: str) -> None:
    from streamsnow import sf_exec as sx

    detail = sx._error_detail(f"100038 (22018): Numeric value '{value}' is not recognized", "")
    assert "Brien" not in detail and "b''c" not in detail and "slash" not in detail
    assert "'…' is not recognized" in detail
