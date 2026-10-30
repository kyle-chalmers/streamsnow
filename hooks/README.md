# StreamSnow hooks

`hooks.json` wires these up; Claude Code loads it automatically. The notes that used to sit in
a `"//"` key inside it live here, because the Claude plugin directory warns on any key a hooks
file does not document.

- **SessionStart** (`session_start.py`) prints a one-line skill pointer. It is Python like the
  others, so it runs on native Windows without bash, and because the directory's validator holds
  for review a hook shell script it cannot follow into the programs it runs.
- **PreToolUse** (`deploy_safety.py`) pauses destructive Streamlit and SQL commands.
- **PreToolUse** (`secret_guard.py`) denies Claude's tools access to the CI key directory
  (`~/.streamsnow-ci`). Unlike the others it is not repo-gated, and it denies rather than asks.
- **Stop** (`review_gate_stop.py`) nudges, warn-only and as a system message only, when a
  substantive app change ends unreviewed.

All but the key guard are repo-gated (on `streamsnow.config.yaml`) and fail open; all are
stdlib-only with no network. Each declares an explicit timeout, so a hung hook can never stall
a session.

All four hooks launch through `uv run --no-project --no-config --frozen --offline
--no-python-downloads --quiet ...; exit 0`. `--frozen` does nothing beside `--no-project`, but
the Claude plugin directory requires it on every `uv run`. That one string parses in sh, Git
Bash and PowerShell (Claude Code's hook shells), needs no `python3` on PATH (Windows often has
none, or only the Store stub), uses no network, never touches the user's project environment,
and exits 0 on any launcher failure, because exit 2 from a PreToolUse hook blocks the tool call.
`tests/test_hook_commands.py` runs these strings through every shell.

What each hook does, for users, is in the README's
[Hooks, in full](../README.md#hooks-in-full).
