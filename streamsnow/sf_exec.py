"""Run read-only SQL on Snowflake for the live SQL review, through ``snow sql``.

Why this exists
---------------
The live review (``streamsnow sql-review probe | run | bench``) has to answer
"does this section return what the app shows, under the role the app runs
as?". Deploy's ``verify.run_query_snow`` cannot answer that: it uses whatever
connection is the CLI default and whatever role that connection carries. A
review run that way tests the wrong grants. A person whose default role can
read a schema the app's role cannot would get a clean review of an app that
fails in production. This executor makes the connection explicit and pins the
session before any reviewed statement runs:

- ``-c <connection>`` always: the configured ``snowflake.connection_name`` or
  ``--connection``, never an implicit default;
- ``USE ROLE`` then ``USE SECONDARY ROLES NONE``: without the second statement,
  a user whose default secondary roles are ``ALL`` keeps every other role they
  hold, and a read the app's role cannot do still succeeds;
- ``USE WAREHOUSE``, a ``QUERY_TAG`` (``streamsnow:sql-review:<slug>``) so the
  queries are findable in query history, and ``STATEMENT_TIMEOUT_IN_SECONDS``
  so one runaway section cannot burn the warehouse for an hour.

Every statement the caller passes, never just the app's sections, must pass the
review SQL's read-only allowlist (``sql_review._verify_read_only``) and the
governance checks (``check_schema_refs``) **before** the subprocess starts. A
denied schema is always a :class:`SnowError` and nothing is sent. A name outside
``governance.sources`` and app data, or a two-part name, is refused the same way
under ``governance.boundary: enforce``; under ``warn`` the review still runs and
says so once on stderr, because a review that silently queried outside the
boundary would hide the grant gap the deployed app will hit. The session prefix
above is built here from validated identifiers only, so it is the one part
exempt from the allowlist (``ALTER SESSION`` / ``USE`` are not review SQL).

``snow sql`` runs with ``--enable-templating NONE`` (by default it rewrites
``&name`` and ``<% %>`` inside the SQL, which breaks a literal like
``'R&D'``) and reads the script from stdin, which keeps the SQL out of process
listings and clear of the Windows command-line length limit.

The subprocess is injectable (``runner=``), so tests run with canned ``snow``
output and no Snowflake.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from .config import ConfigError, quote_sql_literal, validate_identifier, validate_name
from .policy import SchemaPolicy

#: (argv, stdin text, timeout seconds) -> (returncode, stdout, stderr)
Runner = Callable[[list[str], str, float], tuple[int, str, str]]

#: One result set: the rows ``snow sql --format json`` printed for a statement.
ResultSet = list[dict]

DEFAULT_TIMEOUT_S = 120
#: Allowance per ``snow`` call for login (SSO can open a browser) and startup.
_LOGIN_ALLOWANCE_S = 120
_TAG_RE = re.compile(r"^[a-z][a-z0-9:-]{0,200}$")


class SnowError(RuntimeError):
    """A statement was refused before sending, or ``snow`` failed. User-facing."""


@dataclass(frozen=True)
class Session:
    """How the review connects. Validated on construction."""

    connection: str
    role: str | None = None
    warehouse: str | None = None
    query_tag: str = "streamsnow:sql-review"
    timeout_s: int = DEFAULT_TIMEOUT_S

    def __post_init__(self) -> None:
        try:
            validate_name(self.connection, "connection")
            if self.role is not None:
                validate_identifier(self.role, "role")
            if self.warehouse is not None:
                validate_identifier(self.warehouse, "warehouse")
        except ConfigError as exc:
            raise SnowError(str(exc)) from exc
        if not _TAG_RE.match(self.query_tag):
            raise SnowError(f"query tag {self.query_tag!r} must match {_TAG_RE.pattern}")
        if not isinstance(self.timeout_s, int) or not 1 <= self.timeout_s <= 3600:
            raise SnowError("timeout must be a whole number of seconds between 1 and 3600")


def _default_runner(argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(  # noqa: S603  (argv list, no shell)
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SnowError(
            "the Snowflake CLI (`snow`) is not on PATH; install it with "
            "`uv tool install snowflake-cli`, then add a connection (`streamsnow doctor` checks both)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SnowError(
            f"`snow sql` did not finish within {int(timeout)}s (login included); "
            "raise --timeout or review one page at a time with --page"
        ) from exc
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def guard(statement: str, policy: SchemaPolicy | None, *, warned: set[str] | None = None) -> None:
    """Refuse anything that is not one read-only statement inside the boundary."""
    from .tools import sql_review as sr  # noqa: PLC0415  (import cycle: tools import config)

    if len(sr._split_statements(statement)) != 1:
        raise SnowError("refused: expected exactly one SQL statement per call")
    problems = sr._verify_read_only(statement)
    if problems:
        raise SnowError("refused, not read-only review SQL: " + "; ".join(problems))
    if policy is None:
        return
    from .tools.check_schema_refs import find_boundary_refs, find_denied_refs  # noqa: PLC0415

    hits = find_denied_refs(statement, policy)
    if hits:
        names = ", ".join(sorted({schema for _, schema in hits}))
        raise SnowError(
            f"refused: references governance-denied schema(s) {names}; "
            "nothing was sent to Snowflake"
        )
    outside = find_boundary_refs(statement, policy)
    if not outside:
        return
    if policy.enforcing:
        names = ", ".join(sorted({r.ref for r in outside}))
        raise SnowError(
            f"refused: references {names} outside governance.sources and app_data "
            "(governance.boundary: enforce); nothing was sent to Snowflake"
        )
    for ref in outside:
        message = f"warning: {ref.detail} (governance.boundary: warn)"
        if warned is None or message not in warned:
            print(message, file=sys.stderr)
            if warned is not None:
                warned.add(message)


def _without_terminator(statement: str) -> str:
    """The statement minus a final ``;`` (and anything after it, which the
    guard has already proven is only comments and whitespace)."""
    from .tools import sql_review as sr  # noqa: PLC0415

    masked = sr._mask_strings_and_comments(statement)
    cut = masked.rfind(";")
    if cut != -1 and not masked[cut + 1 :].strip():
        statement = statement[:cut]
    return statement.strip()


def parse_output(stdout: str, expected: int) -> list[ResultSet]:
    """``snow sql --format json`` output -> one row list per statement.

    One statement prints a flat array of row objects; several print an array of
    arrays. Decimals arrive as strings (``snow`` encodes them with ``str``).
    """
    text = stdout.strip()
    if not text:
        # Never a real result: even one statement prints `[]`. Reading it as
        # empty result sets would report every object missing, every section 0 rows.
        raise SnowError("`snow sql` printed no output; nothing can be reported from this call")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnowError(f"could not read `snow sql` JSON output ({exc.msg})") from exc
    if not isinstance(data, list):
        raise SnowError("unexpected `snow sql` output: not a JSON array")
    if expected == 1 and (not data or all(isinstance(r, dict) for r in data)):
        return [data]
    if not all(isinstance(r, list) for r in data):
        raise SnowError("unexpected `snow sql` output: mixed result shapes")
    if len(data) != expected:
        raise SnowError(f"`snow sql` returned {len(data)} result sets, expected {expected}")
    return data


#: A quoted value in an error is kept only after a word that names an object.
_KEEP_QUOTED_RE = re.compile(
    r"(?i)\b(identifier|object|schema|database|table|view|column|role|warehouse|function|"
    r"connection|stage|integration)\s+$"
)


_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'")


def _mask_values(text: str) -> str:
    """Replace quoted values with '…' unless they name an object.

    A conversion error quotes the offending cell (``Numeric value 'Jane …' is
    not recognized``), and error text lands in the run's JSON files, which must
    hold aggregates only.
    """
    out: list[str] = []
    last = 0
    # A whole literal, doubled-quote and backslash escapes included, so a value
    # like 'O''Brien' is masked as one span, never split around its escape.
    for m in _LITERAL_RE.finditer(text):
        out.append(text[last : m.start()])
        out.append(m.group(0) if _KEEP_QUOTED_RE.search(text[: m.start()]) else "'…'")
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _error_detail(stderr: str, stdout: str) -> str:
    """The useful part of a ``snow`` failure, without its box-drawing frame
    or any quoted data value."""
    text = stderr.strip() or stdout.strip()
    lines = []
    for raw in text.splitlines():
        line = raw.strip().strip("│╭╮╰╯─ ").strip()
        if line and not set(line) <= set("─╭╮╰╯│ "):
            lines.append(line)
    return _mask_values(" ".join(lines))[-600:] or "no error output"


class SnowExec:
    """Execute guarded read-only statements in one pinned ``snow sql`` session."""

    def __init__(
        self,
        session: Session,
        policy: SchemaPolicy | None,
        runner: Runner | None = None,
        snow: str = "snow",
    ) -> None:
        self.session = session
        self.policy = policy
        #: Boundary warnings already printed, so a repeated name warns once per run.
        self.warned: set[str] = set()
        self._runner = runner or _default_runner
        self._snow = snow

    def prefix(self, *, result_cache: bool = True) -> list[str]:
        """The tool-built session statements, from validated values only."""
        s = self.session
        stmts = [
            f"ALTER SESSION SET QUERY_TAG = {quote_sql_literal(s.query_tag)}",
            f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {int(s.timeout_s)}",
        ]
        if not result_cache:
            stmts.append("ALTER SESSION SET USE_CACHED_RESULT = FALSE")
        if s.role:
            stmts.append(f"USE ROLE {s.role}")
        # Always: even on the connection's default role, secondary roles would
        # widen what the session can read beyond what the review reports.
        stmts.append("USE SECONDARY ROLES NONE")
        if s.warehouse:
            stmts.append(f"USE WAREHOUSE {s.warehouse}")
        return stmts

    def argv(self) -> list[str]:
        return [
            self._snow,
            "sql",
            "--stdin",
            "--format",
            "json",
            "--enable-templating",
            "NONE",
            # One token: a connection name can never read as another flag.
            f"--connection={self.session.connection}",
        ]

    def run(self, statements: list[str], *, result_cache: bool = True) -> list[ResultSet]:
        """Run ``statements`` after the session prefix; one result set each.

        Every statement is guarded first, so a refusal sends nothing at all.
        ``snow`` stops at the first failing statement; that surfaces as one
        :class:`SnowError` for the whole call.
        """
        if not statements:
            return []
        for stmt in statements:
            guard(stmt, self.policy, warned=self.warned)
        prefix = self.prefix(result_cache=result_cache)
        # The terminator goes on its own line: a statement ending in a `--`
        # comment would otherwise swallow the `;` after it.
        body = [_without_terminator(s) for s in statements]
        script = "\n;\n".join([*prefix, *body]) + "\n;\n"
        timeout = self.session.timeout_s * len(statements) + _LOGIN_ALLOWANCE_S
        code, out, err = self._runner(self.argv(), script, timeout)
        if code != 0:
            detail = _error_detail(err, out)
            hint = ""
            if self.session.role and re.search(
                r"role .*(does not exist|not authorized|not assigned)", detail, re.I
            ):
                hint = (
                    f" (this review runs as role {self.session.role}; pass --role with a role "
                    "you hold that the app's grants reach)"
                )
            elif self.session.warehouse and re.search(
                r"warehouse .*(does not exist|not authorized)", detail, re.I
            ):
                hint = (
                    f" (this session runs USE WAREHOUSE {self.session.warehouse}; pass "
                    "--warehouse with a warehouse the role can use)"
                )
            raise SnowError(f"`snow sql` failed: {detail}{hint}")
        results = parse_output(out, len(prefix) + len(statements))
        return results[len(prefix) :]


def first_row(result: ResultSet) -> dict:
    """The first row of a result set, keys upper-cased (``snow`` casing varies)."""
    return {str(k).upper(): v for k, v in result[0].items()} if result else {}


def upper_rows(result: ResultSet) -> list[dict]:
    return [{str(k).upper(): v for k, v in row.items()} for row in result]
