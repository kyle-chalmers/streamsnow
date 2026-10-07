"""schema-refs relation-position boundary scan and deny rules (#78, C12, C13)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from streamsnow.config import Config
from streamsnow.policy import SchemaPolicy
from streamsnow.scaffolder import scaffold
from streamsnow.tools.check_schema_refs import check_paths, find_boundary_refs, find_denied_refs
from streamsnow.tools.validate_app import validate_app

EXAMPLE = Path(__file__).resolve().parent.parent / "streamsnow.config.example.yaml"

POLICY = SchemaPolicy(
    sources=("ANALYTICS_DB.REPORTING", "FINANCE_DB.MARTS"),
    app_data="STREAMSNOW_APPS.STREAMSNOW_REPORTING",
    schema_deny=("RAW", "FINANCE_DB.STAGING"),
)
ENFORCE = dataclasses.replace(POLICY, boundary="enforce")


def _refs(sql: str, policy: SchemaPolicy = POLICY, is_python: bool = False):
    return [(r.line, r.ref, r.verdict) for r in find_boundary_refs(sql, policy, is_python)]


def _check(tmp_path: Path, text: str, policy: SchemaPolicy = POLICY, suffix: str = ".sql"):
    """The COMBINED check (deny + boundary) as check_paths reports it: (findings, warnings)."""
    path = tmp_path / f"q{suffix}"
    path.write_text(text, encoding="utf-8")
    res = check_paths([path], policy)

    def rows(entries):
        return [(e["line"], e["reason"], e.get("ref") or e["schema"]) for e in entries]

    return rows(res["findings"]), rows(res["warnings"])


def test_three_part_names_in_two_source_databases_and_app_data_are_allowed():
    sql = (
        "SELECT o.id FROM ANALYTICS_DB.REPORTING.ORDERS o "
        "JOIN FINANCE_DB.MARTS.FEES f ON f.order_id = o.id "
        "JOIN STREAMSNOW_APPS.STREAMSNOW_REPORTING.DAILY d ON d.id = o.id"
    )
    assert _refs(sql) == []


def test_outside_and_two_part_relations_are_reported_with_their_lines():
    sql = "SELECT 1\nFROM SALES_DB.PUBLIC.LEADS l\nJOIN REPORTING.ORDERS o ON o.id = l.id\n"
    assert _refs(sql) == [
        (2, "SALES_DB.PUBLIC.LEADS", "outside_boundary"),
        (3, "REPORTING.ORDERS", "two_part"),
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT t.id, t.amount FROM ANALYTICS_DB.REPORTING.ORDERS t WHERE t.id > 0",
        "SELECT o.id FROM ANALYTICS_DB.REPORTING.ORDERS o "
        "WHERE o.day BETWEEN params.start_date AND params.end_date",
        "SELECT EXTRACT(YEAR FROM t.order_date) AS y FROM ANALYTICS_DB.REPORTING.ORDERS t",
        "SELECT TRIM(BOTH ' ' FROM c.name) FROM ANALYTICS_DB.REPORTING.CUSTOMERS c",
        "SELECT 1 FROM ANALYTICS_DB.REPORTING.ORDERS o WHERE o.a IS DISTINCT FROM p.b",
        "SELECT a.x, b.y FROM ANALYTICS_DB.REPORTING.A a, ANALYTICS_DB.REPORTING.B b "
        "GROUP BY a.x, b.y ORDER BY a.x, b.y",
        "WITH params AS (SELECT 1 AS start_date) SELECT params.start_date FROM params",
        "SELECT * FROM TABLE(FLATTEN(input => t.items)) f",
        "SELECT * FROM ANALYTICS_DB.REPORTING.ORDERS o, LATERAL FLATTEN(input => o.items) f",
        "SELECT table_name FROM INFORMATION_SCHEMA.TABLES",
        "SELECT * FROM ANALYTICS_DB.INFORMATION_SCHEMA.COLUMNS",
        "SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY",
        "SELECT note FROM ANALYTICS_DB.REPORTING.ORDERS WHERE note = 'from SALES_DB.PUBLIC.LEADS'",
        "SELECT x FROM ANALYTICS_DB.REPORTING.ORDERS WHERE y = $$ from SALES_DB.PUBLIC.Z $$",
        "-- FROM SALES_DB.PUBLIC.LEADS\nSELECT 1 FROM ANALYTICS_DB.REPORTING.ORDERS",
        "/* JOIN SALES_DB.PUBLIC.LEADS */ SELECT 1 FROM ANALYTICS_DB.REPORTING.ORDERS",
        "SELECT * FROM T1 JOIN T2 ON T1.A = T2.A WHERE X = 1 ORDER BY A, B",
    ],
)
def test_alias_qualified_columns_and_non_relation_from_are_never_objects(tmp_path, sql):
    assert _check(tmp_path, sql) == ([], [])


def test_every_relation_in_a_comma_list_and_subquery_is_checked():
    sql = (
        "SELECT 1\n"
        "FROM ANALYTICS_DB.REPORTING.A a,\n"
        "     (SELECT x FROM SALES_DB.PUBLIC.B) s,\n"
        "     SALES_DB.PUBLIC.C AS c,\n"
        '     "SALES_DB" . "PUBLIC" . "D" d\n'
        "WHERE a.id IN (SELECT id FROM REPORTING.E)\n"
    )
    assert _refs(sql) == [
        (3, "SALES_DB.PUBLIC.B", "outside_boundary"),
        (4, "SALES_DB.PUBLIC.C", "outside_boundary"),
        (5, "SALES_DB.PUBLIC.D", "outside_boundary"),
        (6, "REPORTING.E", "two_part"),
    ]


def test_a_comma_join_after_an_explicit_join_is_still_checked(tmp_path):
    sql = (
        "SELECT * FROM ANALYTICS_DB.REPORTING.A a JOIN FINANCE_DB.MARTS.B b ON a.id=b.id, "
        "SALES_DB.PUBLIC.C c"
    )
    assert _refs(sql) == [(1, "SALES_DB.PUBLIC.C", "outside_boundary")]
    assert _check(tmp_path, sql, ENFORCE) == ([(1, "outside_boundary", "SALES_DB.PUBLIC.C")], [])
    using = (
        "SELECT * FROM ANALYTICS_DB.REPORTING.A a JOIN FINANCE_DB.MARTS.B b USING (id, k), "
        "SALES_DB.PUBLIC.C c WHERE a.x IN (1, 2)"
    )
    assert _refs(using) == [(1, "SALES_DB.PUBLIC.C", "outside_boundary")]


def test_time_travel_sampling_and_union_keep_the_list_going():
    sql = (
        "SELECT * FROM ANALYTICS_DB.REPORTING.T AT(OFFSET => -60) x, SALES_DB.PUBLIC.Y y\n"
        "UNION ALL\n"
        "SELECT * FROM ANALYTICS_DB.REPORTING.T SAMPLE (10), SALES_DB.PUBLIC.Z\n"
    )
    assert _refs(sql) == [
        (1, "SALES_DB.PUBLIC.Y", "outside_boundary"),
        (3, "SALES_DB.PUBLIC.Z", "outside_boundary"),
    ]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            'SELECT * FROM SALES_DB."PUBLIC.EXTRA".LEADS',
            [(1, 'SALES_DB."PUBLIC.EXTRA".LEADS', "outside_boundary")],
        ),
        (
            'SELECT * FROM "analytics_db"."reporting".T',
            [(1, '"analytics_db"."reporting".T', "outside_boundary")],
        ),
        ('SELECT * FROM "ANALYTICS_DB"."REPORTING".T', []),
        ("SELECT * FROM analytics_db.reporting.t", []),
        ("SELECT *\nFROM FINANCE_DB\n   .MARTS\n   .FEES f", []),
        (
            "SELECT *\nFROM SALES_DB\n   .PUBLIC\n   .LEADS l",
            [(2, "SALES_DB.PUBLIC.LEADS", "outside_boundary")],
        ),
        ('WITH "P.Q" AS (SELECT 1 AS x) SELECT * FROM "P.Q"', []),
    ],
)
def test_quoted_identifiers_keep_their_boundaries_and_case(sql, expected):
    assert _refs(sql) == expected


def test_a_quoted_cte_name_with_a_dot_is_no_finding_at_all(tmp_path):
    assert _check(tmp_path, 'WITH "P.Q" AS (SELECT 1 AS x) SELECT * FROM "P.Q"') == ([], [])


def test_identifier_literals_in_relation_position_are_checked(tmp_path):
    sql = "SELECT * FROM IDENTIFIER('SALES_DB.PUBLIC.LEADS') l"
    hit = (1, "outside_boundary", "SALES_DB.PUBLIC.LEADS")
    assert _check(tmp_path, sql) == ([], [hit])
    assert _check(tmp_path, sql, ENFORCE) == ([hit], [])
    two = "SELECT * FROM IDENTIFIER($$REPORTING.ORDERS$$)"
    assert _check(tmp_path, two) == ([], [(1, "two_part", "REPORTING.ORDERS")])
    ok = "SELECT * FROM IDENTIFIER('\"ANALYTICS_DB\".REPORTING.ORDERS')"
    assert _check(tmp_path, ok) == ([], [])
    # A session variable or bind cannot be resolved statically: ignored.
    assert _check(tmp_path, "SELECT * FROM IDENTIFIER($tbl) t, IDENTIFIER(:1) u") == ([], [])


def test_table_with_a_literal_is_an_identifier_synonym(tmp_path):
    """TABLE('DB.S.T') names a relation exactly like IDENTIFIER('DB.S.T') (Snowflake's
    identifier-literal usage notes), so it must not bypass the boundary."""
    sql = "SELECT * FROM TABLE('SALES_DB.PUBLIC.LEADS') l"
    hit = (1, "outside_boundary", "SALES_DB.PUBLIC.LEADS")
    assert _check(tmp_path, sql) == ([], [hit])
    assert _check(tmp_path, sql, ENFORCE) == ([hit], [])
    two = "SELECT * FROM TABLE($$REPORTING.ORDERS$$)"
    assert _check(tmp_path, two) == ([], [(1, "two_part", "REPORTING.ORDERS")])
    assert _check(tmp_path, two, ENFORCE) == ([(1, "two_part", "REPORTING.ORDERS")], [])
    assert _check(tmp_path, "SELECT * FROM TABLE('FINANCE_DB.MARTS.FEES') f") == ([], [])
    # Table functions and variables stay out: nothing static names a relation there.
    calls = (
        "SELECT * FROM TABLE(FLATTEN(input => t.items)) f, TABLE($tbl) v, "
        "TABLE(SALES_DB.PUBLIC.MY_UDTF(1)) u"
    )
    assert _check(tmp_path, calls) == ([], [])


def test_use_statements_are_boundary_references():
    sql = (
        "USE ROLE ANALYST;\nUSE WAREHOUSE WH;\nUSE DATABASE SALES_DB;\n"
        "USE SCHEMA ANALYTICS_DB.REPORTING;\nUSE SCHEMA REPORTING;\n"
    )
    assert _refs(sql) == [(3, "SALES_DB", "outside_boundary"), (5, "REPORTING", "two_part")]


def test_python_sql_literals_are_scanned_and_prose_is_not(tmp_path):
    src = (
        '"""Module prose: rows come from SALES_DB.PUBLIC.LEADS."""\n'
        "import streamlit as st\n"
        "conn = st.connection('snowflake')\n"
        "df = conn.query('SELECT id FROM SALES_DB.PUBLIC.LEADS')\n"
        "st.caption('Loaded from SALES_DB.PUBLIC.LEADS every night')\n"
        "st.metric('Leads', 3, help='Counted from SALES_DB.PUBLIC.LEADS')\n"
    )
    assert _check(tmp_path, src, suffix=".py") == (
        [],
        [(4, "outside_boundary", "SALES_DB.PUBLIC.LEADS")],
    )


def test_prose_never_trips_the_deny_list_but_sql_strings_still_do(tmp_path):
    """A caption describing where data comes from queries nothing: neither scan reads
    Streamlit element text. SQL literals keep today's deny behavior, bare and qualified."""
    src = (
        "import streamlit as st\n"
        "st.caption('Loaded from FINANCE_DB.STAGING.LEADS every night')\n"
        "st.markdown('Raw rows come from RAW.EVENTS')\n"
        "conn = st.connection('snowflake')\n"
        "df = conn.query('SELECT id FROM RAW.EVENTS')\n"
        "sql = 'SELECT id FROM FINANCE_DB.STAGING.LEADS'\n"
    )
    assert _check(tmp_path, src, suffix=".py") == (
        [(5, "denied", "RAW"), (6, "denied", "STAGING")],
        [],
    )


@pytest.mark.parametrize(
    ("sql", "hits"),
    [
        ("SELECT * FROM ANY_DB.RAW.T", [(1, "RAW")]),
        ('SELECT * FROM ANY_DB."raw".T', [(1, "raw")]),  # case-insensitive (Defaults 2)
        ("SELECT * FROM FINANCE_DB.STAGING.T", [(1, "STAGING")]),
        ('SELECT * FROM "FINANCE_DB"."STAGING".T', [(1, "STAGING")]),
        ("SELECT *\nFROM FINANCE_DB\n   .STAGING\n   .T", [(2, "STAGING")]),
        ("USE SCHEMA FINANCE_DB.STAGING", [(1, "STAGING")]),
        # Three-part names are tested anywhere, strings included (today's scan).
        (
            "SELECT n FROM ANALYTICS_DB.REPORTING.O WHERE n = 'FINANCE_DB.STAGING.FEES'",
            [(1, "STAGING")],
        ),
        ("SELECT * FROM STAGING.T", []),  # two-part: bare entries only
        ("SELECT * FROM SALES_DB.STAGING.T", []),  # qualified entry: its database only
    ],
)
def test_qualified_deny_entries_in_every_position(sql, hits):
    assert find_denied_refs(sql, POLICY) == hits
    assert all(v != "denied" for _, _, v in _refs(sql))  # never double-reported


STAGING = SchemaPolicy(sources=("SALES_DB.STAGING",), schema_deny=("FINANCE_DB.STAGING",))


@pytest.mark.parametrize(
    ("policy", "sql", "findings"),
    [
        (STAGING, "SELECT staging.id FROM SALES_DB.STAGING.T staging", []),
        (STAGING, 'USE ROLE "STAGING.READ_ONLY"', []),
        (STAGING, "SELECT * FROM FINANCE_DB.STAGING.T", [(1, "denied", "STAGING")]),
        # USE ROLE lines are never scanned, even against a bare entry of the same name.
        (SchemaPolicy(schema_deny=("STAGING",)), 'USE ROLE "STAGING.READ_ONLY";', []),
        (SchemaPolicy(schema_deny=("STAGING",)), "USE SECONDARY ROLES STAGING", []),
    ],
)
def test_qualified_deny_never_matches_columns_or_roles(tmp_path, policy, sql, findings):
    assert _check(tmp_path, sql, policy) == (findings, [])


def test_check_paths_boundary_warns_then_fails_under_enforce(tmp_path):
    q = tmp_path / "q.sql"
    q.write_text("SELECT 1\nFROM SALES_DB.PUBLIC.LEADS\n", encoding="utf-8")
    warn = check_paths([q], POLICY)
    assert warn["ok"] and warn["findings"] == [] and warn["boundary"] == "warn"
    (entry,) = warn["warnings"]
    assert (entry["line"], entry["reason"], entry["database"], entry["schema"], entry["ref"]) == (
        2,
        "outside_boundary",
        "SALES_DB",
        "PUBLIC",
        "SALES_DB.PUBLIC.LEADS",
    )
    assert "governance.sources" in entry["detail"]
    enforce = check_paths([q], ENFORCE)
    assert not enforce["ok"] and enforce["warnings"] == []
    assert [f["reason"] for f in enforce["findings"]] == ["outside_boundary"]


def test_denied_refs_fail_in_both_modes_and_carry_their_database(tmp_path):
    q = tmp_path / "q.sql"
    q.write_text("SELECT 1 FROM FINANCE_DB.STAGING.FEES\n", encoding="utf-8")
    for policy in (POLICY, ENFORCE):
        res = check_paths([q], policy)
        assert not res["ok"]
        assert [(f["reason"], f["database"], f["schema"]) for f in res["findings"]] == [
            ("denied", "FINANCE_DB", "STAGING")
        ]


def test_validate_app_shows_boundary_warnings_and_fails_under_enforce(tmp_path):
    cfg = Config.from_dict(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    scaffold(cfg, tmp_path, "lead-app")
    app = tmp_path / "apps" / "lead-app"
    (app / "queries" / "leads.sql").write_text(
        "-- Query: leads\nSELECT id FROM SALES_DB.PUBLIC.LEADS\n", encoding="utf-8"
    )
    policy = SchemaPolicy.from_governance(cfg.governance)
    sr = next(c for c in validate_app(app, policy, cfg)["checks"] if c["name"] == "schema-refs")
    assert sr["ok"] and sr["findings"] == []
    assert [w["ref"] for w in sr["warnings"]] == ["SALES_DB.PUBLIC.LEADS"]
    strict = dataclasses.replace(policy, boundary="enforce")
    sr = next(c for c in validate_app(app, strict, cfg)["checks"] if c["name"] == "schema-refs")
    assert not sr["ok"] and [f["ref"] for f in sr["findings"]] == ["SALES_DB.PUBLIC.LEADS"]


def test_four_part_names_keep_their_deny_coverage(tmp_path):
    """The policy ignores names of four or more parts (a relation never has that many),
    but a denied database.schema prefix must still fail: the scan always did."""
    sql = "SELECT ANALYTICS_DB.RAW.T.COL FROM X.Y.Z"
    assert find_denied_refs(sql, POLICY) == [(1, "RAW")]
    # X.Y.Z is an ordinary outside-boundary relation: a warning beside the deny hit.
    assert _check(tmp_path, sql) == (
        [(1, "denied", "RAW")],
        [(1, "outside_boundary", "X.Y.Z")],
    )
    split = "SELECT 1 FROM FINANCE_DB\n  .STAGING\n  .T\n  .COL"
    assert find_denied_refs(split, POLICY) == [(1, "STAGING")]


@pytest.mark.parametrize(
    ("src", "hits"),
    [
        ("conn.execute(text('SELECT * FROM RAW.EVENTS'))", [(3, "denied", "RAW")]),
        ("df = sa.text('SELECT id FROM ANY_DB.RAW.EVENTS')", [(3, "denied", "RAW")]),
        ("t = text('SELECT id FROM RAW.EVENTS')", [(3, "denied", "RAW")]),
        ("st.write('SELECT * FROM RAW.EVENTS')", []),  # Streamlit prose stays unread
    ],
)
def test_sql_in_text_calls_is_scanned_but_streamlit_prose_is_not(tmp_path, src, hits):
    code = f"import streamlit as st\nimport sqlalchemy as sa\n{src}\n"
    assert _check(tmp_path, code, suffix=".py")[0] == hits


SOURCES = SchemaPolicy(sources=("ANALYTICS_DB.REPORTING", "FINANCE_DB.MARTS"))


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # MERGE and DELETE sources follow USING, not FROM or JOIN.
        (
            "MERGE INTO ANALYTICS_DB.REPORTING.T t USING SALES_DB.PUBLIC.SRC s ON t.id = s.id "
            "WHEN MATCHED THEN UPDATE SET x = 1",
            [(1, "SALES_DB.PUBLIC.SRC", "outside_boundary")],
        ),
        (
            "DELETE FROM ANALYTICS_DB.REPORTING.A USING SALES_DB.PUBLIC.Z z WHERE A.id = z.id",
            [(1, "SALES_DB.PUBLIC.Z", "outside_boundary")],
        ),
        (
            "MERGE INTO ANALYTICS_DB.REPORTING.T t USING (SELECT * FROM SALES_DB.PUBLIC.S) s "
            "ON t.id = s.id WHEN MATCHED THEN DELETE",
            [(1, "SALES_DB.PUBLIC.S", "outside_boundary")],
        ),
        # A JOIN's USING (cols) is a column list, in a MERGE source too.
        (
            "MERGE INTO ANALYTICS_DB.REPORTING.T t USING FINANCE_DB.MARTS.A a "
            "JOIN FINANCE_DB.MARTS.B b USING (id) ON t.id = a.id WHEN MATCHED THEN DELETE",
            [],
        ),
        (
            "INSERT INTO ANALYTICS_DB.REPORTING.T SELECT * FROM FINANCE_DB.MARTS.A a "
            "JOIN FINANCE_DB.MARTS.B b USING (id, k)",
            [],
        ),
        # The first relation inside a parenthesized join.
        (
            "SELECT * FROM (SALES_DB.PUBLIC.X x JOIN ANALYTICS_DB.REPORTING.A a ON x.id = a.id)",
            [(1, "SALES_DB.PUBLIC.X", "outside_boundary")],
        ),
        ("SELECT * FROM (SELECT 1 AS id FROM ANALYTICS_DB.REPORTING.A) s", []),
        ("WITH c AS (SELECT 1) SELECT * FROM (WITH d AS (SELECT 2) SELECT * FROM d) s", []),
        # VALUES is a relation that keeps the FROM list open.
        (
            "SELECT * FROM VALUES (1), (2) v(x), SALES_DB.PUBLIC.X",
            [(1, "SALES_DB.PUBLIC.X", "outside_boundary")],
        ),
        (
            "SELECT * FROM ANALYTICS_DB.REPORTING.A a, VALUES (1, 2), (3, 4) v(p, q), "
            "SALES_DB.PUBLIC.X",
            [(1, "SALES_DB.PUBLIC.X", "outside_boundary")],
        ),
        ("SELECT * FROM VALUES (a), (b) v(x)", []),
        # OFFSET / LIMIT / FETCH are columns inside an ON expression.
        (
            "SELECT * FROM ANALYTICS_DB.REPORTING.A a JOIN FINANCE_DB.MARTS.B b "
            "ON offset = b.off, SALES_DB.PUBLIC.X",
            [(1, "SALES_DB.PUBLIC.X", "outside_boundary")],
        ),
        (
            "SELECT * FROM ANALYTICS_DB.REPORTING.A a JOIN FINANCE_DB.MARTS.B b "
            "ON a.limit = b.fetch, SALES_DB.PUBLIC.X",
            [(1, "SALES_DB.PUBLIC.X", "outside_boundary")],
        ),
        # A real clause still ends the list.
        ("SELECT * FROM ANALYTICS_DB.REPORTING.A a ORDER BY a.x LIMIT 5, 10", []),
    ],
)
def test_using_parenthesized_and_values_relations_are_in_relation_position(sql, expected):
    assert _refs(sql, SOURCES) == expected
    enforced = dataclasses.replace(SOURCES, boundary="enforce")
    assert [(r.line, r.ref, r.verdict) for r in find_boundary_refs(sql, enforced)] == expected


# --- final review fixes: name resolution, concatenation, role suppression, prose ----------

DENY_PUBLIC = dataclasses.replace(ENFORCE, schema_deny=("SALES_DB.PUBLIC",))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM SALES_DB..LEADS",
        "SELECT * FROM IDENTIFIER('SALES_DB..LEADS')",
        "SELECT * FROM SALES_DB . . LEADS",
    ],
)
def test_double_dot_name_is_the_public_schema(sql):
    """``DB..OBJ`` resolves as ``DB.PUBLIC.OBJ`` (Snowflake name resolution)."""
    assert [v for _, _, v in _refs(sql, ENFORCE)] == ["outside_boundary"]
    assert find_denied_refs(sql, ENFORCE) == []
    assert find_denied_refs(sql, DENY_PUBLIC) == [(1, "PUBLIC")]
    assert _refs(sql, DENY_PUBLIC) == []  # denied names are never double-reported


def test_python_constant_concatenation_is_one_statement(tmp_path):
    src = "sql = 'SELECT * FROM ' + 'SALES_DB.PUBLIC.LEADS'\nsession.sql(sql)\n"
    assert _check(tmp_path, src, ENFORCE, ".py") == (
        [(1, "outside_boundary", "SALES_DB.PUBLIC.LEADS")],
        [],
    )


def test_python_fstring_keeps_relation_context_across_interpolations(tmp_path):
    src = 'sql = f"SELECT * FROM {t} a, SALES_DB.PUBLIC.X"\nsession.sql(sql)\n'
    assert _check(tmp_path, src, ENFORCE, ".py") == (
        [(1, "outside_boundary", "SALES_DB.PUBLIC.X")],
        [],
    )


def test_python_fstring_with_unknown_database_or_schema_is_not_guessed(tmp_path):
    src = 'sql = f"SELECT * FROM {db}.{schema}.LEADS"\nsession.sql(sql)\n'
    assert _check(tmp_path, src, ENFORCE, ".py") == ([], [])


def test_use_role_suppression_ignores_strings_and_quoted_identifiers():
    sql = "SELECT GET_DDL('TABLE', 'ANALYTICS_DB.RAW.\"USE ROLE ORDERS\"')"
    assert find_denied_refs(sql, POLICY) == [(1, "RAW")]
    assert find_denied_refs('SELECT * FROM ANALYTICS_DB.RAW."USE ROLE X"', POLICY) == [(1, "RAW")]
    assert find_denied_refs('USE ROLE "STAGING.READ_ONLY"', STAGING_DENY) == []
    assert find_denied_refs("SELECT 1; USE SECONDARY ROLES RAW.X", POLICY) == []


STAGING_DENY = SchemaPolicy(sources=("SALES_DB.STAGING",), schema_deny=("STAGING",))


@pytest.mark.parametrize(
    "src",
    [
        'raise ValueError("could not read from settings.toml")\n',
        'st.expander("Rows from ANALYTICS.SALES")\n',
        'st.text("Data from orders.csv")\n',
    ],
)
def test_prose_literals_get_no_boundary_finding(src):
    assert _refs(src, ENFORCE, is_python=True) == []


@pytest.mark.parametrize(
    "src",
    [
        'session.sql("SELECT * FROM SALES_DB.PUBLIC.X")\n',
        'Q = "SELECT * FROM SALES_DB.PUBLIC.X"\n',
        'Q = """\n  -- note\n  WITH a AS (SELECT 1) SELECT * FROM SALES_DB.PUBLIC.X"""\n',
    ],
)
def test_sql_literals_still_get_boundary_findings(src):
    assert [r for _, r, _ in _refs(src, ENFORCE, is_python=True)] == ["SALES_DB.PUBLIC.X"]


def test_prose_in_selectbox_style_calls_is_skipped_for_both_scans():
    src = 'st.selectbox("From RAW.EVENTS", [1])\nst.tabs("Rows from ANALYTICS.SALES")\n'
    assert _refs(src, ENFORCE, is_python=True) == []
    assert find_denied_refs(src, POLICY, is_python=True) == []
