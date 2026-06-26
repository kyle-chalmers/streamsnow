"""Require @st.cache_data(ttl=...) on data-fetching functions.

Every function that runs a Snowflake query must be cached with an explicit TTL
to control warehouse spend (per-session cache). Heuristic + AST: a function
whose body issues a query (``.query(...)`` / ``.sql(...)`` / ``st.connection``)
must carry ``@st.cache_data`` with a ``ttl`` keyword. ``# noqa: cache-required``
on the def line opts out (e.g. real-time monitors, smoke tests).

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

_FETCH_ATTRS = {"query", "sql"}


def _is_fetch(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if isinstance(fn, ast.Attribute):
        if fn.attr in _FETCH_ATTRS:
            return True
        if fn.attr == "connection" and isinstance(fn.value, ast.Name) and fn.value.id == "st":
            return True
    return False


def _cache_decorator(fn: ast.FunctionDef) -> tuple[bool, bool]:
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
        tree = ast.parse(path.read_text(errors="ignore"))
    except SyntaxError:
        return findings
    # Lines with an opt-out comment.
    lines = path.read_text(errors="ignore").splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if any(_is_fetch(n) for n in ast.walk(node)):
            line = node.lineno
            if "noqa: cache-required" in (lines[line - 1] if line - 1 < len(lines) else ""):
                continue
            has_cache, has_ttl = _cache_decorator(node)
            if not has_cache:
                findings.append(
                    {
                        "file": str(path),
                        "line": line,
                        "func": node.name,
                        "detail": "missing @st.cache_data(ttl=...)",
                    }
                )
            elif not has_ttl:
                findings.append(
                    {
                        "file": str(path),
                        "line": line,
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Require @st.cache_data(ttl=) on data-fetch functions."
    )
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    files: list[Path] = []
    for raw in args.paths or ["apps"]:
        root = Path(raw)
        files.extend([p for p in root.rglob("*.py")] if root.is_dir() else [root])

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
