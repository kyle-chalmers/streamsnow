"""App data (#79): the DDL the deploy job applies, checked before anything prints."""

from __future__ import annotations

import pytest

from streamsnow.app_data import KIND_DYNAMIC_TABLE, KIND_VIEW, ddl_kind, parse_ddl

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
