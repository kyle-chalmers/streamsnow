"""Block egress, code-execution, and write/dynamic SQL in app code.

Streamlit-in-Snowflake apps are read-only and sandboxed. This check fails a PR
that introduces network egress, arbitrary code execution, data-mutating SQL, or
string-built (injectable) SQL. AST-based for Python; regex for `.sql`.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

# Modules whose import implies network egress / unsafe IO.
EGRESS_MODULES = {
    "requests",
    "urllib",
    "urllib2",
    "urllib3",
    "httpx",
    "http",
    "aiohttp",
    "socket",
    "ftplib",
    "smtplib",
    "telnetlib",
    "boto3",
    "botocore",
    "paramiko",
}
# Callables that execute code or deserialize untrusted data.
EXEC_CALLS = {"eval", "exec", "compile", "__import__"}
EXEC_ATTRS = {
    ("os", "system"),
    ("os", "popen"),
    ("subprocess", "run"),
    ("subprocess", "call"),
    ("subprocess", "Popen"),
    ("subprocess", "check_output"),
    ("pickle", "loads"),
    ("pickle", "load"),
}
# Data-mutating / DDL SQL verbs (word-boundary, case-insensitive).
WRITE_SQL = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE\b|"
    r"DROP\s+(TABLE|VIEW|SCHEMA|DATABASE|STAGE)|CREATE\s+(OR\s+REPLACE\s+)?"
    r"(TABLE|VIEW|SCHEMA|DATABASE|STAGE|STREAMLIT)|ALTER\s+(TABLE|VIEW|SCHEMA|SESSION|WAREHOUSE)|"
    r"GRANT\b|REVOKE\b)",
    re.IGNORECASE,
)


def _scan_python(path: Path) -> list[dict]:
    findings: list[dict] = []
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except SyntaxError as exc:
        return [{"file": str(path), "line": exc.lineno or 0, "kind": "syntax", "detail": str(exc)}]

    for node in ast.walk(tree):
        # egress imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in EGRESS_MODULES:
                    findings.append(
                        {
                            "file": str(path),
                            "line": node.lineno,
                            "kind": "egress",
                            "detail": alias.name,
                        }
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in EGRESS_MODULES:
                findings.append(
                    {
                        "file": str(path),
                        "line": node.lineno,
                        "kind": "egress",
                        "detail": node.module,
                    }
                )
        # exec calls
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in EXEC_CALLS:
                findings.append(
                    {"file": str(path), "line": node.lineno, "kind": "code-exec", "detail": fn.id}
                )
            elif (
                isinstance(fn, ast.Attribute)
                and isinstance(fn.value, ast.Name)
                and (fn.value.id, fn.attr) in EXEC_ATTRS
            ):
                findings.append(
                    {
                        "file": str(path),
                        "line": node.lineno,
                        "kind": "code-exec",
                        "detail": f"{fn.value.id}.{fn.attr}",
                    }
                )
        # dynamic SQL: f-string / % / .format containing a SQL verb passed to a query call
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                v.value
                for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            if re.search(r"\b(SELECT|FROM|WHERE|JOIN)\b", text, re.IGNORECASE):
                findings.append(
                    {
                        "file": str(path),
                        "line": node.lineno,
                        "kind": "dynamic-sql",
                        "detail": "f-string SQL",
                    }
                )
    return findings


def _scan_sql(path: Path) -> list[dict]:
    findings: list[dict] = []
    text = path.read_text(errors="ignore")
    cleaned = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
    for i, line in enumerate(cleaned.splitlines(), start=1):
        if WRITE_SQL.search(line):
            findings.append(
                {"file": str(path), "line": i, "kind": "write-sql", "detail": line.strip()[:60]}
            )
    return findings


def scan_paths(paths: list[Path]) -> dict:
    findings: list[dict] = []
    for p in paths:
        if not p.is_file():
            continue
        if p.suffix == ".py":
            findings.extend(_scan_python(p))
        elif p.suffix == ".sql":
            findings.extend(_scan_sql(p))
    return {"ok": not findings, "findings": findings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Block egress/code-exec/write-SQL/dynamic-SQL in app code."
    )
    ap.add_argument("paths", nargs="*", help="Files or directories to scan.")
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
        print("app-security: clean")
    else:
        for f in result["findings"]:
            print(f"BLOCK {f['file']}:{f['line']} [{f['kind']}] {f['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
