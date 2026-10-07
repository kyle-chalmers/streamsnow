"""App data (#79): the DDL the deploy job applies, checked before anything prints."""

from __future__ import annotations

import pytest
from _app_data_fixtures import AD as FAD
from _app_data_fixtures import OBJECTS_DIR, dynamic_table, view, write_app, write_config

from streamsnow.app_data import (
    KIND_DYNAMIC_TABLE,
    KIND_VIEW,
    ddl_kind,
    load_app_data,
    parse_ddl,
)
from streamsnow.config import load_config

AD = "STREAMSNOW_APPS.STREAMSNOW_REPORTING"
DT = f"{AD}.DAILY_REVENUE"
VIEW = f"{AD}.REGION_REVENUE"
DT_PARTS = tuple(DT.split("."))
VIEW_PARTS = tuple(VIEW.split("."))

DT_DDL = f"""-- Object: {DT}
-- Purpose: Daily revenue, pre-computed so the overview reads a few hundred rows.
-- Used by: none
-- Grants: none
CREATE OR ALTER DYNAMIC TABLE {DT}
  TARGET_LAG = '1 hour'
  WAREHOUSE = STREAMSNOW_WH
  INITIALIZE = ON_CREATE
AS
SELECT order_date, region, SUM(revenue) AS revenue
FROM ANALYTICS_DB.REPORTING.ORDERS
GROUP BY order_date, region;
"""

VIEW_DDL = f"""CREATE OR REPLACE VIEW {VIEW} COPY GRANTS AS
SELECT d.region, SUM(d.revenue) AS revenue
FROM {DT} d
JOIN ANALYTICS_DB.REPORTING.REGIONS r ON r.region = d.region
WHERE r.active
GROUP BY d.region;
"""


def _parse(text: str, expect: tuple[str, ...] = DT_PARTS, grants: tuple[str, ...] = ()):
    return parse_ddl(text, expect, warehouse="STREAMSNOW_WH", grants=grants)


def _problems(text: str, **kw) -> str:
    return " | ".join(detail for _line, detail in _parse(text, **kw).problems)


def test_a_dynamic_table_in_the_deployable_form_parses_clean():
    p = _parse(DT_DDL)
    assert p.problems == ()
    assert p.kind == KIND_DYNAMIC_TABLE
    assert p.line == 5
    assert p.statements[0].startswith(f"CREATE OR ALTER DYNAMIC TABLE {DT}")
    assert p.statements[0].endswith("GROUP BY order_date, region")  # no ';', no header comments
    # A line, not a character offset: the scanner reports offsets, parse_ddl counts newlines.
    assert p.reads == ((11, ("ANALYTICS_DB", "REPORTING", "ORDERS")),)


def test_one_part_names_resolve_to_the_objects_schema_and_ctes_do_not():
    """Snowflake resolves an unqualified relation in a view against the view's own
    schema, so a one-part name is an app-data dependency; a CTE name is not."""
    text = (
        f"CREATE OR REPLACE VIEW {VIEW} COPY GRANTS AS\n"
        "WITH recent AS (SELECT * FROM DAILY_REVENUE)\n"
        "SELECT r.region, COUNT(*) AS n\n"
        "FROM recent JOIN REGIONS r ON r.region = recent.region\n"
        "GROUP BY r.region;\n"
    )
    p = _parse(text, expect=VIEW_PARTS)
    assert p.problems == ()
    assert [parts for _line, parts in p.reads] == [
        (*VIEW_PARTS[:2], "DAILY_REVENUE"),
        (*VIEW_PARTS[:2], "REGIONS"),
    ]
    assert [line for line, _parts in p.reads] == [2, 4]


def test_a_view_reads_every_relation_in_its_query():
    p = _parse(VIEW_DDL, expect=VIEW_PARTS)
    assert p.problems == ()
    assert p.kind == KIND_VIEW
    assert [parts for _line, parts in p.reads] == [
        DT_PARTS,
        ("ANALYTICS_DB", "REPORTING", "REGIONS"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            VIEW_DDL.replace("OR REPLACE VIEW", "OR ALTER VIEW").replace(" COPY GRANTS AS", " AS"),
            id="create-or-alter-view",
        ),
        pytest.param(
            VIEW_DDL.replace("OR REPLACE VIEW", "OR REPLACE SECURE VIEW"), id="secure-view"
        ),
        pytest.param(
            VIEW_DDL.replace("COPY GRANTS AS\n", "COPY GRANTS AS -- the overview's regions\n"),
            id="comment",
        ),
    ],
)
def test_other_deployable_view_forms_parse_clean(text):
    assert _problems(text, expect=VIEW_PARTS) == ""


@pytest.mark.parametrize(
    ("text", "expect", "needle"),
    [
        pytest.param(
            DT_DDL.replace("CREATE OR ALTER DYNAMIC", "CREATE OR REPLACE DYNAMIC"),
            DT_PARTS,
            "CREATE OR ALTER DYNAMIC TABLE: CREATE OR REPLACE recreates it",
            id="replace-dynamic-table",
        ),
        pytest.param(
            DT_DDL.replace("CREATE OR ALTER DYNAMIC", "CREATE DYNAMIC"),
            DT_PARTS,
            "a plain CREATE fails on the second deploy",
            id="plain-create-dynamic-table",
        ),
        pytest.param(
            DT_DDL.replace("WAREHOUSE = STREAMSNOW_WH", "WAREHOUSE = ADHOC_WH"),
            DT_PARTS,
            "WAREHOUSE = STREAMSNOW_WH",
            id="other-warehouse",
        ),
        pytest.param(
            DT_DDL.replace("  INITIALIZE = ON_CREATE\n", ""),
            DT_PARTS,
            "INITIALIZE = ON_CREATE",
            id="no-initialize",
        ),
        pytest.param(
            DT_DDL.replace(
                "  INITIALIZE = ON_CREATE\n", "  WAREHOUSE = BIG_WH\n  INITIALIZE = ON_CREATE\n"
            ),
            DT_PARTS,
            "exactly once",
            id="warehouse-twice",
        ),
        pytest.param(
            DT_DDL.replace(
                "  INITIALIZE = ON_CREATE\n",
                "  INITIALIZE = ON_CREATE\n  INITIALIZE = ON_SCHEDULE\n",
            ),
            DT_PARTS,
            "INITIALIZE = ON_CREATE exactly once",
            id="initialize-twice",
        ),
        pytest.param(
            DT_DDL.replace("ON_CREATE", "ON_SCHEDULE"),
            DT_PARTS,
            "INITIALIZE = ON_CREATE",
            id="initialize-on-schedule",
        ),
        pytest.param(
            f"CREATE OR ALTER TABLE {DT} (order_date DATE, revenue NUMBER);\n",
            DT_PARTS,
            "a plain table cannot deploy to app data",
            id="table",
        ),
        pytest.param(
            f"CREATE TRANSIENT TABLE {DT} AS SELECT 1 AS x;\n",
            DT_PARTS,
            "a plain table cannot deploy to app data",
            id="transient-table",
        ),
        pytest.param(
            f"CREATE OR REPLACE MATERIALIZED VIEW {VIEW} AS SELECT region FROM ANALYTICS_DB.REPORTING.ORDERS;\n",
            VIEW_PARTS,
            "only views and dynamic tables deploy",
            id="materialized-view",
        ),
        pytest.param(
            VIEW_DDL.replace(" COPY GRANTS", ""),
            VIEW_PARTS,
            "COPY GRANTS",
            id="replace-view-without-copy-grants",
        ),
        pytest.param(
            VIEW_DDL.replace("CREATE OR REPLACE VIEW", "CREATE VIEW").replace(" COPY GRANTS", ""),
            VIEW_PARTS,
            "a plain CREATE fails on the second deploy",
            id="plain-create-view",
        ),
        pytest.param(
            VIEW_DDL.replace("CREATE OR REPLACE VIEW", "CREATE VIEW IF NOT EXISTS").replace(
                " COPY GRANTS", ""
            ),
            VIEW_PARTS,
            "IF NOT EXISTS never applies a changed definition",
            id="if-not-exists",
        ),
        pytest.param(
            DT_DDL + VIEW_DDL,
            DT_PARTS,
            "exactly one CREATE statement, found 2",
            id="two-creates",
        ),
        pytest.param(
            DT_DDL + f"GRANT SELECT ON VIEW {AD}.OTHER TO ROLE ACME_READER;\n",
            DT_PARTS,
            "may follow the CREATE",
            id="grant-on-another-object",
        ),
        pytest.param(
            DT_DDL + f"GRANT ALL ON DYNAMIC TABLE {DT} TO ROLE ACME_READER;\n",
            DT_PARTS,
            "may follow the CREATE",
            id="grant-all",
        ),
        pytest.param(
            DT_DDL + f"ALTER DYNAMIC TABLE {DT} SUSPEND;\n",
            DT_PARTS,
            "may follow the CREATE",
            id="alter-after",
        ),
        pytest.param(
            f"USE SCHEMA {AD};\n" + DT_DDL,
            DT_PARTS,
            "the CREATE comes first",
            id="use-first",
        ),
        pytest.param(
            DT_DDL.replace("'1 hour'", "'1 hour"),
            DT_PARTS,
            "cannot parse",
            id="unterminated",
        ),
    ],
)
def test_bad_forms_are_findings(text, expect, needle):
    assert needle in _problems(text, expect=expect)


def test_create_target_must_equal_the_file_name():
    text = DT_DDL.replace(f"DYNAMIC TABLE {DT}", f"DYNAMIC TABLE {AD}.DAILY_REVENUE_V2")
    problems = _problems(text)
    assert f"the CREATE names {AD}.DAILY_REVENUE_V2" in problems
    assert f"name {DT}" in problems


def test_names_compare_the_way_snowflake_resolves_them():
    lower = DT_DDL.replace(f"DYNAMIC TABLE {DT}", f"DYNAMIC TABLE {DT.lower()}")
    assert _problems(lower) == ""
    quoted = DT_DDL.replace(f"DYNAMIC TABLE {DT}", f'DYNAMIC TABLE {AD}."daily_revenue"')
    assert "the CREATE names" in _problems(quoted)


def test_grants_must_match_the_index_entry():
    granted = DT_DDL + f"GRANT SELECT ON DYNAMIC TABLE {DT} TO ROLE ACME_READER;\n"
    assert _problems(granted, grants=("ACME_READER",)) == ""
    assert "keep the two the same" in _problems(granted)
    assert "keep the two the same" in _problems(DT_DDL, grants=("ACME_READER",))
    wrong_kind = DT_DDL + f"GRANT SELECT ON VIEW {DT} TO ROLE ACME_READER;\n"
    assert "may follow the CREATE" in _problems(wrong_kind, grants=("ACME_READER",))


def test_a_semicolon_after_a_trailing_comment_still_ends_the_statement():
    text = DT_DDL.replace("GROUP BY order_date, region;", "GROUP BY order_date, region -- daily\n;")
    p = _parse(text)
    assert p.problems == ()
    assert p.statements[0].endswith("GROUP BY order_date, region")


def test_ddl_kind_is_lenient():
    assert ddl_kind(DT_DDL) == KIND_DYNAMIC_TABLE
    assert ddl_kind(VIEW_DDL) == KIND_VIEW
    assert ddl_kind("CREATE TABLE A.B.C (x INT);") == ""
    assert ddl_kind("-- nothing here\n") == ""


def test_a_cr_only_file_reports_the_real_line():
    text = VIEW_DDL.replace("\n", "\r")
    p = _parse(text, expect=VIEW_PARTS)
    assert p.problems == ()
    assert [line for line, _parts in p.reads] == [3, 4]
    assert p.line == 1
    late = ("-- c\r-- c\r" + VIEW_DDL.replace("\n", "\r")).replace("COPY GRANTS", "")
    assert [line for line, _d in _parse(late, expect=VIEW_PARTS).problems] == [3]


REGION_BY_DAY = (
    "SELECT d.order_date, r.region, SUM(d.revenue) AS revenue\n"
    "FROM {src} d\n"
    "JOIN ANALYTICS_DB.REPORTING.REGIONS r ON r.region_id = d.region_id\n"
    "GROUP BY d.order_date, r.region"
)


def _plan(repo, **governance):
    return load_app_data(repo, load_config(write_config(repo, **governance)))


def _details(plan, slug=None) -> str:
    rows = plan.for_app(slug) if slug else plan.findings
    return " | ".join(f["detail"] for f in rows)


def test_a_repo_without_app_data_objects_plans_nothing(tmp_path):
    write_app(tmp_path, "acme-sales", {})
    plan = _plan(tmp_path)
    assert plan.ok and plan.objects == [] and plan.sql() == ""


def test_valid_objects_plan_and_print_in_order(tmp_path):
    write_app(
        tmp_path,
        "acme-sales",
        {
            "DAILY_REVENUE": dynamic_table("DAILY_REVENUE"),
            "REGION_REVENUE": view(
                "REGION_REVENUE", REGION_BY_DAY.format(src=f"{FAD}.DAILY_REVENUE")
            ),
        },
    )
    plan = _plan(tmp_path)
    assert plan.ok, plan.findings
    assert [o.fqn for o in plan.objects] == [f"{FAD}.DAILY_REVENUE", f"{FAD}.REGION_REVENUE"]
    assert plan.objects[1].depends_on == (f"{FAD}.DAILY_REVENUE",)
    sql = plan.sql()
    assert sql.index("CREATE OR ALTER DYNAMIC TABLE") < sql.index("CREATE OR REPLACE VIEW")
    assert "-- Object:" not in sql
    statements = [ln for ln in sql.splitlines() if ln.startswith(("CREATE", "GROUP"))]
    assert statements and sql.rstrip().endswith(";")
    assert plan.declared[f"{FAD}.DAILY_REVENUE"] == ("acme-sales", "dynamic_table")


def test_order_follows_dependencies_not_app_or_name_order(tmp_path):
    """acme-alpha's view A_REGION_REVENUE reads acme-zeta's Z_DAILY_REVENUE: by app and
    by name the view sorts first, but the table must exist before the view."""
    write_app(
        tmp_path,
        "acme-alpha",
        {
            "A_REGION_REVENUE": view(
                "A_REGION_REVENUE", REGION_BY_DAY.format(src=f"{FAD}.Z_DAILY_REVENUE")
            )
        },
    )
    write_app(tmp_path, "acme-zeta", {"Z_DAILY_REVENUE": dynamic_table("Z_DAILY_REVENUE")})
    plan = _plan(tmp_path)
    assert plan.ok, plan.findings
    assert [o.fqn for o in plan.objects] == [f"{FAD}.Z_DAILY_REVENUE", f"{FAD}.A_REGION_REVENUE"]
    sql = plan.sql()
    assert sql.index(f"DYNAMIC TABLE {FAD}.Z_DAILY_REVENUE") < sql.index(
        f"VIEW {FAD}.A_REGION_REVENUE"
    )


def test_a_dependency_cycle_is_a_finding_on_every_member(tmp_path):
    def reads(other: str) -> str:
        return f"SELECT region, COUNT(*) AS n\nFROM {FAD}.{other}\nGROUP BY region"

    write_app(tmp_path, "acme-a", {"V_ONE": view("V_ONE", reads("V_TWO"))})
    write_app(tmp_path, "acme-b", {"V_TWO": view("V_TWO", reads("V_ONE"))})
    plan = _plan(tmp_path)
    assert not plan.ok and plan.sql() == ""
    assert "dependency cycle" in _details(plan, "acme-a")
    assert "dependency cycle" in _details(plan, "acme-b")


def test_a_cycle_through_one_part_names_is_still_a_cycle(tmp_path):
    """V_ONE reads V_TWO and V_TWO reads V_ONE, both unqualified: Snowflake resolves
    them in the views' own schema, so the loader must see the cycle too."""

    def reads(other: str) -> str:
        return f"SELECT region, COUNT(*) AS n\nFROM {other}\nGROUP BY region"

    write_app(
        tmp_path,
        "acme-sales",
        {"V_ONE": view("V_ONE", reads("V_TWO")), "V_TWO": view("V_TWO", reads("V_ONE"))},
    )
    plan = _plan(tmp_path)
    assert not plan.ok and plan.sql() == ""
    assert _details(plan).count("dependency cycle") == 2


def test_an_unqualified_name_no_app_declares_is_unknown(tmp_path):
    write_app(
        tmp_path,
        "acme-sales",
        {"REGION_REVENUE": view("REGION_REVENUE", REGION_BY_DAY.format(src="MISSING"))},
    )
    assert f"reads {FAD}.MISSING, which sits in app data but no app declares" in _details(
        _plan(tmp_path)
    )


def test_an_unqualified_cte_name_is_not_a_dependency(tmp_path):
    query = (
        "WITH recent AS (SELECT * FROM ANALYTICS_DB.REPORTING.ORDERS)\n"
        "SELECT region, COUNT(*) AS n\nFROM recent\nGROUP BY region"
    )
    write_app(tmp_path, "acme-sales", {"RECENT_ORDERS": view("RECENT_ORDERS", query)})
    plan = _plan(tmp_path)
    assert plan.ok, plan.findings
    assert plan.objects[0].depends_on == ()


def test_a_cte_inside_a_subquery_does_not_hide_the_outer_dependency(tmp_path):
    """The outer V_TWO reads the app-data object; the inner one reads the subquery's
    own CTE. The outer read must stay a dependency (here undeclared, so a finding)."""
    query = (
        "SELECT region, COUNT(*) AS n\nFROM V_TWO\n"
        "WHERE EXISTS (WITH V_TWO AS (SELECT 1 AS x) SELECT * FROM V_TWO)\nGROUP BY region"
    )
    write_app(tmp_path, "acme-sales", {"V_ONE": view("V_ONE", query)})
    assert f"reads {FAD}.V_TWO, which sits in app data but no app declares" in _details(
        _plan(tmp_path)
    )


def test_two_files_resolving_to_one_name_are_both_findings(tmp_path):
    """X.sql and x.sql name the same object once unquoted names fold to upper case:
    the loader must say so, never pick one of them silently."""
    app = write_app(tmp_path, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    lower = app / "sql_review" / OBJECTS_DIR / f"{FAD.lower()}.daily_revenue.sql"
    lower.write_text(dynamic_table("DAILY_REVENUE"), encoding="utf-8")
    if len(list((app / "sql_review" / OBJECTS_DIR).iterdir())) == 1:
        pytest.skip("case-insensitive file system: two such files cannot coexist here")
    plan = _plan(tmp_path)
    assert _details(plan).count("resolve to the same object") == 2
    assert plan.sql() == ""
    assert plan.declared[f"{FAD}.DAILY_REVENUE"] == ("acme-sales", "")


def test_an_index_whose_objects_did_not_load_marks_the_inventory_incomplete(tmp_path):
    app = write_app(tmp_path, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    (app / "sql_review" / "index.yaml").write_text("objects: [\n", encoding="utf-8")
    plan = _plan(tmp_path)
    assert plan.incomplete == ["apps/acme-sales/sql_review/index.yaml"]
    assert not plan.ok and "did not load in full" in _details(plan)
    assert "not under objects:" not in _details(plan)  # one finding, not one per file


def test_an_unknown_app_data_name_is_a_finding(tmp_path):
    write_app(
        tmp_path,
        "acme-sales",
        {
            "REGION_REVENUE": view(
                "REGION_REVENUE", REGION_BY_DAY.format(src=f"{FAD}.MISSING_TABLE")
            )
        },
    )
    plan = _plan(tmp_path)
    assert f"reads {FAD}.MISSING_TABLE, which sits in app data but no app declares" in _details(
        plan
    )


def test_one_object_declared_by_two_apps_is_a_finding_on_both(tmp_path):
    for slug in ("acme-sales", "acme-finance"):
        write_app(tmp_path, slug, {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    plan = _plan(tmp_path)
    assert not plan.ok
    for slug in ("acme-sales", "acme-finance"):
        assert "one app owns each app-data object" in _details(plan, slug)


def test_an_undeclared_file_in_app_data_is_a_finding(tmp_path):
    app = write_app(tmp_path, "acme-sales", {})
    (app / "sql_review" / OBJECTS_DIR / f"{FAD}.STRAY.sql").write_text(
        dynamic_table("STRAY"), encoding="utf-8"
    )
    assert "not under objects: in index.yaml" in _details(_plan(tmp_path))


def test_a_declared_object_without_a_file_is_a_finding(tmp_path):
    app = write_app(tmp_path, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    (app / "sql_review" / OBJECTS_DIR / f"{FAD}.DAILY_REVENUE.sql").unlink()
    plan = _plan(tmp_path)
    assert "has no DDL file" in _details(plan)
    assert plan.declared[f"{FAD}.DAILY_REVENUE"] == ("acme-sales", "")


def test_files_outside_app_data_stay_review_only(tmp_path):
    app = write_app(tmp_path, "acme-sales", {})
    legacy = "ANALYTICS_DB.REPORTING.LEGACY_SUMMARY"
    (app / "sql_review" / OBJECTS_DIR / f"{legacy}.sql").write_text(
        f"CREATE TABLE {legacy} (x INT);\nALTER TABLE {legacy} SET COMMENT = 'review only';\n",
        encoding="utf-8",
    )
    plan = _plan(tmp_path)
    assert plan.ok and plan.declared == {} and plan.sql() == ""


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ("FINANCE_DB.MARTS.LEDGER", "outside governance.sources"),
        ("REPORTING.ORDERS", "does not name its database"),
        ("ANALYTICS_DB.RAW.ORDERS", "denied schema"),
    ],
)
def test_deployed_ddl_reads_only_sources_and_app_data(tmp_path, source, needle):
    query = f"SELECT order_date, COUNT(*) AS n\nFROM {source}\nGROUP BY order_date"
    write_app(tmp_path, "acme-sales", {"DAILY_ORDERS": dynamic_table("DAILY_ORDERS", query)})
    assert needle in _details(_plan(tmp_path))


def test_an_app_data_name_must_be_unquoted(tmp_path):
    app = write_app(tmp_path, "acme-sales", {})
    name = f'{FAD}."daily_revenue"'
    (app / "sql_review" / OBJECTS_DIR / f"{name}.sql").write_text(
        dynamic_table('"daily_revenue"'), encoding="utf-8"
    )
    index = app / "sql_review" / "index.yaml"
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            "objects: []", f"objects:\n- name: '{name}'\n  reason: performance"
        ),
        encoding="utf-8",
    )
    assert "three unquoted identifiers" in _details(_plan(tmp_path))


def test_any_finding_empties_the_plan_sql_and_names_the_owning_app(tmp_path):
    write_app(tmp_path, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    write_app(
        tmp_path,
        "acme-finance",
        {"LEDGER_DAILY": dynamic_table("LEDGER_DAILY").replace("OR ALTER", "OR REPLACE")},
    )
    plan = _plan(tmp_path)
    assert not plan.ok
    assert plan.sql() == ""
    assert {f["app"] for f in plan.findings} == {"acme-finance"}
    assert plan.for_app("acme-sales") == []
