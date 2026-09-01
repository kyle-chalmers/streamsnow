"""Block personal absolute paths in committed code and prose.

Faithful ports, quick-help comments, and pasted terminal output routinely leak
the origin machine's filesystem layout into ``AGENTS.md`` / docstrings / inline
help — e.g. a hardcoded ``C:/Users/<user>/Development/acme-sales-dashboard/...``
in an app's docs, or a ``<user-home>/.venv/bin/python`` fallback baked into an
agent instruction file. Such paths are broken for every other developer, and
they leak a real username into a repo that may become public. Historically they
were caught only at human review, if at all; this check makes the rule
grep-able and enforced at commit time.

Scope: ``*.py`` and ``*.md`` files under the given paths (default ``apps``).
The check is worth pointing at docs and agent-instruction directories too —
prose leaks origin paths for exactly the same reasons app code does — so the
generated pre-commit hook may scope it wider than the default.

Patterns (case-insensitive for the Windows prefix; case-sensitive for *nix,
since usernames are case-sensitive on those filesystems):

- ``C:\\Users\\<user>\\`` or ``C:/Users/<user>/`` — Windows user dirs. The
  username portion allows spaces (Windows usernames commonly have them), and
  a trailing separator is required so a bare prose mention of ``C:/Users``
  is not flagged.
- ``/Users/<user>/Development/`` — Mac developer home. Only the
  ``.../Development/`` form is flagged: that is where origin leaks live
  ("I opened the repo locally and pasted a path into a comment"), while a
  generic Mac-home reference like ``~/.snowflake/config.toml`` rendered as an
  absolute path in docs is legitimate and far too noisy to block.
- ``/home/<user>/`` — Linux user home, excluding the GitHub Actions runner
  home (username ``runner``), which appears legitimately in CI examples.

Bracketed username placeholders like ``<user>`` and ``{user}`` are
documentation tokens, not real paths, and never trip the guard — the ``<`` /
``{`` characters fall outside the username character class. Fix a finding by
replacing the path with a docs link or a placeholder, or by deleting a
leftover note.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_SUFFIXES = (".py", ".md")

# Compiled patterns. See the module docstring for the policy each one enforces.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Windows user dir",
        # Match either backslash or forward-slash separators after `C:` so both
        # `C:\Users\<user>\` (Windows-literal) and `C:/Users/<user>/`
        # (forward-slashed in Markdown / Python strings) trip the guard.
        re.compile(r"C:[\\/]+Users[\\/]+[A-Za-z][A-Za-z0-9 ._-]*[\\/]", re.IGNORECASE),
    ),
    (
        "Mac personal dev dir",
        re.compile(r"/Users/[A-Za-z][A-Za-z0-9_.-]*/Development/"),
    ),
    (
        "Linux user home",
        # `runner` is the GitHub Actions user; its home is legitimate in CI docs.
        re.compile(r"/home/(?!runner/)[A-Za-z][A-Za-z0-9_.-]*/"),
    ),
)

_REMEDY = (
    "personal absolute paths are origin leaks — replace with a portable "
    "reference (a docs link, a `<user>` placeholder) or delete the leftover note"
)


def scan_file(path: Path) -> list[dict]:
    """Findings for one file: every line matching any leak pattern."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    findings: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pat in PATTERNS:
            m = pat.search(line)
            if m:
                findings.append(
                    {
                        "file": str(path),
                        "line": lineno,
                        "detail": f"[{name}] {m.group(0)!r} — {_REMEDY}",
                    }
                )
    return findings


def _iter_files(root: Path) -> list[Path]:
    """Walk *root* for scannable files, skipping dotted dirs *below* it.

    The root itself may be a dotted directory (an agent-skills tree passed
    explicitly) — only descendants like ``.git/`` / ``.review/`` /
    ``__pycache__`` are skipped.
    """
    if root.is_file():
        return [root] if root.suffix in _SUFFIXES else []
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if p.suffix not in _SUFFIXES or not p.is_file():
            continue
        rel_parts = p.relative_to(root).parts[:-1]
        if any(part.startswith(".") or part == "__pycache__" for part in rel_parts):
            continue
        out.append(p)
    return out


def scan_paths(paths: list[Path]) -> dict:
    """Scan the given files/directories. Missing paths are skipped silently —
    pre-commit can hand this tool filenames staged for deletion."""
    findings: list[dict] = []
    for root in paths:
        if not root.exists():
            continue
        for p in _iter_files(root):
            findings.extend(scan_file(p))
    return {"ok": not findings, "findings": findings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Block personal absolute paths (origin leaks) in code and prose."
    )
    ap.add_argument("paths", nargs="*", help="Files or directories to scan (default: apps).")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    result = scan_paths([Path(raw) for raw in (args.paths or ["apps"])])
    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print("path-leaks: clean")
    else:
        for f in result["findings"]:
            print(f"BLOCK {f['file']}:{f['line']} {f['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
