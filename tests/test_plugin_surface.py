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
    "build-app",
    "onboard",
    "review-app",
    "sql-review",
    "preview-app",
    "validate-app",
    "ship-app",
    "migrate-app",
}

# Retired names -> the surface that replaced them. The v0.2 alias stubs were
# removed in 0.7.3, /start-app became /build-app in 0.8.0, and after 0.8.0
# /feedback-app became /build-app --feedback and /sql-review replaced
# /audit-lineage; the names stay here so nothing
# re-introduces them.
RETIRED_NAMES = {
    "new-app": "/build-app",
    "refine-requirements": "/build-app --spec",
    "add-page": "/build-app",
    "apply-review": "/review-app --fix",
    "auto-review-app": "/review-app --auto",
    "deep-dive-data": "/sql-review",
    "start-app": "/build-app",
    "feedback-app": "/build-app --feedback",
    "audit-lineage": "/sql-review",
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


WALKTHROUGH = SKILLS_DIR / "_shared" / "playwright-walkthrough.md"
_CLI_PIN = re.compile(r"@playwright/cli@([0-9A-Za-z.\-]+)")


def test_walkthrough_pins_the_playwright_cli():
    """Four skills walk the running app in a browser with the Playwright CLI.
    An exact pin (never @latest, never a prerelease) keeps a new upstream release
    from changing the walk under a released plugin. The recipe is the only place
    the version is written; RELEASING.md owns the bump."""
    pins = set(_CLI_PIN.findall(WALKTHROUGH.read_text(encoding="utf-8")))
    assert len(pins) == 1, pins
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.pop())
    releasing = (REPO_ROOT / "RELEASING.md").read_text(encoding="utf-8")
    assert "pin" in releasing.lower() and "@playwright/cli" in releasing
    # The plugin no longer bundles an MCP server.
    assert not (REPO_ROOT / ".mcp.json").exists()


def test_no_other_file_names_a_playwright_version_or_the_old_mcp():
    """One pin, one browser path: nothing else names a CLI version, and nothing
    still points at the retired Playwright MCP. docs/superpowers/ holds other
    work's design notes and is left alone."""
    files = [
        *SKILLS_DIR.rglob("*.md"),
        REPO_ROOT / "README.md",
        *(REPO_ROOT / "docs").rglob("*.md"),
    ]
    offenders = []
    for path in files:
        if path == WALKTHROUGH or "superpowers" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        versions = [v for v in _CLI_PIN.findall(text) if v not in {"<version>", "<that"}]
        if versions or "@playwright/mcp" in text or "browser_navigate" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_walkthrough_degrade_line_points_at_onboard():
    text = WALKTHROUGH.read_text(encoding="utf-8")
    assert "run `/onboard`" in text
    assert "/reload-plugins" not in text
    assert "restart the session" not in text


def test_readme_trust_section_matches_the_code():
    """The README's secrets promises name real mechanisms, so they can't outlive the code."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index("## What Claude can and can't see")
    section = readme[start : readme.index("\n## ", start + 1)]
    assert "streamsnow ci-key push" in section
    assert "hooks/secret_guard.py" in section
    assert "SECURITY.md#how-streamsnow-handles-secrets" in section
    assert (REPO_ROOT / "hooks" / "secret_guard.py").is_file()
    assert readme.index("## What Claude can and can't see") < readme.index(
        "## Install with your coding agent"
    )
    hooks = readme[readme.index("## Hooks, in full") :]
    assert any(
        line.startswith("|") and "hooks/secret_guard.py" in line for line in hooks.splitlines()
    )
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "## How StreamSnow handles secrets" in security


AGENTS_DIR = REPO_ROOT / "agents"


def test_plugin_agents_are_the_sql_review_briefs_word_for_word():
    """Claude Code runs the reviewers as plugin agents; other agent tools follow
    the same briefs from skills/sql-review/reviewers/, the only copy
    `agent-skills install` ships. One text, two places: a test keeps them equal."""
    briefs = sorted((SKILLS_DIR / "sql-review" / "reviewers").glob("*.md"))
    agents = sorted(AGENTS_DIR.glob("*.md"))
    assert [f"sql-review-{b.name}" for b in briefs] == [a.name for a in agents]
    for brief, agent in zip(briefs, agents, strict=True):
        text = agent.read_text(encoding="utf-8")
        assert text.startswith("---\n"), agent.name
        _, front, body = text.split("---\n", 2)
        assert re.search(rf"^name: {re.escape(agent.stem)}$", front, re.M), agent.name
        assert re.search(r"^description: .{40,}", front, re.M), agent.name
        assert re.search(r"^tools: [A-Za-z, ]+$", front, re.M), agent.name
        assert set(re.findall(r"^([a-z-]+):", front, re.M)) == {"name", "description", "tools"}
        assert body.lstrip("\n") == brief.read_text(encoding="utf-8"), agent.name
        # No relative links: the agents/ copy has no neighbours to point at.
        assert not _LINK_RE.search(body), agent.name


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_build_app_checkpoint_3_has_the_user_type_ship_app():
    """/ship-app is human-only (disable-model-invocation), so a skill that says
    "run /ship-app" sends the agent at a command it cannot start."""
    skill = _flat((SKILLS_DIR / "build-app" / "SKILL.md").read_text(encoding="utf-8"))
    cp3 = skill[skill.index("**CP3:**") :]
    assert "ask the user to type `/ship-app <slug>`" in cp3
    assert "the agent cannot start it" in cp3
    feedback = _flat((SKILLS_DIR / "build-app" / "feedback.md").read_text(encoding="utf-8"))
    assert "ask the user to type `/ship-app <slug>`" in feedback
    assert "cannot start it" in feedback


def test_preview_app_pins_the_ci_roles_data_reads_not_the_deployed_viewer_role():
    """The viewer role has no data grants by default, so "preview as the deployed
    viewer role" yields empty pages. Every doc names the CI role's data reads."""
    text = _flat((SKILLS_DIR / "preview-app" / "SKILL.md").read_text(encoding="utf-8"))
    assert "deployed viewer role" not in text
    assert text.count("data reads match the CI role's") >= 2  # step 4 and "Done when"
    setup = _flat((SKILLS_DIR / "onboard" / "setup.md").read_text(encoding="utf-8"))
    assert "deployed viewer role" not in setup
    assert "what people viewing an app, and local preview, run as" not in setup


def test_ship_app_cleans_up_after_merge_and_checks_merged_before_deleting():
    ship = SKILLS_DIR / "ship-app"
    skill = (ship / "SKILL.md").read_text(encoding="utf-8")
    assert "## After merge" in skill
    assert "(after-merge.md)" in skill
    steps = _flat((ship / "after-merge.md").read_text(encoding="utf-8"))
    order = [
        "git status --short",  # clean tree first
        "gh pr view <num> --json state,headRefOid",
        "must read `MERGED`",
        "git rev-parse <branch>",  # tip check, before anything is asked or deleted
        "git ls-remote --heads origin <branch>",
        "git log --oneline <headRefOid>..<branch>",
        "Ask the user once",
        "git switch main",
        "git pull --ff-only",
        "git branch -D <branch>",
        "git push origin --delete <branch>",
        "git fetch --prune",
    ]
    positions = [steps.index(token) for token in order]
    assert positions == sorted(positions), "after-merge steps are out of order"
    # A branch with commits made after the merge must survive: both tips are compared to the
    # PR's merged head, and the mismatch path deletes nothing.
    tip_check = steps[
        steps.index("Check the branch holds nothing newer") : steps.index("Ask the user once")
    ]
    assert tip_check.count("headRefOid") >= 2
    assert "On any mismatch, stop and delete nothing" in tip_check
    # The remote tip is read from the exact ref line: a suffix match also hits
    # refs/heads/x/<branch>.
    assert "exactly `refs/heads/<branch>`" in tip_check and "first field" in tip_check
    assert "unmerged work" in tip_check
    assert steps.index("headRefOid") < steps.index("git branch -D <branch>")
    assert steps.index("git ls-remote --heads origin <branch>") < steps.index(
        "git push origin --delete <branch>"
    )
    assert "a squash merge" in steps and "`git branch -d` refuses" in steps
    # Both triggers are named, and the PR-open outcome says what "done" means.
    assert "step 4" in steps and "watch" in steps
    assert "never merges" in _flat(skill)
    assert "spent branch removed" in _flat(skill)


def test_ship_app_asks_before_cleanup_only_without_a_merge_approval_in_this_run():
    """A merge the user approved in this run already says the branch is done, so
    the cleanup runs and is reported; with no approval it asks once. The clean
    tree, MERGED and tip checks precede `-D` on both paths."""
    ship = SKILLS_DIR / "ship-app"
    steps = _flat((ship / "after-merge.md").read_text(encoding="utf-8"))
    assert "Ask the user once, only when there was no approval in this run" in steps
    assert "explicitly approved in this conversation (not a GitHub review approval)" in steps
    assert "the PR merging" in steps and "merged it themselves and said so" in steps
    assert "Restore branch" in steps and "reflog" in steps  # why skipping the question is safe
    assert "Cleaned up: deleted `<branch>` locally" in steps
    shared = steps.index("Steps 1 to 3 run on both paths")
    assert shared < steps.index("git branch -D <branch>")
    for check in ("git status --short", "must read `MERGED`", "git rev-parse <branch>"):
        assert steps.index(check) < steps.index("Ask the user once")
    assert "never merges" in steps


def test_ship_app_cleans_up_a_spent_branch_after_the_work_moved():
    """Step 1 needs app changes, so the step-4 trigger always found a dirty tree
    or a moved tip and could never finish. The cleanup waits until the work is
    committed on the fresh branch, and a stop in it does not end the ship."""
    ship = SKILLS_DIR / "ship-app"
    skill = _flat((ship / "SKILL.md").read_text(encoding="utf-8"))
    steps = _flat((ship / "after-merge.md").read_text(encoding="utf-8"))
    step4 = skill[skill.index("**Branch hygiene.**") : skill.index("**Stage only the app:**")]
    assert "after step 6" in step4 and "never ends the ship" in step4
    assert "after `/ship-app` step 6 has committed the work" in steps
    assert "continue the ship at `/ship-app` step 7" in steps
    assert "stays on the new branch" in steps
    assert "`<branch>` in every step below is the spent branch, not the current one" in steps


def test_migrate_app_finishes_the_starter_documents_like_build_app():
    """/build-app rewrites the app AGENTS.md starter lines and adds the README Apps
    row; a migrated app skipped both, so validate-app's starter-text check warned."""
    skill = _flat((SKILLS_DIR / "migrate-app" / "SKILL.md").read_text(encoding="utf-8"))
    step = skill[skill.index("10. Check `snowflake.yml`") : skill.index("11. **Verify:**")]
    assert "starter Pages and Queries lines" in step
    assert "README Apps row" in step and "`apps/<slug>/`" in step
    assert "`starter-text`" in step


def test_ship_app_hands_over_the_app_link_after_a_green_deploy():
    """CI cannot load the page (key-pair sign-in), so the ship ends with the user
    clicking through it from the link `streamsnow app-url` prints."""
    skill = _flat((SKILLS_DIR / "ship-app" / "SKILL.md").read_text(encoding="utf-8"))
    merged = skill[skill.index("**Merged**") : skill.index("## After merge")]
    assert "streamsnow app-url <slug>" in merged and "(click-through.md)" in merged
    ref = _flat((SKILLS_DIR / "ship-app" / "click-through.md").read_text(encoding="utf-8"))
    for item in ("open every page", "change each filter once", "match what preview showed"):
        assert item in ref
    assert "Projects » Streamlit" in ref and "Exit 2" in ref


def test_ship_app_resaves_review_state_after_a_review_first_pass():
    skill = _flat((SKILLS_DIR / "ship-app" / "SKILL.md").read_text(encoding="utf-8"))
    gate = skill[skill.index("**Preflight 0") : skill.index("**Hard gate:**")]
    assert "review first" in gate
    assert "re-run these saves" in gate


def test_ship_app_has_a_fallback_when_the_host_forbids_polling_ci():
    skill = _flat((SKILLS_DIR / "ship-app" / "SKILL.md").read_text(encoding="utf-8"))
    assert "forbids polling CI" in skill
    assert "gh pr checks <num> --watch" in skill
    assert "PR open, checks pending" in skill
    assert skill.count("PR open, checks pending") >= 2  # the step and "Done when"


def test_ship_app_always_watches_the_deploy_run():
    """#103: the host's no-polling rule covers PR checks it tracks itself. Stretched to the
    post-merge deploy run, which no host tracks, it left the deploy outcome unreported."""
    skill = _flat((SKILLS_DIR / "ship-app" / "SKILL.md").read_text(encoding="utf-8"))
    step = skill[skill.index("11. **") : skill.index("## Reporting the outcome")]
    assert "gh run watch" in step and "--exit-status" in step
    assert "PR checks only" in step
    done = skill[skill.index("## Done when") :]
    assert "Polling PR checks not allowed" in done


def test_walkthrough_screenshots_the_whole_page_from_its_top():
    """#99: Streamlit scrolls inside its own container, so `--full-page` captured the
    window, not the page: a page taller than the window lost its bottom, and a container
    left scrolled showed the middle. Reset the inner scroll, grow the window to the
    content, and wait for the script run, not just the first caption."""
    recipe = (SKILLS_DIR / "_shared" / "playwright-walkthrough.md").read_text(encoding="utf-8")
    walk = _flat(recipe[recipe.index("## Steps") : recipe.index("## Output contract")])
    assert "screenshot --full-page" not in recipe
    assert "e.scrollTop = 0" in walk and "setViewportSize" in walk
    assert walk.index("data-test-script-state=notRunning") < walk.index("P S screenshot")


def test_skills_say_who_applies_app_data_ddl():
    """#79: the deploy job applies app-data DDL. "A human applies DDL" without that
    qualifier would send an agent to ask for a manual apply the deploy already does."""
    for rel in (
        "sql-review/SKILL.md",
        "sql-review/authoring.md",
        "sql-review/reviewers/object.md",
    ):
        text = _flat((SKILLS_DIR / rel).read_text(encoding="utf-8"))
        assert "streamsnow objects-sql" in text or "the deploy job applies" in text, rel
        assert "outside app data" in text, rel
    gotchas = _flat((SKILLS_DIR / "_shared" / "production-gotchas.md").read_text(encoding="utf-8"))
    assert "CREATE OR REPLACE DYNAMIC TABLE" in gotchas
    assert "CREATE OR ALTER TABLE" in gotchas
    assert "separate deliberate manual step" not in gotchas
    authoring = _flat((SKILLS_DIR / "sql-review" / "authoring.md").read_text(encoding="utf-8"))
    assert "reason: performance" in authoring and "shared_logic" in authoring
    assert "CREATE OR REPLACE VIEW ... COPY GRANTS" in authoring
    assert authoring.index("CREATE OR REPLACE VIEW") < authoring.index("CREATE OR ALTER VIEW")
