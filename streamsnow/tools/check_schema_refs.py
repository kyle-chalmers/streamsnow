"""Block references to denied Snowflake schemas in app code.

Config-driven: the denylist comes from ``governance.schema_deny`` in
``streamsnow.config.yaml`` (via :class:`streamsnow.policy.SchemaPolicy`), not a
hardcoded constant. One implementation consumed by pre-commit, CI, the
``/validate-app`` skill, and ``streamsnow check schema-refs``.

Detection (mirrors the battle-tested source monorepo
``tools/check_schema_refs.py``):

- ``.py`` (AST-based): only string *literals* that look like SQL are scanned.
  A literal is treated as SQL when it is either (a) an argument to a
  query/sql/execute-style call **or** (b) contains a SQL keyword
  (SELECT/INSERT/UPDATE/DELETE/FROM/JOIN, case-insensitive). Module/class/
  function **docstrings are excluded**, and prose passed to ``st.markdown`` /
  ``st.caption`` / ``st.write`` never trips the guard — the rule blocks
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

Exit codes: 0 = clean, 1 = denied reference found, 2 = tool/usage error.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

from ..config import ConfigError, load_config
from ..policy import SchemaPolicy

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


def _denied_in_line(line: str, denied: set[str], read_exc: set[str]) -> set[tuple[int, str]]:
    """Return (placeholder-lineno-0, schema) hits for a single normalized line.

    The line number is filled in by the caller; this helper only resolves which
    denied schemas a line references. Quoted identifiers (``"BI"."BRIDGE"``) and
    whitespace around dots (``DB . BRIDGE . T``) are normalized first so they
    can't slip past the matcher.
    """
    hits: set[tuple[int, str]] = set()
    norm = re.sub(r"\s*\.\s*", ".", line.replace('"', ""))
    for m in _DOTTED.finditer(norm):
        if m.group(0).upper() in read_exc:
            continue  # sanctioned exact-FQN read
        first, second, third = m.group(1), m.group(2), m.group(3)
        # Only the SCHEMA-position segment is tested — never the database
        # segment and never a trailing object segment (matches the source,
        # which treats a denied name only when followed by a dot):
        #   2-part SCHEMA.OBJECT      -> schema is `first`  (e.g. BRIDGE.T)
        #   3-part DB.SCHEMA.OBJECT   -> schema is `second` (e.g. DB.BRIDGE.T)
        # A trailing `DB.BRIDGE` (BRIDGE in object position) is NOT a hit.
        candidate = second if third else first
        if candidate.upper() in denied:
            hits.add((0, candidate))
    # USE SCHEMA <denied> — not a dotted ref, would otherwise slip past.
    for m in _USE.finditer(norm):
        schema_tok = m.group(2) or m.group(1)
        if schema_tok and schema_tok.upper() in denied:
            hits.add((0, schema_tok))
    return hits


def _scan_text(text: str, denied: set[str], read_exc: set[str]) -> set[tuple[int, str]]:
    """Line-by-line denylist scan over already-comment-stripped *text*."""
    hits: set[tuple[int, str]] = set()
    for i, line in enumerate(text.splitlines(), start=1):
        for _, schema in _denied_in_line(line, denied, read_exc):
            hits.add((i, schema))
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


def _collect_query_arg_ids(tree: ast.AST) -> set[int]:
    """``id()`` of string-literal args passed to query/sql/execute-style calls."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func) not in _QUERY_CALL_NAMES:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                ids.add(id(arg))
    return ids


def _find_denied_refs_py(text: str, denied: set[str], read_exc: set[str]) -> set[tuple[int, str]]:
    """AST scan: only SQL-looking string literals (excluding docstrings) are checked.

    Unparseable Python is skipped (returns nothing): a file with a syntax error
    can't run as an app anyway, and a text fallback would reintroduce the
    docstring/prose false positives this AST scan exists to avoid (ruff/CI catch
    the syntax error separately).
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()

    docstring_ids = _collect_docstring_ids(tree)
    query_arg_ids = _collect_query_arg_ids(tree)
    hits: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstring_ids:
            continue  # docstrings document the ban; they don't query
        literal = node.value
        is_sql = id(node) in query_arg_ids or bool(_SQL_KEYWORD_RE.search(literal))
        if not is_sql:
            continue  # markdown / caption / plain prose — not a SQL instruction
        scanned = _strip_sql_comments(literal)
        base_line = node.lineno
        for offset, schema in _scan_text(scanned, denied, read_exc):
            # Constant.lineno is the literal's first line; offset is 1-based
            # within the literal, so the file line is base_line + offset - 1.
            hits.add((base_line + offset - 1, schema))
    return hits


def find_denied_refs(
    text: str, policy: SchemaPolicy, is_python: bool = False
) -> list[tuple[int, str]]:
    """Return sorted, de-duped (line_number, schema) for each denied reference.

    ``is_python=True`` enables the AST-based SQL-literal scan that excludes
    docstrings and prose. Default (text mode) suits ``.sql`` files.
    """
    if not policy.schema_deny:
        return []
    denied = {d.upper() for d in policy.schema_deny}
    read_exc = {e.upper() for e in policy.read_exceptions}
    if is_python:
        return sorted(_find_denied_refs_py(text, denied, read_exc))
    return sorted(_scan_text(_strip_sql_comments(text), denied, read_exc))


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


def check_paths(paths: list[Path], policy: SchemaPolicy, root: Path | None = None) -> dict:
    findings = []
    for p in paths:
        if p.suffix not in (".py", ".sql") or not p.is_file():
            continue
        if _has_dotted_dir(p, root):
            continue  # dotted dir (.review/, .git/, ...) — not real app code
        text = p.read_text(errors="ignore")
        for line_no, schema in find_denied_refs(text, policy, is_python=p.suffix == ".py"):
            findings.append({"file": str(p), "line": line_no, "schema": schema})
    return {"ok": not findings, "findings": findings, "denylist": list(policy.schema_deny)}


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
    for raw in args.paths or ["."]:
        target = Path(raw)
        root = target if target.is_dir() else None
        findings.extend(check_paths(_iter_files(target), policy, root)["findings"])
    # Keep check_paths' full output contract. Rebuilding this dict from just
    # `findings` dropped `denylist`, which the md success branch below prints
    # and JSON consumers read — so every CLEAN run tracebacked with
    # KeyError: 'denylist', and no test noticed because none called main().
    result = {"ok": not findings, "findings": findings, "denylist": list(policy.schema_deny)}
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        if result["ok"]:
            print(f"schema-refs: clean (denylist: {', '.join(result['denylist']) or 'none'})")
        else:
            for f in result["findings"]:
                print(f"BLOCK {f['file']}:{f['line']} references denied schema {f['schema']!r}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
