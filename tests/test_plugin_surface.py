"""The plugin-surface contract: 8 skills, ≤80-line front pages, no alias stubs.

The CHANGELOG and README advertise this surface; these tests keep it honest so
drift (an 81-line SKILL.md, a resurrected alias or old name) fails CI
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

# Retired v0.2 names -> the surface that replaced them. The alias stubs were
# removed in 0.7.3; the names stay here so nothing re-introduces them.
RETIRED_NAMES = {
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
        p.parent.name: _body_lines(p.read_text(encoding="utf-8"))
        for p in sorted(SKILLS_DIR.glob("*/SKILL.md"))
        if _body_lines(p.read_text(encoding="utf-8")) > 80
    }
    assert not over, f"SKILL.md body over the 80-line cap: {over}"


def test_every_skill_has_matching_frontmatter_name_and_a_description():
    for skill in sorted(EXPECTED_SKILLS):
        text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
        assert re.search(rf"^name: {re.escape(skill)}$", text, re.M), skill
        assert re.search(r"^description: .{40,}", text, re.M), skill


# Human-initiated only: shipping and migrating are decisions, not chores the
# model should start on its own (ticketwright/jobwright convention).
HUMAN_ONLY_SKILLS = {"ship-app", "migrate-app"}


def test_every_skill_declares_argument_hint_and_allowed_tools():
    # jobwright-parity frontmatter: discoverable arguments + pre-approved tools
    # (fewer permission prompts is a first-class adoption concern).
    for skill in sorted(EXPECTED_SKILLS):
        text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
        assert re.search(r"^argument-hint: .+", text, re.M), skill
        assert re.search(r"^allowed-tools: \[.+\]", text, re.M), skill
        has_flag = bool(re.search(r"^disable-model-invocation: true$", text, re.M))
        assert has_flag == (skill in HUMAN_ONLY_SKILLS), skill


def test_no_alias_commands_ship_with_the_plugin():
    # Plugin commands show up in the `/` menu next to the skills; the retired
    # aliases cluttered it, so the plugin ships skills only.
    assert not COMMANDS_DIR.exists(), "commands/ was removed in 0.7.3; ship skills only"


def test_no_retired_skill_name_is_referenced_as_live_inside_skills():
    # Old slash-names may appear in docs and the CHANGELOG, but a /old-name
    # inside skills/ is a dangling reference.
    retired = "|".join(re.escape(s) for s in RETIRED_NAMES)
    pattern = re.compile(rf"/(?:{retired})\b")
    offenders = [
        f"{p.relative_to(REPO_ROOT)}: {m.group(0)}"
        for p in sorted(SKILLS_DIR.rglob("*.md"))
        for m in [pattern.search(p.read_text(encoding="utf-8"))]
        if m
    ]
    assert not offenders, offenders


def test_manifest_does_not_redeclare_the_auto_loaded_hooks_file():
    # Claude Code >=2.1 auto-loads hooks/hooks.json; a manifest "hooks" key
    # pointing at that same file is a duplicate declaration that aborts the
    # whole plugin ("Duplicate hooks file detected"). jobwright hit this in
    # the field (its v0.1.1 fix); this keeps it from coming back here.
    manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
    assert "hooks" not in manifest, (
        'plugin.json must not declare "hooks" — hooks/hooks.json is '
        "auto-loaded, and redeclaring it fails the plugin on Claude Code >=2.1"
    )


def test_every_relative_markdown_link_in_skills_resolves():
    broken = []
    for p in sorted(SKILLS_DIR.rglob("*.md")):
        for target in _LINK_RE.findall(p.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://")):
                continue
            if not (p.parent / target).resolve().exists():
                broken.append(f"{p.relative_to(REPO_ROOT)} -> {target}")
    assert not broken, broken


def test_every_skill_declares_its_repo_overlay_point():
    """The overlay convention only works if every skill actually reads its
    overlay — the line is load-bearing, not decoration."""
    import pathlib

    skills_dir = pathlib.Path(__file__).resolve().parent.parent / "skills"
    for skill in sorted(d for d in skills_dir.iterdir() if d.is_dir() and d.name != "_shared"):
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        assert f".streamsnow/overlays/{skill.name}.md" in text, (
            f"{skill.name}/SKILL.md does not declare its repo-overlay point"
        )
    assert (skills_dir / "_shared" / "overlays.md").is_file()


def test_plugin_bundles_a_pinned_playwright_mcp():
    """Five skills walk the running app in a browser through a Playwright MCP.
    Bundling it in the plugin means nobody has to configure it by hand; an exact
    pin (never @latest) keeps a new upstream release from changing the walk
    under a released plugin. RELEASING.md owns the bump."""
    servers = json.loads((REPO_ROOT / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    pw = servers["playwright"]
    assert pw["command"] == "npx"
    pkg = next(a for a in pw["args"] if a.startswith("@playwright/mcp"))
    assert re.fullmatch(r"@playwright/mcp@\d+\.\d+\.\d+", pkg), pkg
    assert "pin" in (REPO_ROOT / "RELEASING.md").read_text(encoding="utf-8").lower()
    assert "@playwright/mcp" in (REPO_ROOT / "RELEASING.md").read_text(encoding="utf-8")


def test_walkthrough_degrade_line_points_at_the_bundled_setup():
    text = (SKILLS_DIR / "_shared" / "playwright-walkthrough.md").read_text(encoding="utf-8")
    assert "/reload-plugins" in text
    assert "restart the session" not in text


def test_readme_trust_section_matches_the_code():
    """The README's secrets promises name real mechanisms, so they can't outlive the code."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("## What Claude can and can't see")
    section = readme[start : readme.index("\n## ", start + 1)]
    assert "streamsnow ci-key push" in section
    assert "hooks/secret_guard.py" in section
    assert "SECURITY.md" in section
    assert (REPO_ROOT / "hooks" / "secret_guard.py").is_file()
    assert readme.index("## What Claude can and can't see") < readme.index(
        "## Install with your coding agent"
    )
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "## How StreamSnow handles secrets" in security
