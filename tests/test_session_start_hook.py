"""The SessionStart hook: one line where it helps, silence everywhere else."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "session_start.sh"
PLUGIN_VERSION = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]


def _run(project_dir: Path, *, with_cli: bool) -> tuple[str, float]:
    env = {
        "PATH": "/usr/bin:/bin",  # no streamsnow on PATH unless we add a fake one
        "CLAUDE_PROJECT_DIR": str(project_dir),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
    }
    if with_cli:
        fake_bin = project_dir / "fakebin"
        fake_bin.mkdir(exist_ok=True)
        (fake_bin / "streamsnow").write_text("#!/bin/sh\nexit 0\n")
        os.chmod(fake_bin / "streamsnow", 0o755)
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
    started = time.monotonic()
    proc = subprocess.run(["bash", str(HOOK)], capture_output=True, text=True, env=env, check=True)
    return proc.stdout, time.monotonic() - started


def test_silent_outside_streamlit_repos(tmp_path):
    out, elapsed = _run(tmp_path, with_cli=False)
    assert out == ""
    assert elapsed < 5


def test_apps_without_config_get_the_adopt_nudge(tmp_path):
    (tmp_path / "apps" / "x").mkdir(parents=True)
    (tmp_path / "apps" / "x" / "streamlit_app.py").write_text("import streamlit\n")
    out, _ = _run(tmp_path, with_cli=False)
    assert out.count("\n") == 1
    assert "/start-app --setup" in out and PLUGIN_VERSION in out
    assert "uv tool install streamsnow" in out  # CLI missing → say so


def test_configured_repo_banner_names_version_guards_and_skills(tmp_path):
    (tmp_path / "streamsnow.config.yaml").write_text("schema_version: 1\n")
    out, _ = _run(tmp_path, with_cli=True)
    assert out.count("\n") == 1
    assert PLUGIN_VERSION in out
    assert "guard is ACTIVE" in out and "/start-app" in out and "/ship-app" in out
    assert "CLI not on PATH" not in out  # fake streamsnow on PATH → no nag
    assert len(out) < 700  # a banner, not an essay
