"""App data (#79): the DDL the deploy job applies, checked before anything prints."""

from __future__ import annotations

import sys

import pytest
import yaml
from _app_data_fixtures import AD as FAD
from _app_data_fixtures import OBJECTS_DIR, dynamic_table, view, write_app, write_config

from streamsnow.app_data import (
    KIND_DYNAMIC_TABLE,
    KIND_VIEW,
    ddl_kind,
    is_passthrough,
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
    # Windows refuses '"' in a file name; the finding is about the index name, so
    # the DDL file is written only where the file system allows it.
    if sys.platform != "win32":
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


def test_an_object_with_an_unknown_dependency_is_not_in_the_plan(tmp_path):
    write_app(
        tmp_path,
        "acme-sales",
        {"V_ONE": view("V_ONE", REGION_BY_DAY.format(src=f"{FAD}.MISSING"))},
    )
    plan = _plan(tmp_path)
    assert not plan.ok and plan.objects == []


def test_a_reader_of_a_doubly_owned_object_is_not_in_the_plan(tmp_path):
    for slug in ("acme-a", "acme-b"):
        write_app(tmp_path, slug, {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    write_app(
        tmp_path,
        "acme-c",
        {"V_ONE": view("V_ONE", REGION_BY_DAY.format(src=f"{FAD}.DAILY_REVENUE"))},
    )
    plan = _plan(tmp_path)
    assert not plan.ok
    assert [o.fqn for o in plan.objects] == []


def test_a_duplicate_owner_does_not_hide_the_first_ones_findings(tmp_path):
    write_app(
        tmp_path,
        "acme-a",
        {"V_ONE": view("V_ONE", REGION_BY_DAY.format(src=f"{FAD}.MISSING"))},
    )
    write_app(tmp_path, "acme-b", {"V_ONE": dynamic_table("V_ONE")})
    assert f"reads {FAD}.MISSING, which sits in app data but no app declares" in _details(
        _plan(tmp_path)
    )


def test_one_object_listed_twice_in_one_index_is_a_finding(tmp_path):
    app = write_app(tmp_path, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    index = app / "sql_review" / "index.yaml"
    data = yaml.safe_load(index.read_text(encoding="utf-8"))
    data["objects"].append(dict(data["objects"][0]))
    index.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    plan = _plan(tmp_path)
    assert "did not load in full" in _details(plan, "acme-sales")
    assert plan.incomplete == ["apps/acme-sales/sql_review/index.yaml"]
    assert not plan.ok and plan.sql() == ""


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("SELECT region, revenue AS amount FROM ANALYTICS_DB.REPORTING.ORDERS o", True),
        ("SELECT * FROM ANALYTICS_DB.REPORTING.ORDERS", True),
        ("SELECT o.*, o.region region_name FROM ANALYTICS_DB.REPORTING.ORDERS AS o", True),
        ("SELECT region FROM ANALYTICS_DB.REPORTING.ORDERS WHERE region <> 'X'", False),
        ("SELECT SUM(revenue) AS revenue FROM ANALYTICS_DB.REPORTING.ORDERS", False),
        ("SELECT DISTINCT region FROM ANALYTICS_DB.REPORTING.ORDERS", False),
        ("SELECT revenue * 1.1 AS gross FROM ANALYTICS_DB.REPORTING.ORDERS", False),
        ("SELECT 'EUR' AS currency, revenue FROM ANALYTICS_DB.REPORTING.ORDERS", False),
        ("SELECT $$EUR$$ AS currency FROM ANALYTICS_DB.REPORTING.ORDERS", False),
        (
            "SELECT o.region FROM ANALYTICS_DB.REPORTING.ORDERS o "
            "JOIN ANALYTICS_DB.REPORTING.REGIONS r ON r.id = o.region_id",
            False,
        ),
        ("SELECT region FROM ANALYTICS_DB.REPORTING.ORDERS -- all of it\n", True),
    ],
)
def test_is_passthrough(body, expected):
    assert is_passthrough(body) is expected


def test_an_app_data_object_needs_a_reason(tmp_path):
    write_app(
        tmp_path,
        "acme-sales",
        {
            "DAILY_REVENUE": dynamic_table("DAILY_REVENUE"),
            "WEEKLY_REVENUE": dynamic_table("WEEKLY_REVENUE"),
        },
        reasons={"DAILY_REVENUE": "", "WEEKLY_REVENUE": "speed"},
    )
    details = _details(_plan(tmp_path))
    assert f"{FAD}.DAILY_REVENUE needs reason: performance" in details
    assert "(got 'speed')" in details


def test_a_passthrough_view_is_a_finding(tmp_path):
    body = "SELECT region, revenue AS amount\nFROM ANALYTICS_DB.REPORTING.ORDERS"
    write_app(tmp_path, "acme-sales", {"ORDERS_RENAMED": view("ORDERS_RENAMED", body)})
    assert "is a passthrough view" in _details(_plan(tmp_path))


def test_an_object_no_query_reads_is_a_finding(tmp_path):
    write_app(
        tmp_path,
        "acme-sales",
        {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")},
        read_objects=False,
    )
    assert f"no app query reads {FAD}.DAILY_REVENUE" in _details(_plan(tmp_path))


def test_a_read_from_another_apps_query_counts(tmp_path):
    write_app(
        tmp_path,
        "acme-sales",
        {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")},
        read_objects=False,
    )
    write_app(
        tmp_path,
        "acme-finance",
        {},
        queries={"revenue.sql": f"SELECT SUM(revenue) AS r FROM {FAD}.DAILY_REVENUE\n"},
    )
    plan = _plan(tmp_path)
    assert plan.ok, plan.findings


def test_a_metric_read_and_a_python_literal_count_as_references(tmp_path):
    app = write_app(
        tmp_path,
        "acme-sales",
        {
            "DAILY_REVENUE": dynamic_table("DAILY_REVENUE"),
            "WEEKLY_REVENUE": dynamic_table("WEEKLY_REVENUE"),
        },
        read_objects=False,
    )
    (app / "pages").mkdir()
    (app / "pages" / "weekly.py").write_text(
        "import streamlit as st\n\n"
        'conn = st.connection("snowflake")\n'
        f'df = conn.query("SELECT week, revenue FROM {FAD}.WEEKLY_REVENUE")\n'
        f'st.caption("Loaded from {FAD}.DAILY_REVENUE")\n',
        encoding="utf-8",
    )
    index = app / "sql_review" / "index.yaml"
    data = yaml.safe_load(index.read_text(encoding="utf-8"))
    (app / "queries" / "total.sql").write_text("SELECT 1 AS one\n", encoding="utf-8")
    data["pages"] = [
        {
            "path": "pages/weekly.py",
            "metrics": [
                {"key": "total", "query": "queries/total.sql", "reads": [f"{FAD}.DAILY_REVENUE"]}
            ],
        }
    ]
    index.write_text(yaml.safe_dump(data), encoding="utf-8")
    plan = _plan(tmp_path)
    assert "no app query reads" not in _details(plan), plan.findings


def test_an_object_read_only_by_another_object_is_referenced_and_the_chain_is_advisory(tmp_path):
    write_app(
        tmp_path,
        "acme-sales",
        {
            "DAILY_REVENUE": dynamic_table("DAILY_REVENUE"),
            "REGION_REVENUE": view(
                "REGION_REVENUE", REGION_BY_DAY.format(src=f"{FAD}.DAILY_REVENUE")
            ),
        },
        read_objects=False,
        queries={"regions.sql": f"SELECT * FROM {FAD}.REGION_REVENUE\n"},
    )
    plan = _plan(tmp_path)
    assert plan.ok, plan.findings
    advice = " | ".join(a["detail"] for a in plan.advisories)
    assert f"{FAD}.REGION_REVENUE is built on {FAD}.DAILY_REVENUE" in advice


def test_shared_logic_read_by_one_query_is_advisory(tmp_path):
    shared = view("REGION_REVENUE", REGION_BY_DAY.format(src="ANALYTICS_DB.REPORTING.ORDERS"))
    write_app(
        tmp_path,
        "acme-sales",
        {"REGION_REVENUE": shared},
        reasons={"REGION_REVENUE": "shared_logic"},
    )
    plan = _plan(tmp_path)
    assert plan.ok
    assert any("shared_logic but only" in a["detail"] for a in plan.advisories)
    write_app(
        tmp_path,
        "acme-finance",
        {},
        queries={"regions.sql": f"SELECT * FROM {FAD}.REGION_REVENUE\n"},
    )
    assert not any("shared_logic but only" in a["detail"] for a in _plan(tmp_path).advisories)


def test_two_queries_in_one_file_are_two_readers(tmp_path):
    """Count queries, not files: one .sql file holding two statements that read the
    shared view is two readers, so shared_logic is earned."""
    shared = view("REGION_REVENUE", REGION_BY_DAY.format(src="ANALYTICS_DB.REPORTING.ORDERS"))
    write_app(
        tmp_path,
        "acme-sales",
        {"REGION_REVENUE": shared},
        reasons={"REGION_REVENUE": "shared_logic"},
        read_objects=False,
        queries={
            "regions.sql": f"SELECT * FROM {FAD}.REGION_REVENUE;\n"
            f"SELECT COUNT(*) AS n FROM {FAD}.REGION_REVENUE;\n"
        },
    )
    plan = _plan(tmp_path)
    assert plan.ok, plan.findings
    assert not any("shared_logic but only" in a["detail"] for a in plan.advisories)


def test_only_certain_sql_counts_as_a_reader(tmp_path):
    """An error message that mentions the object ("could not read from ...") is not a
    query: only statement chunks are evidence, as in schema-refs."""
    app = write_app(
        tmp_path,
        "acme-sales",
        {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")},
        read_objects=False,
    )
    (app / "helpers.py").write_text(
        f'def fail():\n    raise ValueError("could not read from {FAD}.DAILY_REVENUE")\n',
        encoding="utf-8",
    )
    assert f"no app query reads {FAD}.DAILY_REVENUE" in _details(_plan(tmp_path))


def test_an_unreferenced_object_finding_points_at_its_create_and_reads_plainly(tmp_path):
    write_app(
        tmp_path,
        "acme-sales",
        {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")},
        read_objects=False,
    )
    (row,) = [f for f in _plan(tmp_path).findings if "no app query reads" in f["detail"]]
    assert row["detail"].startswith(
        f"no app query reads {FAD}.DAILY_REVENUE, and app data holds only objects that a query reads."
    )
    assert row["line"] > 1  # the header comment comes first; the CREATE is the finding's line


def test_the_shared_logic_advisory_names_an_object_reader_as_an_object(tmp_path):
    write_app(
        tmp_path,
        "acme-sales",
        {
            "BASE_V": view("BASE_V", "SELECT region FROM ANALYTICS_DB.REPORTING.ORDERS"),
            "TOP_V": view("TOP_V", f"SELECT region FROM {FAD}.BASE_V"),
        },
        reasons={"BASE_V": "shared_logic", "TOP_V": "performance"},
        queries={"top_v.sql": f"SELECT * FROM {FAD}.TOP_V\n"},
        read_objects=False,
    )
    (adv,) = [a for a in _plan(tmp_path).advisories if "shared_logic" in a["detail"]]
    assert adv["detail"].startswith(
        f"{FAD}.BASE_V has reason: shared_logic but only the app-data object {FAD}.TOP_V reads it"
    )


# --------------------------------------------------------------------------- #
# Codex review of #79: SQL comments, symlinks, and what the scanners can see
# --------------------------------------------------------------------------- #


def test_one_line_keeps_any_text_on_one_physical_line():
    """Runs on every platform: whatever a path or name holds, the escaped form has no
    character that ends a `--` comment, so nothing after it can become live SQL."""
    from streamsnow.app_data import one_line

    for text in (
        "apps/acme-sales/payload\nDROP VIEW X;--",
        "a\rb",
        "a\r\nb",
        "a b c\x0bd\x0ce\x85f",
        "plain/path.sql",
    ):
        escaped = one_line(text)
        assert escaped.splitlines() == [escaped]
        assert "\n" not in escaped and "\r" not in escaped
        assert escaped.isascii() and escaped.isprintable()
    # plain identifiers and paths read as written; quotes are escaped, never ambiguous
    assert (
        one_line("STREAMSNOW_APPS.STREAMSNOW_REPORTING") == "STREAMSNOW_APPS.STREAMSNOW_REPORTING"
    )
    assert one_line("apps/acme-sales/x.sql") == "apps/acme-sales/x.sql"
    assert one_line("a'b") == ascii("a'b")


def test_sql_never_lets_a_rendered_path_end_its_comment():
    from streamsnow.app_data import AppDataObject, AppDataPlan

    evil = f"apps/acme-sales/payload\nDROP VIEW {FAD}.OTHER;--"
    plan = AppDataPlan(
        app_data=FAD,
        objects=[
            AppDataObject(
                f"{FAD}.DAILY_REVENUE",
                KIND_VIEW,
                "acme-sales",
                evil,
                "performance",
                (f"CREATE OR REPLACE VIEW {FAD}.DAILY_REVENUE COPY GRANTS AS SELECT 1",),
                (),
            )
        ],
    )
    lines = plan.sql().splitlines()
    live = [ln for ln in lines if ln.strip() and not ln.startswith("--")]
    assert live == [f"CREATE OR REPLACE VIEW {FAD}.DAILY_REVENUE COPY GRANTS AS SELECT 1;"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_a_symlinked_ddl_file_is_a_finding_and_never_rendered(tmp_path):
    app = write_app(tmp_path, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    link = app / "sql_review" / OBJECTS_DIR / f"{FAD}.DAILY_REVENUE.sql"
    target = app / f"payload\nDROP VIEW {FAD}.OTHER;--"
    target.write_text(link.read_text(encoding="utf-8"), encoding="utf-8")
    link.unlink()
    link.symlink_to(target)
    plan = _plan(tmp_path)
    assert not plan.ok
    assert plan.sql() == ""
    (row,) = [f for f in plan.findings if "symbolic link" in f["detail"]]
    assert row["file"] == f"apps/acme-sales/sql_review/{OBJECTS_DIR}/{FAD}.DAILY_REVENUE.sql"
    assert "payload" not in " ".join(f["file"] + f["detail"] for f in plan.findings)


def test_a_quoted_identifier_never_opens_a_cte_that_hides_a_dependency(tmp_path):
    """`AS "WITH Z_DT AS ("` is a column alias. Read as a CTE it hid the read of Z_DT,
    so A_V was built before the dynamic table it reads."""
    write_app(
        tmp_path,
        "acme-sales",
        {
            "A_V": view("A_V", 'SELECT COUNT(*) AS "WITH Z_DT AS (" FROM Z_DT'),
            "Z_DT": dynamic_table("Z_DT"),
        },
    )
    plan = _plan(tmp_path)
    assert plan.ok, _details(plan)
    assert [o.fqn for o in plan.objects] == [f"{FAD}.Z_DT", f"{FAD}.A_V"]
    assert plan.deps[f"{FAD}.A_V"] == (f"{FAD}.Z_DT",)


def test_a_slash_slash_comment_never_hides_a_dependency_or_a_reader(tmp_path):
    """Snowflake reads `//` as a comment, so the apostrophe after it opens no string:
    the FROM on the next line is real, in the DDL and in the app's query alike."""

    def hidden(name):
        return f"SELECT COUNT(*) AS n // '\nFROM {FAD}.{name} // '"

    write_app(
        tmp_path,
        "acme-sales",
        {
            "A_V": view("A_V", hidden("Z_DT")),
            "Y_DT": dynamic_table("Y_DT"),
            "Z_DT": dynamic_table("Z_DT"),
        },
        queries={"a_v.sql": f"SELECT * FROM {FAD}.A_V\n", "y.sql": hidden("Y_DT") + "\n"},
        read_objects=False,
    )
    plan = _plan(tmp_path)
    assert plan.ok, _details(plan)  # Y_DT has its reader: the query in y.sql
    assert [o.fqn for o in plan.objects] == [f"{FAD}.Y_DT", f"{FAD}.Z_DT", f"{FAD}.A_V"]


# --------------------------------------------------------------------------- #
# Codex round 2: a symlink anywhere from the apps root to a DDL file
# --------------------------------------------------------------------------- #

_LINKABLE = [
    "apps",
    "apps/acme-sales",
    "apps/acme-sales/sql_review",
    "apps/acme-sales/sql_review/index.yaml",
    f"apps/acme-sales/sql_review/{OBJECTS_DIR}",
]


def _link_out(repo, outside, component):
    """Move ``repo/component`` outside the repo and leave a symlink to it in its place."""
    import shutil

    src = repo / component
    dest = outside / component.replace("/", "_")
    shutil.move(str(src), str(dest))
    src.symlink_to(dest, target_is_directory=dest.is_dir())


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
@pytest.mark.parametrize("component", _LINKABLE)
def test_a_symlinked_path_component_is_a_finding_and_an_incomplete_inventory(tmp_path, component):
    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    write_app(repo, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    _link_out(repo, outside, component)
    plan = _plan(repo)
    assert not plan.ok
    assert plan.sql() == ""
    assert any(f["file"] == component and "symbolic link" in f["detail"] for f in plan.findings), (
        _details(plan)
    )
    assert plan.incomplete  # teardown holds the CI role back; tombstones fail closed


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_teardown_holds_the_role_back_for_a_symlinked_app_dir(tmp_path):
    from typer.testing import CliRunner

    from streamsnow.cli import app as cli

    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    ad = "STREAMSNOW_DATA.REPORTING"
    cfg = write_config(repo, app_data=ad)
    write_app(repo, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE", ad=ad)}, ad=ad)
    _link_out(repo, outside, "apps/acme-sales")
    r = CliRunner().invoke(cli, ["deploy-setup", "--teardown", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    assert "--   DROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;" in r.stdout
    assert "\nDROP ROLE IF EXISTS STREAMSNOW_DEPLOY_ROLE;" not in r.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra rights on Windows")
def test_the_tombstone_inventory_is_incomplete_for_a_symlinked_index(tmp_path, monkeypatch):
    from streamsnow.config import load_config
    from streamsnow.tools.check_tombstones import live_app_data

    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    cfg = load_config(write_config(repo))
    write_app(repo, "acme-sales", {"DAILY_REVENUE": dynamic_table("DAILY_REVENUE")})
    _link_out(repo, outside, "apps/acme-sales/sql_review/index.yaml")
    monkeypatch.chdir(repo)
    _objects, incomplete = live_app_data(cfg, repo / "apps")
    assert incomplete == ["apps/acme-sales/sql_review/index.yaml"]
