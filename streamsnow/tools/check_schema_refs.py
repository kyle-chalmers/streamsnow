"""Check SQL in app code against the governance boundary.

Config-driven from ``governance`` in ``streamsnow.config.yaml`` through
:class:`streamsnow.policy.SchemaPolicy`. One implementation consumed by
pre-commit, CI, the ``/validate-app`` skill, ``streamsnow check schema-refs``,
the live SQL review's guard and ``migrate``.

Two scans, because they protect different things:

- **Deny list (always fails).** Today's every-dotted-token scan of SQL stays, so a
  denied raw layer cannot slip through a position the relation scan does not
  model (a comma join, a function argument, a string handed to IDENTIFIER). A
  three-part token tests its middle segment against bare entries (``RAW``:
  every database) and qualified ones (``FINANCE.RAW``: that database); a
  two-part token tests its first segment against bare entries only, because
  ``alias.column`` looks exactly like ``schema.object``. Names in relation
  position are tested too, which catches a qualified name split across lines,
  and a name of four or more parts (``DB.SCHEMA.TABLE.COLUMN``, never a
  relation) is tested through its database and schema parts so a trailing
  column cannot hide a denied schema.
  ``USE ROLE`` and ``USE SECONDARY ROLES`` lines name roles, never schemas, and
  are not scanned.
- **Boundary (warn, or fail under ``governance.boundary: enforce``).** Only names
  in relation position count: after FROM (and every comma of a FROM list,
  across JOIN ... ON), JOIN, INTO, UPDATE, after USE DATABASE/SCHEMA, and the
  string literal of ``IDENTIFIER('DB.S.T')`` / ``IDENTIFIER($$DB.S.T$$)`` or of
  its synonym ``TABLE('DB.S.T')`` / ``TABLE($$DB.S.T$$)``
  (https://docs.snowflake.com/en/sql-reference/identifier-literal). A session
  variable, a bind, or a table function inside TABLE(...) (``TABLE(FLATTEN(...))``)
  cannot be resolved statically and is skipped. Alias-qualified columns such as
  ``t.id`` or ``params.start_date``, and the FROM inside ``EXTRACT(... FROM t.day)``,
  are not objects: reading every dotted token as an object would flag them all.
  Names are split the way Snowflake resolves them, so a quoted part keeps its
  case and may hold dots. That assumes the account keeps Snowflake's default
  ``QUOTED_IDENTIFIERS_IGNORE_CASE = FALSE``; with it TRUE, quoted names fold to
  upper case too and the exact-case match is stricter than Snowflake. A
  three-part name outside ``governance.sources`` and app data, or any two-part
  name, is reported. Text handed to a Streamlit element (``st.caption``,
  ``st.markdown``, ...) is prose about data, never SQL, and neither scan reads it.

Detection (mirrors the battle-tested source monorepo
``tools/check_schema_refs.py``):

- ``.py`` (AST-based): only string *literals* that look like SQL are scanned.
  A literal is treated as SQL when it is either (a) an argument to a
  query/sql/execute-style call **or** (b) contains a SQL keyword
  (SELECT/INSERT/UPDATE/DELETE/FROM/JOIN, case-insensitive). Module/class/
  function **docstrings are excluded**, and prose passed to ``st.markdown`` /
  ``st.caption`` / ``st.write`` never trips the guard, the rule blocks
  instructions to the database, not documentation *about* the ban.
- ``.sql`` (text-based): each line is scanned after stripping ``-- ...`` line
  comments and ``/* ... */`` block comments.

Extras StreamSnow keeps over the source: config-driven denylist, exact-FQN
``read_exceptions`` bypass, quoted-identifier + whitespace normalization, and
``USE SCHEMA`` detection.

Only the **schema-position** segment is tested against the denylist, matching
the source (which flags a denied name only when it is followed by a dot):
2-part ``SCHEMA.OBJECT`` tests ``SCHEMA`` (the first segment), 3-part
``DB.SCHEMA.OBJECT`` tests the middle segment. A denied name in the database or
trailing-object position (e.g. ``DB.BRIDGE``) is not flagged.

The file-walk skips dotted directories (``.review/``, ``.git/``, ...) so review
artifacts and VCS metadata are never scanned as app code.

Exit codes: 0 = clean (warnings allowed), 1 = a denied reference, or a boundary
finding under enforce, 2 = tool/usage error.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from ..config import ConfigError, load_config
from ..policy import (
    BOUNDARY_VERDICTS,
    DENIED,
    NAME_PATTERN,
    OUTSIDE_BOUNDARY,
    TWO_PART,
    SchemaPolicy,
    display_name,
    split_name,
)

# A literal is treated as SQL if it contains one of these keywords ...
_SQL_KEYWORD_RE = re.compile(r"(?is)\b(SELECT|INSERT|UPDATE|DELETE|FROM|JOIN)\b")
# ... or if it is passed to a call whose method/function name is one of these.
_QUERY_CALL_NAMES = frozenset({"query", "sql", "execute", "read_sql", "render_sql", "load_sql"})

_DOTTED = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_$]*)\.([A-Za-z_][A-Za-z0-9_$]*)(?:\.([A-Za-z_][A-Za-z0-9_$]*))?"
)
# USE SCHEMA RAW / USE DATABASE.RAW / USE RAW
_USE = re.compile(
    r"\bUSE\s+(?:SCHEMA\s+|DATABASE\s+)?([A-Za-z_][A-Za-z0-9_$]*)(?:\.([A-Za-z_][A-Za-z0-9_$]*))?",
    re.IGNORECASE,
)


def _strip_sql_comments(text: str) -> str:
    # Drop -- line comments and /* */ block comments so commented refs don't trip.
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


# `USE ROLE <name>` and `USE SECONDARY ROLES ...` name roles, never schemas: a role
# called "STAGING.READ_ONLY" must not read as a reference to a STAGING schema.
_USE_ROLE = re.compile(r'(?i)\bUSE\s+(?:SECONDARY\s+ROLES|ROLE)\s+(?:"(?:[^"]|"")*"|\S+)')


def _denied_in_line(line: str, policy: SchemaPolicy, read_exc: set[str]) -> set[tuple[str, str]]:
    """``(DATABASE, schema)`` for each denied reference in one line (``""``: unknown).

    Quoted identifiers (``"BI"."BRIDGE"``) and whitespace around dots
    (``DB . BRIDGE . T``) are normalized first so they cannot slip past. Only
    the schema-position segment is tested: a 3-part ``DB.SCHEMA.OBJECT`` tests
    the middle one with its database (bare and qualified entries); a 2-part
    ``SCHEMA.OBJECT`` tests the first one against bare entries only. A trailing
    ``DB.BRIDGE`` is not a hit.
    """
    hits: set[tuple[str, str]] = set()
    norm = re.sub(r"\s*\.\s*", ".", _USE_ROLE.sub(" ", line).replace('"', ""))
    for m in _DOTTED.finditer(norm):
        if m.group(0).upper() in read_exc:
            continue  # sanctioned exact-FQN read
        first, second, third = m.group(1), m.group(2), m.group(3)
        if third:
            if policy.is_denied(second, first):
                hits.add((first.upper(), second))
        elif policy.is_denied(first):
            hits.add(("", first))
    # USE SCHEMA <denied> / USE SCHEMA DB.<denied>: not a dotted object ref.
    for m in _USE.finditer(norm):
        database, schema = (m.group(1), m.group(2)) if m.group(2) else (None, m.group(1))
        if schema and policy.is_denied(schema, database):
            hits.add((database.upper() if database else "", schema))
    return hits


def _scan_text(text: str, policy: SchemaPolicy, read_exc: set[str]) -> set[tuple[int, str, str]]:
    """Line-by-line deny scan over already-comment-stripped *text*."""
    hits: set[tuple[int, str, str]] = set()
    for i, line in enumerate(text.splitlines(), start=1):
        for database, schema in _denied_in_line(line, policy, read_exc):
            hits.add((i, database, schema))
    return hits


def _collect_docstring_ids(tree: ast.AST) -> set[int]:
    """Return ``id()`` of every Constant node that is a module/class/func docstring."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


# Streamlit text elements: their string arguments are prose about data, never SQL.
_PROSE_CALL_NAMES = frozenset(
    {
        "markdown", "caption", "write", "text", "title", "header", "subheader",
        "info", "warning", "error", "success", "toast", "metric",
    }
)  # fmt: skip


def _collect_call_arg_ids(tree: ast.AST, names: frozenset[str]) -> set[int]:
    """``id()`` of string-literal args (positional or keyword) passed to calls named *names*."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) not in names:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                ids.add(id(arg))
    return ids


def _sql_chunks(text: str, is_python: bool, *, skip_prose: bool = False) -> list[tuple[int, str]]:
    """``(first line, sql)`` pieces to scan: the whole text for ``.sql``; for
    ``.py``, the SQL-looking string literals (query-call args, or containing a
    SQL keyword), never docstrings, and with ``skip_prose`` never the text of a
    Streamlit element. Unparseable Python yields nothing (see the module docs)."""
    if not is_python:
        return [(1, text)]
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    docstrings = _collect_docstring_ids(tree)
    queries = _collect_call_arg_ids(tree, _QUERY_CALL_NAMES)
    prose = _collect_call_arg_ids(tree, _PROSE_CALL_NAMES) if skip_prose else set()
    chunks: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings or id(node) in prose:
            continue
        if id(node) in queries or _SQL_KEYWORD_RE.search(node.value):
            chunks.append((node.lineno, node.value))
    return chunks


class BoundaryRef(NamedTuple):
    line: int
    ref: str  # display_name of the parts: plain parts bare, others quoted
    verdict: str  # outside_boundary | two_part
    database: str  # normalized part; "" when the name does not say
    schema: str
    detail: str


_BOUNDARY_DETAIL = {
    OUTSIDE_BOUNDARY: "{ref} is outside governance.sources and app_data: the CI role that "
    "owns the deployed app has no read grant there",
    TWO_PART: "{ref} does not name its database: write DATABASE.SCHEMA.OBJECT (or "
    "DATABASE.SCHEMA after USE SCHEMA), because the session's database differs between "
    "preview and the deployed app",
}
_TOKEN_RE = re.compile(
    rf"(?P<lit>'[^']*'|\$\$.*?\$\$)|(?P<name>{NAME_PATTERN})|(?P<punct>[(),;])|(?P<other>\S)",
    re.S,
)
# Words that can follow a relation but never are its alias: a clause or a join starts.
_NOT_ALIAS = frozenset(
    {
        "WHERE", "GROUP", "ORDER", "HAVING", "QUALIFY", "LIMIT", "OFFSET", "FETCH",
        "WINDOW", "UNION", "EXCEPT", "MINUS", "INTERSECT", "JOIN", "INNER", "LEFT",
        "RIGHT", "FULL", "OUTER", "CROSS", "NATURAL", "ASOF", "LATERAL", "ON", "USING",
        "SELECT", "FROM", "CONNECT", "START", "MATCH_CONDITION", "SET", "VALUES", "WHEN",
        "RETURNING",
    }
)  # fmt: skip
# Words that close a FROM list: after them a top-level comma is no longer a relation.
_CLAUSE_END = frozenset(
    {
        "WHERE", "GROUP", "ORDER", "HAVING", "QUALIFY", "LIMIT", "OFFSET", "FETCH",
        "WINDOW", "UNION", "EXCEPT", "MINUS", "INTERSECT", "SET", "VALUES", "CONNECT",
        "START", "RETURNING",
    }
)  # fmt: skip
# Words after a relation that modify it (time travel, sampling, pivots); a
# parenthesized group follows, and the alias may come after that.
_RELATION_MODIFIERS = frozenset(
    {"AT", "BEFORE", "CHANGES", "SAMPLE", "TABLESAMPLE", "PIVOT", "UNPIVOT", "MATCH_RECOGNIZE"}
)


def _blank(span: str) -> str:
    return re.sub(r"[^\n]", " ", span)


def _mask_sql(text: str) -> str:
    """Comments to spaces; string and ``$$`` literal CONTENTS to spaces with their
    delimiters kept; double-quoted identifiers untouched. Same length, newlines
    kept, so offsets and line numbers match the original text, which is where
    an IDENTIFIER literal's value is read back from."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        two, ch = text[i : i + 2], text[i]
        if two == "--":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(_blank(text[i:j]))
            i = j
        elif two == "/*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(_blank(text[i:j]))
            i = j
        elif two == "$$":
            j = text.find("$$", i + 2)
            if j == -1:
                out.append(_blank(text[i:]))
                break
            out.append("$$" + _blank(text[i + 2 : j]) + "$$")
            i = j + 2
        elif ch == '"':
            j = i + 1
            while j < n:
                if text[j] == '"':
                    if text[j + 1 : j + 2] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
        elif ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    if text[j + 1 : j + 2] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                out.append(_blank(text[i:]))
                break
            out.append("'" + _blank(text[i + 1 : j]) + "'")
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _literal(raw: str, start: int, end: int) -> str:
    """The value of the literal at ``raw[start:end]``, from the unmasked text."""
    body = raw[start:end]
    if body.startswith("$$"):
        return body[2:-2]
    return body[1:-1].replace("''", "'").replace("\\'", "'")


@dataclass
class _Frame:
    """Scanner state for one parenthesis depth."""

    select: bool = False  # a SELECT or DELETE opened at this depth
    state: str = ""  # "" | "expect" (a relation comes next) | "after" (one was just read)
    from_clause: bool = False  # inside a FROM clause: a top-level comma starts a relation
    alias: bool = False  # the relation's alias has been read


def _relation_names(sql: str) -> list[tuple[int, tuple[str, ...], str]]:
    """``(line, parts, kind)`` for each name in relation position.

    ``kind`` is ``object`` (after FROM, JOIN, INTO, UPDATE, or the literal of
    IDENTIFIER(...) / TABLE(...)), ``schema`` (USE SCHEMA, or a dotted USE)
    or ``database`` (USE DATABASE). A FROM counts at the top level, or inside
    parentheses opened by a SELECT or DELETE; so EXTRACT(YEAR FROM t.day),
    TRIM(... FROM c.name) and IS DISTINCT FROM are skipped. A FROM clause lasts
    until a clause keyword (WHERE, GROUP, ORDER, UNION, ...): every top-level
    comma inside it, including one after JOIN ... ON or USING (...), starts a
    relation. After a relation, one alias (optionally after AS) and any
    parenthesized modifier are skipped.
    """
    text = _mask_sql(sql)
    newlines = [m.start() for m in re.finditer("\n", text)]
    toks = [(m.lastgroup, m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]

    def line_of(pos: int) -> int:
        return bisect.bisect_left(newlines, pos) + 1

    def peek(k: int) -> tuple[str, str, int, int]:
        return toks[k] if k < len(toks) else ("", "", 0, 0)

    found: list[tuple[int, tuple[str, ...], str]] = []
    frames = [_Frame()]
    prev = ""
    i = 0
    while i < len(toks):
        kind, tok, pos, _end = toks[i]
        up = tok.upper() if kind == "name" else tok
        f = frames[-1]
        i += 1
        if tok == ";":
            frames, prev = [_Frame()], ""
            continue
        if tok == "(":
            if f.state == "expect":
                f.state, f.alias = "after", False  # a subquery stands where a relation would
            frames.append(_Frame())
            prev = tok
            continue
        if tok == ")":
            if len(frames) > 1:
                frames.pop()
            prev = tok
            continue
        if tok == "," and f.from_clause and f.state in ("", "after"):
            f.state = "expect"
            prev = tok
            continue
        if f.state == "expect":
            if kind == "name" and up in ("TABLE", "IDENTIFIER") and peek(i)[1] == "(":
                # TABLE('...') is a synonym of IDENTIFIER('...'): a literal argument names
                # the relation. Anything else (TABLE(FLATTEN(...)), a UDTF call, $var, a
                # bind) cannot be resolved statically and is skipped.
                inner = peek(i + 1)
                if inner[0] == "lit" and peek(i + 2)[1] == ")":
                    parts = split_name(_literal(sql, inner[2], inner[3]))
                    if parts:
                        found.append((line_of(inner[2]), parts, "object"))
                f.state, f.alias = "after", False
                prev = tok
                continue
            if kind == "name" and up not in _NOT_ALIAS:
                found.append((line_of(pos), split_name(tok), "object"))
                f.state, f.alias = "after", False
                prev = tok
                continue
            f.state = ""
        elif f.state == "after":
            if kind == "name" and (up in _RELATION_MODIFIERS or (up == "AS" and not f.alias)):
                prev = tok
                continue
            if (
                kind == "name"
                and len(split_name(tok)) == 1
                and up not in _NOT_ALIAS
                and not f.alias
            ):
                f.alias = True
                prev = tok
                continue
            f.state = ""
        if kind != "name":
            prev = tok
            continue
        if up == "FROM" and prev.upper() != "DISTINCT" and (len(frames) == 1 or f.select):
            f.state, f.from_clause = "expect", True
        elif up == "JOIN":
            f.state = "expect"  # the FROM list stays open: a comma after ON continues it
        elif up in ("INTO", "UPDATE"):
            f.state, f.from_clause = "expect", False
        elif up in ("SELECT", "DELETE"):
            f.select, f.from_clause = True, False
        elif up in _CLAUSE_END:
            f.from_clause = False
        elif up == "USE":
            what, j = "database", i
            head = peek(i)[1].upper() if peek(i)[0] == "name" else ""
            if head in ("SCHEMA", "DATABASE"):
                what, j = head.lower(), i + 1
            elif head in ("ROLE", "WAREHOUSE", "SECONDARY"):
                what = ""
            if what and peek(j)[0] == "name":
                parts = split_name(peek(j)[1])
                found.append((line_of(peek(j)[2]), parts, "schema" if len(parts) == 2 else what))
        prev = tok
    return found


def _db_schema(parts: tuple[str, ...], kind: str) -> tuple[str, str]:
    if kind == "database":
        return parts[0], ""
    if kind == "schema":
        return (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
    return (parts[0], parts[1]) if len(parts) == 3 else ("", parts[0])


def _verdict(policy: SchemaPolicy, parts: tuple[str, ...], kind: str) -> str:
    if kind == "database":
        return policy.classify_database(parts[0])
    if kind == "schema":
        if len(parts) == 2:
            return policy.classify_schema(parts[0], parts[1])
        return policy.classify_schema(None, parts[0])
    return policy.classify_parts(parts)


def _deny_hits(
    text: str, policy: SchemaPolicy, is_python: bool = False
) -> list[tuple[int, str, str]]:
    """Sorted ``(line, DATABASE, schema)`` for each denied reference: the line scan,
    plus denied names in relation position (a qualified name split across lines).
    Streamlit element text is skipped, as in the boundary scan: a caption that says
    "Loaded from FINANCE_DB.STAGING.LEADS" describes data, it queries nothing. SQL
    literals (query-call args, or strings with a SQL keyword) are read as before."""
    if not policy.schema_deny:
        return []
    read_exc = {e.upper() for e in policy.read_exceptions}
    hits: dict[tuple[int, str, str], tuple[int, str, str]] = {}
    for base_line, sql in _sql_chunks(text, is_python, skip_prose=True):
        for offset, database, schema in _scan_text(_strip_sql_comments(sql), policy, read_exc):
            # A literal's lineno is its first line; offset is 1-based within it.
            at = base_line + offset - 1
            hits.setdefault((at, database.upper(), schema.upper()), (at, database, schema))
        for line, parts, kind in _relation_names(sql):
            if len(parts) >= 4 and kind == "object":
                # classify ignores names this long (no relation has four parts), but
                # DENIED_DB.RAW.T.COL must not lose the coverage the line scan gave it.
                denied = policy.is_denied(parts[1], parts[0])
                database, schema = parts[0], parts[1]
            else:
                denied = bool(parts) and _verdict(policy, parts, kind) == DENIED
                database, schema = _db_schema(parts, kind) if parts else ("", "")
            if denied:
                at = base_line + line - 1
                hits.setdefault((at, database.upper(), schema.upper()), (at, database, schema))
    return sorted(hits.values())


def find_denied_refs(
    text: str, policy: SchemaPolicy, is_python: bool = False
) -> list[tuple[int, str]]:
    """Return sorted, de-duped (line_number, schema) for each denied reference.

    ``is_python=True`` enables the AST-based SQL-literal scan that excludes
    docstrings and prose. Default (text mode) suits ``.sql`` files.
    """
    return sorted({(line, schema) for line, _db, schema in _deny_hits(text, policy, is_python)})


def find_boundary_refs(
    text: str, policy: SchemaPolicy, is_python: bool = False
) -> list[BoundaryRef]:
    """Names in relation position outside the boundary, in file order.

    Denied names are left to :func:`find_denied_refs`: they always fail and are
    found in every position, not only relation position. Python contributes the
    SQL-looking literals the deny scan reads, minus Streamlit element text.
    """
    if not policy.boundary_schemas:
        return []
    out: dict[tuple[int, str], BoundaryRef] = {}
    for base_line, sql in _sql_chunks(text, is_python, skip_prose=True):
        for line, parts, kind in _relation_names(sql):
            if not parts:
                continue
            verdict = _verdict(policy, parts, kind)
            if verdict not in BOUNDARY_VERDICTS:
                continue
            at = base_line + line - 1
            ref = display_name(parts)
            database, schema = _db_schema(parts, kind)
            detail = _BOUNDARY_DETAIL[verdict].format(ref=ref)
            out.setdefault((at, ref), BoundaryRef(at, ref, verdict, database, schema, detail))
    return sorted(out.values())


# Directory names that never hold reviewable source. Matched by NAME, never by
# scanning the absolute path's components: filtering on absolute parts meant a
# checkout living under ANY dotted directory (a git worktree at
# `.claude/worktrees/<name>/` is the common case, and this project's own
# guidance recommends exactly that) made every file look hidden, so the scan
# silently examined nothing and reported OK. A gate that passes because it
# looked at zero files is worse than no gate. Name-matching cannot be poisoned
# by where the repo lives, and errs toward scanning MORE rather than less.
_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        ".review",
        ".ruff_cache",
        ".pytest_cache",
        ".mypy_cache",
        "__pycache__",
        "node_modules",
    }
)


def _in_ignored_dir(path: Path, root: Path | None = None) -> bool:
    """True if *path* sits under a directory that never holds reviewable source.

    Only components BELOW the scan root are considered. Filtering on the
    ABSOLUTE path's components made a checkout under any matching directory
    look entirely hidden, so the scan examined nothing and reported OK, a gate
    that passes because it looked at zero files is worse than no gate.

    With no root, the current working directory is used (pre-commit and CI both
    invoke from the repo root). If the path is not under either, NOTHING is
    filtered: erring toward scanning more is the only safe direction here, and
    a repo that merely LIVES under a path segment named `venv` must still be
    scanned in full.
    """
    for base in (root, Path.cwd()):
        if base is None:
            continue
        try:
            return any(p in _IGNORED_DIR_NAMES for p in path.relative_to(base).parts)
        except ValueError:
            continue
    # Not locatable under a root: fall back to matching NAMES anywhere in the
    # path. That still skips a `.review/` artifact handed over as an absolute
    # path outside the tree, and is safe for the case that caused this bug -
    # `.claude/worktrees/<name>/` contains no ignored NAME. The residual gap is
    # a repo living directly under a directory literally called `venv` or
    # `node_modules`; callers that know their root pass it and avoid even that.
    return any(p in _IGNORED_DIR_NAMES for p in path.parts)


def _has_dotted_dir(path: Path, root: Path | None = None) -> bool:
    """True if *path* sits under a directory that never holds reviewable source.

    Skips review artifacts (``.review/``), VCS metadata (``.git/``), virtualenvs
    (``.venv/``) and caches. The file's own name is excluded (a leading-dot
    filename like ``.foo.py`` is still scanned).

    Matched by directory NAME rather than "any dotted component of the absolute
    path", which silently skipped EVERY file whenever the checkout lived under a
    dotted directory - e.g. a git worktree at ``.claude/worktrees/<name>/`` - and
    turned this governance gate into a no-op that reported clean.
    """
    return _in_ignored_dir(path.parent, root)


def _result(findings: list[dict], warnings: list[dict], policy: SchemaPolicy) -> dict:
    return {
        "ok": not findings,
        "findings": findings,
        "warnings": warnings,
        "denylist": list(policy.schema_deny),
        "boundary": policy.boundary,
    }


def check_paths(paths: list[Path], policy: SchemaPolicy, root: Path | None = None) -> dict:
    findings: list[dict] = []
    warnings: list[dict] = []
    for p in paths:
        if p.suffix not in (".py", ".sql") or not p.is_file():
            continue
        if _has_dotted_dir(p, root):
            continue  # dotted dir (.review/, .git/, ...): not real app code
        text = p.read_text(errors="ignore", encoding="utf-8")
        is_py = p.suffix == ".py"
        for line_no, database, schema in _deny_hits(text, policy, is_py):
            findings.append(
                {
                    "file": str(p),
                    "line": line_no,
                    "schema": schema,
                    "database": database,
                    "reason": DENIED,
                }
            )
        for ref in find_boundary_refs(text, policy, is_python=is_py):
            entry = {
                "file": str(p),
                "line": ref.line,
                "schema": ref.schema,
                "database": ref.database,
                "reason": ref.verdict,
                "ref": ref.ref,
                "detail": ref.detail,
            }
            (findings if policy.enforcing else warnings).append(entry)
    return _result(findings, warnings, policy)


def _describe(f: dict) -> str:
    where = f"{f['file']}:{f['line']}"
    if f["reason"] == DENIED:
        return f"{where} references denied schema {f['schema']!r}"
    return f"{where} {f['detail']}"


def _iter_files(root: Path) -> list[Path]:
    """Walk *root* for ``.py``/``.sql`` files, skipping dotted directories."""
    if root.is_file():
        return [root]
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.suffix not in (".py", ".sql") or not p.is_file():
            continue
        if _has_dotted_dir(p, root):
            continue
        out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Block denied Snowflake schema references in app code."
    )
    ap.add_argument("paths", nargs="*", help="Files or directories to scan.")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--config", help="Path to streamsnow.config.yaml (default: discover).")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(Path(args.config) if args.config else None)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    policy = SchemaPolicy.from_governance(cfg.governance)
    # Per-argument root: see the note in check_app_security.main.
    findings: list[dict] = []
    warnings: list[dict] = []
    for raw in args.paths or ["."]:
        target = Path(raw)
        root = target if target.is_dir() else None
        res = check_paths(_iter_files(target), policy, root)
        findings.extend(res["findings"])
        warnings.extend(res["warnings"])
    # Keep check_paths' full output contract (a clean run once tracebacked on a
    # missing `denylist` key): JSON consumers read every key.
    result = _result(findings, warnings, policy)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        for f in result["findings"]:
            print(f"BLOCK {_describe(f)}")
        for w in result["warnings"]:
            print(f"WARN {_describe(w)}")
        if result["ok"]:
            note = f", {len(warnings)} warning(s)" if warnings else ""
            print(
                f"schema-refs: clean (denylist: {', '.join(result['denylist']) or 'none'}; "
                f"boundary: {result['boundary']}{note})"
            )
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
