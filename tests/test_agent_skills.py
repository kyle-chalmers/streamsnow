"""`streamsnow agent-skills`: the skills installed for agents other than Claude Code.

Codex (0.157.1) reads Agent Skills folders from `.agents/skills` between the
working directory and the repo root, and from `~/.agents/skills`. These tests
pin the layout it needs (every skill plus `_shared/`, so `../_shared/` links
resolve), the ownership rules that keep a re-install from clobbering edits, and
the frontmatter constraints both agents parse.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from streamsnow import agent_skills as ags
from streamsnow.cli import app

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
CODEX = ags.AGENTS["codex"]
runner = CliRunner()

_LINK_RE = re.compile(r"\]\(([^)#]+\.md)\)")
# agentskills.io/specification: lowercase letters, digits and single hyphens.
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _skills() -> list[Path]:
    return sorted(p.parent for p in SKILLS_DIR.glob("*/SKILL.md"))


def test_source_is_the_repo_skills_dir_in_a_checkout():
    assert ags.skills_source() == SKILLS_DIR


def test_shipped_entries_are_every_skill_plus_shared():
    entries = ags.shipped_entries(SKILLS_DIR)
    assert "_shared" in entries
    assert {p.name for p in _skills()} <= set(entries)


@pytest.mark.parametrize("skill", _skills(), ids=lambda p: p.name)
def test_frontmatter_meets_the_agent_skills_constraints(skill):
    """Codex (codex-rs/skills/src/parser.rs) needs a --- fenced YAML block with a
    non-empty description and a name of at most 64 characters; the open spec adds
    the name charset, name == folder, and a 1024-character description cap."""
    meta = ags.read_frontmatter(skill / "SKILL.md")
    assert meta, f"{skill.name}: no parseable YAML frontmatter"
    assert meta.get("name") == skill.name
    assert _NAME_RE.match(meta["name"]) and len(meta["name"]) <= 64
    description = " ".join(str(meta.get("description", "")).split())
    assert 0 < len(description) <= 1024, (skill.name, len(description))


def test_repo_install_lays_out_skills_and_shared_so_links_resolve(tmp_path):
    assert ags.main(["install", "--agent", "codex", "--dir", str(tmp_path)]) == 0
    dest = tmp_path / ".agents" / "skills"
    for skill in _skills():
        assert (dest / skill.name / "SKILL.md").read_bytes() == (skill / "SKILL.md").read_bytes()
    broken = [
        f"{p.relative_to(dest)} -> {target}"
        for p in sorted(dest.rglob("*.md"))
        for target in _LINK_RE.findall(p.read_text())
        if not target.startswith(("http://", "https://"))
        and not (p.parent / target).resolve().exists()
    ]
    assert not broken, broken
    # The one link out of the skills tree (into the repo's docs/) points at GitHub instead.
    gotchas = (dest / "_shared" / "production-gotchas.md").read_text()
    assert f"({ags.REPO_BLOB_URL}/docs/production-lessons.md)" in gotchas
    manifest = json.loads((dest / ags.MANIFEST_NAME).read_text())
    assert manifest["agent"] == "codex"
    assert "_shared" in manifest["entries"]
    assert "start-app/SKILL.md" in manifest["files"]


def test_human_only_skills_are_explicit_only_in_codex(tmp_path):
    """Claude Code's disable-model-invocation maps to Codex's
    policy.allow_implicit_invocation: false, and only for those skills."""
    assert ags.main(["install", "--agent", "codex", "--dir", str(tmp_path)]) == 0
    dest = tmp_path / ".agents" / "skills"
    for skill in _skills():
        human_only = ags.read_frontmatter(skill / "SKILL.md").get("disable-model-invocation")
        policy = dest / skill.name / "agents" / "openai.yaml"
        assert policy.is_file() == (human_only is True), skill.name
        if human_only:
            import yaml

            data = yaml.safe_load(policy.read_text())
            assert data == {"policy": {"allow_implicit_invocation": False}}
            assert f"${skill.name}" in policy.read_text()


def test_dry_run_writes_nothing(tmp_path):
    rc = ags.main(["install", "--agent", "codex", "--dir", str(tmp_path), "--dry-run"])
    assert rc == 0
    assert not (tmp_path / ".agents").exists()


def test_user_scope_installs_under_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert ags.main(["install", "--agent", "codex", "--scope", "user"]) == 0
    assert (home / ".agents" / "skills" / "start-app" / "SKILL.md").is_file()


def test_reinstall_is_a_no_op_and_keeps_the_manifest(tmp_path, capsys):
    args = ["install", "--agent", "codex", "--dir", str(tmp_path)]
    assert ags.main(args) == 0
    assert ags.main(args) == 0
    out = capsys.readouterr().out
    assert "unchanged  start-app" in out


def test_refuses_to_overwrite_an_edited_skill_without_force(tmp_path, capsys):
    args = ["install", "--agent", "codex", "--dir", str(tmp_path)]
    assert ags.main(args) == 0
    edited = tmp_path / ".agents" / "skills" / "start-app" / "SKILL.md"
    edited.write_text(edited.read_text() + "\nlocal note\n")
    assert ags.main(args) == 1
    assert "start-app/SKILL.md was edited" in capsys.readouterr().out
    assert "local note" in edited.read_text()  # nothing written
    assert ags.main([*args, "--force"]) == 0
    assert "local note" not in edited.read_text()


def test_refuses_a_same_named_folder_it_did_not_write(tmp_path, capsys):
    foreign = tmp_path / ".agents" / "skills" / "review-app"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: review-app\ndescription: mine\n---\n")
    args = ["install", "--agent", "codex", "--dir", str(tmp_path)]
    assert ags.main(args) == 1
    assert "not installed by streamsnow" in capsys.readouterr().out
    assert "description: mine" in (foreign / "SKILL.md").read_text()
    assert not (tmp_path / ".agents" / "skills" / "start-app").exists()


def _fake_source(root: Path, skills: list[str]) -> Path:
    for name in skills:
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} d\n---\n")
    (root / "_shared").mkdir()
    (root / "_shared" / "notes.md").write_text("shared\n")
    return root


def test_upgrade_updates_changed_skills_and_removes_retired_ones(tmp_path):
    dest = tmp_path / "dest"
    old = _fake_source(tmp_path / "v1", ["start-app", "retired-app"])
    ags.apply_install(ags.plan_install(old, dest, CODEX))

    new = _fake_source(tmp_path / "v2", ["start-app"])
    (new / "start-app" / "SKILL.md").write_text("---\nname: start-app\ndescription: v2\n---\n")
    plan = ags.plan_install(new, dest, CODEX)
    assert plan.conflicts == []
    assert plan.updated == ["start-app"] and plan.removed == ["retired-app"]
    ags.apply_install(plan)
    assert "v2" in (dest / "start-app" / "SKILL.md").read_text()
    assert not (dest / "retired-app").exists()
    assert json.loads((dest / ags.MANIFEST_NAME).read_text())["entries"] == [
        "_shared",
        "start-app",
    ]


def test_a_file_added_inside_an_installed_skill_blocks_the_upgrade(tmp_path):
    dest = tmp_path / "dest"
    src = _fake_source(tmp_path / "v1", ["start-app"])
    ags.apply_install(ags.plan_install(src, dest, CODEX))
    (dest / "start-app" / "team-notes.md").write_text("ours\n")
    plan = ags.plan_install(src, dest, CODEX)
    assert plan.conflicts == ["start-app/team-notes.md was added after the install"]


def test_list_reports_install_state(tmp_path, capsys):
    base = ["--agent", "codex", "--dir", str(tmp_path)]
    assert ags.main(["list", *base]) == 0
    assert "not installed" in capsys.readouterr().out
    assert ags.main(["install", *base]) == 0
    capsys.readouterr()
    assert ags.main(["list", *base]) == 0
    out = capsys.readouterr().out
    assert re.search(r"start-app\s+installed\s", out)
    assert "holds the skills from streamsnow" in out


def test_unknown_agent_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ags.main(["install", "--agent", "nope", "--dir", str(tmp_path)])
    assert exc.value.code == 2


def test_cli_passthrough_runs_the_installer(tmp_path):
    result = runner.invoke(
        app, ["agent-skills", "install", "--agent", "codex", "--dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".agents" / "skills" / "ship-app" / "SKILL.md").is_file()
