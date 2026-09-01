"""Tests for check_session_fallback's git-aware new-only mode.

The tree-wide semantics (broad-handler detection, noqa, handler coverage) are
covered in tests/test_validate.py; this file covers the baseline comparison,
the defensive ref fallback, and the CLI flags.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from streamsnow.tools import check_session_fallback

_UNWRAPPED = (
    "from snowflake.snowpark.context import get_active_session\nsession = get_active_session()\n"
)
_WRAPPED = (
    "import streamlit as st\n"
    "try:\n"
    "    from snowflake.snowpark.context import get_active_session\n"
    "    session = get_active_session()\n"
    "except Exception:\n"
    "    session = st.connection('snowflake').session()\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _repo_with_baseline(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dev@acme.test")
    _git(repo, "config", "user.name", "Acme Dev")
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def test_legacy_violation_not_flagged_against_baseline(tmp_path):
    repo = _repo_with_baseline(tmp_path, "acme-dashboards", {"apps/a/page.py": _UNWRAPPED})
    res = check_session_fallback.scan_paths([repo / "apps/a/page.py"], base_ref="main")
    assert res["ok"]
    assert res["findings"] == []


def test_new_violation_in_existing_file_flagged(tmp_path):
    repo = _repo_with_baseline(tmp_path, "acme-dashboards", {"apps/a/page.py": _WRAPPED})
    target = repo / "apps/a/page.py"
    target.write_text(_WRAPPED + "\nrefresh = get_active_session()\n")
    res = check_session_fallback.scan_paths([target], base_ref="main")
    assert not res["ok"]
    assert any("unwrapped" in f["detail"] for f in res["findings"])


def test_new_file_has_zero_baseline(tmp_path):
    repo = _repo_with_baseline(tmp_path, "acme-dashboards", {"apps/a/page.py": _WRAPPED})
    new = repo / "apps/a/extra.py"
    new.write_text(_UNWRAPPED)
    res = check_session_fallback.scan_paths([new], base_ref="main")
    assert not res["ok"]


def test_count_not_lines_is_the_gate(tmp_path):
    # Moving the one legacy violation to a different line is not "introducing" one.
    repo = _repo_with_baseline(tmp_path, "acme-dashboards", {"apps/a/page.py": _UNWRAPPED})
    target = repo / "apps/a/page.py"
    target.write_text("# revenue page header comment\n" + _UNWRAPPED)
    assert check_session_fallback.scan_paths([target], base_ref="main")["ok"]


def test_unresolvable_ref_falls_back_tree_wide_with_note(tmp_path):
    repo = _repo_with_baseline(tmp_path, "acme-dashboards", {"apps/a/page.py": _UNWRAPPED})
    res = check_session_fallback.scan_paths([repo / "apps/a/page.py"], base_ref="origin/main")
    assert not res["ok"]  # ref missing -> tree-wide, legacy call flagged
    assert any("origin/main" in n and "tree-wide" in n for n in res["notes"])


def test_outside_git_falls_back_tree_wide_with_note(tmp_path):
    p = tmp_path / "page.py"
    p.write_text(_UNWRAPPED)
    res = check_session_fallback.scan_paths([p], base_ref="main")
    assert not res["ok"]
    assert any("not in a git work tree" in n for n in res["notes"])


def test_scan_paths_default_stays_tree_wide(tmp_path):
    # Library callers (validate_app) keep the strict behavior unless they opt in.
    repo = _repo_with_baseline(tmp_path, "acme-dashboards", {"apps/a/page.py": _UNWRAPPED})
    assert not check_session_fallback.scan_paths([repo / "apps/a/page.py"])["ok"]


def test_main_default_is_new_only_and_all_restores_tree_wide(tmp_path, capsys, monkeypatch):
    repo = _repo_with_baseline(tmp_path, "acme-dashboards", {"apps/a/page.py": _UNWRAPPED})
    monkeypatch.chdir(repo)
    assert check_session_fallback.main(["apps", "--base-ref", "main"]) == 0
    assert "clean" in capsys.readouterr().out
    assert check_session_fallback.main(["apps", "--all"]) == 1
    assert "BLOCK" in capsys.readouterr().out


def test_main_json_includes_notes_on_fallback(tmp_path, capsys, monkeypatch):
    repo = _repo_with_baseline(tmp_path, "acme-dashboards", {"apps/a/page.py": _WRAPPED})
    monkeypatch.chdir(repo)
    # Default base ref origin/main doesn't exist in this fresh repo.
    assert check_session_fallback.main(["apps", "--format", "json"]) == 0
    out = capsys.readouterr().out
    assert '"notes"' in out and "origin/main" in out
