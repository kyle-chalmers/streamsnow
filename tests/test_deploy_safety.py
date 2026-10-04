"""The PreToolUse deploy-safety guard (hooks/deploy_safety.py), ported from jobwright.

Each test drives the hook as Claude Code does — a JSON payload on stdin — and
asserts on the emitted permission decision. The guard must be repo-gated
(zero-cost without streamsnow.config.yaml), fail-open (exit 0 always), and
resistant to shell-quote / full-path evasion.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from _portable import bare_env

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "deploy_safety.py"


def _run_guard(
    command: str, project_dir: Path, *, tool: str = "Bash", stdin_encoding: str | None = None
) -> str:
    payload = json.dumps(
        {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(project_dir)},
        ensure_ascii=False,
    )
    env = bare_env(CLAUDE_PROJECT_DIR=str(project_dir), PATH="")
    if stdin_encoding:
        env["PYTHONIOENCODING"] = stdin_encoding  # the Windows default for pipes
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload.encode("utf-8"),  # Claude Code always sends UTF-8
        capture_output=True,
        env=env,
    )
    proc = subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )
    assert proc.returncode == 0, f"guard crashed: {proc.stderr}"
    return proc.stdout.strip()


def _asks(out: str) -> bool:
    return bool(out) and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def _project(tmp_path: Path) -> Path:
    (tmp_path / "streamsnow.config.yaml").write_text(
        "snowflake:\n  database: ANALYTICS_DB\n", encoding="utf-8"
    )
    return tmp_path


def test_guard_asks_on_streamlit_deploy(tmp_path):
    assert _asks(_run_guard("snow streamlit deploy --replace my_app", _project(tmp_path)))


def test_guard_asks_on_create_or_replace_streamlit(tmp_path):
    assert _asks(
        _run_guard(
            'snow sql -q "CREATE OR REPLACE STREAMLIT my_app ROOT_LOCATION = @stage"',
            _project(tmp_path),
        )
    )


def test_guard_asks_on_drop_and_alter_streamlit(tmp_path):
    assert _asks(_run_guard('snow sql -q "DROP STREAMLIT my_app"', _project(tmp_path)))
    assert _asks(
        _run_guard(
            'snow sql -q "ALTER STREAMLIT my_app ADD LIVE VERSION FROM LAST"', _project(tmp_path)
        )
    )


def test_guard_asks_on_stage_remove(tmp_path):
    assert _asks(_run_guard('snow sql -q "REMOVE @app_stage/my_app"', _project(tmp_path)))


def test_guard_asks_on_destructive_sql(tmp_path):
    assert _asks(_run_guard('snow sql -q "DELETE FROM t WHERE x=1"', _project(tmp_path)))


def test_guard_asks_on_sql_hidden_in_file(tmp_path):
    project = _project(tmp_path)
    (project / "deploy.sql").write_text(
        "CREATE OR REPLACE STREAMLIT my_app ROOT_LOCATION = @stage;\n", encoding="utf-8"
    )
    assert _asks(_run_guard("snow sql -f deploy.sql", project))


def test_guard_defends_quote_and_path_evasion(tmp_path):
    assert _asks(_run_guard("sn'ow' streamlit deploy my_app", _project(tmp_path)))
    assert _asks(
        _run_guard('/usr/local/bin/snow sql -q "DROP STREAMLIT my_app"', _project(tmp_path))
    )


# --------------------------------------------------------------------------- #
# Native Windows: the PowerShell tool and Windows-shaped commands
# --------------------------------------------------------------------------- #
def test_guard_inspects_the_powershell_tool(tmp_path):
    """On Windows the PowerShell tool is on by default and is Claude's primary
    shell; a guard that reads only the Bash tool misses most commands there."""
    project = _project(tmp_path)
    assert _asks(_run_guard("snow streamlit deploy my_app", project, tool="PowerShell"))
    assert _asks(_run_guard('snow sql -q "DROP TABLE acme.t"', project, tool="PowerShell"))


def test_guard_ignores_tools_that_do_not_run_commands(tmp_path):
    assert _run_guard("snow streamlit deploy my_app", _project(tmp_path), tool="Read") == ""


def test_guard_catches_windows_executable_shapes(tmp_path):
    project = _project(tmp_path)
    for command in [
        "snow.exe streamlit deploy my_app",
        "SNOW.EXE streamlit deploy my_app",
        "C:\\tools\\snow.exe streamlit deploy my_app",
        "& 'C:\\Program Files\\Snowflake CLI\\snow.exe' sql -q \"DROP TABLE acme.t\"",
        "snow.cmd stage remove @app_stage/my_app",
        'C:\\tools\\snow.exe sql -q "DELETE FROM acme.t"',
    ]:
        assert _asks(_run_guard(command, project, tool="PowerShell")), command


def test_guard_strips_powershell_backtick_escapes(tmp_path):
    assert _asks(_run_guard("sn`ow streamlit deploy my_app", _project(tmp_path), tool="PowerShell"))


def test_guard_reads_sql_piped_from_get_content(tmp_path):
    """PowerShell has no `<` redirect; SQL reaches the CLI through a pipe."""
    project = _project(tmp_path)
    (project / "deploy.sql").write_text("DROP TABLE acme.orders;\n", encoding="utf-8")
    for command in [
        "Get-Content deploy.sql | snow sql --stdin",
        "gc deploy.sql | snow sql --stdin",
        "Get-Content -Path deploy.sql | snow.exe sql --stdin",
        "type deploy.sql | snow sql --stdin",
    ]:
        assert _asks(_run_guard(command, project, tool="PowerShell")), command


def test_guard_reads_a_utf8_payload_whatever_the_stdin_default(tmp_path):
    """Windows pipes default to cp1252; a UTF-8 payload with a character cp1252
    cannot decode (here "Ł") made the guard bail out silently."""
    out = _run_guard(
        'snow sql -q "DROP TABLE acme.ŁÓDŹ"', _project(tmp_path), stdin_encoding="cp1252"
    )
    assert _asks(out)


def test_guard_passes_windows_read_only(tmp_path):
    project = _project(tmp_path)
    for command in [
        "snow.exe --version",
        'snow.exe sql -q "SELECT 1"',
        "snowman --help",
        "Get-Content notes.txt",
        "Get-Content deploy.sql",  # reading a file is not running it
        "C:\\tools\\snowflake-report.exe --drop-cache",
    ]:
        (project / "deploy.sql").write_text("DROP TABLE acme.orders;\n", encoding="utf-8")
        assert _run_guard(command, project, tool="PowerShell") == "", command


def test_guard_passes_read_only(tmp_path):
    assert _run_guard('snow sql -q "SELECT 1"', _project(tmp_path)) == ""
    assert _run_guard("snow streamlit list", _project(tmp_path)) == ""
    assert _run_guard("streamlit run app.py", _project(tmp_path)) == ""


def test_guard_is_zero_cost_without_config(tmp_path):
    # No streamsnow.config.yaml present -> guard does nothing, even for a deploy.
    assert _run_guard("snow streamlit deploy my_app", tmp_path) == ""


def test_guard_fails_open_on_garbage_stdin():
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_hooks_json_registers_the_guard_with_a_timeout():
    hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    pre = hooks["PreToolUse"][0]
    assert set(pre["matcher"].split("|")) == {"Bash", "PowerShell"}
    entry = pre["hooks"][0]
    assert "deploy_safety.py" in entry["command"]
    assert entry.get("timeout"), "PreToolUse guard must declare an explicit timeout"
    session = hooks["SessionStart"][0]["hooks"][0]
    assert session.get("timeout"), "SessionStart hook must declare an explicit timeout"


def test_session_start_announces_the_guard():
    # jobwright's lesson: an invisible safety net reads as no safety net.
    text = (REPO_ROOT / "hooks" / "session_start.sh").read_text(encoding="utf-8")
    assert "guard is ACTIVE" in text
