"""Block the `:N IS NULL OR col = :N` bind-predicate anti-pattern.

The deployed Snowflake Go-driver middleware NULL-binds *every* positional param
when *any* one is Python ``None`` — so optional-filter predicates written as
``(:1 IS NULL OR col = :1)`` silently scan zero rows in production (works fine
locally). Use ``render_sql`` token substitution for optional filters instead.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# :1 IS NULL OR   /   :1 IS NULL ) OR   etc.
_PATTERN = re.compile(r":\d+\s+IS\s+NULL\s*\)?\s+OR", re.IGNORECASE)


def find_bind_predicates(text: str) -> list[int]:
    cleaned = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
    return [i for i, line in enumerate(cleaned.splitlines(), start=1) if _PATTERN.search(line)]


def scan_paths(paths: list[Path]) -> dict:
    findings = []
    for p in paths:
        if p.suffix not in (".py", ".sql") or not p.is_file():
            continue
        for line_no in find_bind_predicates(p.read_text(errors="ignore")):
            findings.append({"file": str(p), "line": line_no})
    return {"ok": not findings, "findings": findings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Block the :N IS NULL OR Go-driver bind-predicate trap."
    )
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    files: list[Path] = []
    for raw in args.paths or ["apps"]:
        root = Path(raw)
        files.extend(
            [p for p in root.rglob("*") if p.suffix in (".py", ".sql")] if root.is_dir() else [root]
        )

    result = scan_paths(files)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print("bind-predicates: clean")
    else:
        for f in result["findings"]:
            print(
                f"BLOCK {f['file']}:{f['line']} ':N IS NULL OR ...' — use render_sql token substitution"
            )
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
