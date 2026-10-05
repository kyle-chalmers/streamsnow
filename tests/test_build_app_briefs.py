"""The /build-app subagent briefs are contracts, so their shape is pinned.

The orchestrator dispatches each brief to a subagent (or, without subagents, follows it
itself) and trusts three things: the brief says what it reads, which files it may write,
and the exact result it returns. A brief missing one of those, a page-level brief that
claims a shared file, or a reviewer that writes anything would break the "one owner per
file" rule the parallel build depends on (skills/build-app/pages.md, Parallel build).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIEFS = REPO_ROOT / "skills" / "build-app" / "briefs"
ROLES = ("data-scout", "app-designer", "page-builder", "perf-reviewer", "viz-critic", "cold-reader")
SECTIONS = ("Role", "Inputs", "Owns", "Steps", "Returns", "Verify", "Degrade")
READ_ONLY = ("data-scout", "app-designer", "perf-reviewer", "viz-critic", "cold-reader")
# Files only the orchestrator writes: every page shares them.
SHARED = ("streamlit_app.py", "sql_review/", "pages/_", "pages/about.py", "REQUIREMENTS.md")


def _sections(role: str) -> dict[str, str]:
    text = (BRIEFS / f"{role}.md").read_text(encoding="utf-8")
    parts = re.split(r"^## (\w+)\s*$", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_briefs_dir_holds_exactly_the_six_roles():
    assert sorted(p.stem for p in BRIEFS.glob("*.md")) == sorted(ROLES)


@pytest.mark.parametrize("role", ROLES)
def test_every_brief_has_the_contract_sections_in_order(role):
    assert tuple(_sections(role)) == SECTIONS


@pytest.mark.parametrize("role", ROLES)
def test_returns_is_a_parseable_json_shape(role):
    block = re.search(r"```json\n(.*?)```", _sections(role)["Returns"], flags=re.DOTALL)
    assert block, f"{role}: Returns needs a fenced json shape"
    json.loads(block.group(1))


@pytest.mark.parametrize("role", READ_ONLY)
def test_read_only_roles_own_nothing(role):
    assert _sections(role)["Owns"].strip().startswith("Nothing")


def test_page_builder_owns_only_its_page_and_its_queries():
    owns = _sections("page-builder")["Owns"]
    claimed = re.findall(r"^- `([^`]+)`", owns, flags=re.MULTILINE)
    assert claimed == ["apps/<slug>/pages/<page>.py", "apps/<slug>/queries/<name>.sql"]
    never = owns.split("Never edit:", 1)[1]
    for shared in SHARED:
        assert shared in never, f"page-builder must name {shared} as off-limits"


def test_skill_dispatches_every_brief():
    skill_dir = REPO_ROOT / "skills" / "build-app"
    text = "".join(p.read_text(encoding="utf-8") for p in skill_dir.glob("*.md"))
    for role in ROLES:
        assert f"briefs/{role}.md" in text, f"no phase dispatches {role}"
