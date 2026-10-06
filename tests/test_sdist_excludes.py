"""Local-only files never ship in the sdist.

Hatchling's sdist selection sweeps in untracked files, so a manual ``uv build`` from a
developer checkout used to package ``.claude/settings.local.json``: an absolute home path
that the privacy gate flags, plus personal permissions. The PyPI release was safe only
because ``publish.yml`` builds from a fresh checkout. The exclude list in ``pyproject.toml``
makes a local build as clean as the release one, and does not depend on a ``.gitignore`` edit.

The first two tests pin the config itself. The third asks hatchling which files it would
pack for a throwaway project carrying the same exclude list, so it proves the patterns
match; it is skipped where hatchling is not installed (it is a build dependency, not a dev one).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Local-only files a dev checkout can hold; none may reach an sdist.
LOCAL_ONLY = [
    ".claude/settings.local.json",
    ".claude/CLAUDE.local.md",
    ".streamsnow/export-denylist.txt",
    ".internal/notes.md",
]

#: Patterns the sdist exclude list must carry.
REQUIRED_EXCLUDES = [
    "/.claude/settings.local.json",
    "/.claude/*.local.*",
    "/.streamsnow/export-denylist.txt",
    "/.internal",
]


def _sdist_excludes() -> list[str]:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]


def test_sdist_exclude_list_covers_local_only_files():
    excludes = _sdist_excludes()
    missing = [p for p in REQUIRED_EXCLUDES if p not in excludes]
    assert not missing, f"sdist exclude list is missing {missing}"


def test_gitignore_covers_claude_local_settings():
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".claude/settings.local.json" in lines


def test_hatchling_selection_skips_local_only_files(tmp_path):
    pytest.importorskip("hatchling")
    from hatchling.builders.sdist import SdistBuilder

    project = tmp_path / "proj"
    for rel in [*LOCAL_ONLY, "pkg/__init__.py", ".claude/CLAUDE.md"]:
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    excludes = ", ".join(f'"{p}"' for p in _sdist_excludes())
    (project / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        '[project]\nname = "demo"\nversion = "0"\n'
        f"[tool.hatch.build.targets.sdist]\nexclude = [{excludes}]\n",
        encoding="utf-8",
    )

    builder = SdistBuilder(str(project))
    selected = {
        Path(f.path).relative_to(project).as_posix() for f in builder.recurse_included_files()
    }

    assert "pkg/__init__.py" in selected
    assert ".claude/CLAUDE.md" in selected, "tracked .claude/CLAUDE.md should still ship"
    assert not selected & set(LOCAL_ONLY), selected & set(LOCAL_ONLY)
