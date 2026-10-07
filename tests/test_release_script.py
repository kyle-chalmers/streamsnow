"""Offline tests for scripts/release.py, the maintainer release tool behind `/release`.

Every command the script would shell out to goes through an injected fake `run`, so no
test touches the network, PyPI, GitHub or a real remote. `prepare` and `gates` run against
a tiny real git repo under tmp_path (git is the one tool the suite already assumes); the
fake passes git through and answers everything else (uv, npm, gh, python -m ...).
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "release_script", REPO_ROOT / "scripts" / "release.py"
)
release = importlib.util.module_from_spec(_SPEC)
sys.modules["release_script"] = release
_SPEC.loader.exec_module(release)

TODAY = date(2031, 4, 9)
SHA = "a" * 40
PIN_FILES = [
    "streamsnow/_templates/repo/ci.yml.j2",
    "streamsnow/_templates/repo/deploy.yml.j2",
    "streamsnow/_templates/repo/deploy.git.yml.j2",
    "README.md",
    "docs/deploying.md",
    "docs/distribution.md",
]
MUTATING = (["git", "tag"], ["git", "push"], ["gh", "release", "create"])


# --------------------------------------------------------------------------- fakes


class FakeRun:
    """Records every command; answers by longest matching prefix; git can pass through."""

    def __init__(self, handlers=None, git_passthrough=True):
        self.handlers = list(handlers or [])
        self.git_passthrough = git_passthrough
        self.calls: list[list[str]] = []

    def on(self, prefix, rc=0, out="", err=""):
        self.handlers.insert(0, (list(prefix), (rc, out, err)))
        return self

    def __call__(self, args, **kw):
        args = [str(a) for a in args]
        self.calls.append(args)
        best = None
        for prefix, resp in self.handlers:
            if args[: len(prefix)] == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, resp)
        if best is not None:
            resp = best[1]
            if callable(resp):
                resp = resp(args, kw)
            rc, out, err = resp
            return subprocess.CompletedProcess(args, rc, out, err)
        if self.git_passthrough and args[0] == "git":
            return subprocess.run(
                args, cwd=kw.get("cwd"), capture_output=True, text=True, encoding="utf-8"
            )
        raise AssertionError(f"unexpected command: {args}")

    def mutations(self):
        return [c for c in self.calls if any(c[: len(m)] == m for m in MUTATING)]


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Acme Dev",
            "-c",
            "user.email=dev@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout


def _pyproject(v):
    return f'[project]\nname = "streamsnow"\nversion = "{v}"\n\n[tool.ruff]\ntarget-version = "py311"\n'


def _plugin(v):
    return json.dumps({"name": "streamsnow", "version": v, "license": "MIT"}, indent=2) + "\n"


def _init(v):
    return (
        '"""Acme fake."""\n\n__all__ = ["__version__"]\n\ntry:\n'
        "    from importlib.metadata import version as _version\n\n"
        '    __version__ = _version("streamsnow")\n'
        "except Exception:  # pragma: no cover\n"
        f'    __version__ = "{v}"\n'
    )


def _lock(v):
    return f'version = 1\n\n[[package]]\nname = "streamsnow"\nversion = "{v}"\nsource = {{ editable = "." }}\n'


PREFACE = "# Changelog\n\nBefore 1.0, a breaking change can land in any minor release.\n\n"
OLD_SECTION = "## [0.4.2] - 2031-01-02\n\n- Generated workflows pin `streamsnow>=0.4.1,<0.5`.\n"


def _changelog(unreleased: str) -> str:
    body = f"\n{unreleased.strip()}\n\n" if unreleased.strip() else "\n"
    return PREFACE + "## [Unreleased]\n" + body + OLD_SECTION


FIXED_ONLY = "### Fixed\n\n- The Acme widget no longer crashes on an empty orders table.\n"
ADDED = "### Added\n\n- **`streamsnow acme-report`** prints a sales summary.\n" + FIXED_ONLY


def make_repo(tmp_path: Path, version="0.4.2", unreleased=FIXED_ONLY, git=True) -> Path:
    root = tmp_path / "repo"
    files = {
        "pyproject.toml": _pyproject(version),
        ".claude-plugin/plugin.json": _plugin(version),
        "streamsnow/__init__.py": _init(version),
        "uv.lock": _lock(version),
        "CHANGELOG.md": _changelog(unreleased),
        "skills/_shared/playwright-walkthrough.md": "Pinned version: `@playwright/cli@0.1.22`.\n",
    }
    for rel in PIN_FILES:
        files[rel] = "run: uv tool install 'streamsnow>=0.4.1,<0.5'\n"
    files["docs/deploying.md"] = (
        "0.3 moved the pin to `streamsnow>=0.3,<0.4`, and 0.4 to\n`streamsnow>=0.4.1,<0.5`.\n"
    )
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    if git:
        _git(root, "init", "-q", "-b", "main")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "chore: acme baseline")
    return root


def _uv_lock_rewrites(root: Path):
    def handler(args, kw):
        v = re.search(
            r'^version = "([^"]+)"$', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M
        )
        (root / "uv.lock").write_text(_lock(v.group(1)), encoding="utf-8", newline="\n")
        return (0, "", "")

    return handler


def prep_run(root: Path) -> FakeRun:
    fake = FakeRun()
    fake.handlers.append((["uv", "lock", "--check"], (0, "", "")))
    fake.handlers.append((["uv", "lock"], _uv_lock_rewrites(root)))
    return fake


def run_main(capsys, argv, **kw):
    capsys.readouterr()
    code = release.main([*argv, "--format", "json"], **kw)
    out = capsys.readouterr().out
    return code, json.loads(out), out


def statuses(payload):
    return {r["name"]: r["status"] for r in payload["results"]}


# --------------------------------------------------------------------------- suggest


@pytest.mark.parametrize(
    ("unreleased", "bump", "decision"),
    [
        (FIXED_ONLY, "patch", False),
        ("### Docs\n\n- Clarify the Acme deploy guide.\n", "patch", False),
        (ADDED, "minor", False),
        ("### Changed\n\n- `validate-app` reads the Acme config.\n", "minor", False),
        ("### Changed\n\n- BREAKING: the Acme flag is gone.\n", "minor", True),
        ("### Removed\n\n- The `--acme` flag of `streamsnow check`.\n", "minor", True),
    ],
)
def test_suggest_recommends_a_bump(tmp_path, capsys, unreleased, bump, decision):
    root = make_repo(tmp_path, unreleased=unreleased, git=False)
    code, payload, _ = run_main(capsys, ["suggest", "--root", str(root)])
    assert code == 0
    assert payload["bump"] == bump
    assert payload["needs_decision"] is decision
    assert payload["reason"]


def test_suggest_breaking_is_major_from_one_dot_oh(tmp_path, capsys):
    root = make_repo(
        tmp_path, version="1.2.0", unreleased="### Changed\n\n- BREAKING: x.\n", git=False
    )
    code, payload, _ = run_main(capsys, ["suggest", "--root", str(root)])
    assert code == 0 and payload["bump"] == "major" and payload["suggested"] == "2.0.0"


def test_suggest_empty_unreleased_is_nothing_to_release(tmp_path, capsys):
    root = make_repo(tmp_path, unreleased="", git=False)
    code, payload, out = run_main(capsys, ["suggest", "--root", str(root)])
    assert code == 1
    assert "nothing to release" in out


# --------------------------------------------------------------------------- prepare


def test_prepare_bumps_every_spot_and_closes_the_changelog(tmp_path, capsys):
    root = make_repo(tmp_path, unreleased=ADDED)
    fake = prep_run(root)
    code, payload, _ = run_main(
        capsys, ["prepare", "0.5.0", "--root", str(root)], run=fake, today=TODAY
    )
    assert code == 0, payload
    assert 'version = "0.5.0"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'target-version = "py311"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        json.loads((root / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
        == "0.5.0"
    )
    assert '    __version__ = "0.5.0"' in (root / "streamsnow/__init__.py").read_text(
        encoding="utf-8"
    )
    assert 'version = "0.5.0"' in (root / "uv.lock").read_text(encoding="utf-8")
    assert ["uv", "lock"] in fake.calls and ["uv", "lock", "--check"] in fake.calls
    log = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert (
        log
        == PREFACE
        + "## [Unreleased]\n\n## [0.5.0] - 2031-04-09\n\n"
        + ADDED.strip()
        + "\n\n"
        + OLD_SECTION
    )
    # no pin change without --pin-floor
    assert "streamsnow>=0.4.1,<0.5" in (root / "README.md").read_text(encoding="utf-8")
    assert fake.mutations() == []
    assert not any(c[:2] == ["git", "commit"] for c in fake.calls)


def test_prepare_twice_refuses_the_second_run(tmp_path, capsys):
    root = make_repo(tmp_path)
    assert (
        release.main(["prepare", "0.4.3", "--root", str(root)], run=prep_run(root), today=TODAY)
        == 0
    )
    code, _, out = run_main(
        capsys, ["prepare", "0.4.3", "--root", str(root)], run=prep_run(root), today=TODAY
    )
    assert code == 1 and "not clean" in out
    _git(root, "commit", "-qam", "chore(0.4.3): release 0.4.3")
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    code, _, out = run_main(
        capsys, ["prepare", "0.4.3", "--root", str(root)], run=prep_run(root), today=TODAY
    )
    assert code == 1 and "not greater" in out
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == before
    assert before.count("## [0.4.3]") == 1


@pytest.mark.parametrize(
    ("version", "code"), [("0.4", 2), ("v0.5.0", 2), ("0.4.2", 1), ("0.3.9", 1)]
)
def test_prepare_rejects_invalid_or_lower_versions(tmp_path, capsys, version, code):
    root = make_repo(tmp_path)
    got, _, _ = run_main(
        capsys, ["prepare", version, "--root", str(root)], run=prep_run(root), today=TODAY
    )
    assert got == code
    assert 'version = "0.4.2"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_prepare_refuses_a_dirty_tree(tmp_path, capsys):
    root = make_repo(tmp_path)
    (root / "README.md").write_text("edited\n", encoding="utf-8")
    code, _, out = run_main(
        capsys, ["prepare", "0.4.3", "--root", str(root)], run=prep_run(root), today=TODAY
    )
    assert code == 1 and "not clean" in out
    assert 'version = "0.4.2"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_prepare_refuses_an_empty_unreleased(tmp_path, capsys):
    root = make_repo(tmp_path, unreleased="")
    code, _, out = run_main(
        capsys, ["prepare", "0.4.3", "--root", str(root)], run=prep_run(root), today=TODAY
    )
    assert code == 1 and "nothing to release" in out


def test_prepare_fails_loudly_when_a_version_pattern_is_missing(tmp_path, capsys):
    root = make_repo(tmp_path)
    (root / "streamsnow/__init__.py").write_text('__version__ = "0.4.2"\n', encoding="utf-8")
    _git(root, "commit", "-qam", "chore: acme drift")
    code, _, out = run_main(
        capsys, ["prepare", "0.4.3", "--root", str(root)], run=prep_run(root), today=TODAY
    )
    assert code == 2 and "__init__.py" in out
    assert 'version = "0.4.2"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_pin_floor_patch_rewrites_exactly_the_listed_files(tmp_path, capsys):
    root = make_repo(tmp_path)
    code, payload, _ = run_main(
        capsys,
        ["prepare", "0.4.3", "--pin-floor", "--root", str(root)],
        run=prep_run(root),
        today=TODAY,
    )
    assert code == 0, payload
    for rel in PIN_FILES:
        text = (root / rel).read_text(encoding="utf-8")
        assert "streamsnow>=0.4.3,<0.5" in text, rel
        assert "streamsnow>=0.4.1,<0.5" not in text, rel
    assert "streamsnow>=0.3,<0.4" in (root / "docs/deploying.md").read_text(encoding="utf-8")
    assert "pin `streamsnow>=0.4.1,<0.5`" in (root / "CHANGELOG.md").read_text(encoding="utf-8")
    changed = _git(root, "diff", "--name-only").split()
    expected = {
        *PIN_FILES,
        "pyproject.toml",
        ".claude-plugin/plugin.json",
        "streamsnow/__init__.py",
        "uv.lock",
        "CHANGELOG.md",
    }
    assert set(changed) == expected


def test_pin_floor_minor_bump_moves_the_upper_bound(tmp_path, capsys):
    root = make_repo(tmp_path, unreleased=ADDED)
    code, _, _ = run_main(
        capsys,
        ["prepare", "0.5.0", "--pin-floor", "--root", str(root)],
        run=prep_run(root),
        today=TODAY,
    )
    assert code == 0
    for rel in PIN_FILES:
        assert "streamsnow>=0.5.0,<0.6" in (root / rel).read_text(encoding="utf-8"), rel


def test_pin_floor_fails_loudly_when_a_file_lacks_the_pin(tmp_path, capsys):
    root = make_repo(tmp_path)
    (root / "docs/distribution.md").write_text("no pin here\n", encoding="utf-8")
    _git(root, "commit", "-qam", "docs: acme drift")
    code, _, out = run_main(
        capsys,
        ["prepare", "0.4.3", "--pin-floor", "--root", str(root)],
        run=prep_run(root),
        today=TODAY,
    )
    assert code == 2 and "docs/distribution.md" in out
    assert 'version = "0.4.2"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "streamsnow>=0.4.1,<0.5" in (root / "README.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- gates


def _released_repo(tmp_path, denylist: str | None = None, message="feat: acme report") -> Path:
    root = make_repo(tmp_path)
    _git(root, "tag", "v0.4.2")
    (root / "CHANGELOG.md").write_text(
        PREFACE
        + "## [Unreleased]\n\n## [0.4.3] - 2031-04-09\n\n"
        + FIXED_ONLY
        + "\n"
        + OLD_SECTION,
        encoding="utf-8",
        newline="\n",
    )
    for rel, text in {
        "pyproject.toml": _pyproject("0.4.3"),
        ".claude-plugin/plugin.json": _plugin("0.4.3"),
        "streamsnow/__init__.py": _init("0.4.3"),
        "uv.lock": _lock("0.4.3"),
    }.items():
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(root, "commit", "-qam", message)
    if denylist is not None:
        (root / ".git" / "info" / "exclude").write_text(".streamsnow/\n", encoding="utf-8")
        (root / ".streamsnow").mkdir()
        (root / ".streamsnow/export-denylist.txt").write_text(denylist, encoding="utf-8")
    return root


def gates_run(scan_rc=0, lock_rc=0, npm="0.1.22\n", npm_missing=False, links_rc=0) -> FakeRun:
    fake = FakeRun()
    fake.on(["uv", "lock", "--check"], rc=lock_rc)
    fake.on([sys.executable, "-m", "streamsnow.tools.check_export_clean"], rc=scan_rc, out="scan\n")
    fake.on([sys.executable, "scripts/check_docs_links.py", "--online"], rc=links_rc)
    if npm_missing:
        fake.handlers.insert(
            0, (["npm"], lambda a, k: (_ for _ in ()).throw(FileNotFoundError("npm")))
        )
    else:
        fake.on(["npm", "view", "@playwright/cli", "version"], out=npm)
    return fake


DENY = "# acme denylist\nglobex-internal\nre:\\bINITECH-\\d+\\b\n"


def test_gates_all_pass_in_order(tmp_path, capsys):
    root = _released_repo(tmp_path, denylist=DENY)
    code, payload, _ = run_main(
        capsys, ["gates", "0.4.3", "--online", "--root", str(root)], run=gates_run()
    )
    assert code == 0, payload
    names = [r["name"] for r in payload["results"]]
    assert names == list(release.GATE_NAMES)
    assert set(statuses(payload).values()) == {"PASS"}


def test_gates_lockstep_mismatch_fails(tmp_path, capsys):
    root = _released_repo(tmp_path, denylist=DENY)
    (root / "streamsnow/__init__.py").write_text(_init("0.4.2"), encoding="utf-8", newline="\n")
    _git(root, "commit", "-qam", "fix: acme")
    code, payload, _ = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 1 and statuses(payload)["version lockstep"] == "FAIL"


def test_gates_uv_lock_check_failure_fails(tmp_path, capsys):
    root = _released_repo(tmp_path, denylist=DENY)
    code, payload, _ = run_main(
        capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run(lock_rc=1)
    )
    assert code == 1 and statuses(payload)["version lockstep"] == "FAIL"


def test_gates_dirty_tree_fails(tmp_path, capsys):
    root = _released_repo(tmp_path, denylist=DENY)
    (root / "README.md").write_text("dirty\n", encoding="utf-8")
    code, payload, _ = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 1 and statuses(payload)["git tree clean"] == "FAIL"


def test_gates_undated_changelog_fails(tmp_path, capsys):
    root = _released_repo(tmp_path, denylist=DENY)
    log = (
        (root / "CHANGELOG.md")
        .read_text(encoding="utf-8")
        .replace("## [0.4.3] - 2031-04-09", "## [0.4.3]")
    )
    (root / "CHANGELOG.md").write_text(log, encoding="utf-8", newline="\n")
    _git(root, "commit", "-qam", "docs: acme")
    code, payload, _ = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 1 and statuses(payload)["changelog closed"] == "FAIL"


def test_gates_privacy_scan_failure_fails(tmp_path, capsys):
    root = _released_repo(tmp_path, denylist=DENY)
    code, payload, _ = run_main(
        capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run(scan_rc=1)
    )
    assert code == 1 and statuses(payload)["privacy scan"] == "FAIL"


def test_gates_warn_without_a_denylist(tmp_path, capsys):
    root = _released_repo(tmp_path, denylist=None)
    code, payload, out = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 0
    assert statuses(payload)["privacy scan"] == "WARN"
    assert "denylist not present, scan is generic only" in out


@pytest.mark.parametrize("message", ["feat: globex-internal report", "fix: see INITECH-42"])
def test_gates_denylist_hit_in_a_commit_message_fails_without_printing_the_term(
    tmp_path, capsys, message
):
    root = _released_repo(tmp_path, denylist=DENY, message=message)
    code, payload, out = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 1 and statuses(payload)["commit messages"] == "FAIL"
    assert "globex" not in out.lower() and "INITECH" not in out


def test_gates_home_path_in_a_commit_message_fails(tmp_path, capsys):
    user = "acmedev"
    root = _released_repo(tmp_path, denylist=DENY, message=f"fix: path /home/{user}/apps leaked")
    code, payload, _ = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 1 and statuses(payload)["commit messages"] == "FAIL"


def test_gates_online_links_failure_fails_and_offline_warns(tmp_path, capsys):
    root = _released_repo(tmp_path, denylist=DENY)
    code, payload, _ = run_main(
        capsys, ["gates", "0.4.3", "--online", "--root", str(root)], run=gates_run(links_rc=1)
    )
    assert code == 1 and statuses(payload)["docs links"] == "FAIL"
    code, payload, _ = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 0 and statuses(payload)["docs links"] == "WARN"


@pytest.mark.parametrize("kw", [{"npm": "0.2.0\n"}, {"npm_missing": True}])
def test_gates_playwright_pin_only_ever_warns(tmp_path, capsys, kw):
    root = _released_repo(tmp_path, denylist=DENY)
    code, payload, _ = run_main(
        capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run(**kw)
    )
    assert code == 0 and statuses(payload)["playwright pin"] == "WARN"


# --------------------------------------------------------------------------- tag


def tag_run(
    version="0.5.0",
    origin_version=None,
    local_tag=False,
    remote_refs="",
    runs=None,
    changelog=None,
) -> FakeRun:
    ov = origin_version or version
    if runs is None:
        runs = [
            {"name": "ci", "status": "completed", "conclusion": "success"},
            {"name": "acme-lint", "status": "completed", "conclusion": "success"},
        ]
    if changelog is None:
        changelog = (
            PREFACE
            + f"## [Unreleased]\n\n## [{version}] - 2031-04-09\n\n"
            + ADDED
            + "\n"
            + OLD_SECTION
        )
    fake = FakeRun(git_passthrough=False)
    t = f"v{version}"
    fake.on(["git", "fetch", "origin", "--tags"])
    fake.on(["git", "rev-parse", "--verify", "origin/main^{commit}"], out=SHA + "\n")
    fake.on(["git", "show", f"{SHA}:pyproject.toml"], out=_pyproject(ov))
    fake.on(["git", "show", f"{SHA}:.claude-plugin/plugin.json"], out=_plugin(ov))
    fake.on(["git", "show", f"{SHA}:streamsnow/__init__.py"], out=_init(ov))
    fake.on(["git", "show", f"{SHA}:CHANGELOG.md"], out=changelog)
    fake.on(["git", "rev-parse", "-q", "--verify", f"refs/tags/{t}"], rc=0 if local_tag else 1)
    fake.on(["git", "ls-remote", "origin"], out=remote_refs)
    fake.on(["gh", "run", "list", "--commit", SHA], out=json.dumps(runs))
    fake.on(["git", "tag", t, SHA])
    fake.on(["git", "push", "origin", f"refs/tags/{t}"])
    fake.on(["gh", "release", "create", t])
    return fake


def test_tag_success_runs_exactly_three_mutations_in_order(tmp_path, capsys):
    fake = tag_run()
    code, payload, out = run_main(capsys, ["tag", "0.5.0", "--root", str(tmp_path)], run=fake)
    assert code == 0, payload
    muts = fake.mutations()
    assert [m[:3] for m in muts] == [
        ["git", "tag", "v0.5.0"],
        ["git", "push", "origin"],
        ["gh", "release", "create"],
    ]
    assert muts[0] == ["git", "tag", "v0.5.0", SHA]
    assert muts[1] == ["git", "push", "origin", "refs/tags/v0.5.0"]
    assert muts[2][:4] == ["gh", "release", "create", "v0.5.0"]
    assert "--verify-tag" in muts[2] and "--notes-file" in muts[2]
    assert muts[2][muts[2].index("--title") + 1] == "v0.5.0"
    assert "/release verify 0.5.0" in out
    assert fake.calls[0] == ["git", "fetch", "origin", "--tags"]


def test_tag_release_notes_are_the_changelog_section(tmp_path, capsys, monkeypatch):
    captured = {}
    fake = tag_run()

    def create(args, kw):
        captured["notes"] = Path(args[args.index("--notes-file") + 1]).read_text(encoding="utf-8")
        return (0, "", "")

    fake.handlers.insert(0, (["gh", "release", "create", "v0.5.0"], create))
    code, _, _ = run_main(capsys, ["tag", "0.5.0", "--root", str(tmp_path)], run=fake)
    assert code == 0
    assert "acme-report" in captured["notes"] and "## [0.4.2]" not in captured["notes"]


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"origin_version": "0.4.9"}, "origin/main"),
        ({"local_tag": True}, "already exists"),
        ({"remote_refs": f"{SHA}\trefs/tags/v0.5.0\n"}, "already exists"),
        ({"remote_refs": f"{SHA}\trefs/heads/v0.5.0\n"}, "branch"),
        ({"runs": [{"name": "ci", "status": "in_progress", "conclusion": ""}]}, "ci"),
        ({"runs": [{"name": "ci", "status": "completed", "conclusion": "failure"}]}, "ci"),
        ({"runs": []}, "no workflow runs"),
        ({"changelog": PREFACE + "## [Unreleased]\n\n" + OLD_SECTION}, "changelog"),
    ],
)
def test_tag_refuses_and_mutates_nothing(tmp_path, capsys, kw, reason):
    fake = tag_run(**kw)
    code, payload, out = run_main(capsys, ["tag", "0.5.0", "--root", str(tmp_path)], run=fake)
    assert code == 1
    assert reason in out
    assert fake.mutations() == []


def test_tag_refuses_on_a_failed_fetch_without_mutating(tmp_path, capsys):
    fake = tag_run().on(["git", "fetch", "origin", "--tags"], rc=128, err="no network")
    code, _, _ = run_main(capsys, ["tag", "0.5.0", "--root", str(tmp_path)], run=fake)
    assert code in (1, 2) and fake.mutations() == []


def test_tag_pending_ci_never_polls(tmp_path, capsys):
    fake = tag_run(runs=[{"name": "ci", "status": "queued", "conclusion": ""}])
    run_main(capsys, ["tag", "0.5.0", "--root", str(tmp_path)], run=fake)
    assert sum(c[:3] == ["gh", "run", "list"] for c in fake.calls) == 1


def test_tag_push_ok_but_release_failed_says_so(tmp_path, capsys):
    fake = tag_run().on(["gh", "release", "create", "v0.5.0"], rc=1, err="boom")
    code, _, out = run_main(capsys, ["tag", "0.5.0", "--root", str(tmp_path)], run=fake)
    assert code == 1
    assert "--release-only" in out and "publishing has started" in out


def test_tag_release_only_needs_the_tag_on_origin(tmp_path, capsys):
    fake = tag_run(remote_refs="")
    fake.on(["gh", "release", "view", "v0.5.0"], rc=1)
    code, _, out = run_main(
        capsys, ["tag", "0.5.0", "--release-only", "--root", str(tmp_path)], run=fake
    )
    assert code == 1 and fake.mutations() == []

    fake = tag_run(remote_refs=f"{SHA}\trefs/tags/v0.5.0\n")
    fake.on(["gh", "release", "view", "v0.5.0"], rc=1)
    code, _, _ = run_main(
        capsys, ["tag", "0.5.0", "--release-only", "--root", str(tmp_path)], run=fake
    )
    assert code == 0
    assert [m[:3] for m in fake.mutations()] == [["gh", "release", "create"]]


# --------------------------------------------------------------------------- verify


def verify_run(runs, uvx_out="streamsnow 0.5.0\n") -> FakeRun:
    fake = FakeRun(git_passthrough=False)
    fake.on(
        ["gh", "run", "list", "--workflow", "publish.yml", "--branch", "v0.5.0"],
        out=json.dumps(runs),
    )
    fake.on(["uvx", "--from", "streamsnow==0.5.0", "streamsnow", "--version"], out=uvx_out)
    return fake


def test_verify_pending_exits_3(tmp_path, capsys):
    fake = verify_run(
        [{"status": "in_progress", "conclusion": "", "url": "https://example.com/r/1"}]
    )
    code, _, out = run_main(capsys, ["verify", "0.5.0", "--root", str(tmp_path)], run=fake)
    assert code == 3 and "pending" in out


def test_verify_failed_exits_1_with_url(tmp_path, capsys):
    fake = verify_run(
        [{"status": "completed", "conclusion": "failure", "url": "https://example.com/r/2"}]
    )
    code, _, out = run_main(capsys, ["verify", "0.5.0", "--root", str(tmp_path)], run=fake)
    assert code == 1 and "https://example.com/r/2" in out


def test_verify_success_checks_pypi_and_smoke_tests(tmp_path, capsys):
    fake = verify_run(
        [{"status": "completed", "conclusion": "success", "url": "https://example.com/r/3"}]
    )
    fetched = []

    def fetch(url):
        fetched.append(url)
        return {"info": {"version": "0.5.0"}}

    code, payload, _ = run_main(
        capsys, ["verify", "0.5.0", "--root", str(tmp_path)], run=fake, fetch_json=fetch
    )
    assert code == 0, payload
    assert fetched and "pypi.org" in fetched[0]
    assert set(statuses(payload).values()) == {"PASS"}
    assert ["uvx", "--from", "streamsnow==0.5.0", "streamsnow", "--version"] in fake.calls


def test_verify_pypi_behind_fails(tmp_path, capsys):
    fake = verify_run([{"status": "completed", "conclusion": "success", "url": "u"}])
    code, payload, _ = run_main(
        capsys,
        ["verify", "0.5.0", "--root", str(tmp_path)],
        run=fake,
        fetch_json=lambda url: {"info": {"version": "0.4.2"}},
    )
    assert code == 1 and statuses(payload)["pypi latest"] == "FAIL"


# --------------------------------------------------------------------------- skill + doc drift

SKILL = REPO_ROOT / ".claude" / "skills" / "release" / "SKILL.md"


def _frontmatter_and_body(text: str) -> tuple[str, str]:
    assert text.startswith("---\n")
    _, fm, body = text.split("---\n", 2)
    return fm, body


def test_release_skill_runs_on_haiku_and_only_by_hand():
    fm, body = _frontmatter_and_body(SKILL.read_text(encoding="utf-8"))
    assert re.search(r"^name: release$", fm, re.M)
    assert re.search(r"^model: haiku$", fm, re.M)
    assert re.search(r"^disable-model-invocation: true$", fm, re.M)
    assert re.search(r"^argument-hint: .+", fm, re.M)
    assert re.search(r"^description: .{40,}", fm, re.M)
    assert "Bash(uv run python scripts/release.py *)" in fm
    for banned in ("git tag", "gh release", "uv publish", "--tags"):
        assert banned not in fm, banned
    assert len(body.splitlines()) <= 80
    assert "—" not in body and "–" not in body


def test_release_skill_never_tells_the_agent_to_tag_or_release_directly():
    _, body = _frontmatter_and_body(SKILL.read_text(encoding="utf-8"))
    for line in body.splitlines():
        if re.search(r"git tag|gh release|uv publish|git push --tags|refs/tags", line):
            assert "never" in line.lower(), line
        for m in re.finditer(r"git push[^`\n]*", line):
            if "never" not in line.lower():
                assert m.group(0).startswith("git push -u origin claude/release-"), line


def test_gate_list_tracks_releasing_md():
    doc = (REPO_ROOT / "RELEASING.md").read_text(encoding="utf-8")
    src = (REPO_ROOT / "scripts" / "release.py").read_text(encoding="utf-8")
    # (keyword RELEASING.md must still say, keyword the script must still implement)
    pairs = [
        ("check_export_clean", "streamsnow.tools.check_export_clean"),
        ("export-denylist.txt", "export-denylist.txt"),
        ("`re:` prefix", '"re:"'),
        ("git log", '"log"'),
        ("uv lock --check", '"--check"'),
        (".claude-plugin/plugin.json", ".claude-plugin/plugin.json"),
        ("streamsnow/__init__.py", "streamsnow/__init__.py"),
        ("## [Unreleased]", "## [Unreleased]"),
        ("`## [X.Y.Z] - YYYY-MM-DD`", "## [{version}] - "),
        ("npm view @playwright/cli version", '"@playwright/cli"'),
        ("check_docs_links.py --online", "check_docs_links.py"),
        ("refs/tags/vX.Y.Z", "refs/tags/"),
        ("GitHub Release", '"--verify-tag"'),
    ]
    for doc_kw, src_kw in pairs:
        assert doc_kw in doc, f"RELEASING.md no longer mentions {doc_kw!r}; update release.py"
        assert src_kw in src, f"release.py no longer implements {src_kw!r} ({doc_kw})"
    assert len(release.GATE_NAMES) == 7
