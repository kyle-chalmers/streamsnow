"""SchemaPolicy: the database-aware governance boundary (#78)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from streamsnow.config import Config
from streamsnow.policy import (
    ALLOWED,
    BOUNDARY_VERDICTS,
    DENIED,
    IGNORED,
    OUTSIDE_BOUNDARY,
    TWO_PART,
    SchemaPolicy,
    display_name,
    split_name,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "streamsnow.config.example.yaml"

POLICY = SchemaPolicy(
    sources=("ANALYTICS_DB.REPORTING", "FINANCE_DB.MARTS"),
    app_data="STREAMSNOW_APPS.STREAMSNOW_REPORTING",
    schema_deny=("RAW", "FINANCE_DB.STAGING"),
    read_exceptions=("ANALYTICS_DB.RAW.CALENDAR_DIM",),
)


@pytest.mark.parametrize(
    ("text", "parts"),
    [
        ("analytics_db.reporting.orders", ("ANALYTICS_DB", "REPORTING", "ORDERS")),
        ('SALES_DB."PUBLIC.EXTRA".LEADS', ("SALES_DB", "PUBLIC.EXTRA", "LEADS")),
        ('"analytics_db"."reporting".t', ("analytics_db", "reporting", "T")),
        ('"a""b".x', ('a"b', "X")),
        ("FINANCE_DB\n   .MARTS .\tFEES", ("FINANCE_DB", "MARTS", "FEES")),
        ('"P.Q"', ("P.Q",)),
        ("not a name", ()),
    ],
)
def test_split_name_follows_snowflake_identifier_rules(text, parts):
    assert split_name(text) == parts


def test_display_name_quotes_only_what_needs_it():
    assert display_name(("SALES_DB", "PUBLIC.EXTRA", "LEADS")) == 'SALES_DB."PUBLIC.EXTRA".LEADS'
    assert display_name(("analytics_db", "reporting", "T")) == '"analytics_db"."reporting".T'
    assert display_name(("SALES_DB", "PUBLIC", "D")) == "SALES_DB.PUBLIC.D"


@pytest.mark.parametrize(
    ("ref", "verdict"),
    [
        ("ANALYTICS_DB.REPORTING.ORDERS", ALLOWED),
        ("finance_db.marts.fees", ALLOWED),  # unquoted folds to upper case
        ('"FINANCE_DB"."MARTS"."FEES"', ALLOWED),  # quoted, and exactly the stored name
        ('"analytics_db"."reporting".T', OUTSIDE_BOUNDARY),  # quoted lower case: another schema
        ('SALES_DB."PUBLIC.EXTRA".LEADS', OUTSIDE_BOUNDARY),  # three parts, not four
        ("STREAMSNOW_APPS.STREAMSNOW_REPORTING.DAILY_REVENUE", ALLOWED),  # app data
        ("ANALYTICS_DB.RAW.CALENDAR_DIM", ALLOWED),  # exact read exception
        ("ANALYTICS_DB.RAW.EVENTS", DENIED),  # bare deny: every database
        ("SALES_DB.RAW.EVENTS", DENIED),
        ('SALES_DB."raw".EVENTS', DENIED),  # deny stays case-insensitive (Defaults 2)
        ("FINANCE_DB.STAGING.FEES", DENIED),  # qualified deny: its database
        ("SALES_DB.STAGING.LEADS", OUTSIDE_BOUNDARY),  # ...and only there
        ("SALES_DB.PUBLIC.LEADS", OUTSIDE_BOUNDARY),
        ("ANALYTICS_DB.MARTS.ORDERS", OUTSIDE_BOUNDARY),  # source schema name, other database
        ("REPORTING.ORDERS", TWO_PART),
        ("RAW.EVENTS", DENIED),  # two-part names meet bare entries
        ("STAGING.FEES", TWO_PART),  # ...never qualified ones (the two_part class handles it)
        ("INFORMATION_SCHEMA.TABLES", IGNORED),
        ("SALES_DB.INFORMATION_SCHEMA.TABLES", IGNORED),
        ("SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY", IGNORED),
        ('"P.Q"', IGNORED),  # a one-part (CTE) name, even with a dot inside its quotes
        ("params", IGNORED),
    ],
)
def test_classify(ref, verdict):
    assert POLICY.classify(ref) == verdict


def test_without_a_boundary_only_the_deny_list_speaks():
    deny_only = SchemaPolicy(schema_deny=("RAW",))
    assert deny_only.classify("SALES_DB.PUBLIC.LEADS") == IGNORED
    assert deny_only.classify("REPORTING.ORDERS") == IGNORED
    assert deny_only.classify("SALES_DB.RAW.EVENTS") == DENIED


def test_boundary_schemas_are_sources_then_app_data_deduplicated():
    p = SchemaPolicy(sources=("a.b", "A.B", "C.D"), app_data="c.d")
    assert p.boundary_schemas == ("A.B", "C.D")
    assert p.in_boundary("A", "B") and not p.in_boundary("a", "b")


def test_is_denied_bare_and_qualified():
    assert POLICY.is_denied("raw") and POLICY.is_denied("RAW", "ANY_DB")
    assert POLICY.is_denied("STAGING", "FINANCE_DB")
    assert POLICY.is_denied("staging", "finance_db")  # case-insensitive
    assert not POLICY.is_denied("STAGING", "SALES_DB")
    assert not POLICY.is_denied("STAGING")  # database unknown: bare entries only
    assert not POLICY.is_denied("REPORTING")


def test_schema_and_database_references():
    assert POLICY.classify_schema("ANALYTICS_DB", "REPORTING") == ALLOWED
    assert POLICY.classify_schema("SALES_DB", "PUBLIC") == OUTSIDE_BOUNDARY
    assert POLICY.classify_schema(None, "REPORTING") == TWO_PART
    assert POLICY.classify_schema("FINANCE_DB", "STAGING") == DENIED
    assert POLICY.classify_database("FINANCE_DB") == ALLOWED
    assert POLICY.classify_database("SALES_DB") == OUTSIDE_BOUNDARY


def test_enforcing_and_the_verdicts_that_follow_it():
    assert not POLICY.enforcing
    assert SchemaPolicy(boundary="enforce").enforcing
    assert BOUNDARY_VERDICTS == (OUTSIDE_BOUNDARY, TWO_PART)


def test_from_governance_reads_the_v2_api():
    cfg = Config.from_dict(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    policy = SchemaPolicy.from_governance(cfg.governance)
    assert policy.sources == ("ANALYTICS_DB.ANALYTICS", "ANALYTICS_DB.REPORTING")
    assert policy.boundary == "warn"
    assert policy.app_data == "STREAMSNOW_APPS.STREAMSNOW_REPORTING"


def test_split_name_reads_an_empty_middle_part_as_public():
    """Snowflake resolves ``DB..OBJ`` as ``DB.PUBLIC.OBJ`` (name resolution docs)."""
    from streamsnow.policy import split_name

    assert split_name("sales_db..leads") == ("SALES_DB", "PUBLIC", "LEADS")
    assert split_name('"Sales"..T') == ("Sales", "PUBLIC", "T")
    assert split_name("A.B.C") == ("A", "B", "C")
    assert split_name("A..") == ()
