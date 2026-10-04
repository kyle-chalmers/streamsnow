#!/usr/bin/env python3
"""PreToolUse hook — mechanical deploy-safety guard.

Makes the prose-only rule "deploys go through /ship-app" mechanical, so it holds
even when the agent forgets:

1. **Streamlit-destructive commands** — `snow streamlit deploy` is a
   destructive replace of the live app definition (`CREATE OR REPLACE
   STREAMLIT` under the hood), and `DROP/ALTER STREAMLIT` or stage `REMOVE`
   mutate what users see in Snowsight right now. These require human
   confirmation outside the sanctioned `/ship-app` gate.

2. **Warehouse writes** — a warehouse CLI (`snow`/`bq`/`psql`/…) carrying a
   destructive SQL statement requires confirmation, including SQL hidden in a
   `-f` file or a `< file` stdin redirect.

The matcher defends against shell-quote evasion (`sn'ow' streamlit deploy`) and
full-path invocations (`/usr/local/bin/snow`). When a referenced SQL file is too
large to scan, the hook asks rather than letting it pass unseen. It errs toward
an extra confirmation — a guard should over-ask, never under-ask.

Native Windows. Claude Code's PowerShell tool is on by default there and is
Claude's primary shell, so a guard that read only the Bash tool missed most
Windows commands. The hook therefore inspects both tools (hooks.json matches
`Bash|PowerShell`) and the shapes Windows adds: an `.exe`/`.cmd`/`.bat`/`.ps1`
suffix, backslash paths and the `&` call operator (`& 'C:\\...\\snow.exe'`),
case-blind command names, PowerShell's backtick escape (`` sn`ow ``), and SQL
piped in with `Get-Content deploy.sql | snow sql --stdin` (PowerShell has no
`<` redirect). The payload is read as UTF-8 bytes: Windows pipes default to
cp1252, which fails on some UTF-8 characters and silently skipped the guard.

Repo-gated (does nothing unless a `streamsnow.config.yaml` is found, so it is
zero-cost in unrelated repos), stdlib-only, no network, and fail-open — it never
crashes a session and only ever *adds* a confirmation, never bypasses one.

Ported from jobwright's deploy_safety.py (its headline safety feature); the
Streamlit patterns below are streamsnow's single-stack equivalent of
jobwright's per-platform adapter patterns.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

CONFIG_FILENAME = "streamsnow.config.yaml"

#: Claude Code's command-running tools. PowerShell is the default on Windows.
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})

WAREHOUSE_CLIS = [
    "snow",
    "snowsql",
    "bq",
    "dbsqlcli",
    "psql",
    "mysql",
    "sqlcmd",
    "duckdb",
    "redshift-data",
]

DESTRUCTIVE_SQL = re.compile(
    r"\b(CREATE\s+OR\s+REPLACE|CREATE|ALTER|DROP|DELETE|UPDATE|INSERT|TRUNCATE|MERGE|GRANT|REVOKE|REPLACE\s+INTO)\b",
    re.IGNORECASE,
)

# A Windows executable suffix: `snow.exe`, or a shim npm/pip/scoop installed.
_WIN_EXT = r"(?:\.exe|\.cmd|\.bat|\.ps1)?"

# Streamlit-in-Snowflake destructive surface. Single-stack, so the patterns
# live here (no adapter indirection); tests/test_deploy_safety.py asserts each
# one fires.
STREAMLIT_DESTRUCTIVE: list[dict[str, str]] = [
    {
        "pattern": rf"snow{_WIN_EXT}\s+streamlit\s+deploy\b",
        "reason": "`snow streamlit deploy` replaces the live app definition (CREATE OR REPLACE under the hood). The sanctioned path is /ship-app: validate first, confirm the target, then deploy.",
    },
    {
        "pattern": rf"snow{_WIN_EXT}\s+streamlit\s+drop\b",
        "reason": "`snow streamlit drop` removes a live app users may be viewing in Snowsight.",
    },
    {
        "pattern": r"\bCREATE\s+OR\s+REPLACE\s+STREAMLIT\b",
        "reason": "`CREATE OR REPLACE STREAMLIT` overwrites the live app definition — the /ship-app gate exists for exactly this statement.",
    },
    {"pattern": r"\bDROP\s+STREAMLIT\b", "reason": "`DROP STREAMLIT` removes a live app."},
    {
        "pattern": r"\bALTER\s+STREAMLIT\b",
        "reason": "`ALTER STREAMLIT` mutates a live app (including ADD LIVE VERSION / commit swaps that change what users see).",
    },
    {
        "pattern": rf"\bREMOVE\s+@|snow{_WIN_EXT}\s+stage\s+remove\b",
        "reason": "Removing files from a stage can break the deployed app that serves from it.",
    },
]

# SQL can live in a file (-f/--file) or a stdin redirect (`psql db < deploy.sql`).
_FILE_FLAG = re.compile(r"(?:-f|-i|--file|--filename|--input-file|--query)[=\s]+([^\s;|&]+)")
_STDIN_REDIR = re.compile(r"<\s*([^\s;|&<>]+)")
# PowerShell has no `<`; SQL is piped in: `Get-Content deploy.sql | snow sql --stdin`.
_PIPED_FILE = re.compile(
    r"\b(?:Get-Content|gc|cat|type)\s+(?:-(?:Path|LiteralPath)\s+)?([^\s;|&]+)",
    re.IGNORECASE,
)
_MAX_SCAN_BYTES = 2_000_000


def _dequote(command: str) -> str:
    """Remove shell quote characters so `sn'ow' streamlit deploy` matches a pattern,
    and PowerShell's backtick escape (`` sn`ow ``) for the same reason.
    Used for pattern matching only — never for opening files."""
    return command.replace("'", "").replace('"', "").replace("`", "")


def find_config(cwd: str) -> Path | None:
    starts: list[Path] = []
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        starts.append(Path(os.environ["CLAUDE_PROJECT_DIR"]))
    if cwd:
        starts.append(Path(cwd))
    starts.append(Path.cwd())
    for start in starts:
        try:
            here = start.resolve()
        except OSError:
            continue
        for directory in (here, *here.parents):
            candidate = directory / CONFIG_FILENAME
            if candidate.is_file():
                return candidate
    return None


def invokes_warehouse(command: str) -> str | None:
    for cli in WAREHOUSE_CLIS:
        # A path prefix (/usr/local/bin/snow, C:\tools\snow.exe) or PowerShell's
        # `&` call operator is a boundary; Windows command names are case-blind.
        if re.search(rf"(^|[\s;&|(/\\]){re.escape(cli)}{_WIN_EXT}(\s|$)", command, re.IGNORECASE):
            return cli
    return None


def referenced_sql(command: str, cwd: str) -> tuple[str, bool]:
    """Return (concatenated SQL text from -f/--file/< files, unscannable_flag).

    unscannable is True if a referenced file exists but is too large to read — the
    caller treats that as a reason to ask, since we can't prove it's safe.
    """
    text = ""
    unscannable = False
    for raw in (
        _FILE_FLAG.findall(command) + _STDIN_REDIR.findall(command) + _PIPED_FILE.findall(command)
    ):
        raw = raw.strip("'\"")  # `-f "deploy.sql"` -> deploy.sql
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute() and cwd:
            p = Path(cwd) / raw
        try:
            if p.is_file():
                if p.stat().st_size <= _MAX_SCAN_BYTES:
                    text += "\n" + p.read_text(errors="replace", encoding="utf-8")
                else:
                    unscannable = True
        except OSError:
            continue
    return text, unscannable


def emit_ask(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main() -> int:
    try:
        # Bytes, not sys.stdin's text mode: Claude Code sends UTF-8, and the
        # Windows pipe default (cp1252) cannot decode every UTF-8 character.
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError, OSError):
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") not in SHELL_TOOLS:
        return 0
    command = (payload.get("tool_input") or {}).get("command", "") or ""
    if not command.strip():
        return 0

    cwd = payload.get("cwd", "")
    if find_config(cwd) is None:
        return 0  # not a streamsnow repo — zero cost

    dequoted = _dequote(command)

    # 1) Streamlit-destructive commands.
    for pat in STREAMLIT_DESTRUCTIVE:
        try:
            if re.search(pat["pattern"], dequoted, re.IGNORECASE):
                emit_ask(f"streamsnow deploy-safety: {pat['reason']}")
                return 0
        except re.error:
            continue

    # 2) Warehouse writes (destructive SQL via a warehouse CLI, incl. -f / stdin).
    cli = invokes_warehouse(dequoted)
    if cli:
        sql_text, unscannable = referenced_sql(command, cwd)
        scan_text = dequoted + _dequote(sql_text)
        m = DESTRUCTIVE_SQL.search(scan_text)
        if m:
            verb = m.group(1).upper()
            emit_ask(
                f"streamsnow deploy-safety: this `{cli}` command contains a destructive SQL "
                f"statement ({verb}). Show the exact SQL and target environment, and proceed "
                f"only on explicit approval."
            )
            return 0
        if unscannable:
            emit_ask(
                f"streamsnow deploy-safety: this `{cli}` command runs a SQL file too large to scan "
                f"({_MAX_SCAN_BYTES} byte cap). Confirm it contains no destructive statements before running."
            )
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
