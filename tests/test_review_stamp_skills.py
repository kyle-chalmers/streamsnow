"""Pins the skill text that makes a default /review-app pass stamp the gate (#21).

The tools only help if the skills call them in the right order: capture the
baseline before dispatch, write the report in the shape the parser reads, stamp
with ``--expect-baseline``, and have /ship-app save the review state before its
rebase rewrites the commit SHAs.
"""

from __future__ import annotations

import re
from pathlib import Path

from streamsnow.tools import review_loop as rl

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(*parts: str) -> str:
    return SKILLS_DIR.joinpath(*parts).read_text(encoding="utf-8")


def test_review_app_captures_baseline_before_dispatch_and_stamps_after_report() -> None:
    skill = _flat(_read("review-app", "SKILL.md"))
    baseline = skill.index("streamsnow review-gate baseline <slug>")
    dispatch = skill.index("Fan out the 5 reviewers")
    report = skill.index("Write the report")
    stamp = skill.index("streamsnow review-gate stamp <report> --slug <slug> --expect-baseline")
    assert baseline < dispatch < report < stamp
    assert "reviewed means reviewed, not clean" in skill
    assert "It never stamps the review gate." in skill  # --fix
    assert "(report-and-stamp.md)" in skill


def test_documented_report_sample_parses() -> None:
    """The heading format the skill tells reviewers to write must be what the
    parser reads; a drift here turns every report into ``parsed: false``."""
    ref = _read("review-app", "report-and-stamp.md")
    sample = re.search(r"```markdown\n(.*?)```", ref, re.DOTALL)
    assert sample, "report-and-stamp.md lost its sample report"
    text = sample.group(1)
    assert rl.report_is_parseable(text)
    findings = rl.parse_findings(text)
    assert [(f.severity, f.dimension) for f in findings] == [("BLOCK", "SQL")]
    assert findings[0].why  # the ` -- ` separator splits summary from why


def test_ship_app_saves_review_state_before_the_rebase() -> None:
    skill = _flat(_read("ship-app", "SKILL.md"))
    saved = skill.index("commits_since_review")
    rebase = skill.index("Sync with `origin/main` before pushing")
    pr = skill.index("Open the PR")
    assert saved < rebase < pr
    assert "streamsnow review-loop open-findings apps/<slug>/.review" in skill
    assert "`Open critical: N`" in skill[pr:]
    assert "needs_review" in skill and "review depth" in skill
    # /ship-app stays human-only.
    assert "disable-model-invocation: true" in _read("ship-app", "SKILL.md")


def test_pr_body_rules_never_write_zero_for_unknown() -> None:
    ref = _flat(_read("review-app", "report-and-stamp.md"))
    section = ref[ref.index("## In the /ship-app PR body") :]
    assert "Open critical: unknown" in section
    assert "Never write 0 for an unknown" in section
    assert "not-ancestor" in section
    # A report left over from an earlier ship must not supply the count.
    assert "Open critical: not reviewed for this change" in section
    assert "`stamped` is false" in section and "`needs_review: true`" in section
