"""Tests for ``sql-review helper``: refresh an app's ``review.py`` from the scaffold.

``review.py`` renders with no template variables, so refreshing it is writing the
current template. The verb is a dry run unless ``--apply`` is passed, and it
refuses to overwrite a modified file without ``--force`` (version control is the
backup). Everything runs on ``tmp_path`` Acme repos; no Snowflake.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from streamsnow.scaffolder import _env
from streamsnow.tools import sql_review as sr

SLUG = "acme-sales"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "apps" / SLUG).mkdir(parents=True)
    (root / "streamsnow.config.yaml").write_text("{}\n", encoding="utf-8")
    return root


def _shipped() -> str:
    return _env().get_template("app/review.py.j2").render()


def _helper(repo: Path) -> Path:
    return repo / "apps" / SLUG / "review.py"


def _run(repo: Path, *flags: str, slug: str = SLUG) -> int:
    return sr.main(["helper", slug, "--dir", str(repo), *flags])


def test_current_is_a_no_op(repo, capsys):
    _helper(repo).write_text(_shipped(), encoding="utf-8", newline="\n")
    before = _helper(repo).stat().st_mtime_ns
    assert _run(repo, "--apply") == 0
    assert "current" in capsys.readouterr().out
    assert _helper(repo).stat().st_mtime_ns == before


def test_modified_dry_run_prints_state_and_diff_and_writes_nothing(repo, capsys):
    mine = _shipped() + "# my own tweak\n"
    _helper(repo).write_text(mine, encoding="utf-8", newline="\n")
    assert _run(repo) == 0
    out = capsys.readouterr().out
    assert "modified" in out
    assert "-# my own tweak" in out
    assert _helper(repo).read_text(encoding="utf-8") == mine


def test_modified_apply_is_refused_without_force(repo, capsys):
    mine = _shipped() + "# my own tweak\n"
    _helper(repo).write_text(mine, encoding="utf-8", newline="\n")
    assert _run(repo, "--apply") == 1
    err = capsys.readouterr().err
    assert "--force" in err
    assert "git" in err
    assert _helper(repo).read_text(encoding="utf-8") == mine


def test_modified_apply_with_force_writes_the_template(repo):
    _helper(repo).write_text("# old\n", encoding="utf-8", newline="\n")
    assert _run(repo, "--apply", "--force") == 0
    assert _helper(repo).read_bytes() == _shipped().encode("utf-8")
    assert b"\r" not in _helper(repo).read_bytes()


def test_missing_dry_run_writes_nothing(repo, capsys):
    assert _run(repo) == 0
    assert "missing" in capsys.readouterr().out
    assert not _helper(repo).exists()


def test_missing_apply_writes_the_template(repo):
    assert _run(repo, "--apply") == 0
    assert _helper(repo).read_text(encoding="utf-8") == _shipped()


def test_unknown_slug_is_a_tool_error(repo, capsys):
    assert _run(repo, slug="no-such-app") == 2
    assert "no app" in capsys.readouterr().err


def test_no_config_is_a_tool_error(repo, capsys):
    (repo / "streamsnow.config.yaml").unlink()
    assert _run(repo) == 2
    assert "streamsnow.config.yaml" in capsys.readouterr().err
