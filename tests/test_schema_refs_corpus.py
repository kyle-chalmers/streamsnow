"""Regression corpus for the schema-refs scans (deny list and boundary).

One table of inputs with the expected result of each scan, so a fix to one rule cannot
silently reopen another. Policy: sources ANALYTICS_DB.REPORTING, deny RAW and
FINANCE_DB.STAGING, boundary enforce.

The deny column follows one rule: every input that origin/main denied is still denied,
except the accepted exclusions, each marked by a comment in the table: Streamlit prose
calls (rooted at st or at a streamlit import), USE ROLE / USE SECONDARY ROLES
statements, and the policy difference of the qualified FINANCE_DB.STAGING entry. Inputs
origin/main did not deny and this branch does (concatenation, f-strings, double dots,
bare write calls) are stricter on purpose. Each row is (kind, source, deny, boundary)
with deny as (line, schema) and boundary as (line, reason, ref).
"""

from __future__ import annotations

import pytest

from streamsnow.policy import SchemaPolicy
from streamsnow.tools.check_schema_refs import check_paths

POLICY = SchemaPolicy(
    sources=("ANALYTICS_DB.REPORTING",),
    schema_deny=("RAW", "FINANCE_DB.STAGING"),
    boundary="enforce",
)

CORPUS = [
    ("py", 'q = f"{\'SELECT * FROM DB.RAW.X\'}" + " WHERE 1=1"\n', [(1, "RAW")], []),
    ("py", "q = f\"SELECT * {'FROM DB.S.X'}\"\n", [], [(1, "outside_boundary", "DB.S.X")]),
    (
        "py",
        'Q = "EXPLAIN SELECT * FROM SALES_DB.PUBLIC.X"\n',
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.X")],
    ),
    ("py", 'import streamlit as st; st.expander("Rows from " + "DB.RAW.X")\n', [], []),
    ("py", 'session.sql("SELECT * FROM " + "DB.RAW.X")\n', [(1, "RAW")], []),
    (
        "py",
        'q = ("SELECT *" +\n     " FROM " +\n     "DB.RAW.X")\nsession.sql(q)\n',
        [(3, "RAW")],
        [],
    ),
    ("py", 'a = f"SELECT * FROM {db}.RAW.T"\n', [(1, "RAW")], []),
    ("py", 'b = f"SELECT * FROM {db}.{s}.T WHERE x IN (SELECT y FROM RAW.Z)"\n', [(1, "RAW")], []),
    (
        "py",
        'session.sql(f"SELECT * FROM {t} JOIN FINANCE_DB.STAGING.F f ON 1=1").collect()\n',
        [(1, "STAGING")],
        [],
    ),
    ("py", 'x = "select a from raw.events"\n', [(1, "raw")], []),
    # accepted exclusion: Streamlit prose call (st-rooted or streamlit import)
    ("py", 'import streamlit as st; st.write("SELECT * FROM RAW.EVENTS")\n', [], []),
    ("py", 'import streamlit as st; st.caption(f"Rows from {n} RAW.EVENTS")\n', [(1, "RAW")], []),
    ("py", 'run_query("SELECT * FROM " + tbl + " JOIN RAW.E e")\n', [(1, "RAW")], []),
    ("py", 'q = "SELECT * FROM " "RAW.E"\n', [(1, "RAW")], []),
    ("py", 'q = "USE SCHEMA RAW"\n', [(1, "RAW")], []),
    ("py", 'q = "USE SCHEMA FINANCE_DB.STAGING"\n', [(1, "STAGING")], []),
    ("py", 'q = f"""\nSELECT *\nFROM {db}.RAW.T\n"""\n', [(3, "RAW")], []),
    ("py", 'session.table("RAW.EVENTS")\n', [], []),
    ("py", 'session.table("ANALYTICS_DB.RAW.EVENTS")\n', [], []),
    ("py", 'cfg = {"src": "FINANCE_DB.STAGING.FEES"}\n', [], []),
    (
        "py",
        'msg = "copy from RAW.EVENTS failed: " + str(e)\nraise ValueError(msg)\n',
        [(1, "RAW")],
        [],
    ),
    ("py", 'q = "SELECT 1 FROM " + ("RAW" + ".E")\n', [(1, "RAW")], []),
    ("py", "q = f\"SELECT * FROM {'RAW'}.E\"\n", [], []),
    ("py", 'q = "SELECT * FROM %s" % "RAW.E"\n', [], []),
    ("py", 'q = "SELECT * FROM {}".format("RAW.E")\n', [], []),
    (
        "py",
        'def f():\n    """Reads RAW.EVENTS."""\n    return session.sql("select * from raw.events")\n',
        [(3, "raw")],
        [],
    ),
    (
        "py",
        'import streamlit as st; st.markdown("x" + f"{session.sql(\'SELECT * FROM RAW.E\')}")\n',
        [(1, "RAW")],
        [],
    ),
    (
        "py",
        'import streamlit as st; st.expander("From " + "x").write(session.sql("SELECT * FROM RAW.E"))\n',
        [(1, "RAW")],
        [],
    ),
    (
        "py",
        'import streamlit as st; st.caption("from " + q("SELECT * FROM RAW.E"))\n',
        [(1, "RAW")],
        [],
    ),
    ("py", 'labels = ["SELECT * FROM RAW.E" + "", 1]\n', [(1, "RAW")], []),
    ("sql", "SELECT * FROM RAW.EVENTS\n", [(1, "RAW")], []),
    (
        "sql",
        "select * from finance_db.staging.f join analytics_db.raw.x on 1=1\n",
        [(1, "raw"), (1, "staging")],
        [],
    ),
    ("sql", 'SELECT * FROM "RAW"."E"\n', [(1, "RAW")], []),
    ("sql", "USE SCHEMA RAW;\nSELECT 1;\n", [(1, "RAW")], []),
    ("sql", "-- RAW.E in a comment\nSELECT * FROM ANALYTICS_DB.REPORTING.X\n", [], []),
    (
        "py",
        'session.sql("SELECT * FROM SALES_" +\n    "DB.PUBLIC.X")\n',
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.X")],
    ),
    ("py", 'session.sql("SELECT * FROM R" +\n    "AW.E")\n', [(1, "RAW")], []),
    ("py", 'session.sql("SEL" + "ECT * FROM RAW.E")\n', [(1, "RAW")], []),
    ("py", 'session.sql("SELECT * FROM RAW" +\n    ".E")\n', [(1, "RAW")], []),
    (
        "py",
        'q = ("SELECT *" +\n     " FROM " +\n     "SALES_DB.PUBLIC.X")\nsession.sql(q)\n',
        [],
        [(3, "outside_boundary", "SALES_DB.PUBLIC.X")],
    ),
    (
        "py",
        'import io\nq = io.StringIO()\nq.write("SELECT * FROM DB.RAW.X")\nsession.sql(q.getvalue())\n',
        [(3, "RAW")],
        [],
    ),
    ("py", 'write("SELECT * FROM DB.RAW.X")\n', [(1, "RAW")], []),
    ("py", 'sql_file.write("SELECT * FROM DB.RAW.X")\n', [(1, "RAW")], []),
    ("py", 'col.write("SELECT * FROM DB.RAW.X")\n', [(1, "RAW")], []),
    ("py", 'other.caption("SELECT * FROM DB.RAW.X")\n', [(1, "RAW")], []),
    # accepted exclusion: Streamlit prose call (st-rooted or streamlit import)
    ("py", 'import streamlit as st; st.sidebar.write("SELECT * FROM DB.RAW.X")\n', [], []),
    # accepted exclusion: Streamlit prose call (st-rooted or streamlit import)
    ("py", 'import streamlit as stl\nstl.caption("SELECT * FROM DB.RAW.X")\n', [], []),
    # accepted exclusion: Streamlit prose call (st-rooted or streamlit import)
    ("py", 'from streamlit import markdown\nmarkdown("SELECT * FROM DB.RAW.X")\n', [], []),
    # accepted exclusion: Streamlit prose call (st-rooted or streamlit import)
    ("py", 'import streamlit\nstreamlit.info("SELECT * FROM DB.RAW.X")\n', [], []),
    (
        "py",
        'import streamlit as st; st.caption("Total: " + str(fetch("SELECT * FROM FINANCE_DB.STAGING.F")))\n',
        [(1, "STAGING")],
        [],
    ),
    (
        "py",
        'import streamlit as st; st.metric("n", "x" + f"{fetch(\'SELECT * FROM RAW.E\')}")\n',
        [(1, "RAW")],
        [],
    ),
    (
        "py",
        'import streamlit as st; st.write(label="rows " + run("SELECT * FROM SALES_DB.PUBLIC.X"))\n',
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.X")],
    ),
    ("py", 'import streamlit as st; st.text("Rows from RAW.EVENTS")\n', [(1, "RAW")], []),
    # accepted exclusion: Streamlit prose call (st-rooted or streamlit import)
    ("py", 'import streamlit as st; st.selectbox("From RAW.EVENTS", [1])\n', [], []),
    ("py", 'import streamlit as st; st.tabs("Rows from ANALYTICS.SALES")\n', [], []),
    ("py", 'import streamlit as st; st.expander("Rows from ANALYTICS.SALES")\n', [], []),
    ("py", 'raise ValueError("could not read from settings.toml")\n', [], []),
    ("py", 'import streamlit as st; st.text("Data from orders.csv")\n', [], []),
    (
        "py",
        'session.sql("SELECT * FROM SALES_DB.PUBLIC.X")\n',
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.X")],
    ),
    (
        "py",
        'Q = "SELECT * FROM SALES_DB.PUBLIC.X"\n',
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.X")],
    ),
    (
        "py",
        'sql = "SELECT * FROM " + "SALES_DB.PUBLIC.LEADS"\nsession.sql(sql)\n',
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.LEADS")],
    ),
    (
        "py",
        'sql = f"SELECT * FROM {t} a, SALES_DB.PUBLIC.X"\nsession.sql(sql)\n',
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.X")],
    ),
    ("py", 'sql = f"SELECT * FROM {db}.{schema}.LEADS"\nsession.sql(sql)\n', [], []),
    (
        "py",
        'b = f"SELECT * FROM SALES_DB.PUBLIC.{t}"\n',
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.<expr>")],
    ),
    (
        "py",
        'session.sql("SELECT * FROM SALES_DB..LEADS")\n',
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.LEADS")],
    ),
    (
        "py",
        "session.sql(\"SELECT * FROM IDENTIFIER('SALES_DB..LEADS')\")\n",
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.LEADS")],
    ),
    (
        "py",
        'session.sql("SELECT * FROM ANALYTICS_DB..LEADS")\n',
        [],
        [(1, "outside_boundary", "ANALYTICS_DB.PUBLIC.LEADS")],
    ),
    (
        "py",
        "session.sql(\"SELECT GET_DDL('TABLE', 'ANALYTICS_DB.RAW.\\\"USE ROLE ORDERS\\\"')\")\n",
        [(1, "RAW")],
        [],
    ),
    # accepted exclusion: USE ROLE / USE SECONDARY ROLES name roles, never schemas
    ("py", "session.sql('USE ROLE \"STAGING.READ_ONLY\"')\n", [], []),
    # accepted exclusion: USE ROLE / USE SECONDARY ROLES name roles, never schemas
    ("py", 'session.sql("USE SECONDARY ROLES RAW.X")\n', [], []),
    (
        "py",
        'session.sql("SELECT * FROM ANALYTICS_DB.REPORTING.ORDERS o JOIN FINANCE_DB.STAGING.F f ON 1=1")\n',
        [(1, "STAGING")],
        [],
    ),
    ("py", 'session.sql("SELECT * FROM ANALYTICS_DB.REPORTING.ORDERS")\n', [], []),
    (
        "py",
        'session.sql("SELECT * FROM REPORTING.ORDERS")\n',
        [],
        [(1, "two_part", "REPORTING.ORDERS")],
    ),
    (
        "sql",
        "SELECT * FROM SALES_DB..LEADS\n",
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.LEADS")],
    ),
    (
        "sql",
        "SELECT * FROM SALES_DB . . LEADS\n",
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.LEADS")],
    ),
    (
        "sql",
        "SELECT * FROM IDENTIFIER('SALES_DB..LEADS')\n",
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.LEADS")],
    ),
    ("sql", "SELECT GET_DDL('TABLE', 'ANALYTICS_DB.RAW.\"USE ROLE ORDERS\"')\n", [(1, "RAW")], []),
    # accepted exclusion: USE ROLE / USE SECONDARY ROLES name roles, never schemas
    ("sql", 'USE ROLE "STAGING.READ_ONLY"\n', [], []),
    # accepted exclusion: USE ROLE / USE SECONDARY ROLES name roles, never schemas
    ("sql", "USE SECONDARY ROLES RAW.X;\nSELECT 1;\n", [], []),
    # accepted exclusion: USE ROLE / USE SECONDARY ROLES name roles, never schemas
    ("sql", "SELECT 1; USE ROLE RAW.X\n", [], []),
    # line fix: main reported line 2 (a block comment dropped a newline); the name is on line 3
    (
        "sql",
        "/* header\n   spans lines */\nSELECT * FROM ANALYTICS_DB.RAW.EVENTS\n",
        [(3, "RAW")],
        [],
    ),
    (
        "sql",
        "SELECT * FROM ANALYTICS_DB.REPORTING.O o, SALES_DB.PUBLIC.C c\n",
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.C")],
    ),
    (
        "sql",
        "SELECT * FROM ANALYTICS_DB.REPORTING.O o JOIN FINANCE_DB.MARTS.F f ON 1=1\n",
        [],
        [(1, "outside_boundary", "FINANCE_DB.MARTS.F")],
    ),
    ("sql", "SELECT EXTRACT(YEAR FROM t.day) FROM ANALYTICS_DB.REPORTING.T t\n", [], []),
    ("sql", "SELECT * FROM TABLE(FLATTEN(INPUT => x))\n", [], []),
    ("sql", "SELECT * FROM FINANCE_DB.STAGING.F\n", [(1, "STAGING")], []),
    ("sql", 'SELECT * FROM "FINANCE_DB"."STAGING".F\n', [(1, "STAGING")], []),
    # policy difference: FINANCE_DB.STAGING is a qualified deny entry, main's bare STAGING denied every database
    (
        "sql",
        "SELECT * FROM SALES_DB.STAGING.F\n",
        [],
        [(1, "outside_boundary", "SALES_DB.STAGING.F")],
    ),
    # policy difference: FINANCE_DB.STAGING is a qualified deny entry, main's bare STAGING denied every database
    ("sql", "SELECT * FROM STAGING.F\n", [], [(1, "two_part", "STAGING.F")]),
    ("sql", "SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY\n", [], []),
    ("sql", "SELECT * FROM ANALYTICS_DB.INFORMATION_SCHEMA.TABLES\n", [], []),
    (
        "sql",
        "USE SCHEMA ANALYTICS_DB.REPORTING;\nUSE SCHEMA REPORTING;\n",
        [],
        [(2, "two_part", "REPORTING")],
    ),
    ("sql", "USE DATABASE SALES_DB;\n", [], [(1, "outside_boundary", "SALES_DB")]),
    # st rebound to a StringIO: not Streamlit, so the write is a file write and the SQL is scanned
    (
        "py",
        'import io\nst = io.StringIO()\nst.write("SELECT * FROM DB.RAW.X")\nsession.sql(st.getvalue())\n',
        [(3, "RAW")],
        [],
    ),
    # accepted exclusion: Streamlit prose call, st bound by import streamlit as st
    ("py", 'import streamlit as st\nst.caption("Rows from DB.RAW.X")\n', [], []),
    # a module without a streamlit import has no prose roots
    ("py", 'st.caption("Rows from DB.RAW.X")\n', [(1, "RAW")], []),
    # a statement-keyword literal outside a query call reports three-part names only
    ("py", 'raise ValueError("Select a file from data.csv")\n', [], []),
    ("py", 'session.sql("SELECT * FROM PUBLIC.X")\n', [], [(1, "two_part", "PUBLIC.X")]),
    # a lone CR is a line break for the scanners and no line for the source
    ("py", 'session.sql("SELECT 1\\rFROM DB.RAW.X")\n', [(1, "RAW")], []),
    # CRLF: the \\n escape is one more line in the text, as for any "\\n" escape
    ("py", 'session.sql("SELECT 1\\r\\nFROM DB.RAW.X")\n', [(2, "RAW")], []),
    # SQL held in a variable keeps the two-part check: its opener is all upper or all lower case
    (
        "py",
        'Q = "SELECT * FROM SALES.ORDERS"\nsession.sql(Q)\n',
        [],
        [(1, "two_part", "SALES.ORDERS")],
    ),
    (
        "py",
        'Q = """\nSELECT *\nFROM SALES.ORDERS\n"""\nsession.sql(Q)\n',
        [],
        [(3, "two_part", "SALES.ORDERS")],
    ),
    ("py", 'q = "select * from sales.orders"\n', [], [(1, "two_part", "SALES.ORDERS")]),
    # a title-case opener is prose: no two-part finding
    ("py", 'raise ValueError("Update from data.csv failed")\n', [], []),
    ("py", 'st_note = "With data from orders.csv"\n', [], []),
    # `//` starts a comment in Snowflake: the apostrophe after it opens no string
    (
        "sql",
        "SELECT COUNT(*) AS n // '\nFROM SALES_DB.PUBLIC.X // '\n",
        [],
        [(2, "outside_boundary", "SALES_DB.PUBLIC.X")],
    ),
    (
        "py",
        "session.sql(\"SELECT 1 AS n // '\\nFROM SALES_DB.PUBLIC.X // '\")\n",
        [],
        [(2, "outside_boundary", "SALES_DB.PUBLIC.X")],
    ),
    # ... and the deny scan agrees: a denied name after `//` is commented out
    ("sql", "SELECT 1 // FROM DB.RAW.T\n", [], []),
    ("py", 'session.sql("SELECT 1 // FROM DB.RAW.T")\n', [], []),
    # `//`, `--` and `/*` inside a string literal start no comment: the name after it counts
    ("sql", "SELECT '//' AS s FROM DB.RAW.T\n", [(1, "RAW")], []),
    ("sql", "SELECT 'a--b' AS s, DB.RAW.T.c FROM ANALYTICS_DB.REPORTING.X\n", [(1, "RAW")], []),
    ("sql", "SELECT '/*' AS s, DB.RAW.T.c FROM ANALYTICS_DB.REPORTING.X -- */\n", [(1, "RAW")], []),
    # a block comment still hides a name, and keeps the line numbers after it
    ("sql", "SELECT 1 /* DB.RAW.T\n */ FROM DB.RAW.U\n", [(2, "RAW")], []),
    # a file the parser rejects is read in full, the boundary included
    (
        "py",
        'session.sql("SELECT * FROM SALES_DB.PUBLIC.X")\x00\n',
        [],
        [(1, "outside_boundary", "SALES_DB.PUBLIC.X")],
    ),
]


def _scan(tmp_path, kind, src):
    path = tmp_path / f"case.{kind}"
    path.write_text(src, encoding="utf-8")
    res = check_paths([path], POLICY)
    deny = sorted({(f["line"], f["schema"]) for f in res["findings"] if f["reason"] == "denied"})
    boundary = sorted(
        {(f["line"], f["reason"], f["ref"]) for f in res["findings"] if f["reason"] != "denied"}
    )
    return deny, boundary, res


@pytest.mark.parametrize(("kind", "src", "deny", "boundary"), CORPUS)
def test_corpus(tmp_path, kind, src, deny, boundary):
    got_deny, got_boundary, res = _scan(tmp_path, kind, src)
    assert (got_deny, got_boundary) == (sorted(deny), sorted(boundary))
    assert "__expr__" not in str(res).lower()  # the folding placeholder never reaches output


def test_corpus_is_big_enough():
    assert len(CORPUS) >= 60


@pytest.mark.parametrize(("kind", "src", "deny", "boundary"), CORPUS)
def test_check_paths_never_raises_on_the_corpus(tmp_path, kind, src, deny, boundary):
    path = tmp_path / f"case.{kind}"
    path.write_text(src, encoding="utf-8")
    assert isinstance(check_paths([path], POLICY)["findings"], list)


@pytest.mark.parametrize("terms", [1000, 5000])
def test_long_concatenation_is_folded_without_recursion(tmp_path, terms):
    src = 'q = "SELECT * FROM DB.RAW.X"' + ' + ""' * terms + "\n"
    deny, boundary, _ = _scan(tmp_path, "py", src)
    assert deny == [(1, "RAW")]
