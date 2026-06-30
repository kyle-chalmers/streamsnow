"""Require @st.cache_data(ttl=...) on public data-load functions.

A public function that runs a *named* Snowflake query — one whose SQL comes from
a string literal or a ``load_sql(...)`` / ``render_sql(...)`` helper — and returns
the result must carry ``@st.cache_data`` with an explicit ``ttl`` keyword to
control warehouse spend (the cache is per-session). ``# noqa: cache-required`` on
the ``def`` line or on a fetch-call line opts out (real-time monitors, smoke
tests, Cortex-style ad-hoc executors).

A *named* query is a string literal, a ``load_sql(...)`` / ``render_sql(...)``
result, or a local variable assigned from one of those (the canonical
``sql = load_sql("x"); conn.query(sql)`` idiom). A runtime-built statement (a
parameter, an f-string, ``sql = sanitize(x)``) is NOT named — it is a generic
executor whose cache belongs on whoever built the named query.

A public loader can run the fetch one of two ways and both must be caught:

* **Directly** — it calls ``.query(...)`` / ``.sql(...)`` itself with a named query.
* **By delegation** — it hands a named query to a *private fetch helper*
  (``_run_query(load_sql("x"))``). The helper is private (never flagged on its
  own), so without this rule the cache requirement would silently vanish into
  the gap between them. A private function is a fetch helper if it performs a
  ``.query`` / ``.sql`` directly OR forwards to another fetch helper (delegation
  chains are followed transitively), and any public caller that passes a named
  query into the chain is held to the same cache rule as a direct fetch. Only the
  SQL-bearing argument (first positional, or ``sql=`` / ``query=`` / ``statement=``)
  is inspected, so an unrelated string kwarg never counts as the query.

To avoid false positives the check is deliberately conservative — it skips:

* **Private helpers** — any ``def`` whose name starts with ``_`` is never flagged
  itself; the public caller carries the cache (see delegation, above).
* **Connection / session factories** — a function whose body only creates or
  returns a connection/session (``st.connection(...)``, ``get_active_session()``)
  is not a data fetch; only ``.query(...)`` / ``.sql(...)`` calls that return data
  count. A factory that never calls ``.query``/``.sql`` is never flagged.
* **Query primitives** — a function literally named ``query`` or ``sql`` (a
  connection-adapter shim that *defines* the fetch primitive rather than
  consuming a named query).
* **Generic SQL executors** — a fetch whose SQL argument is a runtime value
  (a bare variable, a function parameter, or any non-builder call result) rather
  than a named query. The cache belongs on the specific caller, not the executor.
  Delegation is held to the same bar: a public caller that hands a private fetch
  helper a *runtime* value (not a named query) is a generic executor, not flagged.

Delegation detection is per-file and matches bare-name calls to ``def``-based
private fetch helpers (the structure ``streamsnow init`` scaffolds). Cross-module
helpers, ``self._helper(...)`` method delegation, and lambda/partial-assigned
helpers are out of scope.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

# Attribute names that return query results when called on a connection/session.
_FETCH_ATTRS = {"query", "sql"}
# Helpers that load a *named* in-file SQL query (queries/<name>.sql).
_SQL_BUILDERS = {"load_sql", "render_sql"}
# Keyword-argument names that carry the SQL statement when a fetch / private fetch
# helper is called by keyword. Used so an unrelated string kwarg (e.g.
# ``query_tag="adhoc"``) is NOT mistaken for the query itself.
_SQL_PARAM_NAMES = {"sql", "query", "statement", "stmt"}


def _data_fetch_calls(fn: ast.AST) -> list[ast.Call]:
    """Return every ``.query(...)`` / ``.sql(...)`` call inside ``fn``.

    Connection/session factories (``st.connection(...)``, ``get_active_session()``)
    are intentionally NOT fetches — they create a handle but do not return data.
    """
    calls: list[ast.Call] = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _FETCH_ATTRS
        ):
            calls.append(node)
    return calls


def _is_builder_call(value: ast.AST) -> bool:
    """True if ``value`` is a ``load_sql(...)`` / ``render_sql(...)`` call."""
    if isinstance(value, ast.Call):
        builder = value.func
        name = builder.attr if isinstance(builder, ast.Attribute) else getattr(builder, "id", None)
        return name in _SQL_BUILDERS
    return False


def _iter_scope(scope: ast.AST):
    """Yield descendants of ``scope`` without crossing into a nested function,
    lambda, or class body — those introduce their own variable scope, so an
    assignment to ``sql`` inside a nested ``def`` must not affect the outer one.
    """
    for child in ast.iter_child_nodes(scope):
        yield child
        if not isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            yield from _iter_scope(child)


def _named_locals(fn: ast.AST) -> set[str]:
    """Local names assigned (within ``fn``'s own scope) from a named-query source.

    Catches the canonical loader idiom ``sql = load_sql("x"); conn.query(sql)`` —
    the SQL is named even though it reaches the fetch call through a variable. A
    name assigned from anything that is NOT a string literal / load_sql /
    render_sql is *poisoned* and excluded, so a runtime-built statement
    (``sql = sanitize(x)``) never counts as named. Nested function/class scopes
    are not descended into, so an inner rebinding of the same name can't poison
    the outer loader's local.
    """
    named: set[str] = set()
    poisoned: set[str] = set()
    for node in _iter_scope(fn):
        if isinstance(node, ast.Assign):
            value = node.value
            is_named = (
                isinstance(value, ast.Constant) and isinstance(value.value, str)
            ) or _is_builder_call(value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    (named if is_named else poisoned).add(target.id)
    return named - poisoned


def _arg_is_named(value: ast.AST, named_locals: set[str]) -> bool:
    """True if ``value`` is a named query: string literal, builder call, or named local."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return True
    if _is_builder_call(value):
        return True
    return isinstance(value, ast.Name) and value.id in named_locals


def _is_named_query_fetch(call: ast.Call, named_locals: set[str]) -> bool:
    """True if a direct ``.query()`` / ``.sql()`` loads a *named* query to cache.

    The SQL is the first positional argument. A bare variable / parameter / other
    call result is a runtime-built statement (generic executor) and is NOT flagged
    — unless it is a local assigned from a named-query source (see _named_locals).
    """
    if not call.args:
        # e.g. ``conn.query()`` with everything passed by keyword — uncommon;
        # treat as a named load so a genuinely uncached loader is still caught.
        return True
    return _arg_is_named(call.args[0], named_locals)


def _delegated_sql_is_named(call: ast.Call, named_locals: set[str]) -> bool:
    """True if the SQL handed to a private fetch helper is a *named* query.

    Only the SQL-bearing argument is inspected — the first positional arg or a
    ``sql=`` / ``query=`` / ``statement=`` keyword. An unrelated string keyword
    (``_run_query(stmt, query_tag="adhoc")``) is NOT the query, so a generic
    executor that merely tags its call is not mistaken for a named load.
    """
    candidates: list[ast.AST] = []
    if call.args:
        candidates.append(call.args[0])
    candidates.extend(kw.value for kw in call.keywords if kw.arg in _SQL_PARAM_NAMES)
    return any(_arg_is_named(c, named_locals) for c in candidates)


def _calls_any(fn: ast.AST, names: set[str]) -> bool:
    """True if ``fn`` contains a bare-name call to any name in ``names``."""
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in names
        for n in ast.walk(fn)
    )


def _private_fetch_helpers(tree: ast.AST) -> set[str]:
    """Names of private (``_``-prefixed) functions that perform a fetch.

    A private function is a fetch helper if it either calls ``.query()`` / ``.sql()``
    directly OR forwards to another private fetch helper. The transitive closure
    catches delegation chains (``_run_query`` -> ``_execute`` -> ``.query``) so a
    public caller handing a named query into the chain is still held to the cache
    rule. The cache belongs on that public caller, never on the helper.
    """
    private_fns: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("_"):
            private_fns.setdefault(node.name, node)
    helpers = {name for name, fn in private_fns.items() if _data_fetch_calls(fn)}
    changed = True
    while changed:
        changed = False
        for name, fn in private_fns.items():
            if name not in helpers and _calls_any(fn, helpers):
                helpers.add(name)
                changed = True
    return helpers


def _delegated_named_fetches(
    fn: ast.AST, helper_names: set[str], named_locals: set[str]
) -> list[ast.Call]:
    """Bare-name calls inside ``fn`` to a private fetch helper passing a named query."""
    if not helper_names:
        return []
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in helper_names
        and _delegated_sql_is_named(node, named_locals)
    ]


def _cache_decorator(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[bool, bool]:
    """Return (has_cache_data, has_ttl)."""
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if name == "cache_data":
            ttl = isinstance(dec, ast.Call) and any(k.arg == "ttl" for k in dec.keywords)
            return True, ttl
    return False, False


def scan_file(path: Path) -> list[dict]:
    findings: list[dict] = []
    try:
        text = path.read_text(errors="ignore")
        tree = ast.parse(text)
    except SyntaxError:
        return findings
    lines = text.splitlines()

    def _has_noqa(line_no: int) -> bool:
        return 0 < line_no <= len(lines) and "noqa: cache-required" in lines[line_no - 1]

    helper_names = _private_fetch_helpers(tree)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Skip private helpers and the query/sql primitive shims themselves.
        if node.name.startswith("_") or node.name in _FETCH_ATTRS:
            continue
        named_locals = _named_locals(node)
        direct = _data_fetch_calls(node)
        # A named fetch this function is responsible for caching: either a direct
        # .query()/.sql() of a named query, or a named query handed to a private
        # fetch helper. A function with neither is a factory, a generic executor,
        # or just doesn't fetch — not flagged.
        named_direct = [c for c in direct if _is_named_query_fetch(c, named_locals)]
        delegated = _delegated_named_fetches(node, helper_names, named_locals)
        if not named_direct and not delegated:
            continue
        # noqa on the def line OR on a line that actually drove the flag (a named
        # direct fetch or a named delegation) opts out. A noqa on an unrelated
        # runtime-executor line does NOT silence a real finding.
        relevant_lines = (
            {c.lineno for c in named_direct} | {c.lineno for c in delegated} | {node.lineno}
        )
        if any(_has_noqa(ln) for ln in relevant_lines):
            continue
        has_cache, has_ttl = _cache_decorator(node)
        if not has_cache:
            findings.append(
                {
                    "file": str(path),
                    "line": node.lineno,
                    "func": node.name,
                    "detail": "missing @st.cache_data(ttl=...)",
                }
            )
        elif not has_ttl:
            findings.append(
                {
                    "file": str(path),
                    "line": node.lineno,
                    "func": node.name,
                    "detail": "@st.cache_data without ttl=",
                }
            )
    return findings


def scan_paths(paths: list[Path]) -> dict:
    findings = []
    for p in paths:
        if p.suffix == ".py" and p.is_file():
            findings.extend(scan_file(p))
    return {"ok": not findings, "findings": findings}


def _iter_py_files(root: Path) -> list[Path]:
    """Walk ``root`` for ``.py`` files, skipping dotted dirs (.git, .review, …)."""
    if root.is_file():
        return [root]
    out: list[Path] = []
    for p in root.rglob("*.py"):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Require @st.cache_data(ttl=) on public data-load functions."
    )
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    files: list[Path] = []
    for raw in args.paths or ["apps"]:
        files.extend(_iter_py_files(Path(raw)))

    result = scan_paths(files)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print("caching: clean")
    else:
        for f in result["findings"]:
            print(f"FLAG {f['file']}:{f['line']} {f['func']}() — {f['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
