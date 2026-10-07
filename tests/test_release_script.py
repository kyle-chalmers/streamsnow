"""Offline tests for scripts/release.py, the maintainer release tool behind `/release`.

Every command the script would shell out to goes through an injected fake `run`, so no
test touches the network, PyPI, GitHub or a real remote. `prepare`, `open-pr` and `gates`
run against a tiny real git repo under tmp_path (git is the one tool the suite already
assumes); the fake passes git through and answers everything else (uv, npm, gh, pushes).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tarfile
import urllib.error
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
    "docs/distribution.md",
]
DEPLOYING = (
    "0.9 moves every generated pin to `streamsnow>=0.9,<0.10`, and 0.10 to\n"
    "`streamsnow>=0.4.1,<0.5`.\n"
)
MUTATING = (
    ["git", "tag"],
    ["git", "push"],
    ["git", "commit"],
    ["gh", "release", "create"],
    ["gh", "pr", "create"],
)
TRAILER = "Co-Authored-By: Acme Bot <bot@example.com>"
DENY = "# acme denylist\nglobex-internal\nre:\\bINITECH-\\d+\\b\n"


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
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return proc.stdout


def _pyproject(v):
    return (
        f'[project]\nname = "streamsnow"\nversion = "{v}"\n\n'
        '[tool.ruff]\ntarget-version = "py311"\n'
    )


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
    return (
        f'version = 1\n\n[[package]]\nname = "streamsnow"\nversion = "{v}"\n'
        'source = { editable = "." }\n'
    )


PREFACE = "# Changelog\n\nBefore 1.0, a breaking change can land in any minor release.\n\n"
OLD_SECTION = "## [0.4.2] - 2031-01-02\n\n- Generated workflows pin `streamsnow>=0.4.1,<0.5`.\n"


def _changelog(unreleased: str) -> str:
    body = f"\n{unreleased.strip()}\n\n" if unreleased.strip() else "\n"
    return PREFACE + "## [Unreleased]\n" + body + OLD_SECTION


FIXED_ONLY = "### Fixed\n\n- The Acme widget no longer crashes on an empty orders table.\n"
ADDED = "### Added\n\n- **`streamsnow acme-report`** prints a sales summary.\n" + FIXED_ONLY


def _write_denylist(root: Path, text: str = DENY) -> None:
    (root / ".streamsnow").mkdir(exist_ok=True)
    (root / ".streamsnow/export-denylist.txt").write_text(text, encoding="utf-8")


def make_repo(
    tmp_path: Path, version="0.4.2", unreleased=FIXED_ONLY, git=True, denylist=DENY
) -> Path:
    root = tmp_path / "repo"
    files = {
        "pyproject.toml": _pyproject(version),
        ".claude-plugin/plugin.json": _plugin(version),
        "streamsnow/__init__.py": _init(version),
        "uv.lock": _lock(version),
        "CHANGELOG.md": _changelog(unreleased),
        "skills/_shared/playwright-walkthrough.md": "Pinned version: `@playwright/cli@0.1.22`.\n",
        "docs/deploying.md": DEPLOYING,
    }
    for rel in PIN_FILES:
        files[rel] = "run: uv tool install 'streamsnow>=0.4.1,<0.5'\n"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    if git:
        _git(root, "init", "-q", "-b", "main")
        for key, val in {
            "user.name": "Acme Dev",
            "user.email": "dev@example.com",
            "commit.gpgsign": "false",
            "tag.gpgsign": "false",
            "core.hooksPath": ".git/no-hooks",
        }.items():
            _git(root, "config", key, val)
        (root / ".git" / "info").mkdir(exist_ok=True)
        (root / ".git" / "info" / "exclude").write_text(".streamsnow/\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "chore: acme baseline")
    if denylist is not None:
        _write_denylist(root, denylist)
    return root


def _uv_lock_rewrites(root: Path):
    def handler(args, kw):
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        v = re.search(r'^version = "([^"]+)"$', text, re.M)
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


def _prepare(capsys, root, version, *extra):
    return run_main(
        capsys,
        ["prepare", version, *extra, "--root", str(root)],
        run=prep_run(root),
        today=TODAY,
    )


def test_semver_must_match_fully():
    assert release.parse_semver("1.2.3") == (1, 2, 3)
    for bad in ("1.2.3\n", "1.2", "v1.2.3", "01.2.3", "1.2.3-rc1"):
        with pytest.raises(release.ToolError):
            release.parse_semver(bad)


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
    code, payload, out = run_main(
        capsys, ["prepare", "0.5.0", "--root", str(root)], run=fake, today=TODAY
    )
    assert code == 0, payload
    assert 'version = "0.5.0"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'target-version = "py311"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    plugin = json.loads((root / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert plugin["version"] == "0.5.0"
    init = (root / "streamsnow/__init__.py").read_text(encoding="utf-8")
    assert '    __version__ = "0.5.0"' in init
    assert 'version = "0.5.0"' in (root / "uv.lock").read_text(encoding="utf-8")
    assert ["uv", "lock"] in fake.calls and ["uv", "lock", "--check"] in fake.calls
    log = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    expected = PREFACE + "## [Unreleased]\n\n## [0.5.0] - 2031-04-09\n\n" + ADDED.strip() + "\n\n"
    assert log == expected + OLD_SECTION
    # no pin change without --pin-floor
    assert "streamsnow>=0.4.1,<0.5" in (root / "README.md").read_text(encoding="utf-8")
    assert fake.mutations() == []
    # the next step is open-pr (which commits, then gates on a clean tree), not gates
    assert "open-pr 0.5.0" in payload["next"]
    assert "gates" not in payload["next"]


def test_prepare_prints_the_human_review_checklist(tmp_path, capsys):
    root = make_repo(tmp_path)
    code, payload, _ = _prepare(capsys, root, "0.4.3")
    assert code == 0
    text = " ".join(payload["checklist"])
    assert "git log" in text and "LICENSE" in text and "/preview-app" in text


def test_prepare_twice_refuses_the_second_run(tmp_path, capsys):
    root = make_repo(tmp_path)
    assert _prepare(capsys, root, "0.4.3")[0] == 0
    code, _, out = _prepare(capsys, root, "0.4.3")
    assert code == 1 and "not clean" in out
    _git(root, "commit", "-qam", "chore(0.4.3): release 0.4.3")
    before = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    code, _, out = _prepare(capsys, root, "0.4.3")
    assert code == 1 and "not greater" in out
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == before
    assert before.count("## [0.4.3]") == 1


@pytest.mark.parametrize(
    ("version", "code"),
    [("0.4", 2), ("v0.5.0", 2), ("0.4.3\n", 2), ("0.4.2", 1), ("0.3.9", 1)],
)
def test_prepare_rejects_invalid_or_lower_versions(tmp_path, capsys, version, code):
    root = make_repo(tmp_path)
    got, _, _ = _prepare(capsys, root, version)
    assert got == code
    assert 'version = "0.4.2"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_prepare_refuses_a_dirty_tree(tmp_path, capsys):
    root = make_repo(tmp_path)
    (root / "README.md").write_text("edited\n", encoding="utf-8")
    code, _, out = _prepare(capsys, root, "0.4.3")
    assert code == 1 and "not clean" in out
    assert 'version = "0.4.2"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_prepare_refuses_an_empty_unreleased(tmp_path, capsys):
    root = make_repo(tmp_path, unreleased="")
    code, _, out = _prepare(capsys, root, "0.4.3")
    assert code == 1 and "nothing to release" in out


def test_prepare_fails_loudly_when_a_version_pattern_is_missing(tmp_path, capsys):
    root = make_repo(tmp_path)
    (root / "streamsnow/__init__.py").write_text('__version__ = "0.4.2"\n', encoding="utf-8")
    _git(root, "commit", "-qam", "chore: acme drift")
    code, _, out = _prepare(capsys, root, "0.4.3")
    assert code == 2 and "__init__.py" in out
    assert 'version = "0.4.2"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_prepare_requires_the_version_line_under_project(tmp_path, capsys):
    root = make_repo(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "streamsnow"\ndynamic = ["version"]\n\n[tool.acme]\nversion = "0.4.2"\n',
        encoding="utf-8",
    )
    _git(root, "commit", "-qam", "chore: acme drift")
    code, _, out = _prepare(capsys, root, "0.4.3")
    assert code == 2 and "[project]" in out


def test_pin_floor_patch_rewrites_exactly_the_listed_files(tmp_path, capsys):
    root = make_repo(tmp_path)
    code, payload, out = _prepare(capsys, root, "0.4.3", "--pin-floor")
    assert code == 0, payload
    for rel in PIN_FILES:
        text = (root / rel).read_text(encoding="utf-8")
        assert "streamsnow>=0.4.3,<0.5" in text, rel
        assert "streamsnow>=0.4.1,<0.5" not in text, rel
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


def test_pin_floor_leaves_the_deploying_history_alone_and_says_so(tmp_path, capsys):
    root = make_repo(tmp_path)
    code, payload, out = _prepare(capsys, root, "0.4.3", "--pin-floor")
    assert code == 0
    assert "docs/deploying.md" not in release.PIN_FILES
    text = (root / "docs/deploying.md").read_text(encoding="utf-8")
    assert text == DEPLOYING
    assert "and 0.10 to" in text
    assert any("docs/deploying.md" in item for item in payload["checklist"])


def test_pin_floor_minor_bump_moves_the_upper_bound(tmp_path, capsys):
    root = make_repo(tmp_path, unreleased=ADDED)
    code, _, _ = _prepare(capsys, root, "0.5.0", "--pin-floor")
    assert code == 0
    for rel in PIN_FILES:
        assert "streamsnow>=0.5.0,<0.6" in (root / rel).read_text(encoding="utf-8"), rel


def test_pin_floor_refuses_from_one_dot_oh(tmp_path, capsys):
    root = make_repo(tmp_path, unreleased=ADDED)
    code, _, out = _prepare(capsys, root, "1.0.0", "--pin-floor")
    assert code == 2 and "1.0" in out
    assert 'version = "0.4.2"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_pin_floor_fails_loudly_when_a_file_lacks_the_pin(tmp_path, capsys):
    root = make_repo(tmp_path)
    (root / "docs/distribution.md").write_text("no pin here\n", encoding="utf-8")
    _git(root, "commit", "-qam", "docs: acme drift")
    code, _, out = _prepare(capsys, root, "0.4.3", "--pin-floor")
    assert code == 2 and "docs/distribution.md" in out
    assert 'version = "0.4.2"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "streamsnow>=0.4.1,<0.5" in (root / "README.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- gates


def _released_repo(tmp_path, denylist: str | None = DENY, message="feat: acme report") -> Path:
    root = make_repo(tmp_path, denylist=denylist)
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
    return root


def gates_run(
    scan_rc=0, lock_rc=0, npm="0.1.22\n", npm_missing=False, links_rc=0, base=None
) -> FakeRun:
    fake = base or FakeRun()
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


def _scan_call(fake):
    return next(c for c in fake.calls if "streamsnow.tools.check_export_clean" in c)


def test_gates_all_pass_in_order(tmp_path, capsys):
    root = _released_repo(tmp_path)
    fake = gates_run()
    code, payload, _ = run_main(
        capsys, ["gates", "0.4.3", "--online", "--root", str(root)], run=fake
    )
    assert code == 0, payload
    names = [r["name"] for r in payload["results"]]
    assert names == list(release.GATE_NAMES)
    assert set(statuses(payload).values()) == {"PASS"}
    scan = _scan_call(fake)
    assert scan[scan.index("--denylist") + 1] == str(root / ".streamsnow/export-denylist.txt")


def test_gates_find_the_denylist_in_the_main_worktree(tmp_path, capsys):
    main = _released_repo(tmp_path)
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt), "HEAD")
    fake = gates_run()
    code, payload, _ = run_main(capsys, ["gates", "0.4.3", "--root", str(wt)], run=fake)
    assert statuses(payload)["privacy scan"] == "PASS", payload
    assert statuses(payload)["commit messages"] == "PASS", payload
    scan = _scan_call(fake)
    found = Path(scan[scan.index("--denylist") + 1])
    assert found.resolve() == (main / ".streamsnow/export-denylist.txt").resolve()


def test_gates_lockstep_mismatch_fails(tmp_path, capsys):
    root = _released_repo(tmp_path)
    (root / "streamsnow/__init__.py").write_text(_init("0.4.2"), encoding="utf-8", newline="\n")
    _git(root, "commit", "-qam", "fix: acme")
    code, payload, _ = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 1 and statuses(payload)["version lockstep"] == "FAIL"


def test_gates_uv_lock_check_failure_fails(tmp_path, capsys):
    root = _released_repo(tmp_path)
    code, payload, _ = run_main(
        capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run(lock_rc=1)
    )
    assert code == 1 and statuses(payload)["version lockstep"] == "FAIL"


def test_gates_dirty_tree_fails(tmp_path, capsys):
    root = _released_repo(tmp_path)
    (root / "README.md").write_text("dirty\n", encoding="utf-8")
    code, payload, _ = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 1 and statuses(payload)["git tree clean"] == "FAIL"


def test_gates_undated_changelog_fails(tmp_path, capsys):
    root = _released_repo(tmp_path)
    log = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    log = log.replace("## [0.4.3] - 2031-04-09", "## [0.4.3]")
    (root / "CHANGELOG.md").write_text(log, encoding="utf-8", newline="\n")
    _git(root, "commit", "-qam", "docs: acme")
    code, payload, _ = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 1 and statuses(payload)["changelog closed"] == "FAIL"


def test_gates_privacy_scan_failure_fails(tmp_path, capsys):
    root = _released_repo(tmp_path)
    code, payload, _ = run_main(
        capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run(scan_rc=1)
    )
    assert code == 1 and statuses(payload)["privacy scan"] == "FAIL"


def test_gates_fail_without_a_denylist(tmp_path, capsys):
    root = _released_repo(tmp_path, denylist=None)
    code, payload, out = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 1
    assert statuses(payload)["privacy scan"] == "FAIL"
    assert statuses(payload)["commit messages"] == "FAIL"
    assert ".streamsnow/export-denylist.txt" in out


@pytest.mark.parametrize("cmd", ["gates", "open-pr"])
def test_the_denylist_bypass_exists_only_on_tag(tmp_path, capsys, cmd):
    # gates and open-pr are pre-approved in the skill, so a bypass flag on them would skip
    # the permission prompt; only `tag`, which always prompts, accepts it.
    root = _released_repo(tmp_path, denylist=None)
    code = release.main([cmd, "0.4.3", "--allow-no-denylist", "--root", str(root)], run=gates_run())
    assert code == 2


@pytest.mark.parametrize("message", ["feat: globex-internal report", "fix: see INITECH-42"])
def test_gates_denylist_hit_in_a_commit_message_fails_without_printing_the_term(
    tmp_path, capsys, message
):
    root = _released_repo(tmp_path, message=message)
    code, payload, out = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 1 and statuses(payload)["commit messages"] == "FAIL"
    assert "globex" not in out.lower() and "INITECH" not in out


def test_gates_home_path_in_a_commit_message_fails(tmp_path, capsys):
    user = "acmedev"
    root = _released_repo(tmp_path, message=f"fix: path /home/{user}/apps leaked")
    code, payload, _ = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 1 and statuses(payload)["commit messages"] == "FAIL"


def test_gates_online_links_failure_fails_and_offline_warns(tmp_path, capsys):
    root = _released_repo(tmp_path)
    code, payload, _ = run_main(
        capsys, ["gates", "0.4.3", "--online", "--root", str(root)], run=gates_run(links_rc=1)
    )
    assert code == 1 and statuses(payload)["docs links"] == "FAIL"
    code, payload, _ = run_main(capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run())
    assert code == 0 and statuses(payload)["docs links"] == "WARN"


@pytest.mark.parametrize("kw", [{"npm": "0.2.0\n"}, {"npm_missing": True}])
def test_gates_playwright_pin_only_ever_warns(tmp_path, capsys, kw):
    root = _released_repo(tmp_path)
    code, payload, _ = run_main(
        capsys, ["gates", "0.4.3", "--root", str(root)], run=gates_run(**kw)
    )
    assert code == 0 and statuses(payload)["playwright pin"] == "WARN"


# --------------------------------------------------------------------------- open-pr


def _pr_repo(tmp_path, capsys, version="0.4.3", branch=None, extra_commit=False, prepare=True):
    root = make_repo(tmp_path)
    _git(root, "tag", "v0.4.2")
    # stand in for the fetched remote: origin/main is the baseline commit
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(root, "switch", "-q", "-c", branch or f"claude/release-{version}")
    if extra_commit:
        (root / "docs/extra.md").write_text("acme extra\n", encoding="utf-8")
        _git(root, "add", "docs/extra.md")
        _git(root, "commit", "-qm", "feat: acme extra")
    if prepare:
        code, payload, _ = _prepare(capsys, root, version)
        assert code == 0, payload
    return root


def pr_run(root: Path, captured: dict, **gate_kw) -> FakeRun:
    fake = gates_run(base=prep_run(root), **gate_kw)
    fake.on(["git", "push"])
    fake.on(["git", "fetch", "origin"])

    def create(args, kw):
        captured["body"] = Path(args[args.index("--body-file") + 1]).read_text(encoding="utf-8")
        return (0, "https://example.com/acme/pull/7\n", "")

    fake.handlers.insert(0, (["gh", "pr", "create"], create))
    return fake


def test_open_pr_commits_then_gates_then_pushes_and_opens_the_pr(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    captured: dict = {}
    fake = pr_run(root, captured)
    code, payload, out = run_main(
        capsys,
        ["open-pr", "0.4.3", "--trailer", TRAILER, "--root", str(root)],
        run=fake,
    )
    assert code == 0, payload
    msg = _git(root, "log", "-1", "--format=%B")
    assert msg.splitlines()[0] == "chore(0.4.3): release 0.4.3"
    assert TRAILER in msg
    # end to end: prepare's edits are committed, so every gate ran on a clean tree
    gates = {r["name"]: r["status"] for r in payload["gates"]}
    assert gates["git tree clean"] == "PASS"
    assert set(gates.values()) == {"PASS"}, gates
    push = [c for c in fake.calls if c[:2] == ["git", "push"]]
    branch = "refs/heads/claude/release-0.4.3"
    assert push == [["git", "push", "--set-upstream", "origin", f"{branch}:{branch}"]]
    pr = next(c for c in fake.calls if c[:3] == ["gh", "pr", "create"])
    assert "--body-file" in pr and pr[pr.index("--title") + 1] == "chore(0.4.3): release 0.4.3"
    assert "git tree clean" in captured["body"] and "PASS" in captured["body"]
    assert "/release tag 0.4.3" in out
    assert _git(root, "status", "--porcelain") == ""


def test_open_pr_adds_the_pin_floor_note_to_the_body(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    captured: dict = {}
    code, _, _ = run_main(
        capsys,
        ["open-pr", "0.4.3", "--pin-floor-note", "deploy calls acme-sync", "--root", str(root)],
        run=pr_run(root, captured),
    )
    assert code == 0
    assert "deploy calls acme-sync" in captured["body"]


@pytest.mark.parametrize(
    "trailer",
    [
        "Co-Authored-By: Acme Bot",
        "Signed-off-by: Acme Bot <bot@example.com>",
        "Co-Authored-By: Acme <bot@example.com>\nevil",
        "Co-Authored-By: Acme <bot @example.com>",
        "Co-Authored-By: Acme\tBot <bot@example.com>",
        "Co-Authored-By: Acme\x1b[31m <bot@example.com>",
        "Co-Authored-By: Acme <bot@example.com\x7f>",
    ],
)
def test_open_pr_rejects_a_malformed_trailer(tmp_path, capsys, trailer):
    root = _pr_repo(tmp_path, capsys)
    fake = pr_run(root, {})
    code, _, _ = run_main(
        capsys, ["open-pr", "0.4.3", "--trailer", trailer, "--root", str(root)], run=fake
    )
    assert code == 2 and fake.mutations() == []


def test_open_pr_refuses_the_wrong_branch(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys, branch="claude/release-0.4.30")
    fake = pr_run(root, {})
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1 and "claude/release-0.4.3" in out
    assert fake.mutations() == []


def test_open_pr_refuses_untracked_files(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    (root / "notes.txt").write_text("acme scratch\n", encoding="utf-8")
    fake = pr_run(root, {})
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1 and "notes.txt" in out and fake.mutations() == []


def test_open_pr_refuses_unexpected_modified_files(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    (root / "skills/_shared/playwright-walkthrough.md").write_text("edited\n", encoding="utf-8")
    fake = pr_run(root, {})
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1 and "playwright-walkthrough.md" in out and fake.mutations() == []


def test_open_pr_scans_the_committed_tree_with_the_denylist_before_pushing(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    fake = pr_run(root, {})
    code, _, _ = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 0
    scan_at = next(
        i for i, c in enumerate(fake.calls) if "streamsnow.tools.check_export_clean" in c
    )
    scan = fake.calls[scan_at]
    assert scan[scan.index("--denylist") + 1] == str(root / ".streamsnow/export-denylist.txt")
    commit_at = next(i for i, c in enumerate(fake.calls) if c[:2] == ["git", "commit"])
    push_at = next(i for i, c in enumerate(fake.calls) if c[:2] == ["git", "push"])
    assert commit_at < scan_at < push_at


def test_open_pr_runs_the_real_scan_and_refuses_a_leak_in_the_files(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys, prepare=False)
    log = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    log = log.replace("orders table.", "orders table for globex-internal.")
    (root / "CHANGELOG.md").write_text(log, encoding="utf-8", newline="\n")
    _git(root, "commit", "-qam", "docs: acme wording")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert _prepare(capsys, root, "0.4.3")[0] == 0
    fake = pr_run(root, {})
    fake.handlers.insert(0, ([sys.executable, "-m", "streamsnow.tools.check_export_clean"], _real))
    code, payload, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1
    assert {g["name"]: g["status"] for g in payload["gates"]}["privacy scan"] == "FAIL"
    assert "globex" not in out.lower() and not _pushed(fake)


DENY_RELEASE = DENY + "re:release 0\\.4\\.3\n"


@pytest.mark.parametrize(
    ("argv", "deny", "what"),
    [
        (["--pin-floor-note", "deploy calls the globex-internal sync"], DENY, "--pin-floor-note"),
        (["--pin-floor-note", "see INITECH-9"], DENY, "--pin-floor-note"),
        (["--trailer", "Co-Authored-By: Globex-Internal Bot <bot@example.com>"], DENY, "--trailer"),
        ([], DENY_RELEASE, "title"),
    ],
)
def test_open_pr_runs_free_text_through_the_denylist(tmp_path, capsys, argv, deny, what):
    root = _pr_repo(tmp_path, capsys)
    _write_denylist(root, deny)
    fake = pr_run(root, {})
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", *argv, "--root", str(root)], run=fake)
    assert code == 1, out
    assert what in out and "denylist" in out
    assert "globex" not in out.lower() and "INITECH" not in out
    assert fake.mutations() == []


def test_open_pr_checks_the_generated_pr_body_before_pushing(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    fake = pr_run(root, {})
    # a gate detail is copied into the PR body; here the docs checker echoes a term
    fake.on([sys.executable, "scripts/check_docs_links.py", "--online"], out="ok globex-internal\n")
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1 and "PR body" in out
    assert "globex" not in out.lower()
    assert not _pushed(fake)


def test_open_pr_needs_a_denylist_before_committing(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    (root / ".streamsnow/export-denylist.txt").unlink()
    fake = pr_run(root, {})
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1 and "export-denylist.txt" in out
    assert fake.mutations() == []


def test_open_pr_stops_before_the_push_when_a_gate_fails(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    fake = pr_run(root, {}, scan_rc=1)
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1
    assert not any(c[:2] == ["git", "push"] for c in fake.calls)
    assert not any(c[:3] == ["gh", "pr", "create"] for c in fake.calls)


def _pushed(fake) -> bool:
    return any(c[:2] == ["git", "push"] or c[:3] == ["gh", "pr", "create"] for c in fake.calls)


@pytest.mark.parametrize("note", ["see /home/{u}/x", "see /Users/{u}/x", "ask a@b.test"])
def test_open_pr_rejects_a_private_pin_floor_note(tmp_path, capsys, note):
    note = note.format(u="acmedev")  # built at runtime so the repo's own privacy scan stays clean
    root = _pr_repo(tmp_path, capsys)
    fake = pr_run(root, {})
    code, _, _ = run_main(
        capsys, ["open-pr", "0.4.3", "--pin-floor-note", note, "--root", str(root)], run=fake
    )
    assert code == 2 and fake.mutations() == []


def test_open_pr_refuses_a_branch_not_cut_from_origin_main(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    # origin/main moved to a commit this branch does not contain
    _git(root, "switch", "-q", "-c", "acme-other", "main")
    _git(root, "commit", "-q", "--allow-empty", "-m", "feat: acme elsewhere")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(root, "switch", "-q", "claude/release-0.4.3")
    fake = pr_run(root, {})
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1 and "origin/main" in out
    assert fake.mutations() == []


def test_open_pr_refuses_an_extra_non_release_commit(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys, extra_commit=True)
    fake = pr_run(root, {})
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1 and "feat: acme extra" in out
    assert fake.mutations() == []


def test_open_pr_refuses_a_stray_file_in_the_release_commit(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    (root / "notes.txt").write_text("acme scratch\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "chore(0.4.3): release 0.4.3")
    fake = pr_run(root, {})
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1 and "notes.txt" in out and not _pushed(fake)


def test_open_pr_refuses_two_release_commits(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    _git(root, "commit", "-qam", "chore(0.4.3): release 0.4.3")
    _git(root, "commit", "-q", "--allow-empty", "-m", "chore(0.4.3): release 0.4.3")
    fake = pr_run(root, {})
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1 and "exactly one" in out and not _pushed(fake)


def test_open_pr_accepts_the_already_committed_release(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    _git(root, "commit", "-qam", "chore(0.4.3): release 0.4.3")
    fake = pr_run(root, {})
    code, payload, _ = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 0, payload
    assert _pushed(fake)


def test_open_pr_refuses_a_deleted_release_file(tmp_path, capsys):
    root = _pr_repo(tmp_path, capsys)
    (root / "README.md").unlink()
    fake = pr_run(root, {})
    code, _, out = run_main(capsys, ["open-pr", "0.4.3", "--root", str(root)], run=fake)
    assert code == 1 and "README.md" in out and "delet" in out
    assert fake.mutations() == []


# --------------------------------------------------------------------------- tag


def tag_run(
    tmp_path: Path,
    version="0.5.0",
    origin_version=None,
    lock_version=None,
    local_tag=False,
    remote_refs="",
    remote_main=SHA,
    subject=None,
    runs=None,
    changelog=None,
    messages="feat: acme report\x00",
    denylist=DENY,
    tree=None,
) -> FakeRun:
    ov = origin_version or version
    if denylist is not None:
        _write_denylist(tmp_path, denylist)
    if runs is None:
        runs = [
            {
                "name": "ci",
                "status": "completed",
                "conclusion": "success",
                "event": "push",
                "createdAt": "2031-04-09T10:00:00Z",
            },
            {
                "name": "acme-labels",
                "status": "completed",
                "conclusion": "failure",
                "event": "push",
                "createdAt": "2031-04-09T10:00:01Z",
            },
        ]
    if changelog is None:
        changelog = (
            PREFACE
            + f"## [Unreleased]\n\n## [{version}] - 2031-04-09\n\n"
            + ADDED
            + "\n"
            + OLD_SECTION
        )
    if subject is None:
        subject = f"chore({version}): release {version} (#42)"
    fake = FakeRun(git_passthrough=False)
    t = f"v{version}"
    fake.on(["git", "fetch", "origin", "--tags"])
    fake.on(["git", "rev-parse", "--verify", "origin/main^{commit}"], out=SHA + "\n")
    fake.on(
        ["git", "worktree", "list", "--porcelain"],
        out=f"worktree {tmp_path}\nHEAD {SHA}\nbranch refs/heads/main\n\n",
    )
    fake.on(["git", "show", f"{SHA}:pyproject.toml"], out=_pyproject(ov))
    fake.on(["git", "show", f"{SHA}:.claude-plugin/plugin.json"], out=_plugin(ov))
    fake.on(["git", "show", f"{SHA}:streamsnow/__init__.py"], out=_init(ov))
    fake.on(["git", "show", f"{SHA}:uv.lock"], out=_lock(lock_version or ov))
    fake.on(["git", "show", f"{SHA}:CHANGELOG.md"], out=changelog)
    fake.on(["git", "log", "-1", "--format=%s", SHA], out=subject + "\n")
    fake.on(["git", "describe", "--tags", "--abbrev=0", SHA], out="v0.4.2\n")
    fake.on(["git", "log", "--format=%h%x00%B%x1e", f"v0.4.2..{SHA}"], out="abc1234\x00" + messages)
    fake.on(["git", "rev-parse", "-q", "--verify", f"refs/tags/{t}"], rc=0 if local_tag else 1)
    fake.on(["git", "ls-remote", "origin"], out=remote_refs)
    fake.on(
        ["git", "ls-remote", "origin", "refs/heads/main"], out=f"{remote_main}\trefs/heads/main\n"
    )
    fake.on(["gh", "run", "list", "--commit", SHA], out=json.dumps(runs))
    fake.handlers.append((["git", "archive", "--format=tar", "-o"], _archive(tree)))
    fake.handlers.append(([sys.executable, "-m", "streamsnow.tools.check_export_clean"], _real))
    fake.on(["git", "tag", t, SHA])
    fake.on(["git", "push", "origin", f"refs/tags/{t}"])
    fake.on(["gh", "release", "create", t])
    return fake


CLEAN_TREE = {"README.md": "# Acme dashboards\n", "apps/sales/app.py": "print('acme')\n"}


def _archive(tree: dict | None, extra_member=None):
    """Answer `git archive --format=tar -o <path> <sha>` by writing a tar of ``tree``."""

    def handler(args, kw):
        assert args[-1] == SHA, args
        with tarfile.open(args[4], "w") as tf:
            for name, text in (tree if tree is not None else CLEAN_TREE).items():
                data = text.encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            if extra_member is not None:
                tf.addfile(extra_member)
        return (0, "", "")

    return handler


def _real(args, kw):
    """Run the real privacy scanner: it is local and offline."""
    proc = subprocess.run(args, cwd=kw.get("cwd"), capture_output=True, text=True, encoding="utf-8")
    return (proc.returncode, proc.stdout, proc.stderr)


def _tag(capsys, tmp_path, fake, *extra):
    return run_main(capsys, ["tag", "0.5.0", *extra, "--root", str(tmp_path)], run=fake)


def test_tag_success_runs_exactly_three_mutations_in_order(tmp_path, capsys):
    fake = tag_run(tmp_path)
    code, payload, out = _tag(capsys, tmp_path, fake)
    assert code == 0, payload
    muts = fake.mutations()
    assert muts[0] == ["git", "tag", "v0.5.0", SHA]
    assert muts[1] == ["git", "push", "origin", "refs/tags/v0.5.0"]
    assert muts[2][:4] == ["gh", "release", "create", "v0.5.0"]
    assert len(muts) == 3
    assert "--verify-tag" in muts[2] and "--notes-file" in muts[2]
    assert muts[2][muts[2].index("--title") + 1] == "v0.5.0"
    assert "/release verify 0.5.0" in out
    assert fake.calls[0] == ["git", "fetch", "origin", "--tags"]


def _scan_index(fake):
    return next(i for i, c in enumerate(fake.calls) if "streamsnow.tools.check_export_clean" in c)


def test_tag_scans_the_candidate_tree_before_any_mutation(tmp_path, capsys):
    fake = tag_run(tmp_path)
    code, _, _ = _tag(capsys, tmp_path, fake)
    assert code == 0
    scan = fake.calls[_scan_index(fake)]
    assert scan[scan.index("--denylist") + 1] == str(tmp_path / ".streamsnow/export-denylist.txt")
    first_mutation = fake.calls.index(fake.mutations()[0])
    assert _scan_index(fake) < first_mutation
    archive = next(c for c in fake.calls if c[:2] == ["git", "archive"])
    assert fake.calls.index(archive) < _scan_index(fake)


@pytest.mark.parametrize(
    "tree",
    [
        {"README.md": "# Acme\nBuilt for globex-internal.\n"},
        {"docs/notes.md": "Ticket INITECH-77 tracks this.\n"},
        # split so the repo's own privacy scan does not read this file as a leak
        {"README.md": "Mail ops@" + "acme-corp.com\n"},
    ],
)
def test_tag_refuses_a_leak_in_the_candidate_files(tmp_path, capsys, tree):
    fake = tag_run(tmp_path, tree=tree)
    code, _, out = _tag(capsys, tmp_path, fake)
    assert code == 1, out
    assert "privacy scan" in out
    assert "globex" not in out.lower() and "INITECH" not in out and "acme-corp" not in out
    assert fake.mutations() == []


def test_tag_without_a_denylist_scans_the_tree_generically_when_allowed(tmp_path, capsys):
    fake = tag_run(tmp_path, denylist=None)
    code, _, out = _tag(capsys, tmp_path, fake, "--allow-no-denylist")
    assert code == 0, out
    assert "--denylist" not in fake.calls[_scan_index(fake)]
    user = "acmedev"
    fake = tag_run(tmp_path, denylist=None, tree={"README.md": f"see /home/{user}/x\n"})
    code, _, _ = _tag(capsys, tmp_path, fake, "--allow-no-denylist")
    assert code == 1 and fake.mutations() == []


@pytest.mark.parametrize("name", ["../escape.txt", "/abs/escape.txt"])
def test_tag_refuses_an_archive_member_that_escapes(tmp_path, capsys, name):
    fake = tag_run(tmp_path)
    bad = tarfile.TarInfo(name)
    bad.size = 0
    fake.handlers.insert(0, (["git", "archive", "--format=tar", "-o"], _archive(None, bad)))
    code, _, out = _tag(capsys, tmp_path, fake)
    assert code == 2 and "escape" in out
    assert fake.mutations() == []


def test_tag_refuses_a_symlink_that_points_outside(tmp_path, capsys):
    fake = tag_run(tmp_path)
    link = tarfile.TarInfo("apps/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../outside"
    fake.handlers.insert(0, (["git", "archive", "--format=tar", "-o"], _archive(None, link)))
    code, _, out = _tag(capsys, tmp_path, fake)
    assert code == 2 and fake.mutations() == []


def test_tag_scanner_tool_error_refuses(tmp_path, capsys):
    fake = tag_run(tmp_path)
    fake.on([sys.executable, "-m", "streamsnow.tools.check_export_clean"], rc=2, out="boom")
    code, _, _ = _tag(capsys, tmp_path, fake)
    assert code == 2 and fake.mutations() == []


def test_tag_release_notes_are_the_changelog_section(tmp_path, capsys):
    captured = {}
    fake = tag_run(tmp_path)

    def create(args, kw):
        captured["notes"] = Path(args[args.index("--notes-file") + 1]).read_text(encoding="utf-8")
        return (0, "", "")

    fake.handlers.insert(0, (["gh", "release", "create", "v0.5.0"], create))
    code, _, _ = _tag(capsys, tmp_path, fake)
    assert code == 0
    assert "acme-report" in captured["notes"] and "## [0.4.2]" not in captured["notes"]


def _ci(status="completed", conclusion="success", event="push", name="ci"):
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "event": event,
        "createdAt": "2031-04-09T10:00:00Z",
    }


MOVED = PREFACE + "## [Unreleased]\n\n- Acme follow-up.\n\n## [0.5.0] - 2031-04-09\n\n- x\n"


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"remote_main": "b" * 40}, "origin/main"),
        ({"origin_version": "0.4.9"}, "origin/main"),
        ({"lock_version": "0.4.9"}, "uv.lock"),
        ({"subject": "feat: acme follow-up (#43)"}, "moved past the release commit"),
        ({"changelog": MOVED}, "moved past the release commit"),
        ({"local_tag": True}, "already exists"),
        ({"remote_refs": f"{SHA}\trefs/tags/v0.5.0\n"}, "already exists"),
        ({"runs": [_ci(status="in_progress", conclusion="")]}, "still running"),
        ({"runs": [_ci(conclusion="failure")]}, "did not succeed"),
        ({"runs": []}, "no `ci` push run"),
        ({"runs": [_ci(name="labels")]}, "no `ci` push run"),
        ({"runs": [_ci(event="pull_request")]}, "no `ci` push run"),
        ({"changelog": PREFACE + "## [Unreleased]\n\n" + OLD_SECTION}, "changelog"),
        ({"messages": "fix: globex-internal leak\x00"}, "denylist"),
        ({"denylist": None}, "--allow-no-denylist"),
    ],
)
def test_tag_refuses_and_mutates_nothing(tmp_path, capsys, kw, reason):
    fake = tag_run(tmp_path, **kw)
    code, payload, out = _tag(capsys, tmp_path, fake)
    assert code == 1, out
    assert reason in out
    assert "globex" not in out
    assert fake.mutations() == []


def test_tag_allow_no_denylist_still_scans_home_paths(tmp_path, capsys):
    fake = tag_run(tmp_path, denylist=None)
    code, _, out = _tag(capsys, tmp_path, fake, "--allow-no-denylist")
    assert code == 0, out
    assert "NO DENYLIST" in out
    user = "acmedev"
    fake = tag_run(tmp_path, denylist=None, messages=f"fix: /home/{user}/x\x00")
    code, _, _ = _tag(capsys, tmp_path, fake, "--allow-no-denylist")
    assert code == 1 and fake.mutations() == []


def test_tag_allows_a_release_branch_with_the_tag_name(tmp_path, capsys):
    # RELEASING.md: vX.Y.Z branches are the convention; refs/tags/ keeps the push unambiguous
    fake = tag_run(tmp_path, remote_refs=f"{SHA}\trefs/heads/v0.5.0\n")
    code, _, out = _tag(capsys, tmp_path, fake)
    assert code == 0, out
    assert ["git", "push", "origin", "refs/tags/v0.5.0"] in fake.calls


def test_tag_ignores_unrelated_workflows(tmp_path, capsys):
    runs = [_ci(), _ci(name="acme-stale", status="in_progress", conclusion="")]
    fake = tag_run(tmp_path, runs=runs)
    code, _, _ = _tag(capsys, tmp_path, fake)
    assert code == 0


def test_tag_refuses_on_a_failed_fetch_without_mutating(tmp_path, capsys):
    fake = tag_run(tmp_path).on(["git", "fetch", "origin", "--tags"], rc=128, err="no network")
    code, _, _ = _tag(capsys, tmp_path, fake)
    assert code == 2 and fake.mutations() == []


def test_tag_pending_ci_never_polls(tmp_path, capsys):
    fake = tag_run(tmp_path, runs=[_ci(status="queued", conclusion="")])
    _tag(capsys, tmp_path, fake)
    assert sum(c[:3] == ["gh", "run", "list"] for c in fake.calls) == 1


def test_tag_push_ok_but_release_failed_says_so(tmp_path, capsys):
    fake = tag_run(tmp_path).on(["gh", "release", "create", "v0.5.0"], rc=1, err="boom")
    code, _, out = _tag(capsys, tmp_path, fake)
    assert code == 1
    assert "--release-only" in out and "publishing has started" in out


def _push_fails(fake, tag_lands: bool):
    seen = {"n": 0}

    def ls_remote(args, kw):
        seen["n"] += 1
        landed = tag_lands and seen["n"] > 1
        return (0, f"{SHA}\trefs/tags/v0.5.0\n" if landed else "", "")

    fake.handlers.insert(0, (["git", "ls-remote", "origin", "refs/tags/v0.5.0"], ls_remote))
    fake.on(["git", "push", "origin", "refs/tags/v0.5.0"], rc=1, err="remote hung up")
    fake.on(["git", "tag", "-d", "v0.5.0"])
    return fake


def test_tag_push_failure_rechecks_origin_and_removes_the_local_tag(tmp_path, capsys):
    fake = _push_fails(tag_run(tmp_path), tag_lands=False)
    code, _, out = _tag(capsys, tmp_path, fake)
    assert code == 1
    assert ["git", "tag", "-d", "v0.5.0"] in fake.calls
    assert "not on origin" in out
    assert not any(c[:3] == ["gh", "release", "create"] for c in fake.calls)


def test_tag_push_failure_but_tag_landed_says_so(tmp_path, capsys):
    fake = _push_fails(tag_run(tmp_path), tag_lands=True)
    code, _, out = _tag(capsys, tmp_path, fake)
    assert code == 1
    assert ["git", "tag", "-d", "v0.5.0"] not in fake.calls
    assert "is on origin" in out and "--release-only" in out


def _release_only(tmp_path, view_rc=1, view_err="release not found", ancestor_rc=0, refs=None):
    fake = tag_run(tmp_path, remote_refs=f"{SHA}\trefs/tags/v0.5.0\n" if refs is None else refs)
    fake.on(["gh", "release", "view", "v0.5.0"], rc=view_rc, err=view_err)
    fake.on(["git", "merge-base", "--is-ancestor", SHA, "origin/main"], rc=ancestor_rc)
    return fake


def test_tag_release_only_creates_just_the_release(tmp_path, capsys):
    fake = _release_only(tmp_path)
    code, _, _ = _tag(capsys, tmp_path, fake, "--release-only")
    assert code == 0
    assert [m[:3] for m in fake.mutations()] == [["gh", "release", "create"]]


@pytest.mark.parametrize(
    ("kw", "code", "reason"),
    [
        ({"refs": ""}, 1, "not on origin"),
        ({"ancestor_rc": 1}, 1, "ancestor"),
        ({"view_rc": 0, "view_err": ""}, 1, "already exists"),
        ({"view_err": "HTTP 401: Bad credentials"}, 2, "gh release view"),
    ],
)
def test_tag_release_only_refusals(tmp_path, capsys, kw, code, reason):
    fake = _release_only(tmp_path, **kw)
    got, _, out = _tag(capsys, tmp_path, fake, "--release-only")
    assert got == code and reason in out
    assert fake.mutations() == []


# --------------------------------------------------------------------------- verify

SMOKE = ["uvx", "--refresh-package", "streamsnow", "--from", "streamsnow==0.5.0"]


def verify_run(runs, uvx_out="streamsnow 0.5.0\n", tag_on_origin=True) -> FakeRun:
    fake = FakeRun(git_passthrough=False)
    refs = f"{SHA}\trefs/tags/v0.5.0\n" if tag_on_origin else ""
    fake.on(["git", "ls-remote", "origin", "refs/tags/v0.5.0"], out=refs)
    fake.on(
        ["gh", "run", "list", "--workflow", "publish.yml", "--branch", "v0.5.0"],
        out=json.dumps(runs),
    )
    fake.on([*SMOKE, "streamsnow", "--version"], out=uvx_out)
    return fake


OK_RUN = [{"status": "completed", "conclusion": "success", "url": "https://example.com/r/3"}]


def _pypi(version="0.5.0"):
    fetched = []

    def fetch(url):
        fetched.append(url)
        return {"info": {"version": version}}

    return fetch, fetched


def test_verify_tag_missing_on_origin_exits_1(tmp_path, capsys):
    fake = verify_run(OK_RUN, tag_on_origin=False)
    code, _, out = run_main(capsys, ["verify", "0.5.0", "--root", str(tmp_path)], run=fake)
    assert code == 1 and "not on origin" in out


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
    fake = verify_run(OK_RUN)
    fetch, fetched = _pypi()
    code, payload, _ = run_main(
        capsys, ["verify", "0.5.0", "--root", str(tmp_path)], run=fake, fetch_json=fetch
    )
    assert code == 0, payload
    assert fetched == ["https://pypi.org/pypi/streamsnow/0.5.0/json"]
    assert set(statuses(payload).values()) == {"PASS"}
    assert [*SMOKE, "streamsnow", "--version"] in fake.calls


def test_verify_pypi_404_is_pending(tmp_path, capsys):
    def fetch(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    code, _, out = run_main(
        capsys,
        ["verify", "0.5.0", "--root", str(tmp_path)],
        run=verify_run(OK_RUN),
        fetch_json=fetch,
    )
    assert code == 3 and "pending" in out


def test_verify_pypi_wrong_version_fails(tmp_path, capsys):
    fetch, _ = _pypi("0.4.2")
    code, payload, _ = run_main(
        capsys,
        ["verify", "0.5.0", "--root", str(tmp_path)],
        run=verify_run(OK_RUN),
        fetch_json=fetch,
    )
    assert code == 1 and statuses(payload)["pypi release"] == "FAIL"


@pytest.mark.parametrize("out", ["streamsnow 0.5.0.dev1\n", "acme 0.5.0\n", "streamsnow 10.5.0\n"])
def test_verify_smoke_output_must_match_exactly(tmp_path, capsys, out):
    fetch, _ = _pypi()
    code, payload, _ = run_main(
        capsys,
        ["verify", "0.5.0", "--root", str(tmp_path)],
        run=verify_run(OK_RUN, uvx_out=out),
        fetch_json=fetch,
    )
    assert code == 1 and statuses(payload)["smoke test"] == "FAIL"


# --------------------------------------------------------------------------- skill + doc drift

SKILL = REPO_ROOT / ".claude" / "skills" / "release" / "SKILL.md"
ALLOWED = [
    "Bash(uv run python scripts/release.py suggest)",
    "Bash(uv run python scripts/release.py prepare *)",
    "Bash(uv run python scripts/release.py open-pr *)",
    "Bash(uv run python scripts/release.py gates *)",
    "Bash(uv run python scripts/release.py verify *)",
    "Bash(git status *)",
    "Bash(git fetch origin)",
    "Bash(git switch -c claude/release-* origin/main)",
]


def _frontmatter_and_body(text: str) -> tuple[str, str]:
    assert text.startswith("---\n")
    _, fm, body = text.split("---\n", 2)
    return fm, body


def _allowed_tools(fm: str) -> list[str]:
    line = re.search(r"^allowed-tools: (\[.+\])$", fm, re.M).group(1)
    return json.loads(line)


def test_release_skill_runs_on_haiku_and_only_by_hand():
    fm, body = _frontmatter_and_body(SKILL.read_text(encoding="utf-8"))
    assert re.search(r"^name: release$", fm, re.M)
    assert re.search(r"^model: haiku$", fm, re.M)
    assert re.search(r"^disable-model-invocation: true$", fm, re.M)
    assert re.search(r"^argument-hint: .+", fm, re.M)
    assert re.search(r"^description: .{40,}", fm, re.M)
    assert len(body.splitlines()) <= 80
    assert "—" not in body and "–" not in body
    assert "@" not in body  # no email address lives in the tree; the model passes its own


def test_release_skill_pre_approves_only_the_narrow_patterns():
    fm, _ = _frontmatter_and_body(SKILL.read_text(encoding="utf-8"))
    tools = _allowed_tools(fm)
    assert tools == ALLOWED
    for entry in tools:
        for banned in ("push", "tag", "commit", "--amend", "--no-verify", "gh ", "publish"):
            assert banned not in entry, (entry, banned)


def _matches(pattern: str, command: str) -> bool:
    """Claude Code's Bash(...) rule: `*` matches anything, the rest is literal."""
    inner = pattern[len("Bash(") : -1]
    return re.fullmatch(".*".join(map(re.escape, inner.split("*"))), command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "uv run python scripts/release.py tag 0.5.0",
        "uv run python scripts/release.py tag 0.5.0 --release-only",
        "uv run python scripts/release.py tag 0.5.0 --allow-no-denylist",
        "git push origin refs/tags/v0.5.0",
        "git push --tags",
        "git commit --amend --no-edit",
        "git commit --no-verify -m x",
        "gh release create v0.5.0",
    ],
)
def test_no_pre_approved_pattern_covers_an_irreversible_command(command):
    fm, _ = _frontmatter_and_body(SKILL.read_text(encoding="utf-8"))
    hits = [p for p in _allowed_tools(fm) if _matches(p, command)]
    assert not hits, (command, hits)


def test_allow_no_denylist_is_never_pre_approved():
    # The flag only exists on `tag`, which no pattern covers; on the pre-approved
    # subcommands argparse rejects it (see test_the_denylist_bypass_exists_only_on_tag).
    fm, body = _frontmatter_and_body(SKILL.read_text(encoding="utf-8"))
    assert "--allow-no-denylist" not in fm
    assert "permission prompt" in body


def test_release_skill_never_tells_the_agent_to_push_tag_or_release_directly():
    _, body = _frontmatter_and_body(SKILL.read_text(encoding="utf-8"))
    banned = r"git tag|git push|git commit|gh release|gh pr|uv publish|refs/tags"
    for line in body.splitlines():
        if re.search(banned, line):
            assert "never" in line.lower(), line


def test_release_skill_states_the_maintainer_exception():
    _, body = _frontmatter_and_body(SKILL.read_text(encoding="utf-8"))
    assert ".claude/CLAUDE.md" in body and "exception" in body


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
        ("lint-and-test", '"ci"'),
        ("refs/tags/vX.Y.Z", "refs/tags/"),
        ("GitHub Release", '"--verify-tag"'),
    ]
    for doc_kw, src_kw in pairs:
        assert doc_kw in doc, f"RELEASING.md no longer mentions {doc_kw!r}; update release.py"
        assert src_kw in src, f"release.py no longer implements {src_kw!r} ({doc_kw})"
    assert len(release.GATE_NAMES) == 7


# Update only after reading the new "Cut a release" text against scripts/release.py.
CUT_A_RELEASE_SHA256 = "51efcd751ec6754f80c61fd5dd00571d937b612fee491259874383a0e9ae0f26"


def _normalized_section(doc: str, heading: str) -> str:
    m = re.search(rf"^{re.escape(heading)}\n(.*?)(?=^## )", doc, re.M | re.S)
    assert m, (
        f"RELEASING.md has no {heading!r} section followed by another `## ` heading; "
        "the release script and this pin need a review"
    )
    lines = [ln.rstrip() for ln in m.group(1).replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def test_section_normalization_ignores_whitespace_only_edits():
    a = "## Cut a release\n\n1. Bump.\n\n## Next\n"
    b = "## Cut a release\n1. Bump.   \n\n\n\n## Next\n"
    assert _normalized_section(a, "## Cut a release") == _normalized_section(b, "## Cut a release")
    with pytest.raises(AssertionError, match="no '## Cut a release' section"):
        _normalized_section("## Cut a release\n\n1. Bump.\n", "## Cut a release")


def test_cut_a_release_section_is_pinned():
    doc = (REPO_ROOT / "RELEASING.md").read_text(encoding="utf-8")
    section = _normalized_section(doc, "## Cut a release")
    digest = hashlib.sha256(section.encode("utf-8")).hexdigest()
    assert digest == CUT_A_RELEASE_SHA256, (
        "RELEASING.md's 'Cut a release' section changed. Review scripts/release.py and "
        ".claude/skills/release/SKILL.md against the new procedure, update them to match, "
        f"then set CUT_A_RELEASE_SHA256 in this file to {digest}."
    )
