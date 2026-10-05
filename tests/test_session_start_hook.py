"""The SessionStart hook: one line where it helps, silence everywhere else."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

# The hook is a bash script for now; on native Windows it is replaced by a
# Python hook that the native-Windows hook work tests on every OS.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash hook; POSIX only for now")

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "hooks" / "session_start.sh"
PLUGIN_VERSION = json.loads(
    (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
)["version"]


def _run(project_dir: Path, *, with_cli: bool) -> tuple[str, float]:
    env = {
        "PATH": "/usr/bin:/bin",  # no streamsnow on PATH unless we add a fake one
        "CLAUDE_PROJECT_DIR": str(project_dir),
        "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT),
        "GIT_CONFIG_NOSYSTEM": "1",  # a system core.hooksPath must not move the hook
    }
    if with_cli:
        fake_bin = project_dir / "fakebin"
        fake_bin.mkdir(exist_ok=True)
        (fake_bin / "streamsnow").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(fake_bin / "streamsnow", 0o755)
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
    started = time.monotonic()
    proc = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout, time.monotonic() - started


def test_silent_outside_streamlit_repos(tmp_path):
    out, elapsed = _run(tmp_path, with_cli=False)
    assert out == ""
    assert elapsed < 5


def test_apps_without_config_get_the_adopt_nudge(tmp_path):
    (tmp_path / "apps" / "x").mkdir(parents=True)
    (tmp_path / "apps" / "x" / "streamlit_app.py").write_text(
        "import streamlit\n", encoding="utf-8"
    )
    out, _ = _run(tmp_path, with_cli=False)
    assert out.count("\n") == 1
    assert "/onboard" in out and "/build-app --setup" not in out and PLUGIN_VERSION in out
    assert "uv tool install streamsnow" in out  # CLI missing → say so


def test_configured_repo_banner_names_version_guards_and_skills(tmp_path):
    (tmp_path / "streamsnow.config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    out, _ = _run(tmp_path, with_cli=True)
    assert out.count("\n") == 1
    assert PLUGIN_VERSION in out
    assert "guard is ACTIVE" in out and "/build-app" in out and "/ship-app" in out
    assert "Key guard is ACTIVE" in out
    assert "CLI not on PATH" not in out  # fake streamsnow on PATH → no nag
    assert len(out) < 700  # a banner, not an essay


_GIT = shutil.which("git", path="/usr/bin:/bin")
needs_git = pytest.mark.skipif(_GIT is None, reason="git not on /usr/bin:/bin")
# A developer's global git config (init.templateDir, core.hooksPath) could plant
# or redirect the hook these tests look for; keep it out.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(*args: str) -> None:
    subprocess.run([_GIT or "git", *args], check=True, env=_GIT_ENV)


def test_plugin_enabled_without_config_gets_the_onboard_nudge(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        '{"enabledPlugins": {"streamsnow@streamsnow": true}}', encoding="utf-8"
    )
    out, _ = _run(tmp_path, with_cli=True)
    assert out.count("\n") == 1
    assert "isn't set up yet: run /onboard" in out


@needs_git
def test_clone_without_pre_commit_hook_gets_the_nudge(tmp_path):
    _git("init", "-q", str(tmp_path))
    (tmp_path / "streamsnow.config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    out, _ = _run(tmp_path, with_cli=True)
    assert "no pre-commit hook): run /onboard" in out
    assert len(out) < 700


@needs_git
def test_clone_with_pre_commit_hook_gets_no_nudge(tmp_path):
    _git("init", "-q", str(tmp_path))
    (tmp_path / "streamsnow.config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (tmp_path / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    out, _ = _run(tmp_path, with_cli=True)
    assert "pre-commit hook" not in out


@needs_git
def test_worktree_uses_the_main_repos_hook(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", str(main))
    (main / "streamsnow.config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    _git("-C", str(main), "add", "-A")
    _git("-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    (main / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    wt = tmp_path / "wt"
    _git("-C", str(main), "worktree", "add", "-q", str(wt))
    out, _ = _run(wt, with_cli=True)
    assert "pre-commit hook" not in out


def test_configured_banner_lists_onboard(tmp_path):
    (tmp_path / "streamsnow.config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    out, _ = _run(tmp_path, with_cli=True)
    assert "/onboard" in out


def test_disabled_plugin_gets_no_nudge(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        '{"enabledPlugins": {"streamsnow@streamsnow": false}}', encoding="utf-8"
    )
    out, _ = _run(tmp_path, with_cli=True)
    assert out == ""
