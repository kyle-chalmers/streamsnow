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


def test_page_builder_runs_and_returns_the_sql_tokens_check():
    """`sql-tokens` flags a {TOKEN} inside a SQL comment, and a query header is a comment."""
    sections = _sections("page-builder")
    assert "streamsnow check sql-tokens apps/<slug>" in sections["Steps"]
    block = re.search(r"```json\n(.*?)```", sections["Returns"], flags=re.DOTALL)
    assert "sql-tokens" in json.loads(block.group(1))["checks"]
    assert "five checks" in sections["Verify"]


def test_query_header_template_names_tokens_without_braces():
    pages = (BRIEFS.parent / "pages.md").read_text(encoding="utf-8")
    header = [line for line in pages.splitlines() if line.strip().startswith("-- Tokens:")]
    assert header, "pages.md has no query header template"
    for line in header:
        assert "{" not in line and "TOKEN_NAME" in line, line


def test_form_rule_is_conditional_everywhere_it_is_stated():
    """An `st.form` costs the reader an Apply click; over cached data it buys nothing."""
    skills = BRIEFS.parents[1]
    perf = " ".join(
        (skills / "_shared" / "streamlit-performance.md").read_text(encoding="utf-8").split()
    )
    dims = " ".join((skills / "review-app" / "dimensions.md").read_text(encoding="utf-8").split())
    brief = " ".join(_sections("perf-reviewer")["Steps"].split())
    assert "only when each rerun is expensive" in perf
    assert "only when each rerun is expensive" in dims
    assert "when each rerun is expensive" in brief
    for text in (perf, dims, brief):
        assert "plain widgets are fine" in text and "nice-to-have" in text
