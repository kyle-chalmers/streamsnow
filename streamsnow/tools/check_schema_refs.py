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
  name, is reported. ``DB..OBJ`` is read as ``DB.PUBLIC.OBJ``, the way Snowflake resolves it
  (https://docs.snowflake.com/en/sql-reference/name-resolution). Python string
  concatenation and f-strings are folded into one statement first, with ``__expr__``
  for each interpolation, so a name is not cut off from the FROM before it. In Python
  the boundary scan reads only literals that are certainly SQL (a query-call argument,
  or one opening with SELECT, WITH, INSERT, UPDATE, DELETE, MERGE, CREATE or USE); the
  deny scan keeps reading every literal with a SQL keyword. Text handed to a Streamlit element (``st.caption``,
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

Only the **schema-position** segment is tested against the denylist: see the
deny-list bullet above. A denied name in the database or trailing-object
position (e.g. ``DB.BRIDGE``) is not flagged.

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
    PUBLIC_SCHEMA,
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
    # Drop -- line comments and /* */ block comments so commented refs don't trip. Block
    # comments become blanks of the same length, so line numbers and offsets still match.
    text = re.sub(r"/\*.*?\*/", lambda m: _blank(m.group(0)), text, flags=re.DOTALL)
    return "\n".join(line.split("--", 1)[0] for line in text.split("\n"))


# `USE ROLE <name>` and `USE SECONDARY ROLES ...` name roles, never schemas: a role
# called "STAGING.READ_ONLY" must not read as a reference to a STAGING schema.
_USE_ROLE = re.compile(
    r'(?i)(?:^|(?<=;))\s*(USE\s+(?:SECONDARY\s+ROLES|ROLE)\s+(?:"(?:[^"]|"")*"|[^\s;]+))'
)
_QUOTED_IDENT = re.compile(r'"(?:[^"]|"")*"')
# `DB..OBJ`: an empty schema part is PUBLIC (https://docs.snowflake.com/en/sql-reference/name-resolution)
_EMPTY_SCHEMA = re.compile(r"([A-Za-z0-9_$])\.\.(?=[A-Za-z_])")


def _without_role_statements(line: str) -> str:
    """*line* with every ``USE ROLE`` / ``USE SECONDARY ROLES`` statement blanked.

    Only a real statement counts: one at the start of the line or after a ``;``,
    and outside string literals and quoted identifiers. Matching the text blindly
    also blanked ``GET_DDL('TABLE', 'DB.RAW."USE ROLE X"')``, which hid a denied
    ``RAW`` behind a table name that merely contains the words.
    """
    masked = _mask_sql(line)
    quoted = [m.span() for m in _QUOTED_IDENT.finditer(masked)]
    out = line
    for m in _USE_ROLE.finditer(masked):
        start, end = m.span(1)
        if any(a < start < b for a, b in quoted):
            continue
        out = out[:start] + _blank(out[start:end]) + out[end:]
    return out


def _is_expr(part: str) -> bool:
    """True for the stand-in of an interpolated part (:data:`_EXPR`)."""
    return part.upper() == _EXPR.upper()


def _denied_in_line(
    line: str, policy: SchemaPolicy, read_exc: set[str]
) -> set[tuple[int, str, str]]:
    """``(column, DATABASE, schema)`` for each denied reference in one line (``""``: unknown;
    the column is where the match starts in the normalized line, close to the original).

    Quoted identifiers (``"BI"."BRIDGE"``) and whitespace around dots
    (``DB . BRIDGE . T``) are normalized first so they cannot slip past. Only
    the schema-position segment is tested: a 3-part ``DB.SCHEMA.OBJECT`` tests
    the middle one with its database (bare and qualified entries); a 2-part
    ``SCHEMA.OBJECT`` tests the first one against bare entries only. A trailing
    ``DB.BRIDGE`` is not a hit.
    """
    hits: set[tuple[int, str, str]] = set()
    norm = re.sub(r"\s*\.\s*", ".", _without_role_statements(line).replace('"', ""))
    norm = _EMPTY_SCHEMA.sub(rf"\1.{PUBLIC_SCHEMA}.", norm)
    for m in _DOTTED.finditer(norm):
        if m.group(0).upper() in read_exc:
            continue  # sanctioned exact-FQN read
        first, second, third = m.group(1), m.group(2), m.group(3)
        if third:
            if policy.is_denied(second, first):
                hits.add((m.start(), "" if _is_expr(first) else first.upper(), second))
        elif policy.is_denied(first):
            hits.add((m.start(), "", first))
    # USE SCHEMA <denied> / USE SCHEMA DB.<denied>: not a dotted object ref.
    for m in _USE.finditer(norm):
        database, schema = (m.group(1), m.group(2)) if m.group(2) else (None, m.group(1))
        if schema and policy.is_denied(schema, database):
            known = "" if not database or _is_expr(database) else database.upper()
            hits.add((m.start(), known, schema))
    return hits


def _scan_text(
    text: str, policy: SchemaPolicy, read_exc: set[str]
) -> set[tuple[int, int, str, str]]:
    """Line-by-line deny scan over already-comment-stripped *text*:
    ``(line, column, DATABASE, schema)``."""
    hits: set[tuple[int, int, str, str]] = set()
    for i, line in enumerate(text.split("\n"), start=1):
        for col, database, schema in _denied_in_line(line, policy, read_exc):
            hits.add((i, col, database, schema))
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
# ``text`` is not one of them: ``st.text`` is rare, while ``sqlalchemy.text`` wraps SQL.
_PROSE_CALL_NAMES = frozenset(
    {
        "markdown", "caption", "write", "title", "header", "subheader",
        "info", "warning", "error", "success", "toast", "metric",
        "expander", "selectbox", "radio", "tabs", "multiselect",
    }
)  # fmt: skip


def _collect_enclosed_string_ids(tree: ast.AST, names: frozenset[str]) -> set[int]:
    """``id()`` of every string literal anywhere inside the arguments of a call named
    *names*, so ``conn.execute(text("SELECT ..."))`` counts as an argument to ``execute``."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) not in names:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for inner in ast.walk(arg):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    ids.add(id(inner))
    return ids


#: Stands for an interpolated or non-literal part of a Python string expression. A
#: lone identifier, so ``FROM {t} a, X.Y.Z`` keeps its relation context, and a name
#: with an unknown database or schema part (``{db}.{schema}.T``) is recognizable.
_EXPR = "__expr__"
# A string that opens with a SQL statement keyword is SQL wherever it sits in Python.
_STATEMENT_START_RE = re.compile(
    r"(?i)\s*(SELECT|WITH|INSERT|UPDATE|DELETE|MERGE|CREATE|USE|EXPLAIN|SHOW|DESCRIBE|DESC|COPY)\b"
)


class _Chunk(NamedTuple):
    sql: str  # the text the scanners read: every line break is a "\n"
    statement: bool  # certainly SQL: a query-call argument or opens with a statement keyword
    starts: tuple[tuple[int, int], ...]  # (offset in sql, source line) per source piece
    full: bool  # the full boundary checks apply, two-part names included (see _sql_chunks)
    raw: str  # the text as written; same length as ``sql``, so offsets agree

    def line_at(self, offset: int) -> int:
        """The source line of the piece holding *offset*, plus the newlines inside it
        (counted in the text as written: a ``\\r`` escape is not a source line)."""
        offset = max(0, min(offset, len(self.raw)))
        i = bisect.bisect_right([o for o, _ in self.starts], offset) - 1
        start, line = self.starts[max(i, 0)]
        return line + self.raw.count("\n", start, offset)


def _normalize_breaks(text: str) -> str:
    """``\\r\\n`` and a lone ``\\r`` become line breaks, keeping the length. The comment
    stripper, the line scan and the offsets must all agree on what a line is: splitting
    on ``splitlines()`` in one and ``\\n`` in another gave an IndexError on ``"a\\rb"``."""
    return text.replace("\r\n", " \n").replace("\r", "\n")


_Folded = tuple[str, list[tuple[int, int]]]


def _is_add(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)


def _add_leaves(node: ast.AST) -> list[ast.AST]:
    """The operands of a ``+`` tree that are not themselves ``+``, left to right.
    Iterative: a source with thousands of ``+ ""`` terms must not hit the recursion limit."""
    leaves: list[ast.AST] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if _is_add(n):
            stack.append(n.right)
            stack.append(n.left)
        else:
            leaves.append(n)
    return leaves


def _joined_parts(node: ast.JoinedStr) -> list[tuple[ast.AST, str, int]]:
    """``(part, text, first line)`` for each part of an f-string, interpolations as
    :data:`_EXPR`. Before Python 3.12 (PEP 701) the parts carry the f-string's own
    position, so a part's line is the f-string's first line plus the newlines of the
    parts before it, the same on every supported version."""
    parts, line = [], node.lineno
    for v in node.values:
        piece = v.value if isinstance(v, ast.Constant) else f" {_EXPR} "
        parts.append((v, piece, line))
        line += piece.count("\n")
    return parts


def _part_lines(node: ast.AST) -> dict[int, int]:
    """``id()`` of each literal folded into *node* -> its first source line."""
    lines: dict[int, int] = {}
    for leaf in _add_leaves(node):
        if isinstance(leaf, ast.Constant):
            lines[id(leaf)] = leaf.lineno
        elif isinstance(leaf, ast.JoinedStr):
            for v, _piece, line in _joined_parts(leaf):
                if isinstance(v, ast.Constant):
                    lines[id(v)] = line
    return lines


def _fold_leaf(node: ast.AST) -> _Folded | None:
    if isinstance(node, ast.Constant):
        return (node.value, [(0, node.lineno)]) if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        # Before Python 3.12 (PEP 701) the parts of an f-string carry the f-string's own
        # position, so each part's line is the f-string's first line plus the newlines
        # of the parts before it, the same on every supported version.
        text, starts = "", []
        for _v, piece, line in _joined_parts(node):
            starts.append((len(text), line))
            text += piece
        return text, starts or [(0, node.lineno)]
    return None


def _fold(node: ast.AST, known_none: set[int] | None = None) -> _Folded | None:
    """The text of a Python string expression with where each piece starts in the
    source, or ``None`` when *node* is not one.

    ``"a" + "b"`` folds to one string, so ``"SELECT * FROM " + "DB.S.T"`` reads as
    the statement it builds. An f-string joins its literal parts around
    :data:`_EXPR`, and a non-literal operand of ``+`` next to a string becomes
    :data:`_EXPR` too. The text is exactly the concatenation, never padded: a name
    split across operands (``"SALES_" + "DB.PUBLIC.X"``) is the name Snowflake sees.
    The piece starts map a match back to its source line. The ``+`` tree is folded
    bottom-up with an explicit stack; *known_none* remembers the ``+`` nodes already
    found not to be strings, so a long chain of non-strings is walked once.
    """
    if not _is_add(node):
        return _fold_leaf(node)
    order: list[ast.AST] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if _is_add(n) and (known_none is None or id(n) not in known_none):
            order.append(n)
            stack.append(n.left)
            stack.append(n.right)
    done: dict[int, _Folded | None] = {}

    def value(n: ast.AST) -> _Folded | None:
        if not _is_add(n):
            return _fold_leaf(n)
        if id(n) in done:
            return done.pop(id(n))  # each result is used once: free it
        return None  # a node known not to be a string

    for n in reversed(order):
        left, right = value(n.left), value(n.right)
        if left is None and right is None:
            done[id(n)] = None
            if known_none is not None:
                known_none.add(id(n))
            continue
        left = left or (f" {_EXPR} ", [(0, n.left.lineno)])
        right = right or (f" {_EXPR} ", [(0, n.right.lineno)])
        shift = len(left[0])
        done[id(n)] = (left[0] + right[0], left[1] + [(o + shift, ln) for o, ln in right[1]])
    return done.get(id(node))


def _fold_string(node: ast.AST) -> str | None:
    """Just the folded text of :func:`_fold`."""
    folded = _fold(node)
    return None if folded is None else folded[0]


def _string_expressions(tree: ast.AST) -> list[tuple[ast.AST, str, list[tuple[int, int]]]]:
    """Each maximal string expression with its folded text, in source order.

    The pieces of a folded expression are not visited again; the expressions
    interpolated into an f-string, and the operands of ``+`` that are not strings, are,
    since they may hold strings of their own. Iterative, for deeply nested source.
    """
    found: list[tuple[ast.AST, str, list[tuple[int, int]]]] = []
    known_none: set[int] = set()
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        folded = _fold(node, known_none)
        if folded is None:
            stack.extend(reversed(list(ast.iter_child_nodes(node))))
            continue
        found.append((node, *folded))
        inner: list[ast.AST] = []
        for leaf in _add_leaves(node):
            if isinstance(leaf, ast.JoinedStr):
                inner.extend(v.value for v in leaf.values if isinstance(v, ast.FormattedValue))
            elif _fold_leaf(leaf) is None:
                inner.append(leaf)
        stack.extend(reversed(inner))
    return found


_LITERAL_RE = re.compile(
    r"""\"\"\"(?P<a>.*?)\"\"\"|\'\'\'(?P<b>.*?)\'\'\'|\"(?P<c>(?:[^"\\\n]|\\.)*)\"|\'(?P<d>(?:[^'\\\n]|\\.)*)\'""",
    re.S,
)


def _unparsed_chunks(text: str) -> list[_Chunk]:
    """Chunks for Python the parser could not read (too deep, a NUL byte): the whole text
    for the deny scan, plus every quoted literal that opens like a statement for the
    boundary scan, which needs the SQL apart from the Python around it."""
    chunks = [_Chunk(_normalize_breaks(text), False, ((0, 1),), False, text)]
    for m in _LITERAL_RE.finditer(text):
        body = next(g for g in m.group("a", "b", "c", "d") if g is not None)
        opener = _STATEMENT_START_RE.match(_strip_sql_comments(body))
        if opener is None:
            continue
        line = text.count("\n", 0, m.start("a" if m.group("a") is not None else m.lastgroup)) + 1
        shouted = opener.group(1).isupper() or opener.group(1).islower()
        chunks.append(_Chunk(_normalize_breaks(body), True, ((0, line),), shouted, body))
    return chunks


def _sql_chunks(text: str, is_python: bool, *, skip_prose: bool = False) -> list[_Chunk]:
    """The SQL pieces to scan: the whole text for ``.sql``; for ``.py``, the
    SQL-looking string expressions (query-call args, or containing a SQL keyword),
    never docstrings, and with ``skip_prose`` never the text of a Streamlit
    element. Unparseable Python yields nothing (see the module docs).

    ``statement`` marks a chunk that is certainly SQL (a query-call argument, or
    one that opens with a statement keyword). The deny scan reads every chunk; the
    boundary scan only these, because ``raise ValueError("could not read from
    settings.toml")`` has a FROM and is not a query."""
    if not is_python:
        return [_Chunk(_normalize_breaks(text), True, ((0, 1),), True, text)]
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        # A plain syntax error is left unread, as it always was; a NUL byte is not a
        # syntax problem of the file's own and must not make it look clean.
        return _unparsed_chunks(text) if "null bytes" in str(exc) else []
    except (RecursionError, ValueError, MemoryError):
        # The parser gave up (nesting too deep): read the text anyway, deny list and
        # boundary, rather than report a clean file nobody looked at.
        return _unparsed_chunks(text)
    docstrings = _collect_docstring_ids(tree)
    queries = _collect_enclosed_string_ids(tree, _QUERY_CALL_NAMES)
    prose = _prose_ids(tree) if skip_prose else set()
    chunks: list[_Chunk] = []

    def add(
        node: ast.AST,
        sql: str,
        starts: list,
        inherited: tuple[bool, bool] = (False, False),
        deny_only: bool = False,
    ) -> tuple[bool, bool]:
        """Add one chunk; returns ``(statement, full)`` so the parts of a folded whole
        belong to the statement it opens. ``deny_only`` keeps it out of the boundary
        scan: a piece already inside the folded whole, whose cut-off name
        (``FROM DB.S.`` before ``{t}``) is no name."""
        queried = any(id(inner) in queries for inner in ast.walk(node))
        if id(node) in docstrings or (id(node) in prose and not queried):
            return False, False
        opener = _STATEMENT_START_RE.match(_strip_sql_comments(sql))
        # "SELECT ..." and "select ..." are SQL; "Select a file from data.csv" is prose
        # that happens to start with the word, so it only gets three-part checks.
        shouted = opener is not None and (opener.group(1).isupper() or opener.group(1).islower())
        in_statement = queried or inherited[0] or opener is not None
        if in_statement or _SQL_KEYWORD_RE.search(sql):
            statement = not deny_only and in_statement
            full = not deny_only and (queried or inherited[1] or shouted)
            chunks.append(_Chunk(_normalize_breaks(sql), statement, tuple(starts), full, sql))
        return queried or opener is not None, queried or shouted

    for node, sql, starts in _string_expressions(tree):
        flags = add(node, sql, starts)
        if isinstance(node, ast.Constant):
            continue
        # The folded whole is an extra chunk: every literal inside it is still read on
        # its own, as before folding, and belongs to the statement the whole opens.
        folded = _folded_parts(node)
        part_lines = _part_lines(node)
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                line = part_lines.get(id(inner), inner.lineno)
                add(inner, inner.value, [(0, line)], flags, id(inner) in folded)
    return chunks


def _folded_parts(node: ast.AST) -> set[int]:
    """``id()`` of the literals whose text is already part of *node*'s folded string:
    f-string parts and operands of ``+``. Literals inside an interpolation are not."""
    ids: set[int] = set()
    for leaf in _add_leaves(node):
        if isinstance(leaf, ast.Constant):
            ids.add(id(leaf))
        elif isinstance(leaf, ast.JoinedStr):
            ids.update(id(v) for v in leaf.values if isinstance(v, ast.Constant))
    return ids


def _streamlit_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """``(module names, bare names)`` that mean Streamlit in this module.

    A name counts only when the module binds it from streamlit (``import streamlit
    [as X]``; ``from streamlit import a [as b]`` for bare names) and never binds it
    again: a plain assignment, a parameter, a ``def``/``class``, a ``for``/``with``
    target or another import of the same name takes it away. Without that,
    ``st = io.StringIO(); st.write("SELECT ... RAW.X")`` read as a caption. A module
    with no streamlit import has no prose roots at all."""
    modules: set[str] = set()
    bare: set[str] = set()
    other: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "streamlit":
                    modules.add(a.asname or a.name)
                else:
                    other.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                (bare if node.module == "streamlit" else other).add(a.asname or a.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            other.add(node.id)
        elif isinstance(node, ast.arg):
            other.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            other.add(node.name)
        elif isinstance(node, ast.ExceptHandler):
            other.add(node.name or "")
    return modules - other, bare - other


def _is_streamlit_call(func: ast.AST, modules: set[str], bare: set[str]) -> bool:
    """True for ``st.write``, ``st.sidebar.write``, ``st.expander(...).write`` and a bare
    name imported from streamlit; never ``buf.write`` or a bare ``write``."""
    if isinstance(func, ast.Name):
        return func.id in bare
    node = func
    while isinstance(node, (ast.Attribute, ast.Call, ast.Subscript)):
        node = node.value if not isinstance(node, ast.Call) else node.func
    return isinstance(node, ast.Name) and node.id in modules and isinstance(func, ast.Attribute)


def _prose_ids(tree: ast.AST) -> set[int]:
    """``id()`` of the string pieces of an argument to a Streamlit text element: a
    literal, or the operands of a ``+`` concatenation (``st.caption("Rows from " + "DB.S.T")``).
    Only Streamlit calls are prose: ``buf.write("SELECT ...")`` writes a file. A literal
    inside a nested call (``st.caption("n: " + str(run("SELECT ...")))``) is not a piece of
    the argument, so it is still scanned. An f-string argument keeps being scanned."""
    modules, bare = _streamlit_names(tree)
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) not in _PROSE_CALL_NAMES:
            continue
        if not _is_streamlit_call(node.func, modules, bare):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, (ast.Constant, ast.BinOp)) and _fold_string(arg) is not None:
                ids.add(id(arg))
                ids |= _folded_parts(arg)
    return ids


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
# Non-reserved clause words that are also plain identifiers: inside a JOIN's ON
# expression `offset = b.off` is a column, so they end the FROM list only outside one.
_SOFT_CLAUSE_END = frozenset({"LIMIT", "OFFSET", "FETCH"})
# Words after a relation that modify it (time travel, sampling, pivots); a
# parenthesized group follows, and the alias may come after that.
_RELATION_MODIFIERS = frozenset(
    {"AT", "BEFORE", "CHANGES", "SAMPLE", "TABLESAMPLE", "PIVOT", "UNPIVOT", "MATCH_RECOGNIZE"}
)


def _unresolved(parts: tuple[str, ...], kind: str) -> bool:
    """True when an interpolated part (:data:`_EXPR`) hides what the name says about the
    boundary: any part of a ``USE`` name, or the database or schema of an object name.
    ``DB.SCHEMA.{table}`` still says everything the boundary needs."""
    hidden = parts if kind != "object" else parts[:-1]
    return _EXPR.upper() in hidden


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
    merge: bool = False  # a MERGE statement is open at this depth
    delete: bool = False  # a DELETE statement is open at this depth
    using_ok: bool = False  # the next USING introduces a source relation (MERGE / DELETE)
    values: bool = False  # the current FROM item is a VALUES list: commas between rows
    in_on: bool = False  # inside a JOIN's ON / USING expression


def _relation_names(sql: str) -> list[tuple[int, tuple[str, ...], str]]:
    """``(offset, parts, kind)`` for each name in relation position (``offset``: where the
    name starts in *sql*).

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
    toks = [(m.lastgroup, m.group(0), m.start(), m.end()) for m in _TOKEN_RE.finditer(text)]

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
            stay = False
            if f.state == "expect":
                f.state, f.alias = "after", False  # a subquery stands where a relation would
                # ... unless the parenthesis groups a join: FROM (A a JOIN B b ON ...), where
                # the first relation follows the parenthesis directly.
                stay = not (peek(i)[0] == "name" and peek(i)[1].upper() in ("SELECT", "WITH"))
            frames.append(_Frame(state="expect" if stay else ""))
            prev = tok
            continue
        if tok == ")":
            if len(frames) > 1:
                frames.pop()
            prev = tok
            continue
        if tok == "," and f.from_clause and f.state in ("", "after"):
            if f.values and peek(i)[1] == "(":
                prev = tok  # a comma between the rows of a VALUES list, not a new relation
                continue
            f.state, f.values, f.in_on = "expect", False, False
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
                        found.append((inner[2], parts, "object"))
                f.state, f.alias = "after", False
                prev = tok
                continue
            if kind == "name" and up == "VALUES":
                f.state, f.alias, f.values = "after", False, True  # VALUES (...) v(x) is a relation
                prev = tok
                continue
            if kind == "name" and up not in _NOT_ALIAS:
                found.append((pos, split_name(tok), "object"))
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
            f.state, f.from_clause, f.in_on = "expect", True, False
            f.using_ok = f.delete  # DELETE FROM t USING source
        elif up == "JOIN":
            f.state, f.in_on = "expect", False  # the list stays open: a comma after ON continues it
        elif up in ("INTO", "UPDATE"):
            f.state, f.from_clause = "expect", False
            f.using_ok = f.merge and up == "INTO"  # MERGE INTO t USING source
        elif up == "MERGE":
            f.merge = True
        elif up in ("SELECT", "DELETE"):
            f.select, f.from_clause = True, False
            f.delete = f.delete or up == "DELETE"
        elif up == "USING" and f.using_ok:
            f.state, f.using_ok = "expect", False  # a later JOIN ... USING (cols) is not a source
        elif up in ("ON", "USING"):
            f.in_on = f.in_on or f.from_clause
        elif up in _CLAUSE_END and not (up in _SOFT_CLAUSE_END and f.in_on):
            f.from_clause = f.in_on = f.using_ok = False
        elif up == "USE":
            what, j = "database", i
            head = peek(i)[1].upper() if peek(i)[0] == "name" else ""
            if head in ("SCHEMA", "DATABASE"):
                what, j = head.lower(), i + 1
            elif head in ("ROLE", "WAREHOUSE", "SECONDARY"):
                what = ""
            if what and peek(j)[0] == "name":
                parts = split_name(peek(j)[1])
                found.append((peek(j)[2], parts, "schema" if len(parts) == 2 else what))
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
    for chunk in _sql_chunks(text, is_python, skip_prose=True):
        sql = chunk.sql
        line_starts = [0] + [m.end() for m in re.finditer("\n", sql)]
        for line, col, database, schema in _scan_text(_strip_sql_comments(sql), policy, read_exc):
            at = chunk.line_at(line_starts[min(line, len(line_starts)) - 1] + col)
            hits.setdefault((at, database.upper(), schema.upper()), (at, database, schema))
        for pos, parts, kind in _relation_names(sql):
            if len(parts) >= 4 and kind == "object":
                # classify ignores names this long (no relation has four parts), but
                # DENIED_DB.RAW.T.COL must not lose the coverage the line scan gave it.
                denied = policy.is_denied(parts[1], parts[0])
                database, schema = parts[0], parts[1]
            else:
                denied = bool(parts) and _verdict(policy, parts, kind) == DENIED
                database, schema = _db_schema(parts, kind) if parts else ("", "")
            database = "" if _is_expr(database) else database
            if denied:
                at = chunk.line_at(pos)
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
    for chunk in _sql_chunks(text, is_python, skip_prose=True):
        if not chunk.statement:
            continue
        sql = chunk.sql
        for pos, parts, kind in _relation_names(sql):
            if not parts or _unresolved(parts, kind):
                continue
            verdict = _verdict(policy, parts, kind)
            if verdict not in BOUNDARY_VERDICTS:
                continue
            if verdict == TWO_PART and not chunk.full:
                # Title-case prose that opens with a statement word ("Select a file from
                # data.csv"): a two-part name is too ambiguous to report there.
                continue
            at = chunk.line_at(pos)
            ref = display_name(parts).replace(_EXPR.upper(), "<expr>")
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
