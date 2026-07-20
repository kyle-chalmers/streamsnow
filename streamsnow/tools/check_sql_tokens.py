"""Flag ``{TOKEN}`` placeholders inside SQL comments.

``render_sql`` substitutes ``{UPPERCASE_TOKEN}`` placeholders via plain
``str.replace`` with no comment awareness — a token name written inside a
``--`` or ``/* */`` comment (e.g. ``-- Filter: {AGENT_FILTER}``) gets replaced
with its full SQL expansion. Multi-line expansions break out of the comment on
the next line and Snowflake parses the orphaned fragments as live SQL. The rule:
document tokens in comments without braces (``-- Agent filter applied here``).

``-- noqa: sql-token`` on the line suppresses the finding.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_TOKEN_RE = re.compile(r"\{[A-Z][A-Z0-9_]*\}")
_WAIVER = "noqa: sql-token"


def _comment_spans(text: str) -> list[tuple[int, str]]:
    """Return ``(line_no, comment_text)`` for every comment line in ``text``.

    Walks the source with the same literal/comment state machine idea as
    ``check_app_security._strip_sql_noise`` but keeps the *comments* instead of
    dropping them. Tokens inside string literals are live SQL, not comments, so
    literals are skipped. A multi-line ``/* */`` block yields one entry per line.
    """
    spans: list[tuple[int, str]] = []
    line_no = 1
    buf: list[str] = []
    in_line_comment = False
    in_block_comment = False
    quote: str | None = None

    def flush() -> None:
        if buf:
            spans.append((line_no, "".join(buf)))
            buf.clear()

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "\n":
            flush()
            in_line_comment = False
            line_no += 1
            i += 1
            continue
        if in_line_comment:
            buf.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                flush()
                in_block_comment = False
                i += 2
                continue
            buf.append(ch)
            i += 1
            continue
        if quote is not None:
            if ch == quote:
                if nxt == quote:  # doubled quote = escaped quote in SQL
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        i += 1
    flush()
    return spans


def find_comment_tokens(text: str) -> list[dict]:
    findings = []
    for line_no, comment in _comment_spans(text):
        if _WAIVER in comment:
            continue
        for match in _TOKEN_RE.finditer(comment):
            findings.append({"line": line_no, "token": match.group(0)})
    return findings


def scan_paths(paths: list[Path]) -> dict:
    findings = []
    for p in paths:
        if p.suffix != ".sql" or not p.is_file():
            continue
        for f in find_comment_tokens(p.read_text(errors="ignore")):
            findings.append({"file": str(p), **f})
    return {"ok": not findings, "findings": findings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Flag {TOKEN} placeholders inside SQL comments (render_sql substitutes them)."
    )
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    files: list[Path] = []
    for raw in args.paths or ["apps"]:
        root = Path(raw)
        files.extend([p for p in root.rglob("*.sql")] if root.is_dir() else [root])

    result = scan_paths(files)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print("sql-tokens: clean")
    else:
        for f in result["findings"]:
            print(
                f"BLOCK {f['file']}:{f['line']} {f['token']} inside a SQL comment — "
                "render_sql substitutes tokens in comments too, injecting SQL into the "
                "comment block. Describe the token without braces, or add "
                "`-- noqa: sql-token`."
            )
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
