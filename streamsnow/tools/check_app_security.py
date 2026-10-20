"""Block egress, code-execution, and write/dynamic SQL in app code.

Streamlit-in-Snowflake apps are read-only and sandboxed. This check fails a PR
that introduces network egress, arbitrary code execution, data-mutating SQL, or
string-built (injectable) SQL. AST-based for Python; statement-aware text scan
for ``.sql``.

This is a faithful port of the battle-tested ``tools/check_app_security.py`` in
the source monorepo. The detection rules here are universal Python/SQL security
primitives (egress modules, exec calls, write verbs) rather than org-specific
schema governance, so they live as module constants — schema allow/deny still
flows through ``streamsnow.policy.SchemaPolicy`` in ``check_schema_refs``.

Detection
=========

``.py`` (AST-based):

- **egress** — ``import``/``from`` of a module in :data:`EGRESS_MODULES`, matched
  at submodule granularity (``urllib.request`` flagged, ``urllib.parse`` not).
- **code-exec** — a ``Call`` to a dangerous primitive (bare ``eval``/``exec``/…,
  or a dotted call like ``os.system``/``subprocess.run``/``pickle.loads``).
- **dynamic-sql** — a non-constant SQL expression (f-string, ``%``/``+``
  ``BinOp``, or ``.format(...)`` call) **passed as the first argument** to a
  ``.sql(...)`` / ``.query(...)`` call. A bare ``Name`` argument (e.g.
  ``session.sql(sql)`` where ``sql = render_sql(...)``) is **not** flagged —
  inline string-building at the call site is the risk, not a pre-built query
  variable or a token-builder helper that returns a fragment. An f-string passed
  to ``st.caption()`` / ``help=`` / ``st.markdown(...)`` is never flagged.
- **token-sql** (warning, not yet a finding): a ``render_sql(...)`` keyword
  argument built by f-string, ``%``, ``+`` or ``.format()`` from a value the
  check cannot prove constant, either inline or in the local variable passed
  (``REGION_FILTER=f"AND region = '{region}'"``). ``render_sql`` pastes tokens
  in with ``str.replace``, so that is the injection dynamic-sql blocks, one
  variable removed, and the deployed app runs it with the owning role's grants
  (docs/snowflake-docs.md D13). A widget's options do not bound the value. The
  safe shape branches on the value to pick a constant fragment and binds the
  value itself (``"" if region == "All" else "AND region = :3"``). A name counts
  as constant when every assignment to it in scope is; a parameter or a helper's
  return value is not traced, so the check stays quiet rather than guess.
  Reported under ``warnings`` (exit 0) so repos that passed before keep passing;
  it moves to ``findings`` in a later release.
- **write-sql** — a constant SQL string passed to ``.sql()`` / ``.query()``
  whose first statement keyword is a write/DDL/DML verb.

``.sql`` (text, statement-aware):

- **write-sql** — after stripping ``--`` line comments, ``/* */`` block comments,
  and quoted string literals, the file is split on ``;`` and each statement's
  leading keyword is checked against :data:`WRITE_KEYWORDS`. String literals are
  masked first, so ``WHERE status = 'DELETED'`` never trips the guard, and a
  write verb is only flagged when it is statement-initial. Maintained DDL directly
  in an app's ``sql_review/app_specific_reporting_objects/`` may use ``CREATE``,
  ``ALTER`` and ``GRANT``; a file whose DB.SCHEMA is ``governance.app_data`` is run
  by the deploy job (``streamsnow objects-sql``) and may use only ``CREATE`` and
  ``GRANT`` (see :func:`_is_maintained_ddl` and :func:`_ddl_verbs`).

Waivers
=======

- ``# noqa: dynamic-sql`` on (or spanning) the offending line suppresses that
  one kind (a provably server-controlled metadata command). ``# noqa: token-sql``
  on the call or on the line that builds the fragment does the same for a token
  built only from bind placeholders (an ``IN (:3, :4)`` list). Egress, code-exec
  and write-sql are never waivable, since a self-service waiver there would be a
  security bypass.
- The narrow Snowflake Cortex Analyst REST exception: a bare ``import requests``
  carrying a trailing ``# snowflake-cortex-rest`` comment is **not** flagged as
  egress (the container-runtime Cortex Analyst client). General ``requests``
  imports remain banned.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

from ..config import ConfigError, find_config, load_config
from ..policy import split_name
from .sql_review import OBJECTS_DIR

# --------------------------------------------------------------------------- #
# Denylists (universal Python/SQL security primitives — not org schema policy) #
# --------------------------------------------------------------------------- #

# Network / exfiltration modules. A Snowflake dashboard reads from Snowflake and
# renders in the browser — it has no business opening outbound sockets. Matched
# against the imported module name and its dotted prefixes, so ``boto3.session``
# matches ``boto3``. ``urllib`` and ``http`` are matched at submodule granularity
# so the harmless ``urllib.parse`` / ``http`` (HTTPStatus) helpers don't
# false-positive — only the request-issuing submodules are listed.
EGRESS_MODULES: frozenset[str] = frozenset(
    {
        "requests",
        "httpx",
        "aiohttp",
        "urllib.request",
        "urllib.error",
        "http.client",
        "socket",
        "ssl",
        "smtplib",
        "ftplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "nntplib",
        "xmlrpc",
        "boto3",
        "botocore",
        "paramiko",
        "pycurl",
        "websocket",
        "websockets",
    }
)

# Bare-name calls (``ast.Name``) that execute code or import dynamically.
EXEC_BARE_NAMES: frozenset[str] = frozenset({"eval", "exec", "execfile", "compile", "__import__"})

# Dotted attribute calls, keyed by the fully-qualified dotted name of the call
# target. ``os`` is restricted to the process/command entrypoints so ordinary
# ``os.environ`` / ``os.path`` usage is untouched.
EXEC_DOTTED: frozenset[str] = frozenset(
    {
        "os.system",
        "os.popen",
        "os.execl",
        "os.execle",
        "os.execlp",
        "os.execlpe",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.execvpe",
        "os.spawnl",
        "os.spawnle",
        "os.spawnlp",
        "os.spawnlpe",
        "os.spawnv",
        "os.spawnve",
        "os.spawnvp",
        "os.spawnvpe",
        "subprocess.run",
        "subprocess.call",
        "subprocess.Popen",
        "subprocess.check_output",
        "subprocess.check_call",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "pickle.load",
        "pickle.loads",
        "marshal.load",
        "marshal.loads",
        "pty.spawn",
    }
)
# Root modules where *any* attribute call is dangerous.
EXEC_DOTTED_ROOTS: frozenset[str] = frozenset({"subprocess"})

# Write / DDL / DML statement keywords. Apps must be read-only
# (SELECT / WITH / SHOW / DESC[RIBE] / EXPLAIN only). A write verb is only a
# finding when it is the leading keyword of a statement.
WRITE_KEYWORDS: frozenset[str] = frozenset(
    {
        "DROP",
        "DELETE",
        "TRUNCATE",
        "INSERT",
        "UPDATE",
        "MERGE",
        "ALTER",
        "CREATE",
        "GRANT",
        "REVOKE",
        "CALL",
        "UPSERT",
        "REPLACE",
    }
)

# Method names whose first argument is raw SQL text.
SQL_METHODS: frozenset[str] = frozenset({"sql", "query"})

# Functions that paste their keyword arguments into SQL text unescaped.
TOKEN_SQL_FUNCS: frozenset[str] = frozenset({"render_sql"})

# Kinds reported under ``warnings`` instead of ``findings``: a new rule ships
# warn-only first so a repo that passed before keeps passing (docs/versioning.md).
WARN_ONLY_KINDS: frozenset[str] = frozenset({"token-sql"})

# Narrow waiver for the Snowflake-internal Cortex Analyst REST endpoint used by
# container-runtime Streamlit apps. General ``requests`` imports remain banned.
SNOWFLAKE_CORTEX_REST_WAIVER = "snowflake-cortex-rest"
SNOWFLAKE_CORTEX_ANALYST_ENDPOINT = "/api/v2/cortex/analyst/message"

_LEADING_KEYWORD_RE = re.compile(r"^\s*([A-Za-z_]+)")


# --------------------------------------------------------------------------- #
# Waiver handling                                                              #
# --------------------------------------------------------------------------- #


def _line_has_token(lines: list[str], start: int, end: int, token: str) -> bool:
    """True if any 1-based source line in ``[start, end]`` carries ``token``."""
    return any(1 <= ln <= len(lines) and token in lines[ln - 1] for ln in range(start, end + 1))


def _is_waived(lines: list[str], start: int, end: int, kind: str) -> bool:
    """True if a ``# noqa: <kind>`` waiver appears on any line in ``[start, end]``."""
    return _line_has_token(lines, start, end, f"noqa: {kind}")


# --------------------------------------------------------------------------- #
# SQL statement analysis (shared by .sql files and inline .py constants)       #
# --------------------------------------------------------------------------- #


def _strip_sql_noise(text: str) -> str:
    """Remove ``--`` line comments, ``/* */`` block comments, and string literals.

    String literals are dropped so that a write keyword appearing *inside* a
    literal (``WHERE note = 'please update'``) can never be mistaken for a
    statement-initial verb. Newlines are preserved so line numbers stay accurate,
    and ``;`` statement separators are preserved.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_line_comment = False
    in_block_comment = False
    quote: str | None = None
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            if ch == "\n":
                out.append(ch)
            i += 1
            continue
        if quote is not None:
            # Inside a string literal; consume until the matching quote. Doubled
            # quotes ('') are an escaped quote in SQL — skip both.
            if ch == quote:
                if nxt == quote:
                    i += 2
                    continue
                quote = None
            elif ch == "\n":
                out.append(ch)
            i += 1
            continue
        # Not in any comment or literal.
        if ch == "-" and nxt == "-":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _leading_write_keyword(statement: str) -> str | None:
    """Return the write keyword that *starts* ``statement``, or ``None``."""
    m = _LEADING_KEYWORD_RE.match(statement)
    if not m:
        return None
    kw = m.group(1).upper()
    return kw if kw in WRITE_KEYWORDS else None


def _split_keep_offsets(text: str, sep: str) -> list[tuple[str, int]]:
    """Split ``text`` on ``sep``, returning ``(segment, start_offset)`` pairs."""
    segments: list[tuple[str, int]] = []
    start = 0
    for idx, ch in enumerate(text):
        if ch == sep:
            segments.append((text[start:idx], start))
            start = idx + 1
    segments.append((text[start:], start))
    return segments


def _scan_sql_text(text: str) -> list[tuple[int, str]]:
    """Return ``(lineno, keyword)`` for each statement-initial write verb.

    ``lineno`` is the 1-based line where the offending statement begins, computed
    against the noise-stripped text (which preserves newlines) so messages point
    at real source lines.
    """
    cleaned = _strip_sql_noise(text)
    hits: list[tuple[int, str]] = []
    for statement, start in _split_keep_offsets(cleaned, ";"):
        kw = _leading_write_keyword(statement)
        if kw is not None:
            lead = len(statement) - len(statement.lstrip())
            lineno = cleaned.count("\n", 0, start + lead) + 1
            hits.append((lineno, kw))
    return hits


# --------------------------------------------------------------------------- #
# Python AST scanning                                                          #
# --------------------------------------------------------------------------- #


def _dotted_name(node: ast.AST) -> str | None:
    """Reconstruct a dotted name from an ``ast.Attribute``/``ast.Name`` chain."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return ".".join(parts)
    return None


def _module_is_egress(module: str) -> bool:
    """True if ``module`` (a dotted import path) matches the egress denylist."""
    if module in EGRESS_MODULES:
        return True
    # Match dotted prefixes: ``boto3.session`` → ``boto3``.
    parts = module.split(".")
    for i in range(1, len(parts)):
        if ".".join(parts[:i]) in EGRESS_MODULES:
            return True
    # Bare top-level (``import requests`` → "requests"); only when undotted, so
    # ``import urllib.parse`` does not match the bare ``urllib`` that isn't even
    # in the denylist.
    return parts[0] in EGRESS_MODULES and "." not in module


def _scan_imports(tree: ast.AST, lines: list[str]) -> list[dict]:
    findings: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        end = getattr(node, "end_lineno", None) or node.lineno
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _module_is_egress(alias.name):
                    continue
                # Narrow Snowflake Cortex Analyst REST exception: a bare
                # ``import requests  # snowflake-cortex-rest`` is sanctioned.
                if (
                    alias.name == "requests"
                    and alias.asname is None
                    and _line_has_token(lines, node.lineno, end, SNOWFLAKE_CORTEX_REST_WAIVER)
                ):
                    continue
                findings.append(
                    {
                        "file": "",
                        "line": node.lineno,
                        "kind": "egress",
                        "detail": alias.name,
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod and _module_is_egress(mod):
                findings.append(
                    {
                        "file": "",
                        "line": node.lineno,
                        "kind": "egress",
                        "detail": mod,
                    }
                )
    return findings


def _scan_exec_calls(tree: ast.AST) -> list[dict]:
    findings: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        detail: str | None = None
        if isinstance(func, ast.Name) and func.id in EXEC_BARE_NAMES:
            detail = func.id
        elif isinstance(func, ast.Attribute):
            dotted = _dotted_name(func)
            if dotted is not None:
                root = dotted.split(".")[0]
                if dotted in EXEC_DOTTED or root in EXEC_DOTTED_ROOTS:
                    detail = dotted
        if detail is None:
            continue
        findings.append({"file": "", "line": node.lineno, "kind": "code-exec", "detail": detail})
    return findings


def _is_dynamic_sql_expr(arg: ast.expr) -> bool:
    """True if ``arg`` builds a string inline (f-string, %/+ BinOp, .format)."""
    if isinstance(arg, ast.JoinedStr):  # f-string
        return True
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Mod, ast.Add)):
        return True
    # ``"...".format(...)`` — a Call whose func is an attribute named "format".
    return (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "format"
    )


def _scan_sql_calls(tree: ast.AST, lines: list[str]) -> list[dict]:
    """Flag dynamic / write SQL **only** at the first arg of a .sql()/.query() call.

    The destination matters: an f-string passed to ``st.caption()`` or used as a
    ``render_sql`` token builder is harmless. Only string-building at the SQL
    call site is a finding. A bare ``Name`` arg (``sess.sql(sql)``) is allowed.
    """
    findings: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in SQL_METHODS):
            continue
        if not node.args:
            continue
        sql_arg = node.args[0]
        if _is_dynamic_sql_expr(sql_arg):
            start = getattr(sql_arg, "lineno", node.lineno)
            end = getattr(sql_arg, "end_lineno", None) or start
            if _is_waived(lines, node.lineno, end, "dynamic-sql"):
                continue
            findings.append(
                {
                    "file": "",
                    "line": start,
                    "kind": "dynamic-sql",
                    "detail": f".{func.attr}(<interpolated SQL>)",
                }
            )
        elif isinstance(sql_arg, ast.Constant) and isinstance(sql_arg.value, str):
            for _, kw in _scan_sql_text(sql_arg.value):
                findings.append(
                    {
                        "file": "",
                        "line": sql_arg.lineno,
                        "kind": "write-sql",
                        "detail": f".{func.attr}(...) statement begins with {kw}",
                    }
                )
    return findings


class _Scope:
    """One function (or the module): what each local name is assigned, and its parameters."""

    def __init__(self, parent: _Scope | None = None, params: frozenset[str] = frozenset()):
        self.parent = parent
        self.params = params
        self.assigns: dict[str, list[ast.expr]] = {}

    def lookup(self, name: str) -> tuple[_Scope, list[ast.expr]] | None:
        """The scope binding ``name`` and its assigned values, or ``None`` when untraceable.

        A parameter is untraceable: its value comes from the caller.
        """
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.params:
                return None
            if name in scope.assigns:
                return scope, scope.assigns[name]
            scope = scope.parent
        return None


def _params(args: ast.arguments) -> frozenset[str]:
    names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
    names += [a.arg for a in (args.vararg, args.kwarg) if a is not None]
    return frozenset(names)


def _is_token_sql_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in TOKEN_SQL_FUNCS


def _collect_scopes(node: ast.AST, scope: _Scope, calls: list[tuple[ast.Call, _Scope]]) -> None:
    """Record assignments per scope and every ``render_sql(...)`` call with its scope."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            _collect_scopes(child, _Scope(scope, _params(child.args)), calls)
            continue
        if isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Name):
                    scope.assigns.setdefault(target.id, []).append(child.value)
        elif isinstance(child, (ast.AnnAssign, ast.NamedExpr)) and child.value is not None:
            if isinstance(child.target, ast.Name):
                scope.assigns.setdefault(child.target.id, []).append(child.value)
        elif isinstance(child, ast.AugAssign) and isinstance(child.target, ast.Name):
            # ``x += piece`` is ``x = <earlier x> + piece``; the earlier values are
            # already recorded, so the piece is checked as one more ``+`` operand.
            built = ast.BinOp(left=ast.Constant(""), op=ast.Add(), right=child.value)
            scope.assigns.setdefault(child.target.id, []).append(ast.copy_location(built, child))
        elif _is_token_sql_call(child):
            calls.append((child, scope))
        _collect_scopes(child, scope, calls)


def _is_constant(expr: ast.expr, scope: _Scope, seen: frozenset[tuple[int, str]]) -> bool:
    """True when ``expr`` can only ever evaluate to text written in the code."""
    if isinstance(expr, ast.Constant):
        return True
    if isinstance(expr, ast.Name):
        key = (id(scope), expr.id)
        if key in seen:
            return True  # a self-reference adds nothing the other assignments don't
        found = scope.lookup(expr.id)
        if found is None:
            return False
        owner, values = found
        return all(_is_constant(v, owner, seen | {key}) for v in values)
    if isinstance(expr, ast.JoinedStr):
        return all(
            _is_constant(v.value, scope, seen)
            for v in expr.values
            if isinstance(v, ast.FormattedValue)
        )
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Mod)):
        return _is_constant(expr.left, scope, seen) and _is_constant(expr.right, scope, seen)
    if isinstance(expr, (ast.Tuple, ast.List)):
        return all(_is_constant(e, scope, seen) for e in expr.elts)
    if isinstance(expr, ast.IfExp):
        return _is_constant(expr.body, scope, seen) and _is_constant(expr.orelse, scope, seen)
    if isinstance(expr, ast.BoolOp):
        return all(_is_constant(v, scope, seen) for v in expr.values)
    func = expr.func if isinstance(expr, ast.Call) else None
    if isinstance(expr, ast.Call) and isinstance(func, ast.Attribute) and func.attr == "format":
        parts = [func.value, *expr.args, *(k.value for k in expr.keywords)]
        return all(_is_constant(p, scope, seen) for p in parts)
    return False


def _nonconstant_build(
    expr: ast.expr, scope: _Scope, seen: frozenset[tuple[int, str]] = frozenset()
) -> ast.expr | None:
    """The string-building expression behind ``expr`` that uses a non-constant, if any.

    Follows a name through its assignments in scope and both arms of a
    conditional, since ``REGION_FILTER=region_filter`` with ``region_filter``
    built two lines up is the same token. Anything else it cannot see through
    (a parameter, a helper's return, a dict lookup of constant fragments) is
    not reported.
    """
    if _is_dynamic_sql_expr(expr):
        return None if _is_constant(expr, scope, seen) else expr
    if isinstance(expr, ast.Name):
        key = (id(scope), expr.id)
        found = None if key in seen else scope.lookup(expr.id)
        if found is None:
            return None
        owner, values = found
        for value in values:
            hit = _nonconstant_build(value, owner, seen | {key})
            if hit is not None:
                return hit
        return None
    if isinstance(expr, ast.IfExp):
        return _nonconstant_build(expr.body, scope, seen) or _nonconstant_build(
            expr.orelse, scope, seen
        )
    if isinstance(expr, ast.BoolOp):
        for value in expr.values:
            hit = _nonconstant_build(value, scope, seen)
            if hit is not None:
                return hit
    return None


def _scan_token_sql(tree: ast.AST, lines: list[str]) -> list[dict]:
    """Flag ``render_sql`` tokens built from a value the check cannot prove constant."""
    calls: list[tuple[ast.Call, _Scope]] = []
    _collect_scopes(tree, _Scope(), calls)
    findings: list[dict] = []
    for call, scope in calls:
        for kw in call.keywords:
            if kw.arg is None:  # **tokens: the dict's contents are not visible here
                continue
            hit = _nonconstant_build(kw.value, scope)
            if hit is None:
                continue
            call_end = getattr(call, "end_lineno", None) or call.lineno
            hit_end = getattr(hit, "end_lineno", None) or hit.lineno
            if _is_waived(lines, call.lineno, call_end, "token-sql") or _is_waived(
                lines, hit.lineno, hit_end, "token-sql"
            ):
                continue
            where = "" if hit.lineno == kw.value.lineno else f" (built on line {hit.lineno})"
            findings.append(
                {
                    "file": "",
                    "line": kw.value.lineno,
                    "kind": "token-sql",
                    "detail": (
                        f"render_sql({kw.arg}=...) token is built from a non-constant value"
                        f"{where}: pick a constant fragment and bind the value in params"
                    ),
                }
            )
    return findings


def _literal_strings(tree: ast.AST) -> list[tuple[int, str]]:
    return [
        (getattr(n, "lineno", 1), n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _assigned_string(tree: ast.AST, name: str) -> str | None:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    return None


def _assigned_name_refs(tree: ast.AST, name: str) -> set[str]:
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            for child in ast.walk(node.value):
                if isinstance(child, ast.Name):
                    refs.add(child.id)
    return refs


def _has_cortex_rest_import_waiver(tree: ast.AST, lines: list[str]) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        if not _line_has_token(lines, node.lineno, end, SNOWFLAKE_CORTEX_REST_WAIVER):
            continue
        if any(a.name == "requests" and a.asname is None for a in node.names):
            return True
    return False


def _scan_cortex_rest(tree: ast.AST, lines: list[str]) -> list[dict]:
    """Validate the narrow Snowflake Cortex Analyst REST exception.

    A ``# snowflake-cortex-rest`` waiver on a ``requests`` import allows egress
    ONLY in the exact container-runtime shape (endpoint = the Cortex Analyst path,
    URL built from SNOWFLAKE_HOST + that endpoint, token read from
    /snowflake/session/token, and a single ``requests.post(CORTEX_ANALYST_URL)``).
    Any deviation — an external URL literal, a non-post call, a different target —
    is flagged, so the waiver can't be abused to exfiltrate data. Ported from the
    source monorepo's check_app_security.py.
    """
    if not _has_cortex_rest_import_waiver(tree, lines):
        return []

    out: list[dict] = []

    def add(line: int, detail: str) -> None:
        out.append({"file": "", "line": line, "kind": "snowflake-cortex-rest", "detail": detail})

    if _assigned_string(tree, "CORTEX_ANALYST_ENDPOINT") != SNOWFLAKE_CORTEX_ANALYST_ENDPOINT:
        add(1, "CORTEX_ANALYST_ENDPOINT must be /api/v2/cortex/analyst/message")
    if not {"SNOWFLAKE_HOST", "CORTEX_ANALYST_ENDPOINT"}.issubset(
        _assigned_name_refs(tree, "CORTEX_ANALYST_URL")
    ):
        add(1, "CORTEX_ANALYST_URL must be built from SNOWFLAKE_HOST + CORTEX_ANALYST_ENDPOINT")
    literals = _literal_strings(tree)
    if not any(v == "/snowflake/session/token" for _, v in literals):
        add(1, "session token must be read from /snowflake/session/token")
    for lineno, v in literals:
        if v.startswith(("http://", "https://")) and v != "https://":
            add(lineno, f"external URL literal is not allowed: {v}")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func)
        if dotted is None or not dotted.startswith("requests."):
            continue
        if dotted != "requests.post":
            add(node.lineno, f"{dotted}(...) is not allowed; use requests.post(...)")
            continue
        url_arg: ast.expr | None = node.args[0] if node.args else None
        for kw in node.keywords:
            if kw.arg == "url":
                url_arg = kw.value
                break
        if not (isinstance(url_arg, ast.Name) and url_arg.id == "CORTEX_ANALYST_URL"):
            add(node.lineno, "requests.post URL must be CORTEX_ANALYST_URL")
    return out


def _scan_python(path: Path) -> list[dict]:
    try:
        source = path.read_text(errors="ignore", encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [{"file": str(path), "line": exc.lineno or 0, "kind": "syntax", "detail": str(exc)}]
    lines = source.splitlines()
    findings: list[dict] = []
    findings.extend(_scan_imports(tree, lines))
    findings.extend(_scan_exec_calls(tree))
    findings.extend(_scan_sql_calls(tree, lines))
    findings.extend(_scan_token_sql(tree, lines))
    findings.extend(_scan_cortex_rest(tree, lines))
    for f in findings:
        f["file"] = str(path)
    return findings


def _scan_sql(path: Path, allowed: frozenset[str] = frozenset()) -> list[dict]:
    try:
        text = path.read_text(errors="ignore", encoding="utf-8")
    except OSError:
        return []
    return [
        {
            "file": str(path),
            "line": lineno,
            "kind": "write-sql",
            "detail": f"statement begins with {kw}",
        }
        for lineno, kw in _scan_sql_text(text)
        if kw not in allowed
    ]


# --------------------------------------------------------------------------- #
# Driver                                                                        #
# --------------------------------------------------------------------------- #


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
    look entirely hidden, so the scan examined nothing and reported OK — a gate
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


def _iter_files(root: Path) -> list[Path]:
    """Yield real ``.py``/``.sql`` app files under ``root``, skipping dotted dirs.

    Dotted directories (``.git/``, ``.review/``, ``.venv/``, ``__pycache__`` …)
    are never real app source — walking them produces noise (gitignored review
    artifacts) and false positives. Only honest app files are scanned.
    """
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.suffix not in (".py", ".sql") or not p.is_file():
            continue
        if _in_ignored_dir(p, root):
            continue
        out.append(p)
    return sorted(out)


# What a maintained DDL file may begin a statement with: defining the object and
# granting it to the app's role. Anything else (DELETE, DROP, TRUNCATE, ...) is
# still a write finding inside that folder.
_DDL_VERBS: frozenset[str] = frozenset({"CREATE", "ALTER", "GRANT"})

# An app-data file is not documentation: the deploy job runs it as the CI role
# (`streamsnow objects-sql`, #79). There a stray ALTER or DROP would run unattended,
# so only the CREATE that defines the object and its GRANTs may begin a statement.
_DEPLOYED_DDL_VERBS: frozenset[str] = frozenset({"CREATE", "GRANT"})


def _ddl_verbs(path: Path) -> frozenset[str]:
    """Write verbs allowed in ``path``: none for app code, :data:`_DDL_VERBS` for
    review-only DDL, :data:`_DEPLOYED_DDL_VERBS` for a file in app data. A missing
    or unreadable config means app data is unknown, so today's allowance applies
    (``streamsnow objects-sql`` refuses to run without a config anyway)."""
    if not _is_maintained_ddl(path):
        return frozenset()
    cfg_path = find_config(path.parent)
    try:
        app_data = load_config(cfg_path).governance.app_data if cfg_path else ""
    except ConfigError:
        app_data = ""
    parts = split_name(path.name[: -len(".sql")])
    if app_data and len(parts) == 3 and ".".join(parts[:2]) == app_data:
        return _DEPLOYED_DDL_VERBS
    return _DDL_VERBS


def _is_maintained_ddl(path: Path) -> bool:
    """True for a file directly in an app's ``sql_review/app_specific_reporting_objects/``.

    Those files exist to hold ``CREATE`` statements: the maintained definition of
    a view or table the app reads. Files whose DB.SCHEMA is app data are applied by
    the deploy job (``streamsnow objects-sql``) and allow only CREATE and GRANT;
    every other file there is review-only and a human applies it (``sql-review
    probe`` only reads it to compare against the live ``GET_DDL``). ``sql-review
    check`` validates each one (the ``objects`` kind). Scanning them as app code made
    every app that declared an object fail ``validate-app`` and the pre-commit hook on
    its own DDL.

    The scope is exactly what ``sql-review`` validates: a direct child of that
    folder in an app root, which is ``apps/<slug>/`` holding a ``snowflake.yml``.
    A look-alike folder deeper in the app (even beside a planted manifest), or a
    subfolder of it, is scanned like any other SQL. Inside the folder the allowed
    verbs are :data:`_DDL_VERBS`, or :data:`_DEPLOYED_DDL_VERBS` for a file in app
    data (see :func:`_ddl_verbs`).
    """
    folder = path.parent
    app_root = folder.parent.parent
    return (
        folder.name == OBJECTS_DIR
        and folder.parent.name == "sql_review"
        and app_root.parent.name == "apps"
        and (app_root / "snowflake.yml").is_file()
    )


def scan_paths(paths: list[Path], root: Path | None = None) -> dict:
    findings: list[dict] = []
    for p in paths:
        if not p.is_file():
            continue
        # Skip files under a junk dir even when passed explicitly. No root is
        # available here, so this matches directory NAMES (see _in_ignored_dir).
        if _in_ignored_dir(p, root):
            continue
        if p.suffix == ".py":
            findings.extend(_scan_python(p))
        elif p.suffix == ".sql":
            verbs = _ddl_verbs(p)
            found = _scan_sql(p, verbs)
            if verbs is _DEPLOYED_DDL_VERBS:
                for f in found:
                    f["detail"] += (
                        ": the deploy job runs this app-data file as the CI role, so it holds "
                        "only CREATE and GRANT"
                    )
            findings.extend(found)
    warnings = [f for f in findings if f["kind"] in WARN_ONLY_KINDS]
    findings = [f for f in findings if f["kind"] not in WARN_ONLY_KINDS]
    return {"ok": not findings, "findings": findings, "warnings": warnings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Block egress/code-exec/write-SQL/dynamic-SQL in app code."
    )
    ap.add_argument("paths", nargs="*", help="Files or directories to scan.")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    # Each argument carries its own root. A directory arg anchors the
    # junk-directory filter to itself, so a checkout living under a path
    # segment named `venv` is scanned in full instead of skipped wholesale;
    # explicitly-named files keep the name-based filter, which is what stops a
    # `.review/` artifact handed over by path from being scanned.
    findings: list[dict] = []
    warnings: list[dict] = []
    for raw in args.paths or ["apps"]:
        target = Path(raw)
        part = scan_paths(_iter_files(target), target) if target.is_dir() else scan_paths([target])
        findings.extend(part["findings"])
        warnings.extend(part["warnings"])
    result = {"ok": not findings, "findings": findings, "warnings": warnings}
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        for f in result["warnings"]:
            print(f"WARN {f['file']}:{f['line']} [{f['kind']}] {f['detail']}")
        for f in result["findings"]:
            print(f"BLOCK {f['file']}:{f['line']} [{f['kind']}] {f['detail']}")
        if result["ok"]:
            print("app-security: clean" + (f" ({len(warnings)} warning(s))" if warnings else ""))
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
