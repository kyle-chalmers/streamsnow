"""Require a broad try/except around ``get_active_session()`` calls.

``get_active_session()`` only works inside the deployed Snowflake runtime — it
raises during local ``streamlit run``, so every call must sit inside a ``try``
whose handler is broad (``except Exception`` or bare ``except``) with an
``st.connection("snowflake")`` fallback. A *narrow* handler such as
``except ImportError`` is also a finding: depending on how the environment was
resolved, the local failure can surface as a different exception type and slip
past the narrow catch (observed under uv-resolved snowpark).

``# noqa: session-fallback`` on the call line suppresses the finding.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

_KIND = "session-fallback"


def _is_broad_handler(handler: ast.ExceptHandler) -> bool:
    """True for ``except:``, ``except Exception``, or a tuple containing Exception."""
    t = handler.type
    if t is None:  # bare except
        return True
    names = t.elts if isinstance(t, ast.Tuple) else [t]
    for n in names:
        if isinstance(n, ast.Name) and n.id in ("Exception", "BaseException"):
            return True
        if isinstance(n, ast.Attribute) and n.attr in ("Exception", "BaseException"):
            return True
    return False


def _call_name(node: ast.Call) -> str | None:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def find_unwrapped_calls(text: str, filename: str = "<string>") -> list[dict]:
    try:
        tree = ast.parse(text, filename=filename)
    except SyntaxError as exc:
        return [{"line": exc.lineno or 0, "detail": f"syntax error: {exc.msg}"}]

    lines = text.splitlines()
    # Map every node to its enclosing Try nodes (innermost last) in one walk.
    findings: list[dict] = []

    def visit(node: ast.AST, try_stack: list[ast.Try]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and _call_name(child) == "get_active_session":
                line = child.lineno
                if 1 <= line <= len(lines) and f"noqa: {_KIND}" in lines[line - 1]:
                    pass
                else:
                    # Only the try BODY is protected — a call inside an except/
                    # finally block of the same try is not covered by its handlers.
                    covering = [t for t in try_stack if _node_in_body(t, child)]
                    if not covering:
                        findings.append(
                            {
                                "line": line,
                                "detail": "unwrapped get_active_session() — raises during "
                                "local `streamlit run`; wrap in try/except Exception with an "
                                'st.connection("snowflake") fallback',
                            }
                        )
                    elif not any(any(_is_broad_handler(h) for h in t.handlers) for t in covering):
                        findings.append(
                            {
                                "line": line,
                                "detail": "get_active_session() wrapped only by a narrow "
                                "except — catch Exception (narrow handlers like ImportError "
                                "miss resolver-dependent failure types)",
                            }
                        )
            if isinstance(child, ast.Try):
                visit(child, try_stack + [child])
            else:
                visit(child, try_stack)

    def _node_in_body(t: ast.Try, target: ast.AST) -> bool:
        """True if ``target`` is within ``t.body`` (not its handlers/else/finally)."""
        for stmt in t.body:
            for sub in ast.walk(stmt):
                if sub is target:
                    return True
        return False

    visit(tree, [])
    return findings


def scan_paths(paths: list[Path]) -> dict:
    findings = []
    for p in paths:
        if p.suffix != ".py" or not p.is_file():
            continue
        for f in find_unwrapped_calls(p.read_text(errors="ignore"), str(p)):
            findings.append({"file": str(p), **f})
    return {"ok": not findings, "findings": findings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Require broad try/except around get_active_session() calls."
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
        print("session-fallback: clean")
    else:
        for f in result["findings"]:
            print(f"BLOCK {f['file']}:{f['line']} {f['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
