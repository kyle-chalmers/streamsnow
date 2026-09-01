"""Tests for hooks/review_gate_stop.py — the plugin's Stop-hook wrapper.

The load-bearing property: the gate must work on a PLUGIN-ONLY install (no
``streamsnow`` pip package anywhere) because the marketplace clones the repo
and the wrapper executes the self-contained gate by path. Every test here
runs the wrapper as a subprocess with a stripped environment to prove that.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "hooks" / "review_gate_stop.py"

#: A bare interpreter environment: no venv, no pip-installed streamsnow.
_BARE_ENV = {"PATH": "/usr/bin:/bin", "CLAUDE_PLUGIN_ROOT": str(REPO_ROOT)}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _make_streamsnow_repo(root: Path) -> Path:
    app = root / "apps" / "acme-sales-dashboard"
    (app / "pages").mkdir(parents=True)
    (app / "pages" / "overview.py").write_text("import streamlit as st\nst.metric('Revenue', 1)\n")
    (root / "streamsnow.config.yaml").write_text("project:\n  name: Acme\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    return root


def _run_wrapper(payload: dict | str, cwd: Path, tmpdir: Path) -> subprocess.CompletedProcess:
    raw = json.dumps(payload) if isinstance(payload, dict) else payload
    return subprocess.run(
        [sys.executable, str(WRAPPER)],
        input=raw,
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**_BARE_ENV, "TMPDIR": str(tmpdir)},
        timeout=30,
    )


def test_plugin_only_install_emits_nudge_for_unreviewed_change(tmp_path: Path) -> None:
    repo = _make_streamsnow_repo(tmp_path / "repo")
    page = repo / "apps" / "acme-sales-dashboard" / "pages" / "overview.py"
    page.write_text("import streamlit as st\nst.metric('Orders', 2)\n")
    proc = _run_wrapper({"cwd": str(repo), "session_id": "hook-s1"}, repo, tmp_path / "st")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert "/review-app" in out["systemMessage"]
    # The measured lesson, enforced at the wrapper level too: never
    # additionalContext from a Stop hook.
    assert "hookSpecificOutput" not in out


def test_silent_outside_a_streamsnow_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    _git(plain, "init", "-q", "-b", "main")
    proc = _run_wrapper({"cwd": str(plain)}, plain, tmp_path / "st")
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_silent_on_clean_repo(tmp_path: Path) -> None:
    repo = _make_streamsnow_repo(tmp_path / "repo")
    proc = _run_wrapper({"cwd": str(repo), "session_id": "hook-s2"}, repo, tmp_path / "st")
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_fail_open_on_garbage_stdin(tmp_path: Path) -> None:
    repo = _make_streamsnow_repo(tmp_path / "repo")
    proc = _run_wrapper("{{{not json", repo, tmp_path / "st")
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_fail_open_when_gate_file_missing(tmp_path: Path) -> None:
    # A plugin checkout missing the gate (partial clone, future refactor)
    # must be a silent no-op, never a broken session.
    fake_root = tmp_path / "plugin"
    (fake_root / "hooks").mkdir(parents=True)
    wrapper_copy = fake_root / "hooks" / "review_gate_stop.py"
    wrapper_copy.write_text(WRAPPER.read_text())
    proc = subprocess.run(
        [sys.executable, str(wrapper_copy)],
        input="{}",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "CLAUDE_PLUGIN_ROOT": str(fake_root)},
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_hooks_json_declares_stop_with_timeout() -> None:
    data = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text())
    stop = data["hooks"]["Stop"]
    entry = stop[0]["hooks"][0]
    assert "review_gate_stop.py" in entry["command"]
    assert "${CLAUDE_PLUGIN_ROOT}" in entry["command"]
    assert entry["timeout"] == 10
    # Every declared hook keeps an explicit timeout (a hung hook stalls turns).
    for event_entries in data["hooks"].values():
        for matcher_block in event_entries:
            for h in matcher_block["hooks"]:
                assert h.get("timeout"), h
