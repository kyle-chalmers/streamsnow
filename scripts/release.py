#!/usr/bin/env python3
"""Deterministic, fail-closed release steps for StreamSnow maintainers (`/release`).

A PyPI publish and a pushed tag cannot be taken back, and RELEASING.md spreads the
procedure over a dozen manual steps: a four-file version bump, closing the changelog,
the privacy scan with a local denylist, a docs link sweep, a green `main`, and an
unambiguous tag refspec. A half-done bump ships a wheel and a plugin that disagree about
their version; `git push origin vX.Y.Z` stops mid-release when a branch shares the tag's
name; a tag on a commit with red CI, or on a commit merged after the release PR, publishes
the wrong code to everyone. This script holds every one of those judgments so the
`/release` skill can be driven by a small model that only runs commands and reports their
output. RELEASING.md stays the source of truth; `tests/test_release_script.py` fails when
the two drift apart.

Subcommands (all accept ``--root DIR`` and ``--format md|json``):

    suggest              read-only: recommend patch, minor or major from [Unreleased]
    prepare X.Y.Z        bump the version files, refresh uv.lock, close the changelog,
                         optionally raise the generated-workflow pin floor (--pin-floor)
    open-pr X.Y.Z        commit prepare's edits on claude/release-X.Y.Z, run the gates on
                         the clean tree, push that branch and open the release PR
    gates X.Y.Z          read-only: the release gates, each PASS, FAIL or WARN
    tag X.Y.Z            tag the release commit on origin/main and create the GitHub
                         Release, only when every precondition holds (--release-only
                         retries just the Release)
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
import tarfile
import tempfile
import tomllib
import urllib.error
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
# docs/deploying.md also names the pin, inside a paragraph of upgrade history; rewriting it
# would change what earlier releases said, so prepare asks for a hand edit instead.
PIN_FILES = (
    PIN_SOURCE,
    "streamsnow/_templates/repo/deploy.yml.j2",
    "streamsnow/_templates/repo/deploy.git.yml.j2",
    "README.md",
    "docs/distribution.md",
)
PIN_HISTORY = "docs/deploying.md"
VERSION_FILES = (PYPROJECT, PLUGIN_JSON, INIT_PY)
RELEASE_EDITS = frozenset({*VERSION_FILES, UV_LOCK, CHANGELOG, *PIN_FILES, PIN_HISTORY})
CI_WORKFLOW = "ci"
PYPI_JSON = "https://pypi.org/pypi/streamsnow/{version}/json"

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
_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_TRAILER = re.compile(r"Co-Authored-By: [^<>\x00-\x1f\x7f]+ <[^<>\s\x00-\x1f\x7f]+>")
_PRIVATE_NOTE = re.compile(r"/Users/|/home/|@")
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
# gates and open-pr are pre-approved in the /release skill, so they take no bypass flag;
# only `tag`, which always shows a permission prompt, accepts --allow-no-denylist.
_NO_DENYLIST_GATES = (
    "no {d} found in the repo root or the main worktree; the gates need it, so add it there"
)
_NO_DENYLIST = (
    "no {d} found in the repo root or the main worktree; add it, or pass "
    "--allow-no-denylist to accept a generic-only check"
)


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
    m = _SEMVER.fullmatch(text)
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
    version = found[0][1]
    if rel == PYPROJECT:
        try:
            project = tomllib.loads(text).get("project", {})
        except tomllib.TOMLDecodeError as exc:
            raise ToolError(f"{rel}: not valid TOML: {exc}") from exc
        if project.get("version") != version:
            raise ToolError(f"{rel}: the version line is not the [project] version")
    return version


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


def find_denylist(root: Path, run: Run) -> Path | None:
    """The denylist in the repo root, else in the main worktree (worktrees do not share it)."""
    here = root / DENYLIST
    if here.is_file():
        return here
    # The first entry of `git worktree list` is always the main worktree, even when the
    # common dir lives elsewhere (a bare repo or a separate --git-dir).
    proc = _exec(run, ["git", "worktree", "list", "--porcelain"], root)
    if proc.returncode == 0:
        first = next((ln for ln in (proc.stdout or "").splitlines() if ln.strip()), "")
        if first.startswith("worktree "):
            main = Path(first[len("worktree ") :]) / DENYLIST
            if main.is_file():
                return main
    return None


def _last_tag(run: Run, root: Path, end: str) -> str | None:
    proc = _exec(run, ["git", "describe", "--tags", "--abbrev=0", end], root)
    return _out(proc) if proc.returncode == 0 and _out(proc) else None


def scan_commits(run: Run, root: Path, end: str, deny: Path | None) -> dict:
    """Count commit messages since the last tag with a denylist term or a home path."""
    tag = _last_tag(run, root, end)
    rng = f"{tag}..{end}" if tag else end
    proc = _exec(run, ["git", "log", "--format=%h%x00%B%x1e", rng], root)
    if proc.returncode != 0:
        raise ToolError(f"git log failed: {_err(proc)}")
    commits = [c.strip("\n") for c in (proc.stdout or "").split("\x1e") if c.strip()]
    terms, patterns = load_denylist(deny) if deny else ([], [])
    hits: list[str] = []
    paths: list[str] = []
    for c in commits:
        sha, _, msg = c.partition("\x00")
        low = msg.lower()
        if any(t in low for t in terms) or any(p.search(msg) for p in patterns):
            hits.append(sha)
        if _HOME_PATH.search(msg):
            paths.append(sha)
    return {
        "count": len(commits),
        "since": tag or "the first commit",
        "hits": hits,
        "paths": paths,
        "terms": len(terms) + len(patterns),
    }


def denylist_hits(text: str, deny: Path) -> int:
    """How many denylist entries ``text`` matches. Callers report the count, never the term."""
    terms, patterns = load_denylist(deny)
    low = text.lower()
    return sum(t in low for t in terms) + sum(bool(p.search(text)) for p in patterns)


def _safe_extract(tar_path: Path, dest: Path) -> None:
    """Extract a `git archive` tar, refusing any member or link that would land outside."""
    dest = dest.resolve()
    with tarfile.open(tar_path) as tf:
        members = tf.getmembers()
        for m in members:
            name = Path(m.name)
            target = (dest / name).resolve()
            if name.is_absolute() or not target.is_relative_to(dest):
                raise ToolError(f"git archive member would escape the export dir: {m.name!r}")
            if m.issym() or m.islnk():
                link = Path(m.linkname)
                base = dest if m.islnk() else target.parent
                if link.is_absolute() or not (base / link).resolve().is_relative_to(dest):
                    raise ToolError(f"git archive link would escape the export dir: {m.name!r}")
            elif not (m.isfile() or m.isdir()):
                raise ToolError(f"git archive member has an unexpected type: {m.name!r}")
        extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tf.extractall(dest, members=members, **extra)  # noqa: S202 (members checked above)


def scan_tree_at(run: Run, root: Path, sha: str, deny: Path | None) -> int:
    """Privacy-scan the files of ``sha`` itself (not the checkout); returns the finding count."""
    with tempfile.TemporaryDirectory(prefix="streamsnow-release-scan-") as tmp:
        tmpdir = Path(tmp)
        tar_path = tmpdir / "tree.tar"
        proc = _exec(run, ["git", "archive", "--format=tar", "-o", str(tar_path), sha], root)
        if proc.returncode != 0 or not tar_path.is_file():
            raise ToolError(f"git archive {sha[:12]} failed: {_err(proc)}")
        tree = tmpdir / "tree"
        tree.mkdir()
        _safe_extract(tar_path, tree)
        args = [sys.executable, "-m", "streamsnow.tools.check_export_clean", str(tree)]
        args += ["--format", "json"]
        if deny:
            args += ["--denylist", str(deny)]
        proc = _exec(run, args, root)
        if proc.returncode not in (0, 1):
            raise ToolError(f"check_export_clean exit {proc.returncode} on {sha[:12]}")
        try:
            result = json.loads(proc.stdout or "")
        except json.JSONDecodeError as exc:
            raise ToolError(f"check_export_clean returned invalid JSON on {sha[:12]}") from exc
        findings = len(result.get("findings") or [])
        if proc.returncode == 1 and not findings:
            raise ToolError(f"check_export_clean failed on {sha[:12]} without findings")
        return findings


def _scan_problems(scan: dict) -> str:
    parts = []
    if scan["hits"]:
        parts.append(f"{len(scan['hits'])} with a denylist term ({', '.join(scan['hits'])})")
    if scan["paths"]:
        parts.append(f"{len(scan['paths'])} with a home path ({', '.join(scan['paths'])})")
    return "; ".join(parts)


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
        parts = [f"`### {k.title()}`" for k in sorted(headings - _PATCH_HEADINGS)]
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


def _porcelain(run: Run, root: Path) -> list[str]:
    proc = _exec(run, ["git", "status", "--porcelain"], root)
    if proc.returncode != 0:
        raise ToolError(f"git status failed: {_err(proc)}")
    return [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]


def _git_clean(run: Run, root: Path) -> tuple[bool, str]:
    dirty = _porcelain(run, root)
    return (not dirty, f"{len(dirty)} uncommitted path(s)" if dirty else "clean")


def plan_pin_floor(root: Path, version: str) -> dict[str, str]:
    """New text for every pin file. Raises ToolError, before anything is written, on drift."""
    major, minor, _ = parse_semver(version)
    if major >= 1:
        raise ToolError(
            "--pin-floor only knows the 0.x pin shape `<0.(B+1)`; from 1.0 the cap should "
            "follow the major version, so set the pin by hand"
        )
    m = _PIN.search(_read(root, PIN_SOURCE))
    if not m:
        raise ToolError(f"{PIN_SOURCE}: no `streamsnow>=A.B[.C],<A.B` pin found")
    if int(m.group(2)) >= 1:
        raise ToolError(f"{PIN_SOURCE}: the current pin is past 1.0; set the pin by hand")
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


def _checklist(run: Run, root: Path, version: str, new_pin: str | None) -> list[str]:
    tag = _last_tag(run, root, "HEAD")
    since = tag or "the first commit"
    items = [
        "Human review (RELEASING.md, Privacy gate step 2): skim the diff and `git log` since "
        f"{since} for real company, people or customer names, internal URLs, ticket IDs, "
        "account locators, and screenshots or data derived from real systems.",
        "Confirm LICENSE, README and CONTRIBUTING say what you intend.",
    ]
    changed = False
    if tag:
        proc = _exec(run, ["git", "diff", "--quiet", tag, "--", WALKTHROUGH], root)
        changed = proc.returncode == 1
    if changed:
        items.append(
            f"REQUIRED: the Playwright CLI pin changed since {tag}. Run one /preview-app "
            "walkthrough on the sample app on the new version before tagging."
        )
    else:
        items.append(
            "If the Playwright CLI pin was bumped for this release, run one /preview-app "
            "walkthrough on the sample app before tagging."
        )
    if new_pin:
        items.append(
            f"{PIN_HISTORY} keeps its pin history as written: add a line for `{new_pin}` to "
            "its upgrade paragraph by hand (open-pr accepts that edit)."
        )
    return items


def prepare(version: str, root: Path, run: Run, today: date, pin_floor: bool) -> Report:
    rep = Report("prepare", version)
    new = parse_semver(version)
    texts = {rel: _read(root, rel) for rel in VERSION_FILES}
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
    new_pin = None
    for rel, text in pins.items():
        _write(root, rel, text)
        new_pin = _PIN.search(text).group(0)
        rep.add(f"pin floor {rel}", "PASS", new_pin)

    for args in (["uv", "lock"], ["uv", "lock", "--check"]):
        proc = _exec(run, args, root)
        if proc.returncode != 0:
            rep.add(" ".join(args), "FAIL", _err(proc))
            rep.exit = 1
            rep.message = "files were edited but uv.lock is not consistent; fix it, then open-pr"
            return rep
        rep.add(" ".join(args), "PASS")
    got = lock_version(_read(root, UV_LOCK))
    if got != version:
        rep.add("uv.lock version", "FAIL", f"uv.lock has streamsnow {got}")
        rep.exit = 1
        return rep
    rep.add("uv.lock version", "PASS", version)
    rep.extra["checklist"] = _checklist(run, root, version, new_pin)
    rep.message = "prepared; nothing was committed or pushed"
    rep.next = (
        f'uv run python scripts/release.py open-pr {version} --trailer "<Co-Authored-By line>"'
    )
    return rep


# --------------------------------------------------------------------------- gates


def _gate_lockstep(version: str, root: Path, run: Run) -> Result:
    found = {rel: extract_version(rel, _read(root, rel)) for rel in VERSION_FILES}
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


def _gate_privacy(root: Path, run: Run, deny: Path | None) -> Result:
    args = [sys.executable, "-m", "streamsnow.tools.check_export_clean", "."]
    if deny:
        args += ["--denylist", str(deny)]
    proc = _exec(run, args, root)
    if proc.returncode != 0:
        return Result("privacy scan", "FAIL", f"check_export_clean exit {proc.returncode}")
    if deny:
        return Result("privacy scan", "PASS", "clean with the local denylist")
    return Result("privacy scan", "FAIL", _NO_DENYLIST_GATES.format(d=DENYLIST))


def _gate_commits(root: Path, run: Run, deny: Path | None) -> Result:
    scan = scan_commits(run, root, "HEAD", deny)
    detail = f"{scan['count']} commit(s) since {scan['since']}"
    problems = _scan_problems(scan)
    if problems:
        return Result("commit messages", "FAIL", f"{detail}: {problems}")
    if deny:
        return Result("commit messages", "PASS", f"{detail}: clean against {scan['terms']} term(s)")
    return Result("commit messages", "FAIL", _NO_DENYLIST_GATES.format(d=DENYLIST))


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
    deny = find_denylist(root, run)
    clean, detail = _git_clean(run, root)
    rep.results.append(Result("git tree clean", "PASS" if clean else "FAIL", detail))
    rep.results.append(_gate_lockstep(version, root, run))
    rep.results.append(_gate_changelog(version, root))
    rep.results.append(_gate_privacy(root, run, deny))
    rep.results.append(_gate_commits(root, run, deny))
    rep.results.append(_gate_links(root, run, online))
    rep.results.append(_gate_playwright(root, run))
    if tuple(r.name for r in rep.results) != GATE_NAMES:
        raise ToolError("internal: the gate list does not match GATE_NAMES")
    failed = [r.name for r in rep.results if r.status == "FAIL"]
    rep.exit = 1 if failed else 0
    rep.message = f"FAILED: {', '.join(failed)}" if failed else "all gates pass"
    return rep


# --------------------------------------------------------------------------- open-pr


def _tmp_text(text: str, prefix: str) -> str:
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".md")
    os.close(fd)
    Path(path).write_text(text, encoding="utf-8")
    return path


def _git_lines(run: Run, root: Path, args: list[str]) -> list[str]:
    proc = _exec(run, args, root)
    if proc.returncode != 0:
        raise ToolError(f"{' '.join(args[:3])} failed: {_err(proc)}")
    return [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]


def _branch_problem(run: Run, root: Path, subject: str) -> str | None:
    """Why the branch is not exactly one release commit on top of origin/main, or None."""
    subjects = _git_lines(run, root, ["git", "log", "--format=%s", "origin/main..HEAD"])
    if subjects != [subject]:
        found = "; ".join(subjects) or "none"
        return (
            f"the branch must hold exactly one commit, `{subject}`, on origin/main; found: {found}"
        )
    names = _git_lines(run, root, ["git", "diff", "--name-only", "origin/main...HEAD"])
    stray = [n for n in names if n not in RELEASE_EDITS]
    if stray:
        return f"the release commit touches files outside the release edits: {', '.join(stray)}"
    return None


def open_pr(
    version: str,
    root: Path,
    run: Run,
    trailer: str | None,
    pin_note: str | None,
) -> Report:
    rep = Report("open-pr", version)
    parse_semver(version)
    if trailer is not None and not _TRAILER.fullmatch(trailer):
        raise ToolError("--trailer must be one line of the form `Co-Authored-By: Name <address>`")
    if pin_note is not None and _PRIVATE_NOTE.search(pin_note):
        raise ToolError(
            "--pin-floor-note goes into a public PR body; leave out home paths and any `@`"
        )
    branch = f"claude/release-{version}"
    subject = f"chore({version}): release {version}"

    proc = _exec(run, ["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    if proc.returncode != 0:
        raise ToolError(f"git rev-parse failed: {_err(proc)}")
    if _out(proc) != branch:
        raise Refused(f"the current branch is {_out(proc)!r}, not {branch!r}")
    rep.add("branch", "PASS", branch)

    # Everything typed into the commit message or the public PR goes through the denylist
    # too: the gates only see files, and these strings land on GitHub verbatim.
    deny = find_denylist(root, run)
    if not deny:
        raise Refused(_NO_DENYLIST_GATES.format(d=DENYLIST))
    for what, text in (
        ("the release title", subject),
        ("--trailer", trailer),
        ("--pin-floor-note", pin_note),
    ):
        hits = denylist_hits(text or "", deny)
        if hits:
            raise Refused(f"{what} matches {hits} denylist term(s); it would be public, reword it")

    proc = _exec(run, ["git", "fetch", "origin"], root)
    if proc.returncode != 0:
        raise ToolError(f"git fetch origin failed: {_err(proc)}")
    proc = _exec(run, ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"], root)
    if proc.returncode == 1:
        raise Refused(
            f"{branch} is not cut from the current origin/main; delete it and cut it again "
            "from origin/main"
        )
    if proc.returncode != 0:
        raise ToolError(f"git merge-base failed: {_err(proc)}")
    rep.add("based on origin/main", "PASS")

    lines = _porcelain(run, root)
    untracked = [ln[3:] for ln in lines if ln.startswith("??")]
    if untracked:
        raise Refused(f"untracked files present: {', '.join(untracked)}")
    deleted = [ln[3:] for ln in lines if "D" in ln[:2]]
    if deleted:
        raise Refused(f"files are deleted, which a release never does: {', '.join(deleted)}")
    unexpected = [
        ln[3:] for ln in lines if " -> " in ln[3:] or ln[3:].strip('"') not in RELEASE_EDITS
    ]
    if unexpected:
        raise Refused(f"files outside the release edits are modified: {', '.join(unexpected)}")
    existing = _git_lines(run, root, ["git", "log", "--format=%s", "origin/main..HEAD"])
    if lines:
        if existing:
            raise Refused(
                "the branch already has commits beyond origin/main "
                f"({'; '.join(existing)}); the release must be exactly one commit"
            )
        proc = _exec(run, ["git", "add", "-u"], root)
        if proc.returncode != 0:
            raise ToolError(f"git add -u failed: {_err(proc)}")
        staged = _git_lines(run, root, ["git", "diff", "--cached", "--name-status"])
        bad = [
            ln
            for ln in staged
            if ln.split("\t")[0].startswith("D") or ln.split("\t")[-1] not in RELEASE_EDITS
        ]
        if bad:
            _exec(run, ["git", "reset", "-q"], root)
            raise Refused(f"unexpected staged changes (unstaged again): {', '.join(bad)}")
        msg = ["-m", subject] + (["-m", trailer] if trailer else [])
        proc = _exec(run, ["git", "commit", *msg], root)
        if proc.returncode != 0:
            raise ToolError(f"git commit failed: {_err(proc)}")
        rep.add("commit", "PASS", subject)
    elif not existing:
        raise Refused("nothing to commit; run prepare first")
    else:
        rep.add("commit", "PASS", "already committed")

    problem = _branch_problem(run, root, subject)
    if problem:
        rep.add("release branch", "FAIL", problem)
        rep.exit = 1
        rep.message = f"REFUSED: {problem}. Nothing was pushed."
        return rep
    rep.add("release branch", "PASS", "one release commit on origin/main, release files only")

    g = gates(version, root, run, online=True)
    rep.extra["gates"] = [asdict(r) for r in g.results]
    if g.exit != 0:
        rep.add("gates", "FAIL", g.message)
        rep.exit = 1
        rep.message = "a gate failed; the release commit is local only and nothing was pushed"
        return rep
    rep.add("gates", "PASS", g.message)

    body = [
        f"Release {version}.",
        "",
        f"Gates (`scripts/release.py gates {version} --online`):",
        "",
    ]
    body += [f"- {r.status} {r.name}" + (f": {r.detail}" if r.detail else "") for r in g.results]
    if pin_note:
        body += ["", "Pin floor raised: " + pin_note]
    body += ["", f"After this merges, the maintainer types `/release tag {version}`."]
    body_text = "\n".join(body) + "\n"
    hits = denylist_hits(body_text, deny)
    if hits:
        # the gate details are what put the term there, so withhold them from the output too
        rep.extra["gates"] = [
            {"name": r.name, "status": r.status, "detail": "(withheld)"} for r in g.results
        ]
        rep.add("pr body", "FAIL", f"matches {hits} denylist term(s)")
        rep.exit = 1
        rep.message = (
            f"the generated PR body matches {hits} denylist term(s); the release commit is "
            "local only and nothing was pushed"
        )
        return rep

    ref = f"refs/heads/{branch}"
    proc = _exec(run, ["git", "push", "--set-upstream", "origin", f"{ref}:{ref}"], root)
    if proc.returncode != 0:
        rep.add("push", "FAIL", _err(proc))
        rep.exit = 1
        rep.message = "the push failed; the release commit is local only"
        return rep
    rep.add("push", "PASS", ref)

    path = _tmp_text(body_text, "streamsnow-pr-")
    try:
        args = ["gh", "pr", "create", "--base", "main", "--head", branch, "--title", subject]
        proc = _exec(run, [*args, "--body-file", path], root)
    finally:
        Path(path).unlink(missing_ok=True)
    if proc.returncode != 0:
        rep.add("pull request", "FAIL", _err(proc))
        rep.exit = 1
        rep.message = f"{branch} is pushed but the PR was not created; open it by hand"
        return rep
    rep.add("pull request", "PASS", _out(proc))
    rep.message = f"release PR opened: {_out(proc)}"
    rep.next = f"once the PR has merged, type /release tag {version}"
    return rep


# --------------------------------------------------------------------------- tag


def _show(run: Run, root: Path, sha: str, rel: str, label: str) -> str:
    proc = _exec(run, ["git", "show", f"{sha}:{rel}"], root)
    if proc.returncode != 0:
        raise Refused(f"cannot read {rel} at {label}: {_err(proc)}")
    return proc.stdout or ""


def _versions_at(run: Run, root: Path, sha: str, version: str, label: str) -> None:
    for rel in VERSION_FILES:
        got = extract_version(rel, _show(run, root, sha, rel, label))
        if got != version:
            raise Refused(f"{label} has {rel} at {got}, not {version}; merge the release PR first")
    got = lock_version(_show(run, root, sha, UV_LOCK, label))
    if got != version:
        raise Refused(f"{label} has uv.lock at streamsnow {got}, not {version}")


def _changelog_at(run: Run, root: Path, sha: str, version: str) -> str:
    notes = changelog_section(_show(run, root, sha, CHANGELOG, sha[:12]), version)
    if not notes:
        raise Refused(f"the changelog on {sha[:12]} has no dated `## [{version}]` section")
    return notes


def _release_commit(run: Run, root: Path, sha: str, version: str) -> None:
    proc = _exec(run, ["git", "log", "-1", "--format=%s", sha], root)
    subject = _out(proc)
    want = rf"chore\({re.escape(version)}\): release {re.escape(version)}( \(#\d+\))?"
    if proc.returncode != 0 or not re.fullmatch(want, subject):
        raise Refused(
            f"main has moved past the release commit: origin/main is {subject!r}, not "
            f"`chore({version}): release {version}`; tag the release commit by hand per RELEASING.md"
        )
    _, body, _ = split_unreleased(_show(run, root, sha, CHANGELOG, sha[:12]))
    if body.strip():
        raise Refused(
            "main has moved past the release commit: `## [Unreleased]` has entries at origin/main"
        )


def _remote_refs(run: Run, root: Path, *refs: str) -> dict[str, str]:
    proc = _exec(run, ["git", "ls-remote", "origin", *refs], root)
    if proc.returncode != 0:
        raise ToolError(f"git ls-remote origin failed: {_err(proc)}")
    out: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        sha, _, ref = line.partition("\t")
        if ref in refs:
            out[ref] = sha
        elif ref.endswith("^{}") and ref[:-3] in refs:
            peeled[ref[:-3]] = sha
    out.update(peeled)  # an annotated tag resolves to its commit
    return out


def _ci_green(run: Run, root: Path, sha: str) -> str:
    proc = _exec(
        run,
        [
            "gh",
            "run",
            "list",
            "--commit",
            sha,
            "--json",
            "name,status,conclusion,event,createdAt",
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
    ci = [r for r in runs if r.get("name") == CI_WORKFLOW and r.get("event") == "push"]
    if not ci:
        raise Refused(
            f"no `{CI_WORKFLOW}` push run for {sha[:12]} yet; it may still be queued, "
            "try again later"
        )
    newest = max(ci, key=lambda r: r.get("createdAt") or "")
    if newest.get("status") != "completed":
        raise Refused(f"`{CI_WORKFLOW}` is still running on {sha[:12]}; try again later")
    if newest.get("conclusion") != "success":
        raise Refused(f"`{CI_WORKFLOW}` did not succeed on {sha[:12]}: {newest.get('conclusion')}")
    return f"`{CI_WORKFLOW}` push run succeeded"


def _create_release(run: Run, root: Path, tag_name: str, notes: str) -> subprocess.CompletedProcess:
    path = _tmp_text(notes + "\n", "streamsnow-release-")
    try:
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


def _release_only(rep: Report, version: str, root: Path, run: Run) -> Report:
    t = f"v{version}"
    tag_ref = f"refs/tags/{t}"
    remote = _remote_refs(run, root, tag_ref)
    if tag_ref not in remote:
        raise Refused(f"{t} is not on origin; run `tag {version}` without --release-only")
    sha = remote[tag_ref]
    _versions_at(run, root, sha, version, f"tag {t}")
    proc = _exec(run, ["git", "merge-base", "--is-ancestor", sha, "origin/main"], root)
    if proc.returncode == 1:
        raise Refused(f"{t} ({sha[:12]}) is not an ancestor of origin/main")
    if proc.returncode != 0:
        raise ToolError(f"git merge-base failed: {_err(proc)}")
    rep.add("tag on origin", "PASS", f"{t} -> {sha[:12]}, on origin/main, version {version}")
    notes = _changelog_at(run, root, sha, version)
    view = _exec(run, ["gh", "release", "view", t], root)
    if view.returncode == 0:
        raise Refused(f"a GitHub Release for {t} already exists")
    if "release not found" not in ((view.stderr or "") + (view.stdout or "")).lower():
        raise ToolError(f"gh release view failed (auth or network?): {_err(view)}")
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


def tag(
    version: str,
    root: Path,
    run: Run,
    release_only: bool = False,
    allow_no_denylist: bool = False,
) -> Report:
    rep = Report("tag", version)
    parse_semver(version)
    t = f"v{version}"
    tag_ref = f"refs/tags/{t}"

    proc = _exec(run, ["git", "fetch", "origin", "--tags"], root)
    if proc.returncode != 0:
        raise ToolError(f"git fetch origin --tags failed: {_err(proc)}")
    rep.add("fetch origin", "PASS")
    if release_only:
        return _release_only(rep, version, root, run)

    proc = _exec(run, ["git", "rev-parse", "--verify", "origin/main^{commit}"], root)
    if proc.returncode != 0 or not _out(proc):
        raise Refused("origin/main does not exist")
    sha = _out(proc)
    remote_main = _remote_refs(run, root, "refs/heads/main").get("refs/heads/main")
    if remote_main != sha:
        raise Refused(
            f"local origin/main ({sha[:12]}) does not match origin's main ({remote_main}); "
            "fetch and try again"
        )
    _versions_at(run, root, sha, version, "origin/main")
    rep.add("origin/main version", "PASS", f"{sha[:12]} at {version}, uv.lock included")
    _release_commit(run, root, sha, version)
    rep.add("release commit", "PASS", f"origin/main is the {version} release commit")

    if _exec(run, ["git", "rev-parse", "-q", "--verify", tag_ref], root).returncode == 0:
        raise Refused(f"tag {t} already exists locally")
    # A vX.Y.Z BRANCH on origin is the release-branch convention (RELEASING.md); the fully
    # qualified refs/tags/ refspec below keeps the push unambiguous, so only a tag refuses.
    if tag_ref in _remote_refs(run, root, tag_ref):
        raise Refused(f"tag {t} already exists on origin")
    rep.add("tag is new", "PASS", f"no {t} tag locally or on origin")

    rep.add("ci green", "PASS", _ci_green(run, root, sha))
    notes = _changelog_at(run, root, sha, version)
    rep.add("changelog section", "PASS", f"## [{version}] on {sha[:12]}")

    deny = find_denylist(root, run)
    if not deny and not allow_no_denylist:
        raise Refused(_NO_DENYLIST.format(d=DENYLIST))
    scan = scan_commits(run, root, sha, deny)
    problems = _scan_problems(scan)
    if problems:
        raise Refused(f"commit messages since {scan['since']}: {problems}")
    if deny:
        rep.add("commit messages", "PASS", f"{scan['count']} commit(s) clean")
    else:
        rep.add("commit messages", "WARN", "NO DENYLIST (--allow-no-denylist): paths only")

    # RELEASING.md's privacy gate scans files; nothing forces `gates` to have run on this
    # exact commit, so scan the files of the commit being tagged.
    findings = scan_tree_at(run, root, sha, deny)
    if findings:
        raise Refused(
            f"the files at {sha[:12]} fail the privacy scan with {findings} finding(s); run "
            "check_export_clean on that commit locally to see them"
        )
    scope = "with the local denylist" if deny else "NO DENYLIST (--allow-no-denylist): generic"
    rep.add("privacy scan", "PASS" if deny else "WARN", f"files at {sha[:12]} clean, {scope}")

    proc = _exec(run, ["git", "tag", t, sha], root)
    if proc.returncode != 0:
        raise ToolError(f"git tag failed: {_err(proc)}")
    rep.add("git tag", "PASS", f"{t} -> {sha[:12]}")
    proc = _exec(run, ["git", "push", "origin", tag_ref], root)
    if proc.returncode != 0:
        rep.add("push tag", "FAIL", _err(proc))
        rep.exit = 1
        landed = _remote_refs(run, root, tag_ref).get(tag_ref)
        if landed:
            rep.message = (
                f"the push reported an error, but {t} is on origin at {landed[:12]}, so "
                "publishing may have started. Check the publish run, then create the Release "
                f"with: uv run python scripts/release.py tag {version} --release-only"
            )
            return rep
        undo = _exec(run, ["git", "tag", "-d", t], root)
        removed = (
            "the local tag was removed"
            if undo.returncode == 0
            else f"removing the local tag failed ({_err(undo)}); delete it before retrying"
        )
        rep.message = f"the push failed and {t} is not on origin; {removed}"
        return rep
    rep.add("push tag", "PASS", f"origin {tag_ref}; the publish workflow starts now")

    rel = _create_release(run, root, t, notes)
    if rel.returncode != 0:
        rep.add("github release", "FAIL", _err(rel))
        rep.exit = 1
        rep.message = (
            "the tag is out and publishing has started, but the GitHub Release failed. "
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


def _pending(rep: Report, name: str, detail: str) -> Report:
    rep.add(name, "FAIL", detail)
    rep.exit = 3
    rep.message = "pending, check again later"
    return rep


def verify(version: str, root: Path, run: Run, fetch_json: Callable[[str], dict]) -> Report:
    rep = Report("verify", version)
    parse_semver(version)
    t = f"v{version}"
    if f"refs/tags/{t}" not in _remote_refs(run, root, f"refs/tags/{t}"):
        rep.add("tag on origin", "FAIL", f"{t} is not on origin")
        rep.exit = 1
        rep.message = f"{t} is not on origin; run /release tag {version} first"
        return rep
    rep.add("tag on origin", "PASS", t)
    args = ["gh", "run", "list", "--workflow", "publish.yml", "--branch", t]
    proc = _exec(run, [*args, "--json", "status,conclusion,url", "--limit", "1"], root)
    if proc.returncode != 0:
        raise ToolError(f"gh run list failed: {_err(proc)}")
    try:
        runs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ToolError(f"gh run list returned invalid JSON: {exc}") from exc
    if not runs:
        return _pending(rep, "publish run", f"no publish run for {t} yet")
    r = runs[0]
    if r.get("status") != "completed":
        return _pending(rep, "publish run", f"{r.get('status')}: {r.get('url', '')}")
    if r.get("conclusion") != "success":
        rep.add("publish run", "FAIL", f"{r.get('conclusion')}: {r.get('url', '')}")
        rep.exit = 1
        rep.message = f"the publish workflow failed: {r.get('url', '')}"
        return rep
    rep.add("publish run", "PASS", r.get("url", ""))
    url = PYPI_JSON.format(version=version)
    try:
        data = fetch_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _pending(rep, "pypi release", f"{url} is 404 (index lag or still uploading)")
        raise ToolError(f"could not read {url}: {exc}") from exc
    except Exception as exc:  # network or JSON error
        raise ToolError(f"could not read {url}: {exc}") from exc
    got = (data.get("info") or {}).get("version")
    if got != version:
        rep.add("pypi release", "FAIL", f"PyPI answered with version {got}")
        rep.exit = 1
        rep.message = "PyPI does not report this version"
        return rep
    rep.add("pypi release", "PASS", version)
    smoke = ["uvx", "--refresh-package", "streamsnow", "--from", f"streamsnow=={version}"]
    proc = _exec(run, [*smoke, "streamsnow", "--version"], root)
    if proc.returncode != 0 or _out(proc) != f"streamsnow {version}":
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
    if rep.extra.get("gates"):
        lines += ["", "Gates:"]
        lines += [
            f"- {g['status']:<4} {g['name']}" + (f": {g['detail']}" if g["detail"] else "")
            for g in rep.extra["gates"]
        ]
    if rep.extra.get("checklist"):
        lines += ["", "Check by hand before tagging:"]
        lines += [f"- [ ] {item}" for item in rep.extra["checklist"]]
    if rep.results or rep.extra.get("gates") or rep.extra.get("checklist"):
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
    o = sub.add_parser("open-pr", parents=[common], help="Commit, gate, push, open the PR.")
    o.add_argument("version")
    o.add_argument("--trailer", help="One `Co-Authored-By: Name <address>` line.")
    o.add_argument("--pin-floor-note", help="Why the pin floor moved, for the PR body.")
    g = sub.add_parser("gates", parents=[common], help="Run the release gates (read-only).")
    g.add_argument("version")
    g.add_argument("--online", action="store_true")
    t = sub.add_parser("tag", parents=[common], help="Tag the release commit, create the Release.")
    t.add_argument("version")
    t.add_argument("--release-only", action="store_true")
    # Only on `tag`: the skill pre-approves every other subcommand, so a bypass flag there
    # would skip the permission prompt. `tag` always prompts.
    t.add_argument(
        "--allow-no-denylist",
        action="store_true",
        help="Accept a generic-only commit-message check when no denylist is found (loud WARN).",
    )
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
        elif args.cmd == "open-pr":
            rep = open_pr(version, root, run, args.trailer, args.pin_floor_note)
        elif args.cmd == "gates":
            rep = gates(version, root, run, args.online)
        elif args.cmd == "tag":
            rep = tag(version, root, run, args.release_only, args.allow_no_denylist)
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
