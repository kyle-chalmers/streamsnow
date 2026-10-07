"""The PreToolUse key guard: Claude's tools never reach the CI key directory."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "secret_guard.py"


def _run(tool_name: str, tool_input: dict) -> str:
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input, "cwd": "/tmp"})
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload.encode("utf-8"), capture_output=True
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.decode("utf-8").strip()


def _denied(out: str) -> bool:
    return bool(out) and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "tool,tool_input",
    [
        ("Read", {"file_path": "~/.streamsnow-ci/streamsnow_ci_rsa_key.p8"}),
        ("Grep", {"pattern": "PRIVATE", "path": "~/.streamsnow-ci"}),
        ("Glob", {"pattern": "**/streamsnow_ci_rsa_key*"}),
        (
            "Edit",
            {
                "file_path": "~/.streamsnow-ci/secrets/SNOWFLAKE_ACCOUNT",
                "old_string": "a",
                "new_string": "b",
            },
        ),
        ("Grep", {"pattern": "PRIVATE", "glob": "~/.streamsnow-ci/**"}),
        ("Write", {"file_path": "C:\\Users\\X\\.streamsnow-ci\\secrets\\X", "content": "x"}),
        ("NotebookEdit", {"notebook_path": "~/.streamsnow-ci/n.ipynb", "new_source": "x"}),
        ("SomeNewTool", {"anything": "~/.streamsnow-ci/streamsnow_ci_rsa_key.p8"}),
        ("Bash", {"command": "cat ~/.streamsnow-ci/streamsnow_ci_rsa_key.p8"}),
        (
            "Bash",
            {"command": "streamsnow ci-key push && cat ~/.streamsnow-ci/secrets/SNOWFLAKE_ACCOUNT"},
        ),
        ("Bash", {"command": "streamsnow ci-key push\ncat ~/.streamsnow-ci/x"}),
        (
            "PowerShell",
            {"command": "Get-Content $env:USERPROFILE\\.streamsnow-ci\\streamsnow_ci_rsa_key.p8"},
        ),
        (
            "PowerShell",
            {"command": "Get-Content C:\\Users\\X\\.STREAMSNOW-CI\\secrets\\SNOWFLAKE_USER"},
        ),
        ("PowerShell", {"command": "& cat ~/.streamsnow-ci/streamsnow_ci_rsa_key.p8"}),
        (
            "PowerShell",
            {
                "command": "& streamsnow.exe ci-key push; cat ~/.streamsnow-ci/secrets/SNOWFLAKE_USER"
            },
        ),
        ("Bash", {"command": "streamsnow ci-key create --dir $(echo ~/.streamsnow-ci)"}),
        (
            "Bash",
            {"command": "streamsnow ci-key push >(cat ~/.streamsnow-ci/secrets/SNOWFLAKE_ACCOUNT)"},
        ),
        (
            "PowerShell",
            {
                "command": "streamsnow ci-key push (Get-Content ~/.streamsnow-ci/secrets/SNOWFLAKE_ACCOUNT)"
            },
        ),
        (
            "PowerShell",
            {"command": "streamsnow ci-key push @(gc ~/.streamsnow-ci/secrets/SNOWFLAKE_ACCOUNT)"},
        ),
        (
            "Bash",
            {"command": "streamsnow ci-key push > ~/.streamsnow-ci/secrets/SNOWFLAKE_ACCOUNT"},
        ),
        (
            "PowerShell",
            {
                "command": 'streamsnow ci-key create > "C:\\Users\\John Smith\\.streamsnow-ci\\'
                'streamsnow_ci_rsa_key.p8"'
            },
        ),
        (
            "Bash",
            {
                "command": "streamsnow deploy-setup 1> '/home/a b/.streamsnow-ci/secrets/SNOWFLAKE_USER'"
            },
        ),
        (
            "Bash",
            {
                "command": "streamsnow deploy-setup >/home/a\\ b/.streamsnow-ci/secrets/SNOWFLAKE_ROLE"
            },
        ),
    ],
)
def test_denied(tool, tool_input):
    assert _denied(_run(tool, tool_input))


@pytest.mark.parametrize(
    "tool,tool_input",
    [
        ("Bash", {"command": "streamsnow ci-key create"}),
        ("Bash", {"command": "streamsnow ci-key push --dir ~/.streamsnow-ci"}),
        ("Bash", {"command": "streamsnow ci-key verify --dir ~/.streamsnow-ci"}),
        (
            "Bash",
            {
                "command": "streamsnow deploy-setup --admin --public-key-file "
                "~/.streamsnow-ci/streamsnow_ci_rsa_key.pub > admin-setup.sql"
            },
        ),
        ("PowerShell", {"command": "streamsnow.exe ci-key push"}),
        (
            "PowerShell",
            {
                "command": "& 'C:\\Users\\x\\.local\\bin\\streamsnow.exe' deploy-setup --admin "
                "--public-key-file C:\\Users\\x\\.streamsnow-ci\\streamsnow_ci_rsa_key.pub "
                "> admin-setup.sql"
            },
        ),
        (
            "Bash",
            {
                "command": "streamsnow ci-key push",
                "description": "Push secrets from ~/.streamsnow-ci",
            },
        ),
        ("Bash", {"command": "ls -la"}),
        ("Read", {"file_path": "/repo/README.md"}),
        # Docs that only mention the key directory stay editable and searchable.
        (
            "Edit",
            {
                "file_path": "/repo/SECURITY.md",
                "old_string": "the key directory",
                "new_string": "the key directory (~/.streamsnow-ci)",
            },
        ),
        ("Write", {"file_path": "/repo/docs/x.md", "content": "Keys live in ~/.streamsnow-ci."}),
        ("Grep", {"pattern": "streamsnow-ci|streamsnow_ci_rsa_key", "path": "/repo"}),
    ],
)
def test_allowed(tool, tool_input):
    assert _run(tool, tool_input) == ""


def test_non_ascii_payload_decodes():
    assert _run("Read", {"file_path": "/repo/café/README.md"}) == ""
    assert _denied(_run("Read", {"file_path": "/home/zoë/.streamsnow-ci/x"}))


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Bash", "tool_input": {"command": ["cat", "~/.streamsnow-ci/x"]}},
        {"tool_name": "Bash", "tool_input": {"command": None, "note": "~/.streamsnow-ci/x"}},
        {"tool_name": ["Read"], "tool_input": {"file_path": "~/.streamsnow-ci/x"}},
        {"tool_name": {"a": 1}, "tool_input": {"file_path": "~/.streamsnow-ci/x"}},
        ["Read", "~/.streamsnow-ci/x"],
        "~/.streamsnow-ci/x",
        {"tool_name": "Bash", "tool_input": "cat ~/.streamsnow-ci/x"},
        {"tool_name": "PowerShell", "tool_input": ["cat", "~/.streamsnow-ci/x"]},
    ],
)
def test_malformed_payload_naming_the_key_dir_is_denied(payload):
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload).encode("utf-8"), capture_output=True
    )
    assert proc.returncode == 0, proc.stderr
    assert _denied(proc.stdout.decode("utf-8").strip())


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Bash", "tool_input": {"command": ["ls", "-la"]}},
        {"tool_name": ["Read"], "tool_input": {"file_path": "/repo/README.md"}},
        ["Read", "/repo/README.md"],
        "just a string",
        {"tool_name": "Bash", "tool_input": "ls"},
        None,
    ],
)
def test_malformed_payload_not_naming_the_key_dir_passes(payload):
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload).encode("utf-8"), capture_output=True
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode("utf-8").strip() == ""


def test_unparseable_payload_naming_the_key_dir_is_denied():
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=b'{"tool_input": ~/.streamsnow-ci', capture_output=True
    )
    assert proc.returncode == 0
    assert _denied(proc.stdout.decode("utf-8").strip())


def test_internal_error_after_a_match_still_denies(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("secret_guard", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def boom(command):
        raise RuntimeError("bug")

    monkeypatch.setattr(mod, "allowed_command", boom)
    payload = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "streamsnow ci-key push --dir ~/.streamsnow-ci"},
        }
    )
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")), encoding="utf-8")
    )
    assert mod.main() == 0
    assert _denied(capsys.readouterr().out.strip())


def test_hooks_json_registers_the_guard_for_every_file_and_shell_tool():
    hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    entries = [e for e in hooks["PreToolUse"] if "secret_guard.py" in e["hooks"][0]["command"]]
    assert len(entries) == 1
    matcher = set(entries[0]["matcher"].split("|"))
    assert {
        "Bash",
        "PowerShell",
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
        "NotebookEdit",
    } <= matcher
