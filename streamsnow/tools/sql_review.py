#!/usr/bin/env python3
"""Generate and verify ``sql_review/``: SQL a person can open, run and trace.

Why this exists
---------------
Apps store their queries as templates under ``apps/<slug>/queries/*.sql`` and
render them at runtime with ``{TOKEN}`` fragments and ``:bind`` params. A
reviewer reading the raw templates cannot run them, and a dashboard whose
numbers nobody can independently re-run is a dashboard nobody can sign off. So
every app carries ``sql_review/``: one SQL file per page, one runnable section
per metric, that a person opens in DataGrip, any editor, or Snowsight and runs
section by section to trace each visual back to the data.

Layout (per app)
----------------
::

    apps/<slug>/sql_review/
      AGENTS.md                       folder rules (CLAUDE.md imports it)
      README.md                       human index; tables between markers are generated
      index.yaml                      the single source of truth (hand-edited)
      01_overview.sql                 one file per page, NN = position in the app's nav
      app_specific_reporting_objects/
        <DB>.<SCHEMA>.<OBJECT>.sql    maintained DDL for objects built for this app

``index.yaml`` (see ``sql_review_index``) lists each page's metrics in
on-screen order with the app query behind each one. A page file's first metric
tag is always line 9, and every section is self-contained::

    --1_total_revenue
    WITH params AS (
        -- Review window from index.yaml. Edit it here to rerun this section.
        SELECT
            DATEADD(DAY, -30, CURRENT_DATE()) AS start_date,
            CURRENT_DATE() AS end_date
    )
    SELECT ... WHERE order_date BETWEEN (SELECT start_date FROM params) AND ...;

A metric with a ``summary:`` in the index also gets a summary section right
after its own: the summary's one-line description, the tag ``--N_key_summary``,
then ``WITH detail AS (<the section above, verbatim>)`` and the summary's
SELECT, which returns exactly what the visual shows. An index without
summaries renders exactly as it did before they existed.

``{TOKENS}`` are replaced with the index's sample values and ``:binds`` with
their index values (``params.<name>`` becomes a scalar read of the section's own
``params`` CTE), so a section runs with the cursor in it and Cmd/Ctrl+Enter.
``generate`` applies sqlfluff's layout and capitalisation fixes to each section
(never the rules that can change what SQL means) and ``check`` lints the app's
own queries with the repo's ``.sqlfluff``.

Import-free ``check`` (the CI / pre-commit / validate hook)
-----------------------------------------------------------
``check`` never imports app code. Each generated page file ends with::

    -- Provenance: schema=2 inputs=<sha256/16> output=<sha256/16>

``inputs`` digests index.yaml, the page's navigation entry, every query the
page renders, and the repo's ``.sqlfluff``. ``output`` digests the file itself
with the provenance line normalized. ``check`` recomputes both: an edited query,
index, or hand-edited page file reads as a named failure. ``check`` also
verifies the ``review_value`` markers in page code (AST, no import), the DDL
folder, comment rules, lint, and coverage. Every finding carries a ``kind``:

``index``       index.yaml is invalid or disagrees with the app
``provenance``  a page file or README table is missing, stale, edited, or orphaned
``marker``      page code and index.yaml disagree on ``review_value`` keys
``objects``     the DDL folder and index.yaml ``objects:`` disagree
``lint``        sqlfluff findings in ``queries/*.sql``; a page section that does not parse
``comments``    a CTE without a comment above it, or a comment line over 100 characters
``readonly``    a page section is not read-only SQL
``bind``        a page section still holds a ``:bind`` or an undefined ``$variable``
``coverage``    a nav page or query file that index.yaml does not account for
``advisory``    reported, never gates (comment density over 25%)

``coverage`` follows ``sql_review.coverage`` in streamsnow.config.yaml (``warn``
by default, ``fail`` to gate); ``advisory`` never fails; everything else always
fails, because it means the committed review SQL does not match what the app runs.

Read-only guard
---------------
Rendered output is verified with a statement-root **allowlist**: only
``SELECT`` / ``WITH``-terminating-in-``SELECT`` / ``SHOW`` / ``DESCRIBE`` /
``EXPLAIN`` statements may be emitted (an allowlist, not a write-verb denylist:
the failure mode of a denylist is the statement type nobody thought of). All
structural analysis runs on text with string literals and comments masked, so
literal contents can never influence parsing. A page whose rendered output
violates this is an error; nothing is written, and ``check`` re-verifies
committed files. ``app_specific_reporting_objects/`` holds DDL on purpose and is
exempt: no review verb ever executes it.

Scope honesty: this guard (and the provenance digests) catches accidents and
drift, and blocks templates from ever emitting a write. It is NOT a proof
against a deliberate committer with write access: the digest algorithm is
public and there is no signing key. Repository review remains the trust
boundary for malicious commits.

Verbs
-----
``generate <slug>``  write page files, README tables, and DDL "Used by" lines
                     from index.yaml (+ provenance).
``check [<slug>]``   the import-free gate above (every app when no slug).
                     ``--lint-files F …`` limits sqlfluff to those query files
                     (the pre-commit hook passes the staged ones; CI lints all).
``probe | run | bench | log <slug>``
                     the live review against Snowflake; see ``sql_review_live``.
``compare <slug>``   the screen against ``run``: what each visual received in
                     review preview mode; see ``sql_review_compare``.

``helper <slug>``    refresh the app's review.py from the scaffold (dry run unless
                     ``--apply``; a modified file needs ``--force``).

Exit codes: 0 = clean, 1 = findings/drift/gaps, 2 = tool error.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

from ..config import ConfigError, find_config, load_config
from ..policy import split_name
from . import sql_review_index as sri

#: Bumped when the rendered-file format changes shape: makes every prior file
#: read as drift, which is correct, because format changes need a regenerate.
GENERATOR_SCHEMA = 2

#: Statement roots a review file may contain. SET is restricted separately
#: to session-variable assignments (see _verify_read_only).
ALLOWED_ROOTS = frozenset({"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"})

# Prefix form, plus Snowflake's documented multi-variable form
# `SET (a, b) = (expr, expr)`.
_SET_STMT_RE = re.compile(r"^SET\s+(?:[A-Za-z_][A-Za-z0-9_$]*|\([^)]*\))\s*=", re.IGNORECASE)
_PROVENANCE_RE = re.compile(
    r"^-- Provenance: schema=(\d+) inputs=([0-9a-f]{16}) output=([0-9a-f]{16})\s*$",
    re.MULTILINE,
)

_HEADER_FIELD_RE = re.compile(r"^--\s*(Query|Feeds|Schemas|Params|Tokens):\s*(.*)$")
_TOKEN_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
# A bind marker is `:1` or `:name` in an operand position, so it never directly
# follows an identifier character. Requiring that excludes `::` casts AND
# Snowflake semi-structured access (`payload:1`, `payload:field`, `v[0]:x`,
# `$1:x`), which would otherwise read as a surviving bind and refuse to
# generate a perfectly valid file.
_BIND_RE = re.compile(r"(?<![:\w\"$\'\)\]]):(\d+|[A-Za-z_][A-Za-z0-9_]*)\b")

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
#: Generated page files: two-digit nav position, then the page stem.
_PAGE_FILE_RE = re.compile(r"^\d{2}_[a-z0-9_]+\.sql$")
#: The removed (schema 1) format's rendered files.
_OLD_REVIEW_FILE_GLOB = "*.review.sql"

#: The DDL folder, and its one-file-per-object naming.
OBJECTS_DIR = "app_specific_reporting_objects"

#: A generated page file's header is exactly this many lines, so the first
#: metric tag is always line HEADER_LINES + 1 (line 9).
HEADER_LINES = 8
#: Comment lines (and the README) are kept to this width.
MAX_LINE = 100


class ToolError(RuntimeError):
    """Cannot proceed: reported on stderr with exit 2."""


# Finding kinds. ``coverage`` follows the config policy and ``advisory`` never
# gates; the rest mean the committed review SQL does not match what the app
# runs, and are never downgraded.
KIND_INDEX = sri.KIND_INDEX
KIND_PROVENANCE = "provenance"
KIND_MARKER = sri.KIND_MARKER
KIND_OBJECTS = "objects"
KIND_LINT = "lint"
KIND_COMMENTS = "comments"
KIND_READONLY = "readonly"
KIND_BIND = "bind"
KIND_COVERAGE = sri.KIND_COVERAGE
KIND_ADVISORY = "advisory"
CORRECTNESS_KINDS = (
    KIND_INDEX,
    KIND_PROVENANCE,
    KIND_MARKER,
    KIND_OBJECTS,
    KIND_LINT,
    KIND_COMMENTS,
    KIND_READONLY,
    KIND_BIND,
)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _app_dir(repo: Path, slug: str) -> Path:
    if not _SLUG_RE.match(slug):
        raise ToolError(f"app slug {slug!r} must be kebab-case (^[a-z][a-z0-9-]*$)")
    app = repo / "apps" / slug
    if not app.is_dir():
        raise ToolError(f"no app at {app}")
    return app


def _review_dir(app: Path) -> Path:
    return app / "sql_review"


def _query_files(app: Path) -> list[Path]:
    qdir = app / "queries"
    return sorted(qdir.glob("*.sql")) if qdir.is_dir() else []


def _rel(app: Path, *parts: str) -> str:
    return "/".join(("apps", app.name, *parts))


# --------------------------------------------------------------------------- #
# Query-template parsing
# --------------------------------------------------------------------------- #
def parse_header(text: str) -> dict[str, str]:
    """The leading ``-- Field: value`` block of a query template."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("--"):
            break
        m = _HEADER_FIELD_RE.match(line)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def strip_header(text: str) -> str:
    """Drop the leading comment block so rendered files don't duplicate it."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].startswith("--") or not lines[i].strip()):
        i += 1
    return "\n".join(lines[i:])


def template_tokens(text: str) -> list[str]:
    """Distinct ``{TOKEN}`` placeholders in body order (header lines excluded —
    the ``-- Tokens:`` documentation line names tokens without braces for
    exactly this reason, but be safe about stray commented examples)."""
    seen: list[str] = []
    for line in strip_header(text).splitlines():
        if line.lstrip().startswith("--"):
            continue
        for m in _TOKEN_RE.finditer(line):
            if m.group(1) not in seen:
                seen.append(m.group(1))
    return seen


# --------------------------------------------------------------------------- #
# Read-only guard (masking, statement split, allowlist, write-verb tripwire)
# --------------------------------------------------------------------------- #
def _mask_with_status(text: str) -> tuple[str, str | None]:
    """Replace string-literal contents and comments with spaces, same length.

    Every structural decision downstream (statement splitting, paren
    balancing, verb extraction) runs on the MASKED text — a ``)`` or ``;`` or
    verb-shaped word inside a string literal must never influence structure.
    This closed two real bypasses:

    * ``WITH x AS (SELECT ')SELECT' …) DELETE …`` — a single-quoted literal
      fooled a raw paren counter into reading the literal's contents as the
      terminal verb.
    * ``WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM t`` — a DOUBLE-quoted
      delimited identifier did the same thing. Snowflake treats ``"…"`` as an
      identifier, not a string, so it was initially left unmasked; the ``)``
      inside it closed the CTE scan early and the trailing ``SELECT`` was read
      as the terminal verb while Snowflake executed the ``DELETE``.

    * ``WITH x AS (SELECT $$ ) SELECT y $$) DELETE FROM t`` — a dollar-quoted
      constant, which was not recognised as a quoting form at all.
    * ``WITH x AS (SELECT '\\') SELECT y') DELETE FROM t`` — a BACKSLASH-escaped
      quote. Snowflake accepts both ``''`` and ``\'``; only the doubling form
      was handled, so the literal looked closed at the wrong place.

    All of these must therefore be masked, for structure only — this
    function's output is never emitted, so losing identifier text is fine.
    Handles ``''`` / ``""`` escaping; an unterminated literal masks to
    end-of-text, which downstream reads as "cannot parse" → not allowed.
    Length and newlines are preserved so nothing shifts.
    """
    out = list(text)
    unterminated: str | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        # A `$$` only OPENS a dollar-quoted constant when it does not continue an
        # identifier: Snowflake permits `$` inside unquoted identifiers, so
        # `x$$y` is a legal column name. Treating every `$$` as an opener made
        # the fail-closed guard refuse that file as "unterminated" — a false
        # positive that blocks generating a legitimate audit trail. The CLOSING
        # `$$` keeps a plain find(): a body may legitimately end in an
        # identifier character (`$$abc$$`).
        prev_is_ident = i > 0 and (text[i - 1].isalnum() or text[i - 1] in "_$")
        if c == "$" and text[i : i + 2] == "$$" and not prev_is_ident:  # dollar-quoted constant
            end = text.find("$$", i + 2)
            if end == -1:
                unterminated = "dollar-quoted constant ($$ with no closing $$)"
            end = n if end == -1 else end + 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
        elif c in "'\"":  # string literal, or double-quoted delimited identifier
            quote = c
            i += 1
            while i < n:
                # Snowflake accepts BOTH doubling ('' / "") and backslash
                # escaping (\') inside a string literal. Missing the backslash
                # form let `'\') SELECT y'` read as a closed literal, so the
                # `)` escaped masking and ended the CTE scan early. Backslash
                # is not an escape inside a double-quoted identifier, so this
                # only applies to string literals.
                if quote == "'" and text[i] == "\\" and i + 1 < n:
                    for j in (i, i + 1):
                        if text[j] != "\n":
                            out[j] = " "
                    i += 2
                    continue
                if text[i] == quote and i + 1 < n and text[i + 1] == quote:
                    out[i] = out[i + 1] = " "  # escaped quote ('' or "")
                    i += 2
                    continue
                if text[i] == quote:
                    break
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i >= n:
                unterminated = (
                    "string literal" if quote == "'" else "quoted identifier"
                ) + f" ({quote} with no closing {quote})"
            i += 1
        elif c == "-" and text[i : i + 2] == "--":  # line comment
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and text[i : i + 2] == "/*":  # block comment
            end = text.find("*/", i + 2)
            if end == -1:
                unterminated = "block comment (/* with no */)"
            end = n if end == -1 else end + 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
        elif c == "/" and text[i : i + 2] == "//":  # Snowflake ALSO accepts // comments
            # Missing this was the sixth masking bypass of the same class: an
            # apostrophe inside a `//` comment opened a phantom string literal
            # that ran to the next `'` in the file and hid real SQL — and since
            # that phantom literal TERMINATES, the fail-closed path never fired.
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out), unterminated


def _mask_strings_and_comments(text: str) -> str:
    """Masked text only. Callers that must FAIL CLOSED use _mask_with_status."""
    return _mask_with_status(text)[0]


def _verify_session_vars_defined(text: str) -> list[str]:
    """Every ``$var`` the body references must have a ``SET`` line in the file.

    The symmetric half of the SET-block pruning. Pruning removes SET lines for
    DECLARED variables the body never references; nothing checked the converse,
    that every variable the body DOES reference was declared. A manifest whose
    ``param_bindings`` name a variable absent from ``set_block`` therefore
    rendered binds into undefined session variables, pruned the SET block to
    empty because neither declared name was referenced, and emitted a header
    stating "no section below takes a bind param or references a session
    variable" — with generate returning 0 and check reporting clean.

    Pasted into Snowsight that file dies on the first block with "Session
    variable '$WINDOW_START' does not exist". A confidently wrong verification
    is the exact failure this release exists to remove, so the check runs over
    the final text: it catches an undeclared variable and any future pruning
    mistake alike, without trusting the manifest.
    """
    masked = _mask_strings_and_comments(text)
    defined = {m.group(1).lower() for m in re.finditer(r"(?mi)^SET\s+([A-Za-z_]\w*)\s*=", masked)}
    # Snowflake's documented multi-variable form: `SET (a, b) = (expr, expr)`.
    # Without this both names read as undefined and a valid hand-edited file
    # was falsely refused.
    for m in re.finditer(r"(?mi)^SET\s+\(([^)]*)\)\s*=", masked):
        defined |= {n.strip().lower() for n in m.group(1).split(",") if n.strip()}
    problems: list[str] = []
    seen: set[str] = set()
    # Same lookbehind discipline as _BIND_RE: a `$` that FOLLOWS an identifier
    # character belongs to that identifier, not to a session variable. Without
    # it, Snowflake's metadata pseudo-columns (`METADATA$FILENAME`) and system
    # functions (`SYSTEM$TYPEOF`) read as undeclared variables and legal
    # read-only SQL was refused outright.
    for m in re.finditer(r"(?<![\w$])\$([A-Za-z_]\w*)\b", masked):
        name = m.group(1)
        key = name.lower()
        if key in defined or key in seen:
            continue
        seen.add(key)
        problems.append(
            f"session variable ${name} is referenced but never SET; page sections set no "
            "session variables, so read the value from the params CTE (params.<name> in "
            "index.yaml binds) instead"
        )
    return problems


def _verify_binds_bound(text: str) -> list[str]:
    """Every ``:1`` / ``:name`` bind must have been substituted in executable lines.

    A surviving bind is not valid Snowflake outside a driver-bound statement,
    so the block errors the moment it is pasted — the exact failure the whole
    artifact exists to avoid. This is the assertion that was missing when a
    manifest declaring only ``:1``/``:2`` rendered seven live ``AND col <= :3``
    predicates: the read-only allowlist passed it, the provenance hashes
    passed it, and coverage passed it, because none of them ask whether the
    output actually runs.

    Comments are masked first, so the ``Params: :1 start_date`` banner lines
    that document the original slots are exempt by construction. ``::`` casts
    are already excluded by ``_BIND_RE``.
    """
    problems: list[str] = []
    masked = _mask_strings_and_comments(text)
    for lineno, line in enumerate(masked.splitlines(), start=1):
        for m in _BIND_RE.finditer(line):
            problems.append(
                f"line {lineno}: unsubstituted bind :{m.group(1)}; give it a value under the "
                "metric's binds: in index.yaml"
            )
    return problems


def _split_statements(text: str) -> list[str]:
    """Split masked text on ``;``. Comments and string contents are already
    spaces (see ``_mask_strings_and_comments``), so a ``;`` in a literal or a
    block comment can neither split a legitimate statement nor hide one."""
    masked = _mask_strings_and_comments(text)
    return [s.strip() for s in masked.split(";") if s.strip()]


def _with_terminal_verb(stmt: str) -> str:
    """The top-level verb a ``WITH`` statement terminates in, or "".

    A CTE prefix is not read-only by itself — ``WITH x AS (SELECT 1) DELETE
    FROM t`` is a delete. Walk ``name [(cols)] AS ( … )`` definitions with
    paren balancing; whatever keyword follows the last CTE is the statement's
    real verb. Anything this walker cannot confidently parse returns "" and
    fails toward "not allowed".

    Callers MUST pass masked text (``_mask_strings_and_comments``): the paren
    balance is only sound when string-literal contents cannot contribute
    parens or verb-shaped words.
    """
    i = _with_terminal_offset(stmt)
    if i < 0:
        return ""
    m = re.match(r"[A-Za-z]+", stmt[i:])
    return m.group(0).upper() if m else ""


def _with_terminal_offset(stmt: str) -> int:
    """Offset of the statement a ``WITH`` terminates in, or -1 when it cannot be parsed.

    The walker behind ``_with_terminal_verb`` (same contract: masked text only),
    also used to split a query's CTE list from its final ``SELECT`` when a
    summary section wraps it.
    """
    tokens = stmt.split()
    if not tokens or tokens[0].upper() != "WITH":
        return -1
    # Re-scan character-wise for balanced parens; token-wise is not enough
    # because CTE bodies contain arbitrary whitespace/commas.
    i = len("WITH")
    n = len(stmt)
    while True:
        # Skip whitespace, optional RECURSIVE, the CTE name, optional column
        # list, AS, then the balanced parenthesised body.
        while i < n and stmt[i].isspace():
            i += 1
        # The CTE name may be a DELIMITED identifier (`WITH "cte name" AS …`).
        # Masking blanks its contents but keeps the quotes, so accept a quoted
        # run here as well — otherwise the walker bails, returns "", and a
        # perfectly valid read-only CTE is REFUSED. A false positive is a
        # defect too: it blocks generating a legitimate audit file.
        m = re.match(
            r'(?:RECURSIVE\s+)?(?:"[^"]*"|[A-Za-z_][A-Za-z0-9_$]*)',
            stmt[i:],
            re.IGNORECASE,
        )
        if not m:
            return -1
        i += m.end()
        while i < n and stmt[i].isspace():
            i += 1
        if i < n and stmt[i] == "(":  # optional column list
            depth = 1
            i += 1
            while i < n and depth:
                depth += stmt[i] == "("
                depth -= stmt[i] == ")"
                i += 1
            while i < n and stmt[i].isspace():
                i += 1
        if stmt[i : i + 2].upper() != "AS":
            return -1
        i += 2
        while i < n and stmt[i].isspace():
            i += 1
        if i >= n or stmt[i] != "(":
            return -1
        depth = 1
        i += 1
        while i < n and depth:
            depth += stmt[i] == "("
            depth -= stmt[i] == ")"
            i += 1
        while i < n and stmt[i].isspace():
            i += 1
        if i < n and stmt[i] == ",":
            i += 1
            continue  # next CTE definition
        return i


# Second, independent layer under the statement-root allowlist. Four masking
# bypasses have been found in this module (single-quote, double-quote,
# dollar-quote, backslash escape), every one of which worked by making the
# structural parser mis-read where a statement began or ended. A recurring
# class like that says the next parser gap should not also be a pass.
#
# Two anchors, deliberately different:
#
# * At the START of a statement, ANY of these verbs is a command. Nothing legal
#   in a read-only file begins with one.
# * Right after a `)`, only RESERVED words are checked. That is the shape every
#   bypass produced (the verb surfacing after a mis-parsed CTE close), but it is
#   also where a bare column alias lives — `SELECT MAX(d) comment FROM t` is
#   legal Snowflake, and `comment` is a real INFORMATION_SCHEMA column this
#   project's own discovery query selects. A reserved word cannot be a bare
#   alias, so the split keeps the bypass coverage without refusing valid SQL.
#   (`AS <verb>` and `"<verb>"` are always fine — masked or clearly an alias.)
#
# There is no `;` anchor: _split_statements strips `;` before this runs, so one
# would be dead code.
_WRITE_VERBS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "TRUNCATE",
    "DROP",
    "UNDROP",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "COMMENT",
    "COPY",
    "PUT",
    "REMOVE",
    "UNLOAD",
    "CALL",
    "EXECUTE",
    "UNSET",
    "SET",
)
# Snowflake reserved words: cannot appear as a bare (unquoted, un-AS'd) alias.
_WRITE_VERBS_RESERVED = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "SET",
)
_WRITE_VERB_AT_START_RE = re.compile(r"\A\s*(" + "|".join(_WRITE_VERBS) + r")\b", re.IGNORECASE)
# The non-reserved verbs CAN be bare column aliases, so they are matched only in
# their two-token COMMAND form. Without this the after-paren anchor was blind to
# `) MERGE INTO t` and `) TRUNCATE TABLE t`. The allowlist catches those today,
# but this layer exists to hold when the walker is fooled, so omitting them
# traded away exactly the coverage it is for. A bare alias is followed by
# FROM / `,` / `;`, never by INTO / TABLE / ON / IMMEDIATE, so these cannot fire
# on `SELECT COUNT(*) merge FROM t`.
# A bare column/table alias is followed by a CLAUSE keyword, so the "verb needs
# an argument" patterns below must exclude those. Without this,
# `SELECT COUNT(*) call FROM t` matched `CALL <identifier>` (FROM is
# identifier-shaped) and legal SQL was refused.
_NOT_CLAUSE = (
    r"(?!(?:FROM|WHERE|GROUP|ORDER|JOIN|ON|LIMIT|OFFSET|HAVING|UNION|EXCEPT|"
    r"INTERSECT|MINUS|QUALIFY|WINDOW|AS|USING|INNER|LEFT|RIGHT|FULL|CROSS|"
    r"LATERAL|AND|OR|IS|NOT|NULL|END|THEN|ELSE|WHEN|FETCH|PIVOT|UNPIVOT|"
    r"SAMPLE|TABLESAMPLE|MATCH_RECOGNIZE|START|CONNECT|AT|BEFORE|CHANGES|NATURAL|ASOF|"
    r"WITH|SELECT|VALUES|SET)\b)"
)

_WRITE_COMMANDS_AFTER_PAREN = (
    r"MERGE\s+INTO\b",
    # `TABLE` is OPTIONAL in Snowflake's TRUNCATE, so match the bare form too.
    # The trailing identifier check is a LOOKAHEAD so the reported verb is
    # `TRUNCATE`, not `TRUNCATE N`.
    r"TRUNCATE\s+(?:IF\s+EXISTS\s+)?(?:TABLE\s+)?" + _NOT_CLAUSE + r"(?=[A-Za-z_\"])",
    r"COMMENT\s+(?:IF\s+EXISTS\s+)?ON\s+(?:TABLE|VIEW|COLUMN|SCHEMA|DATABASE|"
    r"WAREHOUSE|STAGE|SEQUENCE|STREAM|TASK|PIPE|FUNCTION|PROCEDURE|ROLE|USER|"
    r"INTEGRATION|MATERIALIZED|TAG|SHARE|ACCOUNT|ALERT|SECRET|APPLICATION|"
    r"MASKING|ROW|NETWORK|PASSWORD|SESSION|AUTHENTICATION|EXTERNAL|DYNAMIC|"
    r"EVENT|ICEBERG|HYBRID|FILE|NOTEBOOK|STREAMLIT|MODEL|SERVICE|COMPUTE|"
    r"IMAGE|RESOURCE|CONNECTION|LISTING|REPLICATION|FAILOVER|DATA)\b",
    # `COPY FILES INTO` is the stage-to-stage form.
    r"COPY\s+(?:FILES\s+)?INTO\b",
    r"UNDROP\s+(?:ICEBERG\s+|DYNAMIC\s+|EXTERNAL\s+|EVENT\s+)?(?:TABLE|SCHEMA|DATABASE)\b",
    r"EXECUTE\s+(?:IMMEDIATE|TASK)\b",
    r"(?:REMOVE|RM)\s+@",
    r"PUT\s+file://",
    # CALL: either a non-clause identifier follows, OR any identifier is
    # immediately followed by `(` — a procedure named after a clause keyword
    # (`CALL start()`, `CALL changes()`) is still a call, while the bare alias
    # `call FROM (SELECT ...)` has a space before its paren and does not match.
    r"CALL\s+(?:" + _NOT_CLAUSE + r"(?=[A-Za-z_\"$])|(?=[A-Za-z_\"$][\w.$\"]*\())",
    r"UNLOAD\s+(?:TO\s+)?@",
    r"UNSET\s+" + _NOT_CLAUSE + r"(?=[A-Za-z_\"])",
)
_WRITE_VERB_AFTER_PAREN_RE = re.compile(
    r"(?<=\))\s*("
    + "|".join([*(v + r"\b" for v in _WRITE_VERBS_RESERVED), *_WRITE_COMMANDS_AFTER_PAREN])
    + r")",
    re.IGNORECASE,
)


def _valid_set_statement(stmt: str) -> bool:
    """Is *stmt* a `SET <var> = <expr>` with no command smuggled after it?

    ``_SET_STMT_RE`` only anchors the prefix, so `SET x = (SELECT 1) DELETE
    FROM t` matched it and the allowlist accepted the whole statement — every
    token after the `=` was examined by neither layer. Extending the tripwire's
    verb list chased that one verb at a time; checking the whole expression
    closes the class, including verbs nobody listed.

    An earlier attempt required the statement to END at the first balanced
    paren group, which refused the canonical idiom
    `SET end_date = (SELECT MAX(load_date) FROM V)::DATE` — and `- 1`, `/ 2`,
    `|| '…'`. Anchoring a window to a source's last loaded date, cast or
    adjusted, is the main reason `set_block` exists. So a leading group may be
    followed by more expression; what it may NOT contain is a write verb.

    Callers must pass MASKED text: a `)` or a verb-shaped word inside a literal
    must not affect the result.
    """
    m = _SET_STMT_RE.match(stmt)
    if not m:
        return False
    rest = stmt[m.end() :].strip()
    if not rest:
        return False  # `SET x = ;` — no expression at all
    # Scan the WHOLE expression, so wrapping a command in outer parens
    # (`((SELECT 1) CALL p())`) cannot hide it. But scan for the SAME things
    # the after-paren anchor looks for — reserved verbs, and non-reserved verbs
    # only in command form — NOT the bare verb list. Eleven of those verbs are
    # legal Snowflake identifiers, so a bare-token scan refused
    # `SET end_date = (SELECT MAX(d) comment FROM t)::DATE`, where `comment` is
    # a column name. That fragment is accepted in the after-paren position, so
    # refusing it here contradicted the tool's own pinned behaviour and made
    # the coverage gate unsatisfiable for the affected app.
    reserved = r"\b(" + "|".join(_WRITE_VERBS_RESERVED) + r")\b"
    commands = "|".join(_WRITE_COMMANDS_AFTER_PAREN)
    return not re.search(reserved + "|" + commands, rest, re.IGNORECASE)


def _verify_read_only(text: str) -> list[str]:
    """Statement-root allowlist, plus a write-verb tripwire, over the file.

    ``WITH`` is only allowed when its terminal statement is a ``SELECT`` —
    a CTE can prefix DELETE/INSERT/UPDATE/MERGE, so the root alone proves
    nothing. Body lines that look like a provenance record are also refused:
    the check verb trusts exactly one final provenance line, so a template
    must never be able to plant a second.

    Defence in depth: the allowlist depends on parsing statement boundaries
    correctly, and that parsing has been defeated four times. So a write verb
    surviving masking as a bare token is refused independently of any parse
    (see ``_WRITE_VERBS``).
    """
    problems: list[str] = []
    masked_all, unterminated = _mask_with_status(text)
    if unterminated:
        # FAIL CLOSED. Masking runs to end-of-text when a quoting form is left
        # open, so from that point on the file is spaces and EVERY guard goes
        # blind — read-only, binds and session variables alike. This is
        # reachable by ordinary error, not attack: a token literal containing
        # an apostrophe (`AND last_name = 'O'Brien'`) is enough, and both
        # generate and check then reported success on a file whose own header
        # claimed no section referenced a session variable while a section
        # referenced two undeclared ones. The docstring long claimed this
        # failed closed; only the WITH path actually did.
        problems.append(
            f"unterminated {unterminated} — the rest of the file cannot be "
            "analysed, so it is refused rather than passed unchecked"
        )
        return problems
    for stmt in _split_statements(masked_all):
        hits: list[tuple[int, str]] = []
        m0 = _WRITE_VERB_AT_START_RE.search(stmt)
        # Report the VERB only: multi-token patterns (`COMMENT IF EXISTS ON`,
        # `MERGE INTO`) and the whitespace consumed before a lookahead would
        # otherwise surface as 'TRUNCATE ' or 'COMMENT IF EXISTS ON TABLE'.
        if m0:
            hits.append((m0.start(1), m0.group(1).split()[0].upper()))
        # finditer, not search: a `search` that matched the leading `SET` and
        # then `continue`d on the SET exemption left EVERYTHING after the `=`
        # examined by neither layer, so `SET x = (SELECT 1) DELETE FROM t`
        # passed both. The SET exemption may only excuse the match at offset 0.
        hits += [
            (m.start(1), m.group(1).split()[0].upper())
            for m in _WRITE_VERB_AFTER_PAREN_RE.finditer(stmt)
        ]
        is_set_stmt = _valid_set_statement(stmt)
        for offset, verb in hits:
            if offset == 0 and verb == "SET" and is_set_stmt:
                continue  # the one legal write-shaped root; allowlist validates its form
            problems.append(
                f"write verb {verb!r} in command position — audit files are "
                "read-only. If this is an identifier rather than a command, "
                "quote it or introduce it with AS."
            )
    for line in text.splitlines():
        if line.lstrip().startswith("-- Provenance:"):
            problems.append("a review body line may not start with '-- Provenance:'")
    for stmt in _split_statements(text):
        root = stmt.split(None, 1)[0].upper() if stmt.split() else ""
        if root == "WITH":
            if _with_terminal_verb(stmt) == "SELECT":
                continue
            problems.append(
                "WITH statement does not terminate in SELECT — CTE-prefixed writes "
                "are not allowed in review SQL"
            )
            continue
        if root in ALLOWED_ROOTS - {"WITH"}:
            continue
        if root == "SET" and _valid_set_statement(stmt):
            continue
        problems.append(f"statement root {root or stmt[:20]!r} is not allowed in review SQL")
    return problems


def _lf(data: bytes) -> bytes:
    """CRLF pairs to LF, so a Windows (autocrlf) checkout hashes like any other."""
    return data.replace(b"\r\n", b"\n")


def _normalize_for_output_hash(text: str) -> str:
    """Normalize ONLY the volatile piece, preserving every other byte.

    The provenance record contains the output hash, so it cannot be part of it;
    it hashes as a placeholder. Everything else, including trailing whitespace
    and any lone ``\\r``, participates in the digest: an appended statement after
    the provenance line is an edit, and must read as one.

    The one exception is a CRLF pair, which hashes as LF. Git for Windows checks
    text files out with CRLF by default (``core.autocrlf``), so hashing ``\\r\\n``
    literally made every committed review file read as "edited by hand" on a
    Windows clone that nobody had touched. A line-ending conversion carries no
    SQL meaning, and ``check`` re-runs the read-only allowlist on the body
    regardless, so normalizing it gives up nothing the gate relies on.
    """
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.rstrip("\r")
        if stripped == _FINAL_PROVENANCE_PLACEHOLDER or _PROVENANCE_RE.match(stripped + "\n"):
            lines.append(_FINAL_PROVENANCE_PLACEHOLDER)
        else:
            lines.append(line)
    return "\n".join(lines)


_FINAL_PROVENANCE_PLACEHOLDER = "-- Provenance: <normalized>"


def _output_digest(text: str) -> str:
    return hashlib.sha256(_normalize_for_output_hash(text).encode()).hexdigest()[:16]


def parse_provenance(text: str) -> tuple[dict | None, str | None]:
    """Locate the single, FINAL provenance line. Returns (record, problem).

    Content after the provenance line — or a second provenance-shaped line
    anywhere — is how an edit could hide from the digest, so both are
    structural failures, never silently tolerated.
    """
    lines = text.split("\n")
    prov_idx = [i for i, ln in enumerate(lines) if ln.rstrip("\r").startswith("-- Provenance: ")]
    if not prov_idx:
        return None, "no provenance line — regenerate"
    if len(prov_idx) > 1:
        return None, "multiple provenance lines — the file was edited; regenerate"
    idx = prov_idx[0]
    if any(ln.strip() for ln in lines[idx + 1 :]):
        return None, "content after the provenance line — the file was edited; regenerate"
    m = _PROVENANCE_RE.match(lines[idx].rstrip("\r") + "\n")
    if not m:
        return None, "malformed provenance line — regenerate"
    return {"schema": m.group(1), "inputs": m.group(2), "output": m.group(3)}, None


def _stamp_provenance(text: str, inputs: str) -> str:
    """Append the final provenance line, after one blank line.

    The output digest is computed over the file WITH the provenance line
    normalized to its placeholder, exactly the transformation ``check``
    applies to the finished file, so both sides hash the same string.
    """
    body = text.rstrip() + "\n\n"
    output = _output_digest(body + _FINAL_PROVENANCE_PLACEHOLDER + "\n")
    return body + f"-- Provenance: schema={GENERATOR_SCHEMA} inputs={inputs} output={output}\n"


# --------------------------------------------------------------------------- #
# Rendering a page file
# --------------------------------------------------------------------------- #
_PARAMS_REF_RE = re.compile(r"^params\.([a-z_][a-z0-9_]*)$")
# A CTE the query itself names `params` would collide with the section's own.
_PARAMS_CTE_RE = re.compile(r"(?:\bWITH\s+(?:RECURSIVE\s+)?|,)\s*params\s+AS\s*\(", re.IGNORECASE)
_LEADING_WITH_RE = re.compile(r"\s*WITH\b(\s+RECURSIVE\b)?", re.IGNORECASE)


def _split_query(text: str) -> tuple[list[str], str]:
    """``(lead_comments, body)`` of a query file.

    The leading block of comment and blank lines is the file's header
    (``-- Query:``, ``-- Params:`` …) and is dropped: the page file has its own.
    Comment lines directly above the first SQL line that are not header fields
    belong to that SQL (a CTE's purpose), so they are returned to be placed
    right above it again, after the section's ``params`` CTE.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("--")):
        i += 1
    lead: list[str] = []
    j = i - 1
    while (
        j >= 0
        and lines[j].lstrip().startswith("--")
        and not _HEADER_FIELD_RE.match(lines[j].strip())
    ):
        lead.insert(0, lines[j].strip())
        j -= 1
    return lead, "\n".join(lines[i:])


def _bind_value(value: str) -> str:
    """``params.start_date`` reads the section's own params CTE; anything else
    is a SQL literal or expression, inserted as written."""
    ref = _PARAMS_REF_RE.match(value.strip())
    return f"(SELECT {ref.group(1)} FROM params)" if ref else value.strip()


def _substitute_binds(sql: str, binds: dict[str, str]) -> str:
    """Replace each ``:bind`` the masked text shows in an operand position.

    Positions come from the MASKED text, so a ``:1`` inside a string literal or
    a comment is never touched; masking preserves length, so they line up.
    """
    masked = _mask_strings_and_comments(sql)
    out = sql
    for m in reversed(list(_BIND_RE.finditer(masked))):
        if m.group(1) in binds:
            out = out[: m.start()] + _bind_value(binds[m.group(1)]) + out[m.end() :]
    return out


def _params_cte(window: dict[str, str], *, recursive: bool) -> list[str]:
    cols = [
        f"        {expr.replace(chr(10), chr(10) + '        ')} AS {name}"
        for name, expr in window.items()
    ]
    return [
        "WITH RECURSIVE params AS (" if recursive else "WITH params AS (",
        "    -- Review window from index.yaml. Edit it here to rerun this section.",
        "    SELECT",
        ",\n".join(cols),
        ")",
    ]


def _with_params(body: str, lead: list[str], window: dict[str, str]) -> str:
    """Prepend the params CTE, merging into a leading ``WITH`` when there is one."""
    m = _LEADING_WITH_RE.match(_mask_strings_and_comments(body))
    cte = _params_cte(window, recursive=bool(m and m.group(1)))
    if m is None:
        return "\n".join([*cte, *lead, body])
    rest = body[m.end() :].lstrip(" \t")
    rest = rest[1:] if rest.startswith("\n") else rest  # `WITH` alone on its line
    return "\n".join([*cte[:-1], cte[-1] + ",", *lead, rest])


def _prepared_query(
    app: Path, metric: sri.Metric, text: str | None = None
) -> tuple[list[str], str, str]:
    """``(lead_comments, body, masked_body)``: the metric's query with tokens and
    binds substituted, one statement, no trailing ``;``. Raises ToolError."""
    if text is None:
        qpath = app / metric.query
        try:
            text = qpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ToolError(f"cannot read {metric.query} (metric {metric.key!r}): {exc}") from exc
    lead, body = _split_query(text)
    for tok, value in metric.tokens.items():
        body = body.replace("{" + tok + "}", value)
    leftover = template_tokens(body)
    if leftover:
        raise ToolError(
            f"metric {metric.key!r}: {metric.query} still has unresolved tokens {leftover} "
            "after substitution; add sample values under tokens: in index.yaml"
        )
    body = _substitute_binds(body, metric.binds).strip()
    while body.endswith(";"):
        body = body[:-1].rstrip()
    masked = _mask_strings_and_comments(body)
    if not masked.strip():
        raise ToolError(f"metric {metric.key!r}: {metric.query} has no SQL")
    if ";" in masked:
        raise ToolError(
            f"metric {metric.key!r}: {metric.query} holds more than one statement; "
            "a metric's query must be a single statement"
        )
    if _PARAMS_CTE_RE.search(masked):
        raise ToolError(
            f"metric {metric.key!r}: {metric.query} already defines a CTE named params, "
            "which the review section adds; rename the query's CTE"
        )
    return lead, body, masked


def render_section(app: Path, index: sri.Index, metric: sri.Metric, text: str | None = None) -> str:
    """One metric's runnable SQL (no tag line), ending in exactly one ``;``.

    ``text`` stands in for the metric's query file: ``bench --sql-file``
    renders a candidate rewrite exactly as the app's own query would be.
    """
    lead, body, masked = _prepared_query(app, metric, text)
    root = masked.split(None, 1)[0].upper()
    uses_params = any(_PARAMS_REF_RE.match(v.strip()) for v in metric.binds.values())
    if index.review_window and root in ("SELECT", "WITH"):
        sql = _with_params(body, lead, index.review_window)
    elif uses_params:
        raise ToolError(
            f"metric {metric.key!r}: binds read params.*, but {metric.query} is not a "
            "SELECT or WITH statement, so it cannot take a params CTE"
        )
    else:
        sql = "\n".join([*lead, body])
    return sql + ";"


# --------------------------------------------------------------------------- #
# Summary sections: what the visual shows, computed from the detail result
# --------------------------------------------------------------------------- #
#: The CTE a summary section names the app query's result.
DETAIL_CTE = "detail"
#: The tag suffix of a summary section: ``--N_key_summary`` follows ``--N_key``.
SUMMARY_SUFFIX = "_summary"
_DETAIL_REF_RE = re.compile(rf"\b{DETAIL_CTE}\b", re.IGNORECASE)
_PARAMS_REF_ANY_RE = re.compile(r"\bparams\b", re.IGNORECASE)
_CTE_DEF_RE = re.compile(
    r"(?:\bWITH\s+(?:RECURSIVE\s+)?|,)\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:\([^)]*\)\s*)?AS\s*\(",
    re.IGNORECASE,
)
#: A line ``split_page`` would read as a section tag.
_TAG_SHAPED_RE = re.compile(r"^--\d+_", re.MULTILINE)
#: A summary may continue the CTE list after ``detail`` with a leading comma.
_LEADING_COMMA_RE = re.compile(r"\A(\s*),")


def summary_parts(summary: str) -> tuple[str, str]:
    """``(description, sql)`` of an index ``summary:``.

    The first line is the one-line ``--`` description the page file shows above
    the section's tag; the rest is the SQL. A leading ``,`` (the reference
    form ``, cells AS (...) SELECT ...``, continuing the list after ``detail``)
    reads as ``WITH``, on the same line, so line numbers stay true.
    """
    text = summary.replace("\r\n", "\n").strip("\n")
    first, _, rest = text.partition("\n")
    if not first.lstrip().startswith("--"):
        return "", text
    rest = rest.rstrip("\n")
    m = _LEADING_COMMA_RE.match(_mask_strings_and_comments(rest))
    if m:
        rest = rest[: m.end(1)] + "WITH " + rest[m.end() :].lstrip(" ")
    return first.strip(), rest


def summary_problems(summary: str) -> list[str]:
    """Why an index ``summary:`` cannot render as a read-only section ([] when it can).

    A summary sits in the page file under its own one-line description, beside
    a ``detail`` CTE holding the app query's whole statement, so it must be one
    read-only SELECT (or ``WITH`` ... ``SELECT``) that reads ``detail``, with no
    template machinery of its own: tokens and binds belong to the app query,
    and the detail statement already carries their sample values.
    """
    description, sql = summary_parts(summary)
    problems: list[str] = []
    if not description:
        problems.append(
            "needs a one-line description first: a -- comment saying what the visual shows "
            "(the page file prints it above the section's tag)"
        )
    elif (
        len(description) > MAX_LINE
        or not description.startswith("-- ")
        or not description[3:].strip()
        or description.startswith("-- Provenance:")
    ):
        # `-- ` keeps it from ever reading as a `--N_key` tag; the provenance
        # prefix is reserved for the file's last line.
        problems.append(
            f"description must be one '-- <text>' comment of {MAX_LINE} characters or fewer"
        )
    masked, unterminated = _mask_with_status(sql)
    if unterminated:
        return [*problems, f"has an unterminated {unterminated}"]
    body = masked.strip()
    while body.endswith(";"):
        body = body[:-1].rstrip()
    if not body:
        return [*problems, "has no SQL after its description"]
    if ";" in body:
        problems.append("holds more than one statement; it must be a single SELECT")
    root = body.split(None, 1)[0].upper()
    if root not in ("SELECT", "WITH") or (root == "WITH" and _with_terminal_verb(body) != "SELECT"):
        problems.append(f"must be a SELECT (or WITH ... SELECT) over {DETAIL_CTE}, not {root}")
    problems += _verify_read_only(sql)
    if _TAG_SHAPED_RE.search(sql):
        problems.append("has a line shaped like a section tag (--N_...); reword that comment")
    for name in dict.fromkeys(m.group(1) for m in _TOKEN_RE.finditer(body)):
        problems.append(f"uses {{{name}}}; a summary takes no tokens (the query's are applied)")
    for name in dict.fromkeys(m.group(1) for m in _BIND_RE.finditer(body)):
        problems.append(f"uses :{name}; a summary takes no binds (the query's are applied)")
    if not _DETAIL_REF_RE.search(body):
        problems.append(
            f"never reads {DETAIL_CTE}, the app query's result (e.g. SELECT ... FROM {DETAIL_CTE})"
        )
    ctes = {m.group(1).lower() for m in _CTE_DEF_RE.finditer(body)}
    if DETAIL_CTE in ctes:
        problems.append(f"defines a CTE named {DETAIL_CTE}, which the summary section adds")
    if "params" not in ctes and _PARAMS_REF_ANY_RE.search(body):
        problems.append(
            "reads params, which lives inside detail; select what the summary needs from detail"
        )
    return problems


def summary_query_problems(sql: str) -> list[str]:
    """Why a query cannot be wrapped by its metric's summary ([] when it can)."""
    masked = _mask_strings_and_comments(strip_header(sql)).strip()
    root = masked.split(None, 1)[0].upper() if masked else ""
    if root not in ("SELECT", "WITH"):
        return [f"is not a SELECT or WITH statement, so a summary cannot read it as {DETAIL_CTE}"]
    return []


def _split_ctes(sql: str) -> tuple[str, str, str]:
    """``(ctes, between, final)`` of one statement.

    ``ctes`` is the text after ``WITH`` through the last CTE's ``)`` ("" with
    no ``WITH``), ``between`` the comment lines before the final statement, and
    ``final`` that statement. Positions come from the masked text, so nothing
    inside a literal or comment can move a boundary.
    """
    masked = _mask_strings_and_comments(sql)
    m = _LEADING_WITH_RE.match(masked)
    if m is None:
        return "", "", sql.strip()
    lead = len(masked) - len(masked.lstrip())
    off = _with_terminal_offset(masked[lead:])
    if off < 0:
        raise ToolError("cannot find where the summary's WITH clause ends")
    off += lead
    close = masked.rindex(")", 0, off) + 1
    with_at = masked.upper().index("WITH", lead)
    above = sql[:with_at].strip()  # comments before WITH belong to the first CTE
    ctes = "\n".join(x for x in (above, sql[m.end() : close].strip()) if x)
    return ctes, sql[close:off].strip(), sql[off:].strip()


def _indent(sql: str, pad: str = "    ") -> str:
    """Indent every line, except a line that starts inside a multi-line string
    or ``$$`` constant: padding it would change the literal, and the detail CTE
    must run exactly what the detail section runs."""
    out: list[str] = []
    offset = 0
    for line in sql.split("\n"):
        inside = _mask_with_status(sql[:offset])[1] is not None
        out.append(line if inside or not line else pad + line)
        offset += len(line) + 1
    return "\n".join(out)


def render_summary_section(detail: str, summary_sql: str) -> str:
    """A summary section's SQL (no description or tag line), ending in one ``;``.

    ``detail`` is the detail section exactly as the page file holds it (no
    ``;``), so the CTE runs the very statement a reviewer just read above it.
    ``summary_sql`` is the summary without its description. A ``WITH`` in the
    summary continues the CTE list after ``detail``.
    """
    sql = summary_sql.strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    s_ctes, s_between, s_final = _split_ctes(sql)
    out = f"WITH {DETAIL_CTE} AS (\n{_indent(detail.strip())}\n)"
    if s_ctes:
        out += f",\n\n{s_ctes}"
    out += "\n\n" + "\n".join([*([s_between] if s_between else []), s_final])
    # A summary ending in a `-- comment` would comment out a `;` on its line.
    last = out.rsplit("\n", 1)[-1]
    ends_in_comment = _mask_strings_and_comments(last).rstrip() != last.rstrip()
    return out + ("\n;" if ends_in_comment else ";")


def _clip(line: str) -> str:
    line = " ".join(line.split())
    return line if len(line) <= MAX_LINE else line[: MAX_LINE - 1] + "…"


def _page_header(app: Path, index: sri.Index, page: sri.Page) -> list[str]:
    """Exactly HEADER_LINES lines, so the first tag is always line 9."""
    listed = ", ".join(
        f"{m.number} {m.key}" + (" (+summary)" if m.summary else "") for m in page.metrics
    )
    metrics = f"-- Metrics: {listed}"
    if len(metrics) > MAX_LINE:
        metrics = f"-- Metrics: {len(page.metrics)}. Each section starts with its --N_key tag."
    window = (
        "-- Review window: the params CTE at the top of each section (index.yaml review_window)."
        if index.review_window
        else "-- Review window: none (index.yaml declares no review_window)."
    )
    lines = [
        _clip(f"-- Page: {page.title or page.stem} ({page.path})"),
        _clip(f"-- App: {app.name}"),
        window,
        "-- Generated by `streamsnow sql-review generate` from sql_review/index.yaml. Do not edit:",
        "-- change index.yaml or the app's queries/*.sql, then regenerate.",
        metrics,
        "-- Run one section: put the cursor inside it and press Cmd/Ctrl+Enter.",
        "",
    ]
    assert len(lines) == HEADER_LINES
    return lines


def render_page(app: Path, index: sri.Index, page: sri.Page, cfg_text: str) -> str:
    """The full page file (no provenance yet). Refuses anything not read-only."""
    from . import sql_review_lint as srl  # noqa: PLC0415  (lazy: sqlfluff import)

    def fixed(sql: str) -> str:
        # A fix failure leaves the SQL as rendered; check's parse pass reports it.
        with contextlib.suppress(Exception):
            sql = srl.fix_section(sql, cfg_text)
        sql = sql.strip()
        while sql.endswith(";"):
            sql = sql[:-1].rstrip()
        return sql

    lines = _page_header(app, index, page)
    for metric in page.metrics:
        detail = fixed(render_section(app, index, metric))
        lines.append(f"--{metric.number}_{metric.key}")
        lines.extend(f"{detail};".split("\n"))
        lines.append("")
        if metric.summary:
            description, summary_sql = summary_parts(metric.summary)
            # The summary is fixed alone, so the detail inside it stays byte-identical
            # to the section above (only indented).
            section = render_summary_section(detail, fixed(summary_sql + ";"))
            lines.append(description)
            lines.append(f"--{metric.number}_{metric.key}{SUMMARY_SUFFIX}")
            lines.extend(section.split("\n"))
            lines.append("")
    text = "\n".join(line.rstrip() for line in lines).rstrip() + "\n"
    problems = (
        _verify_read_only(text) + _verify_binds_bound(text) + _verify_session_vars_defined(text)
    )
    if problems:
        raise ToolError(f"refusing to write {page.filename}: " + "; ".join(problems))
    return text


def _inputs_digest(app: Path, index: sri.Index, page: sri.Page, cfg_text: str) -> str:
    """Digest of everything a page file is a function of. Import-free.

    index.yaml bytes, the page's navigation entry (its number and title land in
    the file), every query it renders, the sqlfluff config the fixes came from,
    and the generator schema. CRLF hashes as LF (``_lf``): a Windows checkout
    converts line endings, and that must not read as drift.
    """
    h = hashlib.sha256()
    h.update(f"schema={GENERATOR_SCHEMA}".encode())
    h.update(_lf(index.path.read_bytes()))
    h.update(f"\npage:{page.number}:{page.path}:{page.title}\n".encode())
    for rel in sorted({m.query for m in page.metrics}):
        h.update(f"\nsource:{rel}\n".encode())
        qpath = sri._query_file(app, rel)
        h.update(_lf(qpath.read_bytes()) if qpath is not None else b"<missing>")
    h.update(b"\nsqlfluff:\n" + cfg_text.encode())
    return h.hexdigest()[:16]


def _expected_pages(index: sri.Index) -> list[sri.Page]:
    """Pages that get a file: in the navigation and with at least one metric."""
    return [p for p in index.numbered_pages if p.metrics]


def _generated_page_files(app: Path) -> list[Path]:
    rdir = _review_dir(app)
    return (
        sorted(p for p in rdir.glob("*.sql") if _PAGE_FILE_RE.match(p.name))
        if rdir.is_dir()
        else []
    )


# --------------------------------------------------------------------------- #
# README tables
# --------------------------------------------------------------------------- #
_README_START = "<!-- sql-review-index:start -->"
_README_END = "<!-- sql-review-index:end -->"
README_TEMPLATE = "app/sql_review_README.md.j2"
AGENTS_TEMPLATE = "app/sql_review_AGENTS.md.j2"
CLAUDE_TEMPLATE = "app/sql_review_CLAUDE.md.j2"
#: sql_review/AGENTS.md: generate refreshes everything above this line and
#: keeps what a person wrote below it.
AGENTS_MARKER = "<!-- streamsnow: edit below this line; generate keeps it -->"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def readme_block(index: sri.Index) -> str:
    """The generated README tables, markers included."""
    uses = _object_uses(index)
    pages = _expected_pages(index)
    out = [_README_START, "", "### Pages", ""]
    if pages:
        out += ["| File | Page | Metrics |", "|---|---|---|"]
        out += [
            f"| `{p.filename}` | {_cell(p.title or p.stem)} (`{p.path}`) | {len(p.metrics)} |"
            for p in pages
        ]
    else:
        out.append("_No page has a metric in index.yaml yet._")
    out += ["", "### Metrics", ""]
    if pages:
        out += ["| Page file | # | Key | App query | Reads |", "|---|---|---|---|---|"]
        for p in pages:
            for m in p.metrics:
                reads = ", ".join(f"`{r}`" for r in m.reads) or "—"
                key = f"`{m.key}`" + (" + summary" if m.summary else "")
                out.append(f"| `{p.filename}` | {m.number} | {key} | `{m.query}` | {reads} |")
    else:
        out.append("_None yet._")
    out += ["", "### App-specific reporting objects", ""]
    if index.objects:
        out += ["| DDL file | Used by |", "|---|---|"]
        for obj in index.objects:
            used = "; ".join(uses.get(obj.name.upper(), [])) or "—"
            out.append(f"| `{OBJECTS_DIR}/{obj.name}.sql` | {_cell(used)} |")
    else:
        out.append("_None: this app reads only shared objects._")
    out += ["", _README_END]
    return "\n".join(out)


def _render_template(name: str, app: Path) -> str:
    from ..scaffolder import _env, _title_from_slug  # noqa: PLC0415  (import cycle)

    return _env().get_template(name).render(app_slug=app.name, app_title=_title_from_slug(app.name))


def _splice_readme(existing: str, block: str) -> str:
    """Replace the marked block; the rest of the README is the team's and is kept.

    The markers must be unambiguous before anything is rewritten: with
    duplicated or reordered markers, a first-occurrence splice silently
    swallows narrative. Line-anchored, count-checked.
    """
    lines = existing.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.strip() == _README_START]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == _README_END]
    if (len(starts), len(ends)) == (0, 0):
        return existing.rstrip("\n") + "\n\n## Index\n\n" + block + "\n"
    if (len(starts), len(ends)) != (1, 1) or ends[0] < starts[0]:
        raise ToolError(
            f"sql_review/README.md has {len(starts)} start / {len(ends)} end index markers "
            "(or they are reversed); expected exactly one pair. Fix the README, then regenerate."
        )
    return "\n".join([*lines[: starts[0]], block, *lines[ends[0] + 1 :]])


def _readme_block_in(text: str) -> str | None:
    lines = text.replace("\r\n", "\n").split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.strip() == _README_START]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == _README_END]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return None
    return "\n".join(lines[starts[0] : ends[0] + 1])


# --------------------------------------------------------------------------- #
# app_specific_reporting_objects/
# --------------------------------------------------------------------------- #
_OBJ_FIELD_RE = re.compile(r"^--\s*(Object|Purpose|Used by|Grants):[ \t]*(.*)$")
_OBJ_FIELDS = ("Object", "Purpose", "Used by", "Grants")


def _object_uses(index: sri.Index) -> dict[str, list[str]]:
    """Upper-cased object name → ``01_overview.sql #1 total_revenue`` entries."""
    uses: dict[str, list[str]] = {}
    for page in _expected_pages(index):
        for m in page.metrics:
            for name in m.reads:
                uses.setdefault(name.upper(), []).append(f"{page.filename} #{m.number} {m.key}")
    return uses


def _used_by_value(uses: list[str]) -> str:
    return "; ".join(uses) if uses else "none"


def _ddl_files(app: Path) -> list[Path]:
    odir = _review_dir(app) / OBJECTS_DIR
    return sorted(odir.glob("*.sql")) if odir.is_dir() else []


def _ddl_header(text: str) -> dict[str, tuple[str, int]]:
    """Header fields in the leading comment block: name → (value, line)."""
    fields: dict[str, tuple[str, int]] = {}
    for lineno, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        if not line.startswith("--"):
            break
        m = _OBJ_FIELD_RE.match(line)
        if m and m.group(1) not in fields:
            fields[m.group(1)] = (m.group(2).strip(), lineno)
    return fields


def _rewrite_used_by(text: str, value: str) -> str:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("--"):
            break
        m = _OBJ_FIELD_RE.match(line.rstrip("\r"))
        if m and m.group(1) == "Used by":
            lines[i] = f"-- Used by: {value}" + ("\r" if line.endswith("\r") else "")
            break
    return "\n".join(lines)


def _app_data_plan(repo: Path):
    """The repo-wide app-data plan, or None when the repo has no loadable config
    (``streamsnow doctor`` owns that finding, and nothing deploys without it)."""
    from ..app_data import load_app_data  # noqa: PLC0415 (import cycle)

    cfg_path = find_config(repo)
    if cfg_path is None:
        return None
    try:
        cfg = load_config(cfg_path)
    except ConfigError:
        return None
    return load_app_data(repo, cfg)


def _object_findings(app: Path, index: sri.Index, app_data: str = "") -> list[dict]:
    """Per-app rules for the DDL files under ``sql_review/``.

    Names inside ``app_data`` (``governance.app_data``) skip the rules that need a
    view across apps (declared in the index, read by a metric, file present): the
    repo-wide loader decides those, because another app may read the object or own
    its file (#79).
    """
    target = tuple(app_data.split(".")) if app_data else ()

    def deployed(name: str) -> bool:
        parts = split_name(name)
        return bool(target) and len(parts) == 3 and parts[:2] == target

    uses = _object_uses(index)
    declared = {o.name.upper(): o for o in index.objects}
    found: list[dict] = []

    def add(rel: str, line: int, detail: str) -> None:
        found.append({"kind": KIND_OBJECTS, "file": rel, "line": line, "detail": detail})

    on_disk: set[str] = set()
    for path in _ddl_files(app):
        rel = _rel(app, "sql_review", OBJECTS_DIR, path.name)
        name = path.name[: -len(".sql")]
        on_disk.add(name.upper())
        if not sri._FQN_RE.match(name):
            add(rel, 1, "DDL file names must be <DATABASE>.<SCHEMA>.<OBJECT>.sql")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            add(rel, 1, f"unreadable DDL file ({exc})")
            continue
        header = _ddl_header(text)
        missing = [f for f in _OBJ_FIELDS if f not in header]
        if missing:
            add(
                rel,
                1,
                f"header is missing {', '.join(f'-- {f}:' for f in missing)} "
                "(Object, Purpose, Used by and Grants head every DDL file)",
            )
        if "Object" in header and header["Object"][0].upper() != name.upper():
            add(rel, header["Object"][1], f"-- Object: names {header['Object'][0]!r}, not {name!r}")
        if "Purpose" in header and not header["Purpose"][0]:
            add(rel, header["Purpose"][1], "-- Purpose: is empty; say in one line why it exists")
        obj = declared.get(name.upper())
        if obj is None and not deployed(name):
            add(rel, 1, f"{name} is not listed under objects: in index.yaml")
        elif obj is not None and "Grants" in header:
            want = ", ".join(obj.grants) or "none"
            if header["Grants"][0] != want:
                add(
                    rel,
                    header["Grants"][1],
                    f"-- Grants: says {header['Grants'][0]!r} but index.yaml lists {want!r}",
                )
        if not deployed(name) and not uses.get(name.upper()):
            add(rel, 1, f"no metric reads {name}; drop the DDL file or add it to a metric's reads:")
        if "Used by" in header and header["Used by"][0] != _used_by_value(
            uses.get(name.upper(), [])
        ):
            add(
                rel,
                header["Used by"][1],
                f"-- Used by: is stale; run `streamsnow sql-review generate {app.name}`",
            )
    for key, obj in declared.items():
        if key not in on_disk and not deployed(obj.name):
            add(
                _rel(app, "sql_review", "index.yaml"),
                1,
                f"object {obj.name} has no DDL file at sql_review/{OBJECTS_DIR}/{obj.name}.sql",
            )
    return found


# --------------------------------------------------------------------------- #
# Lint and comment rules (app queries)
# --------------------------------------------------------------------------- #
#: Comment lines over this share of a query's comment + SQL lines are reported
#: (advisory): comments that outweigh the SQL usually restate it.
COMMENT_RATIO = 0.25


def _query_tokens_for_lint(index: sri.Index) -> dict[str, dict[str, str]]:
    """Query path → token samples (the first metric that renders it wins)."""
    out: dict[str, dict[str, str]] = {}
    for page in index.pages:
        for m in page.metrics:
            out.setdefault(m.query, m.tokens)
    return out


def _comment_findings(rel: str, text: str, ctes: list[tuple[str, int]]) -> list[dict]:
    """The machine-checkable half of the comment rule.

    Every CTE has a ``--`` comment directly above its name (header fields such
    as ``-- Params:`` do not count); every comment line is at most 100
    characters; comments stay under a quarter of the query (advisory). Whether a
    comment restates the SQL is a reviewer's judgement, not this check's.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    found: list[dict] = []
    for name, line in ctes:
        above = lines[line - 2].strip() if line >= 2 else ""
        if not above.startswith("--") or _HEADER_FIELD_RE.match(above):
            found.append(
                {
                    "kind": KIND_COMMENTS,
                    "file": rel,
                    "line": line,
                    "detail": f"CTE {name} needs a one-line comment directly above it saying "
                    "what it is for (for the first CTE, put WITH on its own line first)",
                }
            )
    comment_lines = code_lines = 0
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("--"):
            if len(line.rstrip()) > MAX_LINE:
                found.append(
                    {
                        "kind": KIND_COMMENTS,
                        "file": rel,
                        "line": lineno,
                        "detail": f"comment line is {len(line.rstrip())} characters; keep "
                        f"each comment to one line of {MAX_LINE} or fewer",
                    }
                )
            if not _HEADER_FIELD_RE.match(stripped):
                comment_lines += 1
        else:
            code_lines += 1
    total = comment_lines + code_lines
    if total and comment_lines / total > COMMENT_RATIO:
        found.append(
            {
                "kind": KIND_ADVISORY,
                "file": rel,
                "line": 1,
                "detail": f"comments are {comment_lines} of {total} lines "
                f"({comment_lines * 100 // total}%); aim for under 25%, one short 'why' "
                "per 5 to 10 lines of SQL",
            }
        )
    return found


def _lint_findings(
    repo: Path, app: Path, index: sri.Index, cfg_text: str, lint_files: set[Path] | None
) -> list[dict]:
    """sqlfluff over the app's queries, plus a parse check of each page section."""
    from . import sql_review_lint as srl  # noqa: PLC0415  (lazy: sqlfluff import)

    fragments = {f.file for f in index.fragments}
    samples = _query_tokens_for_lint(index)
    found: list[dict] = []
    for qpath in _query_files(app):
        rel_q = f"queries/{qpath.name}"
        if rel_q in fragments:
            continue  # inlined into other queries; not a statement on its own
        if lint_files is not None and qpath.resolve() not in lint_files:
            continue
        rel = _rel(app, rel_q)
        try:
            text = qpath.read_text(encoding="utf-8").replace("\r\n", "\n")
        except (OSError, UnicodeDecodeError) as exc:
            found.append(
                {"kind": KIND_LINT, "file": rel, "line": 1, "detail": f"unreadable ({exc})"}
            )
            continue
        tokens = samples.get(rel_q, {})
        if any(t not in tokens for t in template_tokens(text)):
            continue  # no sample values: an uncovered query, reported as coverage
        violations, ctes = srl.lint_query(text, cfg_text, tokens)
        found += [
            {"kind": KIND_LINT, "file": rel, "line": line, "detail": f"{code}: {desc}"}
            for line, code, desc in violations
        ]
        found += _comment_findings(rel, text, ctes)
    if lint_files is None:
        for page in _expected_pages(index):
            path = _review_dir(app) / page.filename
            if not path.is_file():
                continue  # reported as provenance
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            body = "\n".join(
                "" if ln.startswith("-- Provenance: ") else ln for ln in text.split("\n")
            )
            for line, desc in srl.parse_problems(body, cfg_text):
                found.append(
                    {
                        "kind": KIND_LINT,
                        "file": _rel(app, "sql_review", page.filename),
                        "line": line,
                        "detail": f"does not parse as Snowflake SQL: {desc}",
                    }
                )
        found += _summary_lint_findings(app, index, cfg_text)
    return found


def _summary_lint_findings(app: Path, index: sri.Index, cfg_text: str) -> list[dict]:
    """sqlfluff and the comment rules over each index ``summary:``, at index.yaml lines.

    A summary is SQL a reviewer reads in the page file, so it is held to the
    query rules. It lints on its own (``detail`` reads as a table), and the
    density advisory is skipped: one comment on a three-line summary is not
    over-commenting.
    """
    from . import sql_review_lint as srl  # noqa: PLC0415  (lazy: sqlfluff import)

    rel = _rel(app, "sql_review", sri.INDEX_NAME)
    found: list[dict] = []
    for page in index.pages:
        for m in page.metrics:
            if not m.summary:
                continue
            description, sql = summary_parts(m.summary)
            text = (f"{description}\n{sql}" if description else sql) + "\n"
            offset = max(m.summary_line, 1) - 1
            violations, ctes = srl.lint_query(text, cfg_text, {})
            found += [
                {
                    "kind": KIND_LINT,
                    "file": rel,
                    "line": offset + line,
                    "detail": f"summary of {m.key!r}: {code}: {desc}",
                }
                for line, code, desc in violations
            ]
            found += [
                {**f, "line": offset + f["line"], "detail": f"summary of {m.key!r}: {f['detail']}"}
                for f in _comment_findings(rel, text, ctes)
                if f["kind"] == KIND_COMMENTS
            ]
    return found


# --------------------------------------------------------------------------- #
# review_value markers
# --------------------------------------------------------------------------- #
def _marker_findings(app: Path, index: sri.Index) -> list[dict]:
    found: list[dict] = []
    for page in index.numbered_pages:
        rel = _rel(app, page.path)

        def add(line: int, detail: str, rel: str = rel) -> None:
            found.append({"kind": KIND_MARKER, "file": rel, "line": line, "detail": detail})

        path = sri._contained(app, page.path)
        if path is None:
            if page.metrics:
                add(
                    1, "page file is missing (or outside the app), so its markers cannot be checked"
                )
            continue
        try:
            markers = sri.scan_markers(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
            add(1, f"cannot parse the page to read its review_value markers ({exc})")
            continue
        keys = {m.key for m in page.metrics}
        counts: dict[str, int] = {}
        for key, line in markers:
            if key is None:
                add(line, "review_value needs a string literal key as its first argument")
            elif key not in keys:
                add(line, f"review_value({key!r}) has no metric {key!r} on this page in index.yaml")
            else:
                counts[key] = counts.get(key, 0) + 1
        for m in page.metrics:
            n = counts.get(m.key, 0)
            if n == 0:
                add(
                    1,
                    f"metric #{m.number} {m.key!r} has no review_value({m.key!r}, ...) call on "
                    "this page; wrap the value its visual shows",
                )
            elif n > 1:
                add(1, f"review_value({m.key!r}) appears {n} times; mark each metric once")
    return found


# --------------------------------------------------------------------------- #
# Check
# --------------------------------------------------------------------------- #
def _old_format(app: Path) -> list[str]:
    rdir = _review_dir(app)
    old = sorted(p.name for p in rdir.glob(_OLD_REVIEW_FILE_GLOB)) if rdir.is_dir() else []
    if (rdir / "manifests").is_dir():
        old.insert(0, "manifests/")
    return old


def _check_page_files(repo: Path, app: Path, index: sri.Index, cfg_text: str) -> list[dict]:
    found: list[dict] = []
    expected = {p.filename: p for p in _expected_pages(index)}
    for path in _generated_page_files(app):
        if path.name not in expected:
            found.append(
                {
                    "kind": KIND_PROVENANCE,
                    "file": _rel(app, "sql_review", path.name),
                    "line": 1,
                    "detail": "orphaned page file: no page in index.yaml produces it; run "
                    f"`streamsnow sql-review generate {app.name}` (it removes stale files)",
                }
            )
    for name, page in expected.items():
        rel = _rel(app, "sql_review", name)

        def add(kind: str, detail: str, line: int = 1, rel: str = rel) -> None:
            found.append({"kind": kind, "file": rel, "line": line, "detail": detail})

        path = _review_dir(app) / name
        if not path.is_file():
            add(
                KIND_PROVENANCE,
                f"page file missing; run `streamsnow sql-review generate {app.name}`",
            )
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            add(KIND_PROVENANCE, f"unreadable page file ({exc})")
            continue
        record, problem = parse_provenance(text)
        if record is None:
            add(KIND_PROVENANCE, problem or "no provenance")
        else:
            if record["inputs"] != _inputs_digest(app, index, page, cfg_text):
                add(
                    KIND_PROVENANCE,
                    "DRIFT: index.yaml, a query, the page's nav entry or .sqlfluff changed since "
                    f"generation; run `streamsnow sql-review generate {app.name}`",
                )
            if record["output"] != _output_digest(text):
                add(
                    KIND_PROVENANCE,
                    "page file was edited by hand; regenerate (index.yaml and queries/*.sql "
                    "are the editing surface, not the generated file)",
                )
        # Belt over the digest: the committed bytes must still be read-only,
        # runnable SQL whatever their provenance says. Provenance is exempt.
        body = "\n".join(
            ln for ln in text.split("\n") if not ln.rstrip("\r").startswith("-- Provenance: ")
        )
        for detail in _verify_read_only(body):
            add(KIND_READONLY, detail)
        for detail in _verify_binds_bound(body) + _verify_session_vars_defined(body):
            add(KIND_BIND, detail)
    return found


def _check_readme(app: Path, index: sri.Index) -> list[dict]:
    rel = _rel(app, "sql_review", "README.md")
    path = _review_dir(app) / "README.md"
    fix = f"run `streamsnow sql-review generate {app.name}`"
    if not path.is_file():
        return [
            {"kind": KIND_PROVENANCE, "file": rel, "line": 1, "detail": f"README.md missing; {fix}"}
        ]
    block = _readme_block_in(path.read_text(encoding="utf-8"))
    if block is None:
        detail = "README.md needs exactly one pair of sql-review-index markers"
    elif block != readme_block(index):
        detail = "README.md tables are stale"
    else:
        return []
    return [{"kind": KIND_PROVENANCE, "file": rel, "line": 1, "detail": f"{detail}; {fix}"}]


def _coverage_findings(app: Path, index: sri.Index) -> list[dict]:
    used = index.metric_queries() | {f.file for f in index.fragments}
    return [
        {
            "kind": KIND_COVERAGE,
            "file": _rel(app, "queries", q.name),
            "line": 1,
            "detail": "query is not any metric's query in sql_review/index.yaml (and not a "
            "declared fragment), so no review section runs it",
        }
        for q in _query_files(app)
        if f"queries/{q.name}" not in used
    ]


def _check_app(repo: Path, app: Path, lint_files: set[Path] | None = None, plan=None) -> list[dict]:
    """Every finding for one app. Import-free: no app code is executed."""
    from . import sql_review_lint as srl  # noqa: PLC0415

    index = sri.load_index(app)
    if plan is None:
        plan = _app_data_plan(repo)
    # The app's share of the repo-wide app-data findings joins EVERY return below,
    # the missing-index ones included: a stray app-data file in an app with no
    # index.yaml would otherwise pass review and only fail at deploy.
    app_data_findings = plan.for_app(app.name) if plan is not None else []
    old = _old_format(app)
    rel_index = _rel(app, "sql_review", sri.INDEX_NAME)
    if not index.exists:
        if old:
            return [
                {
                    "kind": KIND_INDEX,
                    "file": rel_index,
                    "line": 1,
                    "detail": "this app still uses the removed sql_review format "
                    f"({', '.join(old[:3])}{'…' if len(old) > 3 else ''}). StreamSnow 0.8 reads "
                    "sql_review/index.yaml instead; see docs/auditing-a-visual.md, write the "
                    "index, run generate, then delete manifests/ and *.review.sql",
                },
                *app_data_findings,
            ]
        return [
            {
                "kind": KIND_COVERAGE,
                "file": rel_index,
                "line": 1,
                "detail": "no sql_review/index.yaml, so no page of this app has review SQL; "
                "list its pages and metrics there, then run "
                f"`streamsnow sql-review generate {app.name}`",
            },
            *app_data_findings,
        ]
    findings = list(index.findings)
    if old:
        findings.append(
            {
                "kind": KIND_PROVENANCE,
                "file": _rel(app, "sql_review"),
                "line": 1,
                "detail": f"files from the removed sql_review format remain ({', '.join(old)}); "
                "generate removes *.review.sql, delete manifests/ by hand",
            }
        )
    cfg_text = srl.config_text(repo)
    findings += _check_page_files(repo, app, index, cfg_text)
    findings += _check_readme(app, index)
    findings += _marker_findings(app, index)
    findings += _object_findings(app, index, plan.app_data if plan else "")
    findings += app_data_findings
    findings += _lint_findings(repo, app, index, cfg_text, lint_files)
    findings += _coverage_findings(app, index)
    return findings


def coverage_policy(repo: Path) -> str:
    """``sql_review.coverage`` from the repo's config, ``warn`` when absent.

    A missing or invalid config is not this tool's finding (``streamsnow
    doctor`` owns that); it just means the default policy applies.
    """
    cfg_path = find_config(repo)
    if cfg_path is None:
        return "warn"
    try:
        return load_config(cfg_path).sql_review.coverage
    except ConfigError:
        return "warn"


def split_by_policy(findings: list[dict], policy: str) -> tuple[list[dict], list[dict]]:
    """(hard, soft): findings that fail the gate vs. ones only reported.

    ``advisory`` is always soft. ``coverage`` is soft unless the policy is
    ``fail``. Everything else is a correctness finding and always hard.
    """
    soft_kinds = {KIND_ADVISORY} if policy == "fail" else {KIND_ADVISORY, KIND_COVERAGE}
    hard = [f for f in findings if f.get("kind") not in soft_kinds]
    soft = [f for f in findings if f.get("kind") in soft_kinds]
    return hard, soft


def cmd_check(args: argparse.Namespace) -> int:
    repo = Path(args.dir).resolve()
    if args.slug:
        apps = [_app_dir(repo, args.slug)]
    else:
        apps_root = repo / "apps"
        apps = (
            sorted(p for p in apps_root.iterdir() if (p / "snowflake.yml").is_file())
            if apps_root.is_dir()
            else []
        )
    lint_files = (
        {Path(f).resolve() for f in args.lint_files if f.endswith(".sql")}
        if args.lint_files is not None
        else None
    )
    findings: list[dict] = []
    plan = _app_data_plan(repo)
    for app in apps:
        findings += _check_app(repo, app, lint_files, plan=plan)
    policy = coverage_policy(repo)
    hard, soft = split_by_policy(findings, policy)
    result = {"ok": not hard, "coverage_policy": policy, "findings": hard, "warnings": soft}
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        if hard:
            print("The committed review SQL does not match what the app runs:")
            for f in hard:
                print(f"FAIL [{f['kind']}] {f['file']}:{f['line']} {f['detail']}")
        gaps = [f for f in soft if f["kind"] == KIND_COVERAGE]
        advisories = [f for f in soft if f["kind"] == KIND_ADVISORY]
        if gaps:
            print(
                f"Not covered by sql_review yet (coverage policy: {policy}; set "
                "sql_review.coverage: fail in streamsnow.config.yaml to gate on these):"
            )
            for f in gaps:
                print(f"WARN [{f['kind']}] {f['file']}:{f['line']} {f['detail']}")
        if advisories:
            print("Advisory (never gates):")
            for f in advisories:
                print(f"WARN [{f['kind']}] {f['file']}:{f['line']} {f['detail']}")
        if not hard and not soft:
            print("sql-review: clean")
        elif not hard:
            print(f"sql-review: clean ({len(soft)} warning(s))")
    return 0 if result["ok"] else 1


# --------------------------------------------------------------------------- #
# Generate
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _refresh_agents(app: Path) -> str | None:
    """Create sql_review/AGENTS.md and CLAUDE.md when missing; refresh the
    tool-owned part of AGENTS.md (above its marker), keeping the rest."""
    rdir = _review_dir(app)
    wrote = None
    claude = rdir / "CLAUDE.md"
    if not claude.is_file():
        _write(claude, _render_template(CLAUDE_TEMPLATE, app))
    agents = rdir / "AGENTS.md"
    fresh = _render_template(AGENTS_TEMPLATE, app)
    if not agents.is_file():
        _write(agents, fresh)
        return "AGENTS.md"
    current = agents.read_text(encoding="utf-8").replace("\r\n", "\n")
    if AGENTS_MARKER in current and AGENTS_MARKER in fresh:
        updated = fresh.split(AGENTS_MARKER)[0] + AGENTS_MARKER + current.split(AGENTS_MARKER, 1)[1]
        if updated != current:
            _write(agents, updated)
            wrote = "AGENTS.md"
    return wrote


def cmd_generate(args: argparse.Namespace) -> int:
    from . import sql_review_lint as srl  # noqa: PLC0415

    repo = Path(args.dir).resolve()
    app = _app_dir(repo, args.slug)
    index = sri.load_index(app)
    if not index.exists:
        raise ToolError(
            f"no {_rel(app, 'sql_review', sri.INDEX_NAME)}; list the app's pages and metrics "
            "there first (docs/auditing-a-visual.md shows the format)"
        )
    problems = [f for f in index.findings if f["kind"] == KIND_INDEX]
    if problems:
        raise ToolError(
            "index.yaml is invalid, so nothing was generated:\n"
            + "\n".join(f"  {f['file']}:{f['line']} {f['detail']}" for f in problems)
        )
    cfg_text = srl.config_text(repo)
    rdir = _review_dir(app)
    written: list[str] = []
    rendered: dict[str, str] = {}
    for page in _expected_pages(index):
        text = render_page(app, index, page, cfg_text)
        rendered[page.filename] = _stamp_provenance(
            text, _inputs_digest(app, index, page, cfg_text)
        )
    # Everything rendered before anything is written: one bad page leaves the
    # committed files untouched instead of half-regenerated.
    for name, text in rendered.items():
        path = rdir / name
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            _write(path, text)
            written.append(name)
    for stale in _generated_page_files(app):
        if stale.name not in rendered:
            stale.unlink()
            print(f"removed stale {stale.relative_to(repo).as_posix()}")
    for old in sorted(rdir.glob(_OLD_REVIEW_FILE_GLOB)):
        old.unlink()
        print(f"removed old-format {old.relative_to(repo).as_posix()}")

    readme = rdir / "README.md"
    existing = (
        readme.read_text(encoding="utf-8").replace("\r\n", "\n")
        if readme.is_file()
        else _render_template(README_TEMPLATE, app)
    )
    new_readme = _splice_readme(existing, readme_block(index))
    if not readme.is_file() or new_readme != readme.read_text(encoding="utf-8"):
        _write(readme, new_readme)
        written.append("README.md")

    uses = _object_uses(index)
    for path in _ddl_files(app):
        text = path.read_text(encoding="utf-8")
        new = _rewrite_used_by(
            text, _used_by_value(uses.get(path.name[: -len(".sql")].upper(), []))
        )
        if new != text:
            _write(path, new)
            written.append(f"{OBJECTS_DIR}/{path.name}")

    agents = _refresh_agents(app)
    if agents:
        written.append(agents)
    for name in written:
        print(f"wrote {_rel(app, 'sql_review', name)}")
    if not written:
        print(f"sql-review: {app.name} is up to date")
    return 0


def cmd_helper(args: argparse.Namespace) -> int:
    """Show, and with ``--apply`` refresh, the app's ``review.py`` helper.

    ``review.py`` renders with no template variables, so the current scaffold is
    exactly the refreshed file. ``streamsnow update`` never rewrites app files, which
    left an app scaffolded before a helper change with no supported way to catch up.
    A modified file may hold the user's own changes, so it is overwritten only with
    ``--force``. A symlinked ``review.py`` is refused (exit 1) even then, and an app folder
    that resolves outside the repo or a non-UTF-8 helper is a tool error (exit 2).
    """
    from ..scaffolder import _env  # noqa: PLC0415
    from .sql_review_compare import helper_state  # noqa: PLC0415

    repo = Path(args.dir).resolve()
    if find_config(repo) is None:
        raise ToolError(f"no streamsnow.config.yaml found at or above {repo}")
    app = _app_dir(repo, args.slug)
    if not app.resolve().is_relative_to(repo):
        raise ToolError(f"{_rel(app)} resolves outside the repo; refusing to touch it")
    path = app / "review.py"
    rel = _rel(app, "review.py")
    if path.is_symlink():
        print(
            f"error: {rel} is a symlink; it could write outside the repo, so it is never "
            "touched, even with --force. Replace the link with a regular file first.",
            file=sys.stderr,
        )
        return 1
    try:
        state = helper_state(app)
    except UnicodeDecodeError as exc:
        raise ToolError(f"{rel} is not valid UTF-8 ({exc.reason}); fix or remove it first") from exc
    shipped = _env().get_template("app/review.py.j2").render()
    print(f"{rel}: {state}")
    if state == "current":
        print("Nothing to do: it matches the current scaffold.")
        return 0
    if state == "modified":
        current = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            shipped.splitlines(keepends=True),
            fromfile=f"{rel} (yours)",
            tofile=f"{rel} (scaffold)",
        )
        print("".join(diff), end="")
        if args.apply and not args.force:
            print(
                f"error: {rel} differs from the scaffold and may hold your changes; "
                "nothing written. Rerun with --apply --force to overwrite it "
                "(git is the backup: commit or stash first if you want to keep a copy).",
                file=sys.stderr,
            )
            return 1
    if not args.apply:
        print("Dry run: nothing written. Add --apply to write the current scaffold.")
        return 0
    path.write_text(shipped, encoding="utf-8", newline="\n")
    print(f"Wrote {rel} from the current scaffold.")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="sql_review",
        description="Generate and verify each app's sql_review/ page files, and review "
        "them live against Snowflake.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("generate", help="Write page files and README tables from index.yaml.")
    p.add_argument("slug")
    p.add_argument("--dir", default=".", help="Repo root (default: cwd).")

    p = sub.add_parser("check", help="Import-free gate: provenance, markers, lint, coverage.")
    p.add_argument("slug", nargs="?", default=None, help="App slug (default: every app).")
    p.add_argument("--dir", default=".")
    p.add_argument("--format", choices=("md", "json"), default="md")
    p.add_argument(
        "--lint-files",
        nargs="*",
        default=None,
        metavar="FILE",
        help="Lint only these query files (pre-commit passes the staged ones). "
        "Every other check still runs in full.",
    )

    p = sub.add_parser(
        "helper", help="Refresh apps/<slug>/review.py from the current scaffold (dry run default)."
    )
    p.add_argument("slug")
    p.add_argument("--dir", default=".", help="Repo root (default: cwd).")
    p.add_argument("--apply", action="store_true", help="Write the current scaffold.")
    p.add_argument(
        "--force",
        action="store_true",
        help="With --apply, overwrite a modified review.py (it may hold your changes).",
    )

    # Live review (implemented in sql_review_live; the parsers stay here so the
    # CLI-surface snapshot and the skill/CLI parity test read them).
    def live(p: argparse.ArgumentParser, *, snowflake: bool = True) -> None:
        p.add_argument("slug")
        p.add_argument("--dir", default=".", help="Repo root (default: cwd).")
        p.add_argument(
            "--run",
            default=None,
            help="Run id or 'latest' (default: a new run; for log and compare, the latest run).",
        )
        if snowflake:
            p.add_argument("--connection", default=None, help="snow connection (default: config).")
            p.add_argument("--role", default=None, help="Role (default: snowflake.roles.ci_role).")
            p.add_argument(
                "--warehouse", default=None, help="Warehouse (default: objects.default_warehouse)."
            )
            p.add_argument(
                "--timeout", type=int, default=120, help="Statement timeout, seconds (default 120)."
            )

    p = sub.add_parser("probe", help="Live: objects exist, grants, DDL drift, sections compile.")
    live(p)
    p = sub.add_parser("run", help="Live: each section as aggregates (rows, totals, hash).")
    live(p)
    p.add_argument("--page", default=None, help="Only this page number (e.g. 01).")
    p.add_argument(
        "--slow-s", type=int, default=10, help="Flag sections slower than this (default 10 s)."
    )
    p = sub.add_parser("bench", help="Live: benchmark a section, optionally against a rewrite.")
    live(p)
    p.add_argument("--metric", required=True, help="Page#metric, e.g. 01#2.")
    p.add_argument("--sql-file", default=None, help="Candidate replacement for the query file.")
    p.add_argument("--runs", type=int, default=3, help="Timed runs per variant (1-5, default 3).")
    p = sub.add_parser("compare", help="What each visual received vs run, with tolerance.")
    live(p, snowflake=False)
    p.add_argument(
        "--capture",
        default=None,
        metavar="DIR",
        help="Captures from review preview mode (default: the run's capture/ folder).",
    )
    p.add_argument(
        "--screen",
        default=None,
        metavar="FILE",
        help="The browser walk's notes (default: the run's screen.json, when present).",
    )
    p = sub.add_parser("log", help="Write the committed review log from verified findings.")
    live(p, snowflake=False)
    p.add_argument("--findings", required=True, help="JSON file of verified findings.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the findings against the run and print the log; write nothing.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dispatch = {"generate": cmd_generate, "check": cmd_check, "helper": cmd_helper}
    try:
        if args.cmd not in dispatch:
            from . import sql_review_live as live  # noqa: PLC0415

            return live.dispatch(args)
        return dispatch[args.cmd](args)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
