"""The hooks.json command strings, run exactly as Claude Code runs them.

Claude Code runs a shell-form hook with ``sh -c`` on macOS and Linux, Git Bash
on Windows, or PowerShell when Git Bash is not installed. One command string
must therefore work in all of them, and PowerShell 5.1 has no ``||``. The
launcher is ``uv run ...; exit 0``:

- ``uv`` is a real executable on every OS, and StreamSnow already requires it;
  ``python3`` on Windows is often missing or the Microsoft Store stub.
- ``; exit 0`` parses in sh, bash and both PowerShells, and makes every failure
  (uv missing, no Python found) exit 0. A PreToolUse hook that exits 2 BLOCKS
  the tool call, and uv exits 2 on its own errors, so without it a machine with
  no Python would have every shell command blocked.

Each test substitutes ``${CLAUDE_PLUGIN_ROOT}`` the way Claude Code does (a
forward-slash path, textually) and runs the string through every shell present
on this machine. The Windows CI rows are what exercise Git Bash and PowerShell.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
PYTHON_HOOK_EVENTS = ("PreToolUse", "Stop")


def _command(event: str) -> str:
    return HOOKS[event][0]["hooks"][0]["command"]


def _git_bash() -> str | None:
    """Git for Windows' bash, never System32's bash.exe (the WSL launcher)."""
    git = shutil.which("git")
    if not git:
        return None
    for root in Path(git).resolve().parents:
        candidate = root / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _shells() -> list[tuple[str, list[str]]]:
    """(name, argv prefix) for each shell Claude Code may hand a hook to."""
    found: list[tuple[str, list[str]]] = []
    if os.name == "nt":
        if bash := _git_bash():
            found.append(("git-bash", [bash, "-c"]))
        for name in ("pwsh", "powershell"):
            if exe := shutil.which(name):
                found.append((name, [exe, "-NoProfile", "-NonInteractive", "-Command"]))
    else:
        for name in ("sh", "bash"):
            if exe := shutil.which(name):
                found.append((name, [exe, "-c"]))
        if exe := shutil.which("pwsh"):  # opt-in on macOS/Linux, but possible
            found.append(("pwsh", [exe, "-NoProfile", "-NonInteractive", "-Command"]))
    return found


SHELLS = _shells()
SHELL_IDS = [name for name, _ in SHELLS]
shells = pytest.mark.parametrize("shell", [argv for _, argv in SHELLS], ids=SHELL_IDS)


def _run(
    shell: list[str], event: str, payload: dict, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    command = _command(event).replace("${CLAUDE_PLUGIN_ROOT}", REPO_ROOT.as_posix())
    return subprocess.run(
        [*shell, command],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


def _project(tmp_path: Path) -> Path:
    (tmp_path / "streamsnow.config.yaml").write_text("snowflake: {}\n", encoding="utf-8")
    return tmp_path


def _payload(project: Path, command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(project)}


def _env_without_uv() -> dict[str, str]:
    """This environment with every PATH entry that holds a uv executable removed."""
    env = dict(os.environ)
    keep = [
        p
        for p in env.get("PATH", "").split(os.pathsep)
        if p and not any((Path(p) / n).exists() for n in ("uv", "uv.exe"))
    ]
    env["PATH"] = os.pathsep.join(keep)
    return env


def test_every_python_hook_uses_the_cross_shell_launcher() -> None:
    for event in PYTHON_HOOK_EVENTS:
        command = _command(event)
        assert command.startswith("uv run --no-project "), command
        for flag in ("--no-config", "--offline", "--no-python-downloads", "--quiet"):
            assert flag in command, (flag, command)
        assert command.rstrip().endswith("; exit 0"), command
        assert "||" not in command and "&&" not in command, "PowerShell 5.1 parses neither"


def test_this_machine_has_a_shell_to_test_with() -> None:
    assert SHELLS, "no sh/bash/PowerShell found; the launcher went untested"
    if os.name == "nt":
        # A missing shell would silently drop its parametrized cases; on Windows
        # every shell Claude Code can hand a hook to must actually be exercised.
        assert {"git-bash", "pwsh", "powershell"} <= set(SHELL_IDS), SHELL_IDS


@shells
def test_deploy_guard_asks_through_every_shell(shell: list[str], tmp_path: Path) -> None:
    proc = _run(shell, "PreToolUse", _payload(_project(tmp_path), "snow streamlit deploy my_app"))
    assert proc.returncode == 0, proc.stderr
    decision = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
    assert decision == "ask", proc.stdout


@shells
def test_deploy_guard_passes_read_only_through_every_shell(
    shell: list[str], tmp_path: Path
) -> None:
    proc = _run(shell, "PreToolUse", _payload(_project(tmp_path), "snow streamlit list"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


@shells
@pytest.mark.parametrize("event", PYTHON_HOOK_EVENTS)
def test_hook_fails_open_when_uv_is_missing(shell: list[str], event: str, tmp_path: Path) -> None:
    payload = _payload(_project(tmp_path), "snow streamlit deploy my_app")
    proc = _run(shell, event, payload, env=_env_without_uv())
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert proc.stdout.strip() == ""


@shells
@pytest.mark.parametrize("event", PYTHON_HOOK_EVENTS)
def test_hook_fails_open_when_no_python_is_found(
    shell: list[str], event: str, tmp_path: Path
) -> None:
    """uv exits 2 when it cannot find an interpreter offline; exit 2 from a
    PreToolUse hook would block every shell command in the session."""
    env = dict(os.environ, UV_PYTHON="3.99")  # a version nothing has installed
    payload = _payload(_project(tmp_path), "snow streamlit deploy my_app")
    proc = _run(shell, event, payload, env=env)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert proc.stdout.strip() == ""


def test_shell_discovery_never_picks_the_wsl_launcher() -> None:
    """System32\\bash.exe is the WSL launcher, which would run the hook inside
    Linux. (Windows PowerShell 5.1 legitimately lives under System32.)"""
    for name, argv in SHELLS:
        if "bash" in name:
            assert "system32" not in argv[0].lower(), argv[0]
