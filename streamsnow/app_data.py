"""App data: the views and dynamic tables the deploy job builds for the apps (#79).

Why this exists
---------------
Apps read report-ready sources directly. When a query is slow, or its logic
repeats across pages or apps, the repo may declare an object in
``governance.app_data``: its DDL sits next to the app in
``apps/<slug>/sql_review/app_specific_reporting_objects/<DB>.<SCHEMA>.<OBJECT>.sql``
and the deploy job applies it, owned by the CI role, before any app is
replaced. Before this a person applied that DDL by hand, and an app deployed
against an object nobody had created (or had created from an older file) came
up empty or wrong while local preview worked.

Deployed DDL runs unattended as the CI role, so each file is checked before
anything is printed. Exactly one CREATE, first, whose target is the object the
file name and its index.yaml entry name (a copied file that still creates the
original object would silently redefine it), in a form that survives the next
deploy:

- ``CREATE OR ALTER DYNAMIC TABLE``. ``CREATE OR REPLACE`` recreates the table
  on every deploy: a full refresh each time, and its grants are gone.
- ``CREATE OR REPLACE VIEW ... COPY GRANTS`` (the form to write: a view holds
  no data, so replacing it is cheap and always takes the new query; without
  ``COPY GRANTS`` every deploy drops the view's grants), or ``CREATE OR ALTER
  VIEW`` (accepted; if Snowflake cannot alter the query that way, the deploy
  fails on that statement rather than keep the old query).
- Never a table. ``CREATE OR ALTER TABLE`` drops the data in a renamed or
  removed column and takes no ``AS SELECT``, so a summary table becomes a
  dynamic table instead.

A dynamic table names ``WAREHOUSE = <snowflake.objects.default_warehouse>``,
the one warehouse the CI role that owns it may use, and ``INITIALIZE =
ON_CREATE``, so its first refresh runs inside the deploy and a query that
cannot refresh fails the deploy before any app changes. The only other
statements allowed are ``GRANT SELECT`` on the same object to the roles its
index entry lists. Files whose ``DB.SCHEMA`` is not app data stay review-only:
nothing executes them. Docs: https://docs.snowflake.com/en/sql-reference/sql/create-dynamic-table,
https://docs.snowflake.com/en/sql-reference/sql/create-view and
https://docs.snowflake.com/en/sql-reference/sql/create-or-alter
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .policy import NAME_PATTERN, display_name, split_name
from .tools.check_schema_refs import _normalize_breaks, relation_names
from .tools.sql_review import _mask_with_status

KIND_VIEW = "view"
KIND_DYNAMIC_TABLE = "dynamic_table"
OBJECT_KINDS = (KIND_VIEW, KIND_DYNAMIC_TABLE)
#: The SQL keyword for each kind, in CREATE, GRANT and DROP.
SQL_KIND = {KIND_VIEW: "VIEW", KIND_DYNAMIC_TABLE: "DYNAMIC TABLE"}
#: Why an app-data object may exist (D13): it pre-computes a slow or costly query, or
#: one definition replaces logic that two or more queries, pages or apps would repeat.
REASONS = ("performance", "shared_logic")
KIND_FINDING = "objects"
KIND_ADVISORY = "advisory"

_IDENT = r'"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*'
_NAME_RE = re.compile(NAME_PATTERN)
_CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+(?P<mode>ALTER|REPLACE)\s+)?"
    r"(?P<mods>(?:(?:SECURE|RECURSIVE|TRANSIENT|TEMPORARY|TEMP|VOLATILE|LOCAL|GLOBAL"
    r"|HYBRID|ICEBERG|EXTERNAL|EVENT|MATERIALIZED)\s+)*)"
    r"(?P<kind>DYNAMIC\s+TABLE|[A-Z_]+)\s+(?P<ine>IF\s+NOT\s+EXISTS\s+)?",
    re.I,
)
_GRANT_RE = re.compile(
    r"GRANT\s+(?P<privs>[A-Z_][A-Z_ ,]*?)\s+ON\s+(?P<kind>DYNAMIC\s+TABLE|VIEW)\s+", re.I
)
_TO_ROLE_RE = re.compile(rf"\s+TO\s+ROLE\s+(?P<role>{_IDENT})\s*\Z", re.I)
_WAREHOUSE_RE = re.compile(rf"\bWAREHOUSE\s*=\s*(?P<wh>{_IDENT})", re.I)
_INITIALIZE_RE = re.compile(r"\bINITIALIZE\s*=\s*(?P<v>[A-Z_]+)", re.I)
_COPY_GRANTS_RE = re.compile(r"\bCOPY\s+GRANTS\b", re.I)
_AS_SCAN_RE = re.compile(r"[()]|\bAS\b", re.I)


@dataclass(frozen=True)
class ParsedDDL:
    """One app-data DDL file, parsed. Empty ``problems`` means it may deploy."""

    kind: str = ""  # view | dynamic_table | "" when no deployable CREATE was found
    statements: tuple[str, ...] = ()  # original text, CREATE first, no trailing ';'
    reads: tuple[tuple[int, tuple[str, ...]], ...] = ()  # (file line, parts); one-part resolved
    body: str = ""  # the query after the top-level AS, original text
    line: int = 1  # the CREATE's line
    problems: tuple[tuple[int, str], ...] = ()


def _statements(text: str) -> tuple[list[tuple[int, str, str]], str | None]:
    """``(line, original, masked)`` per statement, split on ``;`` outside literals and
    comments; leading comments dropped, and the original cut where the masked text
    ends, so a trailing comment never swallows the ``;`` printed after it."""
    text = _normalize_breaks(text)  # a lone CR is a line break to the scanners and to us
    masked, unterminated = _mask_with_status(text)
    out: list[tuple[int, str, str]] = []
    start = 0
    for i, ch in enumerate(masked + ";"):
        if ch != ";":
            continue
        piece = masked[start:i]
        if piece.strip():
            lead = len(piece) - len(piece.lstrip())
            body = piece[lead:].rstrip()
            begin = start + lead
            out.append((text.count("\n", 0, begin) + 1, text[begin : begin + len(body)], body))
        start = i + 1
    return out, unterminated


def _kind_of(m: re.Match) -> str:
    word = " ".join(m.group("kind").upper().split())
    mods = set(m.group("mods").upper().split())
    if word == "DYNAMIC TABLE" and mods <= {"TRANSIENT"}:
        return KIND_DYNAMIC_TABLE
    if word == "VIEW" and mods <= {"SECURE", "RECURSIVE"}:
        return KIND_VIEW
    return ""


def _top_level_as(masked: str, start: int) -> re.Match | None:
    depth = 0
    for m in _AS_SCAN_RE.finditer(masked, start):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0:
            return m
    return None


def _grant_role(orig: str, masked: str, kind: str, expect: tuple[str, ...]) -> str | None:
    """The role of ``GRANT SELECT ON <kind> <expect> TO ROLE <role>``, else None."""
    g = _GRANT_RE.match(masked)
    if not g or " ".join(g.group("privs").upper().split()) != "SELECT":
        return None
    if " ".join(g.group("kind").upper().split()) != SQL_KIND[kind]:
        return None
    name = _NAME_RE.match(orig, g.end())
    if not name or split_name(name.group(0)) != expect:
        return None
    r = _TO_ROLE_RE.match(masked, name.end())
    if not r:
        return None
    parts = split_name(orig[r.start("role") : r.end("role")])
    return parts[0] if len(parts) == 1 else None


def ddl_kind(text: str) -> str:
    """The kind the first CREATE in ``text`` builds, or "" (lenient: no other checks)."""
    stmts, _ = _statements(text)
    for _line, _orig, masked in stmts:
        m = _CREATE_RE.match(masked)
        if m:
            return _kind_of(m)
    return ""


def parse_ddl(
    text: str, expect: tuple[str, ...], *, warehouse: str, grants: tuple[str, ...] = ()
) -> ParsedDDL:
    """Check one app-data file against the rules in the module docstring."""
    expect = tuple(expect)
    stmts, unterminated = _statements(text)
    if unterminated:
        return ParsedDDL(problems=((1, f"cannot parse: {unterminated}"),))
    creates = [s for s in stmts if re.match(r"CREATE\b", s[2], re.I)]
    if len(creates) != 1:
        line = creates[1][0] if len(creates) > 1 else 1
        return ParsedDDL(
            problems=(
                (
                    line,
                    f"needs exactly one CREATE statement, found {len(creates)}: the deploy "
                    "job builds one object per file",
                ),
            )
        )
    create = creates[0]
    line, orig, masked = create
    problems: list[tuple[int, str]] = []
    if stmts[0] is not create:
        problems.append(
            (stmts[0][0], "the CREATE comes first: the deploy job runs this file as the CI role")
        )
    m = _CREATE_RE.match(masked)
    kind = _kind_of(m) if m else ""
    if not kind:
        word = " ".join(m.group("kind").upper().split()) if m else ""
        detail = (
            "a plain table cannot deploy to app data: CREATE OR ALTER TABLE drops the data in "
            "a renamed or removed column and takes no AS SELECT. Pre-compute it as a dynamic "
            "table (CREATE OR ALTER DYNAMIC TABLE ... AS SELECT)"
            if word == "TABLE"
            else "only views and dynamic tables deploy to app data (CREATE OR REPLACE VIEW ... "
            "COPY GRANTS, CREATE OR ALTER VIEW, CREATE OR ALTER DYNAMIC TABLE)"
        )
        return ParsedDDL(line=line, problems=(*problems, (line, detail)))
    mode = (m.group("mode") or "").upper()
    if m.group("ine"):
        problems.append(
            (line, "IF NOT EXISTS never applies a changed definition: use CREATE OR ALTER")
        )
    target_m = _NAME_RE.match(orig, m.end())
    target = split_name(target_m.group(0)) if target_m else ()
    if target != expect:
        problems.append(
            (
                line,
                f"the CREATE names {display_name(target) or 'no object'}, but the file and its "
                f"index.yaml entry name {display_name(expect)}",
            )
        )
    after = target_m.end() if target_m else m.end()
    as_m = _top_level_as(masked, after)
    clauses = masked[after : as_m.start()] if as_m else masked[after:]
    body = orig[as_m.end() :] if as_m else ""
    if as_m is None:
        problems.append((line, "no AS <query> follows the object name"))
    if kind == KIND_DYNAMIC_TABLE:
        if mode != "ALTER":
            why = (
                "CREATE OR REPLACE recreates it on every deploy, a full refresh that also "
                "drops its grants"
                if mode == "REPLACE"
                else "a plain CREATE fails on the second deploy"
            )
            problems.append(
                (line, f"a dynamic table deploys with CREATE OR ALTER DYNAMIC TABLE: {why}")
            )
        whs = [
            split_name(orig[after + m_.start("wh") : after + m_.end("wh")])
            for m_ in _WAREHOUSE_RE.finditer(clauses)
        ]
        if whs != [(warehouse.upper(),)]:
            if len(whs) > 1:
                sets = "; this file sets it more than once"
            else:
                sets = f"; this file sets {display_name(whs[0])}" if whs else ""
            problems.append(
                (
                    line,
                    f"set WAREHOUSE = {warehouse} (snowflake.objects.default_warehouse) exactly "
                    "once: the CI role that owns the dynamic table may refresh it on no other "
                    f"warehouse{sets}",
                )
            )
        inits = [m_.group("v").upper() for m_ in _INITIALIZE_RE.finditer(clauses)]
        if inits != ["ON_CREATE"]:
            problems.append(
                (
                    line,
                    "set INITIALIZE = ON_CREATE exactly once: the first refresh then runs inside "
                    "the deploy, so a query that cannot refresh fails the deploy before any app "
                    "changes",
                )
            )
    elif mode == "REPLACE" and not _COPY_GRANTS_RE.search(clauses):
        problems.append(
            (
                line,
                "CREATE OR REPLACE VIEW needs COPY GRANTS before AS, or every deploy drops the "
                "view's grants; or use CREATE OR ALTER VIEW",
            )
        )
    elif not mode:
        problems.append(
            (
                line,
                "a view deploys with CREATE OR REPLACE VIEW ... COPY GRANTS (or CREATE OR "
                "ALTER VIEW): a plain CREATE fails on the second deploy",
            )
        )
    allowed = f"GRANT SELECT ON {SQL_KIND[kind]} {display_name(expect)} TO ROLE <role>"
    roles: list[str] = []
    for stmt in stmts:
        if stmt is create:
            continue
        role = _grant_role(stmt[1], stmt[2], kind, expect)
        if role is None:
            problems.append(
                (
                    stmt[0],
                    f"only `{allowed}` may follow the CREATE: the deploy job runs every "
                    "statement in this file as the CI role",
                )
            )
        else:
            roles.append(role)
    want = sorted({g.upper() for g in grants})
    got = sorted(set(roles))
    if got != want:
        problems.append(
            (
                line,
                f"the file grants SELECT to {', '.join(got) or 'no role'}, but its index.yaml "
                f"grants list {', '.join(want) or 'none'}: keep the two the same",
            )
        )
    reads: list[tuple[int, tuple[str, ...]]] = []
    if as_m is not None:
        # relation_names gives lines within the body; the body starts on this file line.
        base = line + orig.count("\n", 0, as_m.end())
        for rel in relation_names(body):
            if rel.kind != "object" or not rel.parts or rel.cte:
                continue  # a CTE in scope is not a table
            parts = rel.parts
            if len(parts) == 1:
                # Snowflake resolves an unqualified relation in a view's definition in the
                # view's own schema, so it is an app-data dependency (ordered, cycle-checked).
                parts = (*expect[:2], parts[0])
            reads.append((base + rel.line - 1, parts))
    return ParsedDDL(kind, tuple(s[1] for s in stmts), tuple(reads), body, line, tuple(problems))
