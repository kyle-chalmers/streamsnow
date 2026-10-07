"""The read-only source probe behind configure and doctor --live (C15)."""

from __future__ import annotations

import json
import re

from streamsnow import probe
from streamsnow import sf_exec as sx


class FakeSnow:
    """Answers the probe's statements the way `snow sql --format json` prints them.
    SHOW ... LIKE is case-insensitive in Snowflake, so the filter is too."""

    def __init__(self, role="ANALYST", databases=(), schemas=(), code=0, err=""):
        self.role, self.databases, self.schemas = role, databases, schemas
        self.code, self.err = code, err
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv, stdin, timeout):
        self.calls.append((argv, stdin))
        if self.code:
            return self.code, "", self.err
        out = []
        for stmt in [s.strip() for s in stdin.split("\n;\n") if s.strip()]:
            if stmt.startswith(("ALTER SESSION", "USE ")):
                out.append([{"status": "ok"}])
            elif stmt.startswith("SELECT CURRENT_ROLE()"):
                out.append([{"ROLE": self.role}])
            elif stmt == "SHOW DATABASES":
                out.append([{"name": n, "kind": k} for n, k in self.databases])
            elif stmt.startswith("SHOW TERSE SCHEMAS LIKE"):
                want = re.search(r"LIKE '([^']*)'", stmt).group(1)
                out.append(
                    [
                        {"name": s, "database_name": d}
                        for d, s in self.schemas
                        if s.upper() == want.upper()
                    ]
                )
            else:
                raise AssertionError(f"unexpected statement: {stmt}")
        return 0, json.dumps(out), ""


def test_visible_and_not_visible_with_the_role_that_ran():
    fake = FakeSnow(
        databases=[("ANALYTICS_DB", "STANDARD"), ("PARTNER_SHARE", "IMPORTED DATABASE")],
        schemas=[("ANALYTICS_DB", "REPORTING"), ("SALES_DB", "MARTS")],
    )
    report = probe.probe_schemas(
        "acme", ["analytics_db.reporting", "FINANCE_DB.MARTS"], runner=fake
    )
    assert report.role == "ANALYST" and report.error == ""
    assert [(r.target, r.status, r.role) for r in report.results] == [
        ("ANALYTICS_DB.REPORTING", probe.VISIBLE, "ANALYST"),
        ("FINANCE_DB.MARTS", probe.NOT_VISIBLE, "ANALYST"),  # MARTS exists, other database
    ]
    assert report.imported_databases == ("PARTNER_SHARE",)
    argv, stdin = fake.calls[0]
    assert "--connection=acme" in argv
    body = [s for s in stdin.split("\n;\n") if s.strip()][3:]  # after the session prefix
    assert all(s.startswith(("SELECT CURRENT_ROLE()", "SHOW ")) for s in body), body
    assert "USE SECONDARY ROLES NONE" in stdin  # the role's own grants, nothing borrowed


def test_catalog_names_compare_exactly_as_snowflake_stores_them():
    """A schema created as "reporting" (quoted, lower case) is not REPORTING: config
    entries are unquoted, which Snowflake stores upper-case."""
    fake = FakeSnow(
        databases=[("partner_share", "IMPORTED DATABASE")],
        schemas=[("analytics_db", "reporting"), ("ANALYTICS_DB", "MARTS")],
    )
    report = probe.probe_schemas(
        "acme", ["ANALYTICS_DB.REPORTING", "ANALYTICS_DB.MARTS"], runner=fake
    )
    assert [(r.target, r.status) for r in report.results] == [
        ("ANALYTICS_DB.REPORTING", probe.NOT_VISIBLE),
        ("ANALYTICS_DB.MARTS", probe.VISIBLE),
    ]
    assert report.imported_databases == ("partner_share",)


def test_no_connection_is_unverified_and_never_calls_snow():
    report = probe.probe_schemas(None, ["ANALYTICS_DB.REPORTING"], runner=FakeSnow())
    assert report.error and report.role is None
    assert [r.status for r in report.results] == [probe.UNVERIFIED]


def test_a_snow_failure_is_unverified_with_the_reason():
    fake = FakeSnow(code=1, err="Incorrect username or password was specified.")
    report = probe.probe_schemas("acme", ["ANALYTICS_DB.REPORTING"], runner=fake)
    assert [r.status for r in report.results] == [probe.UNVERIFIED]
    assert "Incorrect username" in report.error


def test_a_missing_snow_cli_is_unverified():
    def missing(argv, stdin, timeout):
        raise sx.SnowError("the Snowflake CLI (`snow`) is not on PATH")

    report = probe.probe_schemas("acme", ["ANALYTICS_DB.REPORTING"], runner=missing)
    assert report.results[0].status == probe.UNVERIFIED and "not on PATH" in report.error


def test_show_output_without_database_name_is_unverified_not_a_guess():
    class NoDbColumn(FakeSnow):
        def __call__(self, argv, stdin, timeout):
            code, out, err = super().__call__(argv, stdin, timeout)
            data = json.loads(out)
            data[-1] = [{"name": "REPORTING"}]
            return code, json.dumps(data), err

    fake = NoDbColumn(schemas=[("ANALYTICS_DB", "REPORTING")])
    (result,) = probe.probe_schemas("acme", ["ANALYTICS_DB.REPORTING"], runner=fake).results
    assert result.status == probe.UNVERIFIED and "database_name" in result.detail


def test_probe_schema_is_the_single_target_form():
    fake = FakeSnow(schemas=[("ANALYTICS_DB", "REPORTING")])
    assert probe.probe_schema("acme", "ANALYTICS_DB.REPORTING", runner=fake).status == probe.VISIBLE
