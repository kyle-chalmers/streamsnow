"""Require a broad try/except around ``get_active_session()`` calls.

``get_active_session()`` only works inside the deployed Snowflake runtime — it
raises during local ``streamlit run``, so every call must sit inside a ``try``
whose handler is broad (``except Exception`` or bare ``except``) with an
``st.connection("snowflake")`` fallback. A *narrow* handler such as
``except ImportError`` is also a finding: depending on how the environment was
resolved, the local failure can surface as a different exception type and slip
past the narrow catch (observed under uv-resolved snowpark).

``# noqa: session-fallback`` on the call line suppresses the finding.

New-only mode (the CLI default)
-------------------------------

A repo adopting StreamSnow often carries legacy pages with unwrapped calls
that predate the rule; failing the whole tree on day one blocks adoption
without making anything safer. So the CLI compares each file against a git
base ref (``--base-ref``, default ``origin/main``) and flags a file only when
it *introduces* violations — its violation count exceeds the base version's.
Line-level attribution across a diff is unreliable (edits shift every line
number), so the gate is count-based per file: when the count grows, every
current violation in that file is reported so the author can see the full
set. ``--all`` restores tree-wide behavior — that is also what the
``validate-app`` ship gate uses, so legacy debt still surfaces there.

The base ref is resolved defensively: when it doesn't exist (fresh clone with
no remote, detached CI checkout) or a file isn't inside a git work tree, the
scan falls back to tree-wide for those files and says so in a note rather
than silently passing.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path

_KIND = "session-fallback"
_DEFAULT_BASE_REF = "origin/main"


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


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run git, never raising — (127, "") when git itself is unavailable."""
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
        return proc.returncode, proc.stdout
    except OSError:
        return 127, ""


def _violation_count(text: str, filename: str) -> int:
    """Real violations only — a syntax-error pseudo-finding must not make the
    baseline look violation-free (nor count as one)."""
    return sum(
        1
        for f in find_unwrapped_calls(text, filename)
        if not f["detail"].startswith("syntax error")
    )


def _baseline_counts(files: list[Path], base_ref: str) -> tuple[dict[Path, int], list[str]]:
    """Violation count per file at ``base_ref``. A file absent from the map has
    no usable baseline (not in git, or the ref didn't resolve) — the caller
    scans it tree-wide. Notes explain every fallback."""
    counts: dict[Path, int] = {}
    notes: list[str] = []
    toplevel_cache: dict[Path, Path | None] = {}
    ref_ok_cache: dict[Path, bool] = {}
    outside_git = False

    for p in files:
        parent = p.resolve().parent
        if parent not in toplevel_cache:
            rc, out = _git(["rev-parse", "--show-toplevel"], parent)
            toplevel_cache[parent] = Path(out.strip()) if rc == 0 and out.strip() else None
        top = toplevel_cache[parent]
        if top is None:
            outside_git = True
            continue
        if top not in ref_ok_cache:
            rc, _ = _git(["rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"], top)
            ref_ok_cache[top] = rc == 0
            if rc != 0:
                notes.append(
                    f"base ref {base_ref!r} not found in {top} — "
                    "checking all calls (tree-wide) for its files"
                )
        if not ref_ok_cache[top]:
            continue
        rel = p.resolve().relative_to(top).as_posix()
        rc, out = _git(["show", f"{base_ref}:{rel}"], top)
        # rc != 0 -> file doesn't exist at the base ref: brand-new, baseline 0.
        counts[p] = _violation_count(out, rel) if rc == 0 else 0

    if outside_git:
        notes.append("some paths are not in a git work tree — checked tree-wide")
    return counts, notes


def scan_paths(paths: list[Path], base_ref: str | None = None) -> dict:
    """Scan ``paths``. With ``base_ref`` set, flag only files whose violation
    count exceeds that ref's (new-only mode); ``None`` scans everything — the
    unchanged default for library callers like ``validate_app`` (the ship gate
    must keep seeing legacy debt)."""
    files = [p for p in paths if p.suffix == ".py" and p.is_file()]
    baselines: dict[Path, int] = {}
    notes: list[str] = []
    if base_ref is not None:
        baselines, notes = _baseline_counts(files, base_ref)

    findings = []
    for p in files:
        file_findings = find_unwrapped_calls(p.read_text(errors="ignore"), str(p))
        if base_ref is not None and p in baselines:
            current = sum(1 for f in file_findings if not f["detail"].startswith("syntax error"))
            if current <= baselines[p]:
                continue  # legacy debt, not introduced by this change
        findings.extend({"file": str(p), **f} for f in file_findings)

    result: dict = {"ok": not findings, "findings": findings}
    if notes:
        result["notes"] = sorted(set(notes))
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Require broad try/except around get_active_session() calls."
    )
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument(
        "--base-ref",
        default=_DEFAULT_BASE_REF,
        help="Flag only violations introduced since this git ref (default: "
        f"{_DEFAULT_BASE_REF}; falls back to tree-wide when unresolvable).",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="Check every call tree-wide instead of only newly introduced ones.",
    )
    args = ap.parse_args(argv)

    files: list[Path] = []
    for raw in args.paths or ["apps"]:
        root = Path(raw)
        files.extend([p for p in root.rglob("*.py")] if root.is_dir() else [root])

    result = scan_paths(files, base_ref=None if args.all else args.base_ref)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        for note in result.get("notes", []):
            print(f"NOTE {note}")
        if result["ok"]:
            print(f"{_KIND}: clean")
        else:
            for f in result["findings"]:
                print(f"BLOCK {f['file']}:{f['line']} {f['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
