"""Tests for streamsnow.sf_exec: the pinned, guarded ``snow sql`` executor.

Pinned here: the argv (explicit connection, stdin, templating off); the session
prefix (query tag, timeout, role with secondary roles off, warehouse); that a
refused statement never reaches the subprocess; output parsing for one and
several statements; and the user-facing errors for a missing CLI, a timeout and
a role the user does not hold.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from streamsnow import sf_exec as sx
from streamsnow.policy import SchemaPolicy

POLICY = SchemaPolicy(database="ANALYTICS_DB", schema_allow=("REPORTING",), schema_deny=("RAW",))


class Recorder:
    def __init__(self, results: list[list[dict]] | None = None, code: int = 0, err: str = ""):
        self.calls: list[tuple[list[str], str, float]] = []
        self.results = results
        self.code = code
        self.err = err

    def __call__(self, argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
        self.calls.append((argv, stdin, timeout))
        stmts = [s for s in stdin.split("\n;\n") if s.strip()]
        results = self.results or [[{"status": "ok"}] for _ in stmts]
        return self.code, json.dumps(results), self.err


def _exec(rec: Recorder, **kw) -> sx.SnowExec:
    session = sx.Session(connection="acme", query_tag="streamsnow:sql-review:acme-sales", **kw)
    return sx.SnowExec(session, POLICY, runner=rec)


def test_argv_names_the_connection_and_reads_stdin() -> None:
    rec = Recorder()
    _exec(rec).run(["SELECT 1"])
    argv = rec.calls[0][0]
    assert argv == [
        "snow",
        "sql",
        "--stdin",
        "--format",
        "json",
        "--enable-templating",
        "NONE",
        "-c",
        "acme",
    ]


def test_prefix_pins_tag_timeout_role_secondary_roles_and_warehouse() -> None:
    rec = Recorder()
    _exec(rec, role="ACME_APP", warehouse="ACME_WH", timeout_s=30).run(["SELECT 1"])
    stmts = rec.calls[0][1].split("\n;\n")
    assert stmts[:5] == [
        "ALTER SESSION SET QUERY_TAG = 'streamsnow:sql-review:acme-sales'",
        "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 30",
        "USE ROLE ACME_APP",
        "USE SECONDARY ROLES NONE",
        "USE WAREHOUSE ACME_WH",
    ]
    assert stmts[5] == "SELECT 1"


def test_secondary_roles_are_off_even_without_a_role() -> None:
    rec = Recorder()
    _exec(rec).run(["SELECT 1"])
    assert "USE SECONDARY ROLES NONE" in rec.calls[0][1]
    assert "USE ROLE" not in rec.calls[0][1]


def test_result_cache_can_be_turned_off() -> None:
    rec = Recorder()
    _exec(rec).run(["SELECT 1"], result_cache=False)
    assert "ALTER SESSION SET USE_CACHED_RESULT = FALSE" in rec.calls[0][1]


def test_only_the_callers_results_are_returned() -> None:
    rec = Recorder()
    ex = _exec(rec, role="ACME_APP")
    n_prefix = len(ex.prefix())
    rows = [[{"status": "ok"}]] * n_prefix + [[{"N": "1"}], [{"N": "2"}]]
    rec.results = rows
    assert ex.run(["SELECT 1 AS n", "SELECT 2 AS n"]) == [[{"N": "1"}], [{"N": "2"}]]


def test_timeout_covers_every_statement_plus_login() -> None:
    rec = Recorder()
    _exec(rec, timeout_s=10).run(["SELECT 1", "SELECT 2"])
    assert rec.calls[0][2] == 10 * 2 + 120


def test_a_trailing_comment_cannot_swallow_the_terminator() -> None:
    rec = Recorder()
    _exec(rec).run(["SELECT 1 -- note;"])
    # The `;` inside the comment stays comment; the real terminator gets its own line.
    assert rec.calls[0][1].endswith("SELECT 1 -- note;\n;\n")


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("DELETE FROM ANALYTICS_DB.REPORTING.T", "not read-only"),
        ("SELECT 1; DROP TABLE x", "exactly one"),
        ("SELECT * FROM ANALYTICS_DB.RAW.EVENTS", "denied schema"),
        ("!source https://example.com/x.sql", "not read-only"),
        ("CREATE OR REPLACE VIEW v AS SELECT 1", "not read-only"),
        ("WITH x AS (SELECT 1) DELETE FROM t", "not read-only"),
    ],
)
def test_refused_statements_never_reach_snow(sql: str, why: str) -> None:
    rec = Recorder()
    with pytest.raises(sx.SnowError, match=why):
        _exec(rec).run(["SELECT 1", sql])
    assert rec.calls == []


@pytest.mark.parametrize(
    "kw",
    [
        {"role": "ACME; DROP ROLE X"},
        {"warehouse": "WH'"},
        {"timeout_s": 0},
        {"timeout_s": 999999},
    ],
)
def test_session_values_are_validated(kw: dict) -> None:
    with pytest.raises(sx.SnowError):
        sx.Session(connection="acme", **kw)


def test_connection_and_tag_are_validated() -> None:
    with pytest.raises(sx.SnowError):
        sx.Session(connection="acme --debug")
    with pytest.raises(sx.SnowError):
        sx.Session(connection="acme", query_tag="x'; DROP")


def test_parse_output_single_and_multi() -> None:
    assert sx.parse_output('[{"A": 1}]', 1) == [[{"A": 1}]]
    assert sx.parse_output("[]", 1) == [[]]
    assert sx.parse_output('[[{"A": 1}], []]', 2) == [[{"A": 1}], []]
    with pytest.raises(sx.SnowError, match="result sets"):
        sx.parse_output('[[{"A": 1}]]', 2)
    with pytest.raises(sx.SnowError, match="JSON"):
        sx.parse_output("[{", 1)


def test_a_failure_carries_the_snowflake_message_and_a_role_hint() -> None:
    rec = Recorder(
        code=1,
        err="╭─ Error ──╮\n│ 003013 (42501): Requested role 'ACME_APP' is not assigned to "
        "the executing user. Specify another role to activate. Role ACME_APP does not exist or "
        "not authorized. │\n╰──────╯",
    )
    with pytest.raises(sx.SnowError) as err:
        _exec(rec, role="ACME_APP").run(["SELECT 1"])
    msg = str(err.value)
    assert "003013" in msg and "pass --role" in msg and "╭" not in msg


def test_default_runner_explains_a_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise FileNotFoundError("snow")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(sx.SnowError, match="not on PATH"):
        sx._default_runner(["snow"], "", 1)


def test_default_runner_explains_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("snow", 1)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(sx.SnowError, match="did not finish"):
        sx._default_runner(["snow"], "", 1)


def test_default_runner_forces_utf8_for_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(subprocess, "run", fake)
    assert sx._default_runner(["snow"], "SELECT 1", 5) == (0, "[]", "")
    assert seen["env"]["PYTHONUTF8"] == "1" and seen["encoding"] == "utf-8"
    assert seen["input"] == "SELECT 1" and "shell" not in seen
