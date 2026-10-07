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


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")


@pytest.mark.parametrize("force", [False, True])
def test_symlinked_helper_is_refused_and_writes_nothing_outside(repo, tmp_path, capsys, force):
    outside = tmp_path / "outside.py"
    outside.write_text("# not yours\n", encoding="utf-8")
    _symlink(_helper(repo), outside)
    flags = ("--apply", "--force") if force else ("--apply",)
    assert _run(repo, *flags) == 1
    assert "symlink" in capsys.readouterr().err
    assert outside.read_text(encoding="utf-8") == "# not yours\n"
    assert _helper(repo).is_symlink()


def test_dangling_symlinked_helper_is_refused(repo, tmp_path, capsys):
    outside = tmp_path / "created-by-write.py"
    _symlink(_helper(repo), outside)
    assert _run(repo, "--apply", "--force") == 1
    assert "symlink" in capsys.readouterr().err
    assert not outside.exists()


def test_app_dir_symlinked_out_of_the_repo_is_a_tool_error(tmp_path, capsys):
    root = tmp_path / "repo"
    (root / "apps").mkdir(parents=True)
    (root / "streamsnow.config.yaml").write_text("{}\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _symlink(root / "apps" / SLUG, elsewhere)
    assert _run(root, "--apply") == 2
    assert "outside the repo" in capsys.readouterr().err
    assert list(elsewhere.iterdir()) == []


def test_non_utf8_helper_is_a_tool_error(repo, capsys):
    _helper(repo).write_bytes(b"# caf\xe9\n")
    assert _run(repo, "--apply", "--force") == 2
    assert "UTF-8" in capsys.readouterr().err
    assert _helper(repo).read_bytes() == b"# caf\xe9\n"
