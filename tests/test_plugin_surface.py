"""The v0.3 plugin-surface contract: 8 skills, ≤80-line front pages, 8 alias stubs.

The CHANGELOG and README advertise this surface; these tests keep it honest so
drift (an 81-line SKILL.md, a dropped stub, a resurrected old name) fails CI
instead of shipping.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
COMMANDS_DIR = REPO_ROOT / "commands"
PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"

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


def _body_lines(text: str) -> int:
    """Lines after the closing frontmatter fence — the cap protects front-page
    brevity; frontmatter grew for parity (argument-hint, allowed-tools) and
    shouldn't force cutting instructions to compensate."""
    parts = text.split("---\n", 2)
    body = parts[2] if len(parts) == 3 and text.startswith("---\n") else text
    return len(body.splitlines())


def test_every_skill_front_page_body_is_at_most_80_lines():
    over = {
        p.parent.name: _body_lines(p.read_text())
        for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))
        if _body_lines(p.read_text()) > 80
    }
    assert not over, f"SKILL.md body over the 80-line cap: {over}"


def test_every_skill_has_matching_frontmatter_name_and_a_description():
    for skill in sorted(EXPECTED_SKILLS):
        text = (SKILLS_DIR / skill / "SKILL.md").read_text()
        assert re.search(rf"^name: {re.escape(skill)}$", text, re.M), skill
        assert re.search(r"^description: .{40,}", text, re.M), skill


# Human-initiated only: shipping and migrating are decisions, not chores the
# model should start on its own (ticketwright/jobwright convention).
HUMAN_ONLY_SKILLS = {"ship-app", "migrate-app"}


def test_every_skill_declares_argument_hint_and_allowed_tools():
    # jobwright-parity frontmatter: discoverable arguments + pre-approved tools
    # (fewer permission prompts is a first-class adoption concern).
    for skill in sorted(EXPECTED_SKILLS):
        text = (SKILLS_DIR / skill / "SKILL.md").read_text()
        assert re.search(r"^argument-hint: .+", text, re.M), skill
        assert re.search(r"^allowed-tools: \[.+\]", text, re.M), skill
        has_flag = bool(re.search(r"^disable-model-invocation: true$", text, re.M))
        assert has_flag == (skill in HUMAN_ONLY_SKILLS), skill


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


def test_manifest_does_not_redeclare_the_auto_loaded_hooks_file():
    # Claude Code >=2.1 auto-loads hooks/hooks.json; a manifest "hooks" key
    # pointing at that same file is a duplicate declaration that aborts the
    # whole plugin ("Duplicate hooks file detected"). jobwright hit this in
    # the field (its v0.1.1 fix); this keeps it from coming back here.
    manifest = json.loads(PLUGIN_MANIFEST.read_text())
    assert "hooks" not in manifest, (
        'plugin.json must not declare "hooks" — hooks/hooks.json is '
        "auto-loaded, and redeclaring it fails the plugin on Claude Code >=2.1"
    )


def test_every_relative_markdown_link_in_skills_resolves():
    broken = []
    for p in sorted(SKILLS_DIR.rglob("*.md")):
        for target in _LINK_RE.findall(p.read_text()):
            if target.startswith(("http://", "https://")):
                continue
            if not (p.parent / target).resolve().exists():
                broken.append(f"{p.relative_to(REPO_ROOT)} -> {target}")
    assert not broken, broken
