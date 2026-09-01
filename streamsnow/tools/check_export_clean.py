"""Pre-publish privacy/export gate for the StreamSnow repo itself.

Before StreamSnow goes public, scan the tree for proprietary/internal leakage
and obvious secrets that must never ship in an open-source release. This is the
automated half of the privacy gate (the other half is human review — see
RELEASING.md). Runs in StreamSnow's own CI.

NOTE: this scans the StreamSnow project, not a user's generated repo.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Internal / org-specific terms that must not appear in the OSS release. Generic
# illustrative names in the example config (DATA_APPS, ANALYTICS, etc.) are fine
# and intentionally NOT listed.
DENY_TERMS = [
    "business_intelligence",
    "raw_data_store",
    "fivetran",
    "initech",
    "arca.freshsnow",
    "mvw_loan_tape",
    "data-intell-pr-bot",
    "sonarcloud",
    "1password",
    # Source-organization names — a pasted reference must never pass the gate.
    "happymoney",
    "happy money",
    "happy-money",
]
# Source-domain vocabulary. StreamSnow was extracted from a consumer-lending
# monorepo; the OSS release must carry ZERO domain flavor from it — not just
# literals but the business vocabulary itself. Examples here use a fictional
# retail-analytics team ("Acme": orders, revenue, inventory) instead. These are
# substring matches on lowercased text, chosen to be specific enough not to
# false-positive on ordinary tooling prose.
DENY_TERMS += [
    "loan",
    "lending",
    "borrower",
    "payoff",
    "delinquen",  # delinquent / delinquency
    "charge-off",
    "chargeoff",
    "charged_off",
    "debt sale",
    "refinanc",  # refinance / refinancing
    "bankrupt",
    "underwrit",  # underwrite / underwriting
    "originations",
    "servicing",
]
# Internal ticket prefixes (word-boundary, case-insensitive).
DENY_PATTERNS = [
    re.compile(r"\bDI-\d{2,}\b"),
    re.compile(r"\bDEVOPS-\d{2,}\b"),
    re.compile(r"\bSNIC\b"),
    re.compile(r"\bDPD\b"),  # days-past-due — lending vocabulary
    re.compile(r"\bOTP(s)?\b"),  # one-time-payment — lending vocabulary
    # personal absolute paths
    re.compile(r"/Users/[A-Za-z0-9._-]+/", re.IGNORECASE),
    re.compile(r"/home/[A-Za-z0-9._-]+/", re.IGNORECASE),
    # obvious secrets
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b"),  # GitHub tokens
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
# This scanner + its test legitimately contain the deny terms.
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


def scan_tree(root: Path) -> dict:
    findings: list[dict] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.name in _SKIP_FILES or p.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        low = text.lower()
        rel = str(p.relative_to(root))
        for term in DENY_TERMS:
            idx = low.find(term)
            if idx != -1:
                findings.append(
                    {
                        "file": rel,
                        "line": text.count("\n", 0, idx) + 1,
                        "match": term,
                        "detail": f"denied term {term!r}",
                    }
                )
        for pat in DENY_PATTERNS:
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
    return {"ok": not findings, "findings": findings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pre-publish privacy/export gate for the StreamSnow repo."
    )
    ap.add_argument("root", nargs="?", default=".", help="Repo root to scan (default: cwd).")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    result = scan_tree(Path(args.root).resolve())
    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print("export-clean: no proprietary/internal terms or secrets found")
    else:
        for f in result["findings"]:
            print(f"LEAK {f['file']}: {f['match']!r}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
