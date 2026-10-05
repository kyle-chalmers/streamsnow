#!/usr/bin/env python3
"""StreamSnow key guard (PreToolUse): Claude's tools never reach the CI key directory.

`streamsnow ci-key create` keeps the CI user's private key and the deploy
secret files in ~/.streamsnow-ci, and `streamsnow ci-key push` moves them to
GitHub on gh's stdin, so nothing needs to read them. This hook denies any tool
call whose command or file location names that directory or the key file,
except a plain `streamsnow ci-key ...` or `streamsnow deploy-setup ...` command
(deploy-setup reads the PUBLIC key via --public-key-file).

A backstop, not a sandbox: matching command text can be dodged on purpose
(shell variables, globs, a recursive search started above the directory, a
program written in streamsnow's place), and it only runs inside Claude Code. Not repo-gated:
the key is equally sensitive in any repo. Stdlib only, no network. Any error
after a match still denies (fail closed on a match); everything else passes.
If the launcher cannot start this hook (no uv), the guard is off for that call;
`streamsnow ci-key push`, which keeps secrets off argv and out of Claude's
context, remains the main protection.
"""

from __future__ import annotations

import json
import re
import sys

PROTECTED = re.compile(r"\.streamsnow-ci|streamsnow_ci_rsa_key", re.IGNORECASE)
SHELL_TOOLS = {"Bash", "PowerShell"}
# The inputs that say WHERE a file tool reads, writes or searches. Only these
# are checked, so Claude can still write or search docs that merely mention the
# key directory (README, SECURITY.md, setup.md). Grep's `pattern` is the text
# it looks for, not a location, so it is left out; Glob's `pattern` is a path.
# A tool not listed here has every string checked.
PATH_KEYS = {
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Grep": ("path", "glob"),
    "Glob": ("pattern", "path"),
}
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
# Anything that chains, substitutes or groups: no allowed form needs it. `(` and
# `)` cover bash process substitution (`>(cmd)`) and PowerShell subexpressions
# (`(cmd)`, `@(cmd)`). A single LEADING `&` is PowerShell's call operator and is
# stripped before this check; any other `&` counts.
_CHAINING = re.compile(r"[|;&<()\n\r`]|\$\(")
# A `>` redirect whose target names the key directory would truncate key
# material. A protected path BEFORE the `>` (the public key as an argument) is
# fine; one anywhere AFTER a `>` is not, so a quoted target with spaces
# (`> "C:\Users\A B\.streamsnow-ci\..."`) can't slip past.
_REDIRECT_INTO_PROTECTED = re.compile(
    r">.*?(?:\.streamsnow-ci|streamsnow_ci_rsa_key)", re.IGNORECASE | re.DOTALL
)


def protected(text: str) -> bool:
    return bool(PROTECTED.search(text))


def allowed_command(command: str) -> bool:
    if not _ALLOWED.match(command):
        return False
    rest = re.sub(r"^\s*&\s*", "", command, count=1)
    return not _CHAINING.search(rest) and not _REDIRECT_INTO_PROTECTED.search(rest)


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


def _decide(payload: object, raw: str) -> bool:
    """True to deny. Raises on malformed input; the caller fails closed on a match."""
    if not isinstance(payload, dict):
        return protected(raw)
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return protected(raw)
    if isinstance(tool, str) and tool in SHELL_TOOLS:
        command = tool_input.get("command", "")
        if not isinstance(command, str):
            return protected(raw)
        if not protected(command):
            return False
        return not allowed_command(command)
    if isinstance(tool, str) and tool in PATH_KEYS:
        locations = [tool_input.get(k) for k in PATH_KEYS[tool]]
        return any(protected(s) for v in locations for s in _strings(v))
    return any(protected(s) for s in _strings(tool_input))


def main() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    try:
        deny = _decide(json.loads(raw), raw)
    except Exception:  # noqa: BLE001  fail closed on a match, open otherwise
        deny = protected(raw)
    if deny:
        _emit_deny()
    return 0


if __name__ == "__main__":
    sys.exit(main())
