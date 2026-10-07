#!/usr/bin/env python3
"""Deterministic, fail-closed release steps for StreamSnow maintainers (`/release`).

A PyPI publish and a pushed tag cannot be taken back, and RELEASING.md spreads the
procedure over a dozen manual steps: a four-file version bump, closing the changelog,
the privacy scan with a local denylist, a docs link sweep, a green `main`, and an
unambiguous tag refspec. A half-done bump ships a wheel and a plugin that disagree about
their version; `git push origin vX.Y.Z` stops mid-release when a branch shares the tag's
name; a tag on a commit with red CI publishes a broken package to everyone. This script
holds every one of those judgments so the `/release` skill can be driven by a small model
that only runs commands and reports their output. RELEASING.md stays the source of truth;
`tests/test_release_script.py` fails when the two drift apart.

Subcommands (all accept ``--root DIR`` and ``--format md|json``):

    suggest              read-only: recommend patch, minor or major from [Unreleased]
    prepare X.Y.Z        bump the version files, refresh uv.lock, close the changelog,
                         optionally raise the generated-workflow pin floor (--pin-floor)
    gates X.Y.Z          read-only: the release gates, each PASS, FAIL or WARN
    tag X.Y.Z            tag origin/main and create the GitHub Release, only when every
                         precondition holds (--release-only retries just the Release)
    verify X.Y.Z         read-only: the publish run, PyPI, and a smoke test, checked once

Exit codes: 0 pass, 1 a gate failed or a precondition refused, 2 tool error (bad
arguments, a missing tool, an unreadable file), 3 (verify only) still pending.

Stdlib only. Every function that shells out takes an injectable ``run`` (default
``subprocess.run``) so the tests drive it offline.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

Run = Callable[..., subprocess.CompletedProcess]

PYPROJECT = "pyproject.toml"
PLUGIN_JSON = ".claude-plugin/plugin.json"
INIT_PY = "streamsnow/__init__.py"
UV_LOCK = "uv.lock"
CHANGELOG = "CHANGELOG.md"
DENYLIST = ".streamsnow/export-denylist.txt"
WALKTHROUGH = "skills/_shared/playwright-walkthrough.md"
DOCS_LINKS = "scripts/check_docs_links.py"
PIN_SOURCE = "streamsnow/_templates/repo/ci.yml.j2"
PIN_FILES = (
    PIN_SOURCE,
    "streamsnow/_templates/repo/deploy.yml.j2",
    "streamsnow/_templates/repo/deploy.git.yml.j2",
    "README.md",
    "docs/deploying.md",
    "docs/distribution.md",
)
PYPI_JSON = "https://pypi.org/pypi/streamsnow/json"

GATE_NAMES = (
    "git tree clean",
    "version lockstep",
    "changelog closed",
    "privacy scan",
    "commit messages",
    "docs links",
    "playwright pin",
)

UNRELEASED = "## [Unreleased]"
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_VERSION_PATTERNS = {
    PYPROJECT: re.compile(r'^(version = ")([^"]+)(")$', re.M),
    PLUGIN_JSON: re.compile(r'^(\s*"version": ")([^"]+)(",?)$', re.M),
    INIT_PY: re.compile(r'^(\s+__version__ = ")([^"]+)(")$', re.M),
}
_LOCK_VERSION = re.compile(r'^name = "streamsnow"\nversion = "([^"]+)"$', re.M)
_PIN = re.compile(r"streamsnow>=(\d+\.\d+(?:\.\d+)?),<(\d+)\.(\d+)(?![\d.])")
_DATED = r"^## \[{v}\] - \d{{4}}-\d{{2}}-\d{{2}}$"
_HOME_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")
_PLAYWRIGHT_PIN = re.compile(r"@playwright/cli@(\d+\.\d+\.\d+)")


class ToolError(Exception):
    """Exit 2: bad arguments, a missing tool, or a file the script cannot read safely."""


class Refused(Exception):
    """Exit 1: a precondition does not hold. Nothing was changed."""


@dataclass
class Result:
    name: str
    status: str  # PASS, FAIL, WARN
    detail: str = ""


@dataclass
class Report:
    command: str
    version: str | None = None
    exit: int = 0
    message: str = ""
    next: str = ""
    results: list[Result] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str = "") -> Result:
        r = Result(name, status, detail)
        self.results.append(r)
        return r


# --------------------------------------------------------------------------- helpers


def _exec(run: Run, args: list[str], root: Path) -> subprocess.CompletedProcess:
    try:
        return run(args, cwd=str(root), capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError as exc:
        raise ToolError(f"`{args[0]}` was not found on PATH") from exc


def _out(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or "").strip()


def _err(proc: subprocess.CompletedProcess) -> str:
    text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    return text.splitlines()[-1] if text else f"exit {proc.returncode}"


def parse_semver(text: str) -> tuple[int, int, int]:
    m = _SEMVER.match(text)
    if not m:
        raise ToolError(f"{text!r} is not a version of the form X.Y.Z (no leading v)")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.is_file():
        raise ToolError(f"{rel} not found under {root}")
    return path.read_text(encoding="utf-8")


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).write_text(text, encoding="utf-8", newline="\n")


def extract_version(rel: str, text: str) -> str:
    """The one version string in ``text``; anything but exactly one match is a tool error."""
    found = _VERSION_PATTERNS[rel].findall(text)
    if len(found) != 1:
        raise ToolError(f"{rel}: expected exactly one version line, found {len(found)}")
    return found[0][1]


def replace_version(rel: str, text: str, new: str) -> str:
    extract_version(rel, text)
    out = _VERSION_PATTERNS[rel].sub(lambda m: m.group(1) + new + m.group(3), text)
    if extract_version(rel, out) != new:
        raise ToolError(f"{rel}: version substitution did not take")
    if rel == PLUGIN_JSON and json.loads(out).get("version") != new:
        raise ToolError(f"{rel}: the rewritten file does not parse to version {new}")
    return out


def lock_version(text: str) -> str | None:
    m = _LOCK_VERSION.search(text)
    return m.group(1) if m else None


def split_unreleased(changelog: str) -> tuple[str, str, str]:
    """(everything through the [Unreleased] heading, its body, the rest from the next ## [)."""
    lines = changelog.split("\n")
    try:
        start = lines.index(UNRELEASED)
    except ValueError as exc:
        raise ToolError(f"{CHANGELOG} has no `{UNRELEASED}` heading") from exc
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")), len(lines))
    head = "\n".join(lines[: start + 1])
    body = "\n".join(lines[start + 1 : end])
    rest = "\n".join(lines[end:])
    return head, body, rest


def close_changelog(changelog: str, version: str, today: date) -> str:
    head, body, rest = split_unreleased(changelog)
    if not body.strip():
        raise Refused("nothing to release: `## [Unreleased]` is empty")
    if re.search(rf"^## \[{re.escape(version)}\]", changelog, re.M):
        raise Refused(f"{CHANGELOG} already has a `## [{version}]` section")
    section = f"## [{version}] - {today.isoformat()}\n\n{body.strip()}\n"
    return f"{head}\n\n{section}\n{rest}" if rest else f"{head}\n\n{section}"


def changelog_section(changelog: str, version: str) -> str | None:
    """Body of the dated ``## [version] - date`` section, or None when it is missing."""
    lines = changelog.split("\n")
    pat = re.compile(_DATED.format(v=re.escape(version)))
    for i, line in enumerate(lines):
        if pat.match(line):
            end = next(
                (j for j in range(i + 1, len(lines)) if lines[j].startswith("## [")), len(lines)
            )
            return "\n".join(lines[i + 1 : end]).strip()
    return None


def load_denylist(path: Path) -> tuple[list[str], list[re.Pattern[str]]]:
    """Same format as the privacy scanner: a term per line, ``re:`` for a regex, ``#`` comments."""
    terms: list[str] = []
    patterns: list[re.Pattern[str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            try:
                patterns.append(re.compile(line[3:], re.IGNORECASE))
            except re.error:
                continue
        else:
            terms.append(line.lower())
    return terms, patterns


# --------------------------------------------------------------------------- suggest

_PATCH_HEADINGS = {"fixed", "docs", "documentation", "security"}
_BREAK_HEADINGS = {"breaking", "removed"}


def suggest(root: Path) -> Report:
    rep = Report("suggest")
    current = extract_version(PYPROJECT, _read(root, PYPROJECT))
    major, minor, patch = parse_semver(current)
    _, body, _ = split_unreleased(_read(root, CHANGELOG))
    if not body.strip():
        rep.exit = 1
        rep.message = "nothing to release: `## [Unreleased]` is empty"
        return rep
    headings: set[str] = set()
    untyped = False
    breaking: list[str] = []
    heading = None
    for line in body.splitlines():
        h = re.match(r"^###\s+(.+?)\s*$", line)
        if h:
            heading = h.group(1).lower()
            headings.add(heading)
            if heading in _BREAK_HEADINGS:
                breaking.append(f"a `### {h.group(1)}` section")
            continue
        if not line.strip():
            continue
        if heading is None:
            untyped = True
        if "BREAKING" in line:
            breaking.append("an entry marked BREAKING")
        elif re.search(r"(?i)\b(renamed|removed)\b", line) and "`" in line:
            breaking.append("an entry that renames or removes a named item")
    if breaking:
        bump = "major" if major >= 1 else "minor"
        reason = "found " + ", ".join(dict.fromkeys(breaking))
        reason += (
            "; from 1.0 that needs a major release"
            if major >= 1
            else "; before 1.0 a breaking change can land in a minor release"
        )
    elif untyped or headings - _PATCH_HEADINGS:
        bump = "minor"
        kinds = sorted(headings - _PATCH_HEADINGS)
        parts = [f"`### {k.title()}`" for k in kinds]
        if untyped:
            parts.append("entries with no `###` heading")
        reason = "found " + ", ".join(parts)
    else:
        bump = "patch"
        reason = "only " + ", ".join(f"`### {k.title()}`" for k in sorted(headings)) + " entries"
    nxt = {
        "major": f"{major + 1}.0.0",
        "minor": f"{major}.{minor + 1}.0",
        "patch": f"{major}.{minor}.{patch + 1}",
    }[bump]
    rep.version = current
    rep.message = f"current {current}; recommended bump: {bump} -> {nxt} ({reason})"
    if breaking:
        rep.message += ". The maintainer decides: this looks breaking."
    rep.next = "the maintainer picks the version, then: /release prepare X.Y.Z"
    rep.extra = {
        "current": current,
        "bump": bump,
        "suggested": nxt,
        "reason": reason,
        "needs_decision": bool(breaking),
    }
    return rep


# --------------------------------------------------------------------------- prepare


def _git_clean(run: Run, root: Path) -> tuple[bool, str]:
    proc = _exec(run, ["git", "status", "--porcelain"], root)
    if proc.returncode != 0:
        raise ToolError(f"git status failed: {_err(proc)}")
    dirty = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return (not dirty, f"{len(dirty)} uncommitted path(s)" if dirty else "clean")


def plan_pin_floor(root: Path, version: str) -> dict[str, str]:
    """New text for every pin file. Raises ToolError, before anything is written, on drift."""
    major, minor, _ = parse_semver(version)
    m = _PIN.search(_read(root, PIN_SOURCE))
    if not m:
        raise ToolError(f"{PIN_SOURCE}: no `streamsnow>=A.B[.C],<A.B` pin found")
    current = m.group(0)
    new = f"streamsnow>={version},<{major}.{minor + 1}"
    exact = re.compile(re.escape(current) + r"(?![\d.])")
    planned: dict[str, str] = {}
    for rel in PIN_FILES:
        text = _read(root, rel)
        if not exact.search(text):
            raise ToolError(f"{rel}: the current pin `{current}` is not there; fix it by hand")
        planned[rel] = exact.sub(new, text)
    return planned


def prepare(version: str, root: Path, run: Run, today: date, pin_floor: bool) -> Report:
    rep = Report("prepare", version)
    new = parse_semver(version)
    texts = {rel: _read(root, rel) for rel in _VERSION_PATTERNS}
    found = {rel: extract_version(rel, t) for rel, t in texts.items()}
    clean, detail = _git_clean(run, root)
    if not clean:
        raise Refused(f"git tree is not clean ({detail}); commit or stash first")
    rep.add("git tree clean", "PASS", detail)
    if len(set(found.values())) != 1:
        raise Refused(f"version files disagree before the bump: {found}")
    current = found[PYPROJECT]
    if new <= parse_semver(current):
        raise Refused(f"{version} is not greater than the current version {current}")
    rep.add("version", "PASS", f"{current} -> {version}")
    new_log = close_changelog(_read(root, CHANGELOG), version, today)
    pins = plan_pin_floor(root, version) if pin_floor else {}

    for rel, text in texts.items():
        _write(root, rel, replace_version(rel, text, version))
        rep.add(f"bump {rel}", "PASS", version)
    _write(root, CHANGELOG, new_log)
    rep.add("close changelog", "PASS", f"## [{version}] - {today.isoformat()}")
    for rel, text in pins.items():
        _write(root, rel, text)
        rep.add(f"pin floor {rel}", "PASS", _PIN.search(text).group(0))

    for args in (["uv", "lock"], ["uv", "lock", "--check"]):
        proc = _exec(run, args, root)
        if proc.returncode != 0:
            rep.add(" ".join(args), "FAIL", _err(proc))
            rep.exit = 1
            rep.message = (
                "the files above were edited but uv.lock is not consistent; fix and rerun gates"
            )
            return rep
        rep.add(" ".join(args), "PASS")
    got = lock_version(_read(root, UV_LOCK))
    if got != version:
        rep.add("uv.lock version", "FAIL", f"uv.lock has streamsnow {got}")
        rep.exit = 1
        return rep
    rep.add("uv.lock version", "PASS", version)
    rep.message = "prepared; nothing was committed or pushed"
    rep.next = f"uv run python scripts/release.py gates {version} --online"
    return rep


# --------------------------------------------------------------------------- gates


def _gate_lockstep(version: str, root: Path, run: Run) -> Result:
    found = {rel: extract_version(rel, _read(root, rel)) for rel in _VERSION_PATTERNS}
    found[UV_LOCK] = lock_version(_read(root, UV_LOCK)) or "missing"
    off = {rel: v for rel, v in found.items() if v != version}
    if off:
        return Result("version lockstep", "FAIL", f"not {version}: {off}")
    proc = _exec(run, ["uv", "lock", "--check"], root)
    if proc.returncode != 0:
        return Result("version lockstep", "FAIL", f"uv lock --check: {_err(proc)}")
    return Result("version lockstep", "PASS", f"4 files at {version}; uv lock --check ok")


def _gate_changelog(version: str, root: Path) -> Result:
    log = _read(root, CHANGELOG)
    _, body, _ = split_unreleased(log)
    if changelog_section(log, version) is None:
        return Result("changelog closed", "FAIL", f"no `## [{version}] - YYYY-MM-DD` heading")
    if body.strip():
        return Result("changelog closed", "FAIL", "`## [Unreleased]` still has entries")
    return Result("changelog closed", "PASS", f"## [{version}] dated, [Unreleased] empty")


def _gate_privacy(root: Path, run: Run) -> Result:
    proc = _exec(run, [sys.executable, "-m", "streamsnow.tools.check_export_clean", "."], root)
    if proc.returncode != 0:
        return Result("privacy scan", "FAIL", f"check_export_clean exit {proc.returncode}")
    if not (root / DENYLIST).is_file():
        return Result("privacy scan", "WARN", "denylist not present, scan is generic only")
    return Result("privacy scan", "PASS", "clean with the local denylist")


def _gate_commits(root: Path, run: Run) -> Result:
    tag = _exec(run, ["git", "describe", "--tags", "--abbrev=0"], root)
    rng = f"{_out(tag)}..HEAD" if tag.returncode == 0 and _out(tag) else "HEAD"
    proc = _exec(run, ["git", "log", "--format=%h%x00%B%x1e", rng], root)
    if proc.returncode != 0:
        raise ToolError(f"git log failed: {_err(proc)}")
    commits = [c.strip("\n") for c in (proc.stdout or "").split("\x1e") if c.strip()]
    deny = root / DENYLIST
    terms, patterns = load_denylist(deny) if deny.is_file() else ([], [])
    hits: list[str] = []
    paths: list[str] = []
    for c in commits:
        sha, _, msg = c.partition("\x00")
        low = msg.lower()
        if any(t in low for t in terms) or any(p.search(msg) for p in patterns):
            hits.append(sha)
        if _HOME_PATH.search(msg):
            paths.append(sha)
    since = _out(tag) if tag.returncode == 0 and _out(tag) else "the first commit"
    detail = f"{len(commits)} commit(s) since {since}"
    if hits or paths:
        parts = []
        if hits:
            parts.append(f"{len(hits)} with a denylist term ({', '.join(hits)})")
        if paths:
            parts.append(f"{len(paths)} with a home path ({', '.join(paths)})")
        return Result("commit messages", "FAIL", f"{detail}: " + "; ".join(parts))
    if not deny.is_file():
        return Result("commit messages", "WARN", f"{detail}: no home paths; no denylist to check")
    return Result(
        "commit messages", "PASS", f"{detail}: clean against {len(terms) + len(patterns)} term(s)"
    )


def _gate_links(root: Path, run: Run, online: bool) -> Result:
    if not online:
        return Result("docs links", "WARN", "skipped; pass --online before tagging")
    proc = _exec(run, [sys.executable, DOCS_LINKS, "--online"], root)
    if proc.returncode != 0:
        return Result("docs links", "FAIL", _err(proc))
    return Result("docs links", "PASS", _err(proc) if _out(proc) else "all ok")


def _gate_playwright(root: Path, run: Run) -> Result:
    m = _PLAYWRIGHT_PIN.search(_read(root, WALKTHROUGH))
    if not m:
        return Result("playwright pin", "WARN", f"no @playwright/cli@X.Y.Z pin in {WALKTHROUGH}")
    try:
        proc = _exec(run, ["npm", "view", "@playwright/cli", "version"], root)
    except ToolError:
        return Result("playwright pin", "WARN", f"pinned {m.group(1)}; npm not found to compare")
    latest = _out(proc)
    if proc.returncode != 0 or not latest:
        return Result("playwright pin", "WARN", f"pinned {m.group(1)}; npm view failed")
    if latest != m.group(1):
        return Result(
            "playwright pin",
            "WARN",
            f"pinned {m.group(1)}, npm has {latest}; see RELEASING.md before bumping",
        )
    return Result("playwright pin", "PASS", f"{latest} is current")


def gates(version: str, root: Path, run: Run, online: bool) -> Report:
    rep = Report("gates", version)
    parse_semver(version)
    clean, detail = _git_clean(run, root)
    rep.results.append(Result("git tree clean", "PASS" if clean else "FAIL", detail))
    rep.results.append(_gate_lockstep(version, root, run))
    rep.results.append(_gate_changelog(version, root))
    rep.results.append(_gate_privacy(root, run))
    rep.results.append(_gate_commits(root, run))
    rep.results.append(_gate_links(root, run, online))
    rep.results.append(_gate_playwright(root, run))
    assert tuple(r.name for r in rep.results) == GATE_NAMES
    failed = [r.name for r in rep.results if r.status == "FAIL"]
    rep.exit = 1 if failed else 0
    rep.message = f"FAILED: {', '.join(failed)}" if failed else "all gates pass"
    return rep


# --------------------------------------------------------------------------- tag


def _versions_at(run: Run, root: Path, sha: str, version: str, label: str) -> None:
    for rel in _VERSION_PATTERNS:
        proc = _exec(run, ["git", "show", f"{sha}:{rel}"], root)
        if proc.returncode != 0:
            raise Refused(f"cannot read {rel} at {label}: {_err(proc)}")
        got = extract_version(rel, proc.stdout or "")
        if got != version:
            raise Refused(f"{label} has {rel} at {got}, not {version}; merge the release PR first")


def _changelog_at(run: Run, root: Path, sha: str, version: str) -> str:
    proc = _exec(run, ["git", "show", f"{sha}:{CHANGELOG}"], root)
    notes = changelog_section(proc.stdout or "", version) if proc.returncode == 0 else None
    if not notes:
        raise Refused(f"the changelog on {sha[:12]} has no dated `## [{version}]` section")
    return notes


def _remote_refs(run: Run, root: Path, *refs: str) -> dict[str, str]:
    proc = _exec(run, ["git", "ls-remote", "origin", *refs], root)
    if proc.returncode != 0:
        raise ToolError(f"git ls-remote origin failed: {_err(proc)}")
    out: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        sha, _, ref = line.partition("\t")
        if ref in refs:
            out[ref] = sha
    return out


def _create_release(run: Run, root: Path, tag_name: str, notes: str) -> subprocess.CompletedProcess:
    fd, path = tempfile.mkstemp(prefix="streamsnow-release-", suffix=".md")
    os.close(fd)
    try:
        Path(path).write_text(notes + "\n", encoding="utf-8")
        return _exec(
            run,
            [
                "gh",
                "release",
                "create",
                tag_name,
                "--verify-tag",
                "--title",
                tag_name,
                "--notes-file",
                path,
            ],
            root,
        )
    finally:
        Path(path).unlink(missing_ok=True)


def tag(version: str, root: Path, run: Run, release_only: bool = False) -> Report:
    rep = Report("tag", version)
    parse_semver(version)
    t = f"v{version}"
    tag_ref, head_ref = f"refs/tags/{t}", f"refs/heads/{t}"

    proc = _exec(run, ["git", "fetch", "origin", "--tags"], root)
    if proc.returncode != 0:
        raise ToolError(f"git fetch origin --tags failed: {_err(proc)}")
    rep.add("fetch origin", "PASS")

    if release_only:
        remote = _remote_refs(run, root, tag_ref)
        if tag_ref not in remote:
            raise Refused(f"{t} is not on origin; run `tag {version}` without --release-only")
        sha = remote[tag_ref]
        _versions_at(run, root, sha, version, f"tag {t}")
        rep.add("tag on origin", "PASS", f"{t} -> {sha[:12]} at version {version}")
        notes = _changelog_at(run, root, sha, version)
        view = _exec(run, ["gh", "release", "view", t], root)
        if view.returncode == 0:
            raise Refused(f"a GitHub Release for {t} already exists")
        rel = _create_release(run, root, t, notes)
        if rel.returncode != 0:
            rep.add("github release", "FAIL", _err(rel))
            rep.exit = 1
            rep.message = f"the Release was not created; rerun `tag {version} --release-only`"
            return rep
        rep.add("github release", "PASS", t)
        rep.message = f"GitHub Release {t} created"
        rep.next = f"/release verify {version}"
        return rep

    proc = _exec(run, ["git", "rev-parse", "--verify", "origin/main^{commit}"], root)
    if proc.returncode != 0 or not _out(proc):
        raise Refused("origin/main does not exist")
    sha = _out(proc)
    _versions_at(run, root, sha, version, "origin/main")
    rep.add("origin/main version", "PASS", f"{sha[:12]} at {version}")

    if _exec(run, ["git", "rev-parse", "-q", "--verify", tag_ref], root).returncode == 0:
        raise Refused(f"tag {t} already exists locally")
    remote = _remote_refs(run, root, tag_ref, head_ref)
    if tag_ref in remote:
        raise Refused(f"tag {t} already exists on origin")
    if head_ref in remote:
        raise Refused(f"a branch named {t} exists on origin; delete or rename it first")
    rep.add("tag is new", "PASS", f"no {t} tag locally or on origin, no {t} branch")

    proc = _exec(
        run,
        [
            "gh",
            "run",
            "list",
            "--commit",
            sha,
            "--json",
            "name,status,conclusion",
            "--limit",
            "100",
        ],
        root,
    )
    if proc.returncode != 0:
        raise ToolError(f"gh run list failed: {_err(proc)}")
    try:
        runs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ToolError(f"gh run list returned invalid JSON: {exc}") from exc
    if not runs:
        raise Refused(f"no workflow runs found for {sha[:12]}; CI has not run on it")
    pending = [r["name"] for r in runs if r.get("status") != "completed"]
    failed = [
        f"{r['name']} ({r.get('conclusion')})"
        for r in runs
        if r.get("status") == "completed" and r.get("conclusion") != "success"
    ]
    if pending:
        raise Refused(f"CI still running on {sha[:12]}: {', '.join(pending)}; try again later")
    if failed:
        raise Refused(f"CI did not succeed on {sha[:12]}: {', '.join(failed)}")
    rep.add("ci green", "PASS", f"{len(runs)} run(s) succeeded")

    notes = _changelog_at(run, root, sha, version)
    rep.add("changelog section", "PASS", f"## [{version}] on {sha[:12]}")

    proc = _exec(run, ["git", "tag", t, sha], root)
    if proc.returncode != 0:
        raise ToolError(f"git tag failed: {_err(proc)}")
    rep.add("git tag", "PASS", f"{t} -> {sha[:12]}")
    proc = _exec(run, ["git", "push", "origin", tag_ref], root)
    if proc.returncode != 0:
        _exec(run, ["git", "tag", "-d", t], root)
        rep.add("push tag", "FAIL", _err(proc))
        rep.exit = 1
        rep.message = "the push failed and the local tag was removed; nothing reached origin"
        return rep
    rep.add("push tag", "PASS", f"origin {tag_ref}; the publish workflow starts now")

    rel = _create_release(run, root, t, notes)
    if rel.returncode != 0:
        rep.add("github release", "FAIL", _err(rel))
        rep.exit = 1
        rep.message = (
            f"the tag is out and publishing has started, but the GitHub Release failed. "
            f"Rerun only that step: uv run python scripts/release.py tag {version} --release-only"
        )
        return rep
    rep.add("github release", "PASS", t)
    rep.message = f"{t} tagged and released; PyPI publishing is running"
    rep.next = f"/release verify {version}"
    return rep


# --------------------------------------------------------------------------- verify


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "streamsnow-release"})
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (fixed https URL)
        return json.loads(resp.read().decode("utf-8"))


def verify(version: str, root: Path, run: Run, fetch_json: Callable[[str], dict]) -> Report:
    rep = Report("verify", version)
    parse_semver(version)
    t = f"v{version}"
    proc = _exec(
        run,
        [
            "gh",
            "run",
            "list",
            "--workflow",
            "publish.yml",
            "--branch",
            t,
            "--json",
            "status,conclusion,url",
            "--limit",
            "1",
        ],
        root,
    )
    if proc.returncode != 0:
        raise ToolError(f"gh run list failed: {_err(proc)}")
    try:
        runs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ToolError(f"gh run list returned invalid JSON: {exc}") from exc
    if not runs:
        rep.add("publish run", "FAIL", f"no publish run for {t} yet")
        rep.exit = 3
        rep.message = "pending, check again later"
        return rep
    r = runs[0]
    if r.get("status") != "completed":
        rep.add("publish run", "FAIL", f"{r.get('status')}: {r.get('url', '')}")
        rep.exit = 3
        rep.message = "pending, check again later"
        return rep
    if r.get("conclusion") != "success":
        rep.add("publish run", "FAIL", f"{r.get('conclusion')}: {r.get('url', '')}")
        rep.exit = 1
        rep.message = f"the publish workflow failed: {r.get('url', '')}"
        return rep
    rep.add("publish run", "PASS", r.get("url", ""))
    try:
        latest = (fetch_json(PYPI_JSON).get("info") or {}).get("version")
    except Exception as exc:  # network, HTTP or JSON error
        raise ToolError(f"could not read {PYPI_JSON}: {exc}") from exc
    if latest != version:
        rep.add("pypi latest", "FAIL", f"PyPI reports {latest}; the index can lag a few minutes")
        rep.exit = 1
        rep.message = "PyPI does not report this version as latest yet"
        return rep
    rep.add("pypi latest", "PASS", version)
    proc = _exec(run, ["uvx", "--from", f"streamsnow=={version}", "streamsnow", "--version"], root)
    if proc.returncode != 0 or version not in (proc.stdout or ""):
        rep.add("smoke test", "FAIL", _err(proc))
        rep.exit = 1
        rep.message = "the published package did not report its version"
        return rep
    rep.add("smoke test", "PASS", _out(proc))
    rep.message = f"{version} is live on PyPI"
    return rep


# --------------------------------------------------------------------------- output + CLI


def render(rep: Report, fmt: str) -> str:
    if fmt == "json":
        data = {k: v for k, v in asdict(rep).items() if k != "extra"}
        data.update(rep.extra)
        data["ok"] = rep.exit == 0
        return json.dumps(data, indent=2)
    head = f"release {rep.command}" + (f" {rep.version}" if rep.version else "")
    lines = [f"## {head}", ""]
    lines += [
        f"- {r.status:<4} {r.name}" + (f": {r.detail}" if r.detail else "") for r in rep.results
    ]
    if rep.results:
        lines.append("")
    if rep.message:
        lines.append(rep.message)
    if rep.next:
        lines.append(f"Next: {rep.next}")
    lines.append(f"Exit: {rep.exit}")
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    run: Run = subprocess.run,
    today: date | None = None,
    fetch_json: Callable[[str], dict] = _fetch_json,
) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=".", help="Repository root (default: .).")
    common.add_argument("--format", choices=("md", "json"), default="md")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("suggest", parents=[common], help="Recommend a version bump.")
    p = sub.add_parser("prepare", parents=[common], help="Bump, close the changelog, lock.")
    p.add_argument("version")
    p.add_argument("--pin-floor", action="store_true")
    g = sub.add_parser("gates", parents=[common], help="Run the release gates (read-only).")
    g.add_argument("version")
    g.add_argument("--online", action="store_true")
    t = sub.add_parser("tag", parents=[common], help="Tag origin/main and create the Release.")
    t.add_argument("version")
    t.add_argument("--release-only", action="store_true")
    v = sub.add_parser("verify", parents=[common], help="Check the publish landed (read-only).")
    v.add_argument("version")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2

    root = Path(args.root).resolve()
    version = getattr(args, "version", None)
    rep = Report(args.cmd, version)
    try:
        if args.cmd == "suggest":
            rep = suggest(root)
        elif args.cmd == "prepare":
            rep = prepare(version, root, run, today or date.today(), args.pin_floor)
        elif args.cmd == "gates":
            rep = gates(version, root, run, args.online)
        elif args.cmd == "tag":
            rep = tag(version, root, run, args.release_only)
        else:
            rep = verify(version, root, run, fetch_json)
    except Refused as exc:
        rep.exit = 1
        rep.message = f"REFUSED: {exc}. Nothing was changed."
    except ToolError as exc:
        rep.exit = 2
        rep.message = f"ERROR: {exc}"
    print(render(rep, args.format))
    return rep.exit


if __name__ == "__main__":
    sys.exit(main())
