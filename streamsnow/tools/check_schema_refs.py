"""Block references to denied Snowflake schemas in app code.

Config-driven: the denylist comes from ``governance.schema_deny`` in
``streamsnow.config.yaml`` (via :class:`streamsnow.policy.SchemaPolicy`), not a
hardcoded constant. One implementation consumed by pre-commit, CI, the
``/validate-app`` skill, and ``streamsnow check schema-refs``.

Scans ``.py`` and ``.sql`` files for ``DB.SCHEMA`` or bare ``SCHEMA.`` references
whose schema is on the denylist. Case-insensitive (Snowflake identifiers are).
SQL line comments (``-- ...``) are stripped before matching.

Exit codes: 0 = clean, 1 = denied reference found, 2 = tool/usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ..config import ConfigError, load_config
from ..policy import SchemaPolicy


def _strip_sql_comments(text: str) -> str:
    # Drop -- line comments and /* */ block comments so commented refs don't trip.
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def find_denied_refs(text: str, policy: SchemaPolicy) -> list[tuple[int, str]]:
    """Return (line_number, schema) for each denied schema reference."""
    if not policy.schema_deny:
        return []
    scanned = _strip_sql_comments(text)
    # Match WORD.WORD (optionally DB.SCHEMA.OBJECT) and test the schema position.
    pattern = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_$]*)\.([A-Za-z_][A-Za-z0-9_$]*)(?:\.([A-Za-z_][A-Za-z0-9_$]*))?"
    )
    denied = {d.upper() for d in policy.schema_deny}
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(scanned.splitlines(), start=1):
        for m in pattern.finditer(line):
            first, second, third = m.group(1), m.group(2), m.group(3)
            # DB.SCHEMA.OBJECT -> schema is `second`. For the 2-part form
            # (DB.SCHEMA or SCHEMA.OBJECT) we can't tell which token is the
            # schema, so test both against the denylist.
            candidates = {second} if third else {first, second}
            for candidate in candidates:
                if candidate.upper() in denied:
                    hits.append((i, candidate))
                    break
    return hits


def check_paths(paths: list[Path], policy: SchemaPolicy) -> dict:
    findings = []
    for p in paths:
        if p.suffix not in (".py", ".sql") or not p.is_file():
            continue
        for line_no, schema in find_denied_refs(p.read_text(errors="ignore"), policy):
            findings.append({"file": str(p), "line": line_no, "schema": schema})
    return {"ok": not findings, "findings": findings, "denylist": list(policy.schema_deny)}


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
    files: list[Path] = []
    for raw in args.paths or ["."]:
        root = Path(raw)
        files.extend(root.rglob("*") if root.is_dir() else [root])

    result = check_paths(files, policy)
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
