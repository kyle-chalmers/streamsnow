"""Pins the skill text that keeps app commits off the deploy branch (#112).

`review-gate branch --ensure` only helps if every skill that commits calls it first:
/build-app before its first (foundation) commit, `--feedback` and `/review-app --fix`
before theirs. Without it a build commits straight to `main`, diverging it from
`origin/main` with unreviewed work one `git push` away from deploying.
"""

from __future__ import annotations

from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
ENSURE = "streamsnow review-gate branch --slug <slug> --ensure"


def _flat(*parts: str) -> str:
    return " ".join(SKILLS_DIR.joinpath(*parts).read_text(encoding="utf-8").split())


def test_build_app_branches_before_the_foundation_commit() -> None:
    scaffold = _flat("build-app", "scaffold.md")
    assert scaffold.index(ENSURE) < scaffold.index("Then commit the foundation")


def test_feedback_and_fix_modes_branch_before_committing() -> None:
    feedback = _flat("build-app", "feedback.md")
    assert feedback.index(ENSURE) < feedback.index("## Collect and classify")
    fixes = _flat("review-app", "fixes.md")
    assert fixes.index(ENSURE) < fixes.index("**Bucket A, one commit per finding")


def test_existing_build_branch_is_the_users_call() -> None:
    for parts in (("build-app", "scaffold.md"), ("build-app", "feedback.md")):
        assert "ask the user" in _flat(*parts)
