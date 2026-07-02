"""The v0.3 plugin-surface contract: 8 skills, ≤80-line front pages, 8 alias stubs.

The CHANGELOG and README advertise this surface; these tests keep it honest so
drift (an 81-line SKILL.md, a dropped stub, a resurrected old name) fails CI
instead of shipping.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
COMMANDS_DIR = REPO_ROOT / "commands"

EXPECTED_SKILLS = {
    "start-app",
    "review-app",
    "audit-lineage",
    "feedback-app",
    "preview-app",
    "validate-app",
    "ship-app",
    "migrate-app",
}

# Retired v0.2 name -> the surface that replaced it (stubs must point there).
EXPECTED_STUBS = {
    "new-app": "/start-app",
    "refine-requirements": "/start-app --spec",
    "add-page": "/start-app",
    "onboard": "/start-app --setup",
    "apply-review": "/review-app --fix",
    "auto-review-app": "/review-app --auto",
    "sql-review": "/review-app --sql",
    "deep-dive-data": "/audit-lineage",
}

_LINK_RE = re.compile(r"\]\(([^)#]+\.md)\)")


def test_skills_dir_holds_exactly_the_advertised_surface():
    dirs = {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()}
    assert dirs == EXPECTED_SKILLS | {"_shared"}


def test_every_skill_front_page_is_at_most_80_lines():
    over = {
        p.parent.name: len(p.read_text().splitlines())
        for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))
        if len(p.read_text().splitlines()) > 80
    }
    assert not over, f"SKILL.md over the 80-line cap: {over}"


def test_every_skill_has_matching_frontmatter_name_and_a_description():
    for skill in sorted(EXPECTED_SKILLS):
        text = (SKILLS_DIR / skill / "SKILL.md").read_text()
        assert re.search(rf"^name: {re.escape(skill)}$", text, re.M), skill
        assert re.search(r"^description: .{40,}", text, re.M), skill


def test_alias_stubs_exist_and_point_at_their_replacements():
    stubs = {p.stem for p in COMMANDS_DIR.glob("*.md")}
    assert stubs == set(EXPECTED_STUBS)
    for old, new in EXPECTED_STUBS.items():
        text = (COMMANDS_DIR / f"{old}.md").read_text()
        assert "Deprecated" in text, old
        assert new in text, f"{old} stub must point at {new}"


def test_no_retired_skill_name_is_referenced_as_live_inside_skills():
    # Old slash-names may appear in commands/ stubs, docs, and the CHANGELOG —
    # but a /old-name inside skills/ is a dangling reference.
    retired = "|".join(re.escape(s) for s in EXPECTED_STUBS)
    pattern = re.compile(rf"/(?:{retired})\b")
    offenders = [
        f"{p.relative_to(REPO_ROOT)}: {m.group(0)}"
        for p in sorted(SKILLS_DIR.rglob("*.md"))
        for m in [pattern.search(p.read_text())]
        if m
    ]
    assert not offenders, offenders


def test_every_relative_markdown_link_in_skills_resolves():
    broken = []
    for p in sorted(SKILLS_DIR.rglob("*.md")):
        for target in _LINK_RE.findall(p.read_text()):
            if target.startswith(("http://", "https://")):
                continue
            if not (p.parent / target).resolve().exists():
                broken.append(f"{p.relative_to(REPO_ROOT)} -> {target}")
    assert not broken, broken
