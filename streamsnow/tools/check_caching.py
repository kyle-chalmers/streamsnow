"""Require @st.cache_data(ttl=...) on public data-load functions.

A public function that runs a *named* Snowflake query — one whose SQL comes from
a string literal or a ``load_sql(...)`` / ``render_sql(...)`` helper — and returns
the result must carry ``@st.cache_data`` with an explicit ``ttl`` keyword to
control warehouse spend (the cache is per-session). ``# noqa: cache-required`` on
the ``def`` line or on a fetch-call line opts out (real-time monitors, smoke
tests, Cortex-style ad-hoc executors).

To avoid false positives the check is deliberately conservative — it skips:

* **Private helpers** — any ``def`` whose name starts with ``_``.
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


def _is_named_query_fetch(call: ast.Call) -> bool:
    """True if the fetch loads a *named* query that should be cached.

    Named = a string literal, or the result of ``load_sql(...)`` / ``render_sql(...)``.
    A bare variable / parameter / other call result is a runtime-built statement
    (generic executor pattern) and is NOT flagged — the cache belongs on the caller.
    """
    if not call.args:
        # e.g. ``conn.query()`` with everything passed by keyword — uncommon;
        # treat as a named load so a genuinely uncached loader is still caught.
        return True
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return True
    if isinstance(first, ast.Call):
        builder = first.func
        name = builder.attr if isinstance(builder, ast.Attribute) else getattr(builder, "id", None)
        return name in _SQL_BUILDERS
    # ast.Name (variable / param), f-strings, concatenations → runtime executor.
    return False


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

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Skip private helpers and the query/sql primitive shims themselves.
        if node.name.startswith("_") or node.name in _FETCH_ATTRS:
            continue
        calls = _data_fetch_calls(node)
        if not calls:
            # Pure connection/session factory or a non-fetching function.
            continue
        # Only flag when at least one fetch loads a *named* query; a function
        # that only executes runtime-built SQL is a generic executor.
        if not any(_is_named_query_fetch(c) for c in calls):
            continue
        # noqa on the def line OR on any fetch-call line opts out.
        relevant_lines = {c.lineno for c in calls} | {node.lineno}
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
