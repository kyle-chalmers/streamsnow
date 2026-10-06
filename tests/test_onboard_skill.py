"""Skill-text contracts for /onboard and the /build-app handoff: the flow's promises, pinned."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS = REPO_ROOT / "skills"


def _read(*parts: str) -> str:
    return SKILLS.joinpath(*parts).read_text(encoding="utf-8")


def test_build_app_has_no_setup_or_adopt_mode():
    start = _read("build-app", "SKILL.md")
    assert "--setup" not in start
    assert "adopt" not in start.split("## Phase 0")[0]
    assert 'argument-hint: "[<idea>] | --spec | <slug> --feedback \\"<feedback>\\""' in start


def test_build_app_hands_off_to_onboard_including_when_the_cli_is_missing():
    phase0 = _read("build-app", "SKILL.md").split("## Phase 0")[1].split("## Phase 1")[0]
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


def test_scaffold_installs_the_app_packages():
    scaffold = " ".join(_read("build-app", "scaffold.md").split())
    assert "Install the app's packages for local preview" in scaffold
    assert "/start-app --setup" not in _read("build-app", "spec.md")


def test_a_missing_key_never_means_a_new_key():
    flat = " ".join(SETUP.split())
    assert "never run `ci-key create` on your own" in flat


def test_token_users_never_paste_the_token():
    assert "--token-file-path" in SETUP


def test_walkthrough_cd_runs_in_a_subshell():
    walk = _read("_shared", "playwright-walkthrough.md")
    assert "(cd D && " in walk and "`cd D &&" not in walk


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_every_setup_answer_is_asked_even_when_detected():
    flat = _flat(SETUP)
    assert "Ask every answer, one question each" in flat
    assert 'one "yes" confirms every' not in flat
    assert "marked **found**" not in flat and "**needs you**" not in flat
    skill = _flat(SKILL)
    assert "Detection never counts as confirmation" in skill
    assert "marked found or needs you" not in skill


def test_question_rule_works_without_a_question_tool():
    skill = _flat(SKILL)
    assert "otherwise ask in chat and wait for an explicit answer" in skill
    assert "AskUserQuestion" in skill  # Claude Code's own tool is still named
    assert "Ask with `AskUserQuestion`, batched" not in skill


def test_defaults_are_explained_in_bullets_before_the_keep_or_change_question():
    flat = _flat(SETUP)
    explain = flat.index("explain the defaults the wizard did not ask about")
    ask = flat.index("then ask one question: keep them, or change some")
    assert explain < ask
    for default in ("**Warehouse**", "**CI role and viewer role**", "**App database and schema**"):
        assert default in SETUP, default


def test_admin_script_is_explained_before_who_runs_it_is_asked():
    section = SETUP.split("## 2d ·")[1].split("## 2e ·")[0]
    flat = _flat(section)
    explain = flat.index("Explain, then ask")
    ask = flat.index("Has your Snowflake admin run the StreamSnow setup script")
    assert explain < ask
    for obj in ("**Warehouse**", "**CI role**", "**Viewer role**", "**CI service user**"):
        assert obj in section, obj
    assert "do not link it" not in flat and "do not restate it" not in flat


def test_claude_adds_no_commentary_about_the_admin_file():
    section = _flat(SETUP.split("## 2d ·")[1].split("## 2e ·")[0])
    assert "do not open or quote the admin file" in section
    assert "do not add your own warnings about its contents" in section
    assert "CREATE OR REPLACE" not in SETUP


def test_docs_do_not_promise_bulk_confirmation():
    for path in (REPO_ROOT / "README.md", REPO_ROOT / "docs" / "getting-started.md"):
        text = _flat(path.read_text(encoding="utf-8"))
        assert "ask me only what you could not settle" not in text, path.name
        assert "Clickable choices for what it could not" not in text, path.name
        assert "pre-answers" not in text, path.name


def test_onboard_asks_which_roles_get_the_viewer_role():
    section = _flat(SETUP.split("## 2d ·")[1].split("## 2e ·")[0])
    assert "CURRENT_AVAILABLE_ROLES()" in section
    assert "--viewer-role <ROLE>" in section
    ask = section.index("Which of your roles get access")
    admin_file = section.index("**The admin file.**")
    assert ask < admin_file  # asked before the script is written, so it carries the grants
