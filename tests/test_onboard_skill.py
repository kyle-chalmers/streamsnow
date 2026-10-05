"""Skill-text contracts for /onboard and the /start-app handoff: the flow's promises, pinned."""

from __future__ import annotations

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
