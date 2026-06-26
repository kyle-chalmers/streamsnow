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


_DOTTED = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_$]*)\.([A-Za-z_][A-Za-z0-9_$]*)(?:\.([A-Za-z_][A-Za-z0-9_$]*))?"
)
# USE SCHEMA RAW / USE DATABASE.RAW / USE RAW
_USE = re.compile(
    r"\bUSE\s+(?:SCHEMA\s+|DATABASE\s+)?([A-Za-z_][A-Za-z0-9_$]*)(?:\.([A-Za-z_][A-Za-z0-9_$]*))?",
    re.IGNORECASE,
)


def find_denied_refs(text: str, policy: SchemaPolicy) -> list[tuple[int, str]]:
    """Return sorted, de-duped (line_number, schema) for each denied reference."""
    if not policy.schema_deny:
        return []
    scanned = _strip_sql_comments(text)
    denied = {d.upper() for d in policy.schema_deny}
    # Sanctioned exact-FQN direct reads bypass the denylist.
    read_exc = {e.upper() for e in policy.read_exceptions}
    hits: set[tuple[int, str]] = set()
    for i, line in enumerate(scanned.splitlines(), start=1):
        # Normalize quoted identifiers ("BI"."BRIDGE") and whitespace around
        # dots (DB . BRIDGE . T) so they can't slip past the matcher.
        norm = re.sub(r"\s*\.\s*", ".", line.replace('"', ""))
        for m in _DOTTED.finditer(norm):
            if m.group(0).upper() in read_exc:
                continue  # sanctioned exact-FQN read
            first, second, third = m.group(1), m.group(2), m.group(3)
            # 3-part -> schema is `second`; 2-part is ambiguous, test both.
            for candidate in {second} if third else {first, second}:
                if candidate.upper() in denied:
                    hits.add((i, candidate))
                    break
        # USE SCHEMA <denied> — not a dotted ref, would otherwise slip past.
        for m in _USE.finditer(norm):
            schema_tok = m.group(2) or m.group(1)
            if schema_tok and schema_tok.upper() in denied:
                hits.add((i, schema_tok))
    return sorted(hits)


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
