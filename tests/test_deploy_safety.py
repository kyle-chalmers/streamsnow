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

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "deploy_safety.py"


def _run_guard(command: str, project_dir: Path) -> str:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(project_dir)})
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env={"CLAUDE_PROJECT_DIR": str(project_dir), "PATH": ""},
    )
    assert proc.returncode == 0, f"guard crashed: {proc.stderr}"
    return proc.stdout.strip()


def _asks(out: str) -> bool:
    return bool(out) and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def _project(tmp_path: Path) -> Path:
    (tmp_path / "streamsnow.config.yaml").write_text("snowflake:\n  database: ANALYTICS_DB\n")
    return tmp_path


def test_guard_asks_on_streamlit_deploy(tmp_path):
    assert _asks(_run_guard("snow streamlit deploy --replace my_app", _project(tmp_path)))


def test_guard_asks_on_create_or_replace_streamlit(tmp_path):
    assert _asks(_run_guard(
        'snow sql -q "CREATE OR REPLACE STREAMLIT my_app ROOT_LOCATION = @stage"', _project(tmp_path)
    ))


def test_guard_asks_on_drop_and_alter_streamlit(tmp_path):
    assert _asks(_run_guard('snow sql -q "DROP STREAMLIT my_app"', _project(tmp_path)))
    assert _asks(_run_guard('snow sql -q "ALTER STREAMLIT my_app ADD LIVE VERSION FROM LAST"', _project(tmp_path)))


def test_guard_asks_on_stage_remove(tmp_path):
    assert _asks(_run_guard('snow sql -q "REMOVE @app_stage/my_app"', _project(tmp_path)))


def test_guard_asks_on_destructive_sql(tmp_path):
    assert _asks(_run_guard('snow sql -q "DELETE FROM t WHERE x=1"', _project(tmp_path)))


def test_guard_asks_on_sql_hidden_in_file(tmp_path):
    project = _project(tmp_path)
    (project / "deploy.sql").write_text("CREATE OR REPLACE STREAMLIT my_app ROOT_LOCATION = @stage;\n")
    assert _asks(_run_guard("snow sql -f deploy.sql", project))


def test_guard_defends_quote_and_path_evasion(tmp_path):
    assert _asks(_run_guard("sn'ow' streamlit deploy my_app", _project(tmp_path)))
    assert _asks(_run_guard('/usr/local/bin/snow sql -q "DROP STREAMLIT my_app"', _project(tmp_path)))


def test_guard_passes_read_only(tmp_path):
    assert _run_guard('snow sql -q "SELECT 1"', _project(tmp_path)) == ""
    assert _run_guard("snow streamlit list", _project(tmp_path)) == ""
    assert _run_guard("streamlit run app.py", _project(tmp_path)) == ""


def test_guard_is_zero_cost_without_config(tmp_path):
    # No streamsnow.config.yaml present -> guard does nothing, even for a deploy.
    assert _run_guard("snow streamlit deploy my_app", tmp_path) == ""


def test_guard_fails_open_on_garbage_stdin():
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_hooks_json_registers_the_guard_with_a_timeout():
    hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    pre = hooks["PreToolUse"][0]
    assert pre["matcher"] == "Bash"
    entry = pre["hooks"][0]
    assert "deploy_safety.py" in entry["command"]
    assert entry.get("timeout"), "PreToolUse guard must declare an explicit timeout"
    session = hooks["SessionStart"][0]["hooks"][0]
    assert session.get("timeout"), "SessionStart hook must declare an explicit timeout"


def test_session_start_announces_the_guard():
    # jobwright's lesson: an invisible safety net reads as no safety net.
    text = (REPO_ROOT / "hooks" / "session_start.sh").read_text()
    assert "guard is ACTIVE" in text
