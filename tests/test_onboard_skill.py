"""Skill-text contracts for /onboard and the /start-app handoff: the flow's promises, pinned."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS = REPO_ROOT / "skills"


def _read(*parts: str) -> str:
    return SKILLS.joinpath(*parts).read_text(encoding="utf-8")


def test_start_app_has_no_setup_or_adopt_mode():
    start = _read("start-app", "SKILL.md")
    assert "--setup" not in start
    assert "adopt" not in start.split("## Phase 0")[0]
    assert 'argument-hint: "[<idea>] | --spec"' in start


def test_start_app_hands_off_to_onboard_including_when_the_cli_is_missing():
    phase0 = _read("start-app", "SKILL.md").split("## Phase 0")[1].split("## Phase 1")[0]
    assert "[/onboard](../onboard/SKILL.md)" in phase0
    assert "isn't set up yet" in phase0
    assert "not on PATH" in phase0  # a missing CLI is a handoff, not a dead end


SETUP = _read("onboard", "setup.md")
SKILL = _read("onboard", "SKILL.md")


def test_secrets_are_set_only_through_ci_key_push():
    assert "streamsnow ci-key push" in SETUP
    assert not re.search(r"gh secret set \S+ <", SETUP), "secrets must go through ci-key push"
    assert "--body" not in SETUP


def test_claude_never_runs_the_admin_sql():
    assert "Never run the admin SQL" in SKILL
    assert re.search(r"never runs? (the|that) (admin )?(SQL|file)", SETUP, re.IGNORECASE)
    assert "pbcopy" in SETUP and "Snowsight" in SETUP


def test_admin_file_is_written_where_git_ignores_it():
    assert "> .internal/admin-setup.sql" in SETUP
    assert ".internal/" in SKILL


def test_admin_check_says_not_confirmed_never_missing():
    assert "not confirmed" in SETUP and 'never "missing"' in SETUP


def test_save_the_key_note_survives_the_move():
    assert "password manager" in SETUP


def test_sections_the_front_page_links_exist():
    for heading in ("### 1c ·", "## 2d · Snowflake admin setup", "## 2e · Shared repo settings"):
        assert heading in SETUP, heading


def test_connection_is_offered_both_ways():
    assert "set it up for you" in SETUP and "guide you" in SETUP


def test_access_inventory_covers_the_quiet_sources():
    for source in (
        "SNOWFLAKE_DEFAULT_CONNECTION_NAME",
        "~/.snowsql/config",
        "not connected",
        "variable names only",
    ):
        assert source in SETUP, source


def test_installs_are_one_batched_approval():
    assert "one batched approval" in SETUP.lower()
    assert "Never batch installs" not in SETUP


def test_no_stale_cross_references():
    assert "/start-app --setup" not in SETUP
    assert "belongs to §2d" not in SETUP  # ci-secrets moved to §2e
    assert "Setup mode" not in SETUP and "this skill's default mode" not in SETUP
