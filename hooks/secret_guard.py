#!/usr/bin/env python3
"""StreamSnow key guard (PreToolUse): Claude's tools never reach the CI key directory.

`streamsnow ci-key create` keeps the CI user's private key and the deploy
secret files in ~/.streamsnow-ci, and `streamsnow ci-key push` moves them to
GitHub on gh's stdin, so nothing needs to read them. This hook denies any tool
call that names that directory or the key file, except a plain
`streamsnow ci-key ...` or `streamsnow deploy-setup ...` command (deploy-setup
reads the PUBLIC key via --public-key-file).

A backstop, not a sandbox: matching command text can be dodged on purpose
(shell variables, globs), and it only runs inside Claude Code. Not repo-gated:
the key is equally sensitive in any repo. Stdlib only, no network. Any error
after a match still denies (fail closed on a match); everything else passes.
"""

from __future__ import annotations

import json
import re
import sys

PROTECTED = re.compile(r"\.streamsnow-ci|streamsnow_ci_rsa_key", re.IGNORECASE)
SHELL_TOOLS = {"Bash", "PowerShell"}
REASON = (
    "streamsnow key guard: the CI key directory (~/.streamsnow-ci) is off-limits to "
    "Claude's tools. Use `streamsnow ci-key create` and `streamsnow ci-key push`; "
    "secret values never need to be read."
)

# The program: streamsnow or streamsnow.exe, bare or as a path, quoted or not.
_PROGRAM = (
    r"""(?:'(?:[^']*[\\/])?streamsnow(?:\.exe)?'"""
    r'''|"(?:[^"]*[\\/])?streamsnow(?:\.exe)?"'''
    r"""|(?:[^\s'"]*[\\/])?streamsnow(?:\.exe)?)"""
)
_ALLOWED = re.compile(rf"^\s*(?:&\s*)?{_PROGRAM}\s+(?:ci-key|deploy-setup)(?:\s|$)", re.IGNORECASE)
# Anything that chains or substitutes. A single LEADING `&` is PowerShell's
# call operator and is stripped before this check; any other `&` counts.
_CHAINING = re.compile(r"[|;&<\n\r`]|\$\(")


def protected(text: str) -> bool:
    return bool(PROTECTED.search(text))


def allowed_command(command: str) -> bool:
    if not _ALLOWED.match(command):
        return False
    rest = re.sub(r"^\s*&\s*", "", command, count=1)
    return not _CHAINING.search(rest)


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _emit_deny() -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": REASON,
                }
            }
        )
    )


def main() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except ValueError:
        if protected(raw):
            _emit_deny()
        return 0
    if not isinstance(payload, dict):
        return 0
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool in SHELL_TOOLS:
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        if not protected(command):
            return 0
        try:
            if allowed_command(command):
                return 0
        except Exception:  # noqa: BLE001  fail closed on a match
            pass
        _emit_deny()
        return 0
    if any(protected(s) for s in _strings(tool_input)):
        _emit_deny()
    return 0


if __name__ == "__main__":
    sys.exit(main())
