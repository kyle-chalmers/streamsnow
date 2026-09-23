"""Pre-publish privacy/export gate for the StreamSnow repo itself.

StreamSnow is public and every release ships to PyPI, so the tree must never
carry a secret, a personal path, a real email address, or the name of an
organization a contributor works for. This is the automated half of the
privacy gate (the other half is human review, see RELEASING.md). It runs in
StreamSnow's own CI.

Two layers:

- **Generic checks, always on** (committed here): personal absolute paths,
  private key blocks, GitHub and Slack tokens, and email addresses outside the
  reserved example domains. None of these names anybody.
- **An org-specific denylist, local only**: ``.streamsnow/export-denylist.txt``
  at the scanned root (gitignored), one term per line. A deny list of the
  names you must not leak is itself a leak if it is committed, which is the
  failure that motivated this split: an earlier version of this file spelled
  out the very names it was guarding, and shipped them in every sdist. Keep
  employer names, internal hostnames, table names and ticket prefixes in the
  local file. CI runs without it (generic checks only); run the scan locally,
  with the file present, before a release.

Denylist format: blank lines and ``#`` comments are ignored; a plain line is a
case-insensitive substring; a line starting ``re:`` is a regular expression
(for example ``re:\\bTICKET-\\d{2,}\\b``).

NOTE: this scans the StreamSnow project, not a user's generated repo.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Relative to the scanned root. Gitignored: never commit it.
LOCAL_DENYLIST = Path(".streamsnow") / "export-denylist.txt"

# Domains reserved for documentation and tests (RFC 2606 / RFC 6761), plus the
# GitHub no-reply form. Any other email address in the tree is a finding.
_EMAIL_OK_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com")
_EMAIL_OK_TLDS = ("example", "test", "invalid", "localhost")

# The address must end at a non-identifier character, so a decorator like
# `fn@st.cache_data` is not read as an email.
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?![\w.-])"
)

DENY_PATTERNS = [
    # personal absolute paths
    re.compile(r"/Users/[A-Za-z0-9._-]+/", re.IGNORECASE),
    re.compile(r"/home/[A-Za-z0-9._-]+/", re.IGNORECASE),
    # obvious secrets
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),  # GitHub fine-grained tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),  # Slack tokens
]
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".ai-friend-review",  # local, gitignored multi-AI review reports (contain abs paths)
}
# This scanner + its test legitimately contain the generic patterns.
_SKIP_FILES = {"check_export_clean.py", "test_export_clean.py"}
_TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".txt",
    ".cfg",
    ".ini",
    ".j2",
    ".sh",
    "",
}


def load_denylist(path: Path) -> tuple[list[str], list[re.Pattern[str]]]:
    """Parse a local denylist file into (lowercased terms, compiled regexes).

    A missing file is an empty list, not an error: CI has no local denylist.
    """
    terms: list[str] = []
    patterns: list[re.Pattern[str]] = []
    if not path.is_file():
        return terms, patterns
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            patterns.append(re.compile(line[3:].strip(), re.IGNORECASE))
        else:
            terms.append(line.lower())
    return terms, patterns


def _email_allowed(domain: str) -> bool:
    d = domain.lower()
    if d.rsplit(".", 1)[-1] in _EMAIL_OK_TLDS:
        return True
    return any(d == ok or d.endswith("." + ok) for ok in _EMAIL_OK_DOMAINS)


def scan_tree(root: Path, denylist: Path | None = None) -> dict:
    """Scan ``root`` for leaks. ``denylist`` defaults to the local file under root."""
    deny_path = denylist if denylist is not None else root / LOCAL_DENYLIST
    terms, local_patterns = load_denylist(deny_path)
    deny_resolved = deny_path.resolve() if deny_path.exists() else None
    findings: list[dict] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.name in _SKIP_FILES or p.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        if deny_resolved is not None and p.resolve() == deny_resolved:
            continue  # the denylist necessarily spells its own terms
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        low = text.lower()
        rel = str(p.relative_to(root))
        for term in terms:
            idx = low.find(term)
            if idx != -1:
                findings.append(
                    {
                        "file": rel,
                        "line": text.count("\n", 0, idx) + 1,
                        "match": term,
                        "detail": "local denylist term",
                    }
                )
        for pat in [*DENY_PATTERNS, *local_patterns]:
            m = pat.search(text)
            if m:
                findings.append(
                    {
                        "file": rel,
                        "line": text.count("\n", 0, m.start()) + 1,
                        "match": m.group(0),
                        "detail": f"denied pattern match {m.group(0)!r}",
                    }
                )
        for m in _EMAIL_RE.finditer(text):
            if not _email_allowed(m.group(1)):
                findings.append(
                    {
                        "file": rel,
                        "line": text.count("\n", 0, m.start()) + 1,
                        "match": m.group(0),
                        "detail": "email address outside the reserved example domains",
                    }
                )
    return {
        "ok": not findings,
        "findings": findings,
        "denylist_terms": len(terms) + len(local_patterns),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pre-publish privacy/export gate for the StreamSnow repo."
    )
    ap.add_argument("root", nargs="?", default=".", help="Repo root to scan (default: cwd).")
    ap.add_argument(
        "--denylist",
        default=None,
        help=f"Org-specific denylist file (default: <root>/{LOCAL_DENYLIST}, if present).",
    )
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    denylist = Path(args.denylist) if args.denylist else None
    if denylist is not None and not denylist.is_file():
        print(f"export-clean: denylist {denylist} not found")
        return 2
    result = scan_tree(root, denylist)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        scope = (
            f"generic checks + {result['denylist_terms']} local denylist term(s)"
            if result["denylist_terms"]
            else "generic checks only (no local denylist found)"
        )
        print(f"export-clean: clean ({scope})")
    else:
        for f in result["findings"]:
            print(f"LEAK {f['file']}:{f['line']}: {f['match']!r} ({f['detail']})")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
