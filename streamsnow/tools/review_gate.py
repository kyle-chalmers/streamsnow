#!/usr/bin/env python3
"""Review-gate decision function — "does this change need review, and how deep?"

Single source of truth for the review-escalation decision, shared by
`/ship-app`'s preflight, `/feedback-app`'s followup step, and the warn-only
`Stop` hook. In the source monorepo, before this tool existed, each caller
hand-rolled its own substantive-vs-trivial bash and they drifted: every
automatic path ran a single-pass review while the full review loop ended up
with no executable caller at all. One decision function, many consumers.

Subcommands:

    classify [slug] [--base-ref=origin/main]
        Classify the app diff as ``trivial`` or ``loop`` and report whether a
        review artifact already covers the current tree state. With no slug,
        classifies every app with changes. Exit 0 = nothing needed,
        1 = review recommended, 2 = tool error.

    baseline <slug>
        Print the current baseline digest for an app (see "Baselines" below).

    stamp <artifact.md> --slug=<slug>
        Write/refresh the ``Reviewed-baseline:`` header in a review artifact.
        `/review-app --auto` calls this at the END of a run, after its fix
        commits, so the review it just completed does not read as stale.

    stop-hook [--payload=system-only|both]
        Read Claude Code `Stop` hook JSON on stdin and emit the warn-only
        payload when a substantive app change is ending unreviewed. Always
        exits 0 — this never blocks a turn. Exits 0 instantly outside a
        StreamSnow repo (no ``streamsnow.config.yaml`` at the repo root), so
        the plugin hook is inert everywhere else.

        **The default is ``--payload=system-only``, deliberately.** Measured
        in the source monorepo (2026-08-04): emitting
        ``hookSpecificOutput.additionalContext`` from a Stop hook does NOT
        merely queue a reminder for the user's next request — it starts a
        fresh assistant turn with that context injected, with no user input.
        That is a soft continuation, and it makes every substantive app change
        cost an extra unrequested turn. ``system-only`` emits just
        ``systemMessage``, which surfaces to the user and ends the turn as
        normal. Do not switch to ``both`` without re-measuring; the hook docs
        are ambiguous on this point and one reading of them is wrong.

This file is deliberately **self-contained and stdlib-only** (PyYAML is used
opportunistically when present, never required): the Claude Code plugin's Stop
hook executes it by path from ``${CLAUDE_PLUGIN_ROOT}`` on machines that may
not have the ``streamsnow`` package installed. Do not add package-relative
imports or third-party requirements.

Coverage is per-change, not per-app
-----------------------------------
"Has this app ever been reviewed" is the wrong question — what matters is
whether *the specific changes being shipped* were reviewed. So each artifact
records an identity for every file its run looked at::

    Reviewed-baseline: <digest>
    Reviewed-files:
      <coverage-key>  apps/<slug>/pages/overview.py
      <coverage-key>  apps/<slug>/queries/revenue_daily.sql
    Reviewed-files-end.

The trailing fence is load-bearing: without it an ordinary report-body line
shaped like ``<hex>  apps/<slug>/foo.py`` would parse as coverage and could mark
an unreviewed change reviewed, and re-stamping could delete that body line.

A change is covered when every substantive changed file's **current** coverage
key appears in the union of coverage across artifacts. The key is *semantic*,
not byte-exact — see ``coverage_key``: for Python it is a digest of the AST shape
with comments and docstrings stripped, matching the triviality rule exactly.
That symmetry is the point: a change too trivial to *require* a review must also
be too trivial to *invalidate* one. Consequences:

- Edit one file after a review and only *that* file reads as unreviewed;
  ``unreviewed_files`` names it, so the caller can say which change is uncovered
  instead of just "something changed".
- Reword a comment or docstring in a reviewed file and it stays reviewed.
- Revert a file to content that was reviewed before and it reads as reviewed
  again — the union means already-inspected code doesn't need a second pass.
- Deleting a substantive file always reads as unreviewed (sentinel
  ``deleted``); removing a page or query deserves a look.
- Trivial changes never require coverage at all, so a README edit after a
  review does not reopen it.

Why not timestamps: the review loop writes ``review-<ts>.md`` and *then* makes
its per-finding fix commits, so an mtime comparison marks the review it just
finished as stale and the gate nags after every *successful* loop.

``Reviewed-baseline`` (sha256 over the app's tracked tree plus dirty files,
``.review/`` excluded) remains as the whole-tree fallback for artifacts written
before per-file tracking existed; ``coverage_mode`` reports which path was used.
`stamp` writes both.

Trivial-vs-substantive
----------------------
A ``*.py`` diff is trivial only when the two versions are token-identical after
stripping **comments and docstrings**. A changed caption string is a STRING
token change and is NOT treated as trivial — a caption literal and a SQL
literal are indistinguishable at that level, and over-triggering toward review
is the safe direction.

Fail-open
---------
Any unexpected error exits 0 with no output in ``stop-hook`` mode: a broken
gate must never wedge a turn. ``classify`` surfaces errors as exit 2 so skills
and CI can see them.

Off-switches: ``REVIEW_GATE_OFF=1`` (env), ``apps/<slug>/.review/SKIP``
(per-app marker file), or ``review_gate: {enabled: false}`` in
``streamsnow.config.yaml``.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Suffixes that can never require a code review on their own.
TRIVIAL_SUFFIXES = frozenset({".md", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"})

#: Exact filenames that are trivial regardless of suffix.
TRIVIAL_NAMES = frozenset({"VERSION"})

#: Path segments that are trivial regardless of what is inside them.
TRIVIAL_DIRS = frozenset({"screenshots", ".review"})

#: Suffixes that make a diff substantive unless proven otherwise.
CODE_SUFFIXES = frozenset({".py", ".sql"})

BASELINE_HEADER = "Reviewed-baseline:"

#: Per-file coverage block. Lines below it are ``<coverage-key>  <repo-rel-path>``.
FILES_HEADER = "Reviewed-files:"

#: Artifact filename prefixes that can carry a baseline. Matched
#: case-insensitively: StreamSnow's `/review-app` writes lowercase
#: ``review-<ts>.md`` while artifacts stamped by other tooling may use the
#: uppercase dialect. Both must be stampable or coverage silently never
#: matches — the likeliest subtle-bug site in this file.
ARTIFACT_PREFIXES = ("review-", "loop-", "walk-")

CONFIG_FILENAME = "streamsnow.config.yaml"

SKIP_MARKER = "SKIP"

VERDICT_TRIVIAL = "trivial"
VERDICT_LOOP = "loop"

#: Sentinel blob for a deleted file — never present in coverage, so a deletion
#: always reads as unreviewed. Removing a page or query deserves a look.
DELETED_BLOB = "deleted"

DEFAULT_APPS_DIR = "apps"


@dataclass
class AppVerdict:
    """Per-app classification result."""

    slug: str
    verdict: str  # trivial | loop
    baseline: str
    reviewed: bool  # every substantive changed file's current content was reviewed
    needs_review: bool  # verdict == loop and not reviewed and not skipped
    skipped: bool  # <apps_dir>/<slug>/.review/SKIP present
    changed_files: list[str]
    reason: str
    #: Substantive changed files whose CURRENT content no artifact covers. This
    #: is the actionable part — "which of my changes are unreviewed", not just
    #: "has this app ever been reviewed".
    unreviewed_files: list[str] = field(default_factory=list)
    reviewed_files: list[str] = field(default_factory=list)
    #: True when coverage came from the whole-tree digest because no artifact
    #: carried a per-file block (artifacts written before per-file tracking).
    coverage_mode: str = "per-file"  # per-file | whole-tree


# ---------------------------------------------------------------------------
# Gate configuration (best-effort, never required)
# ---------------------------------------------------------------------------


def gate_config(root: Path) -> dict:
    """The ``review_gate:`` block of streamsnow.config.yaml, or {}.

    PyYAML is imported lazily and optionally: the plugin hook runs this file
    on machines without the pip package (and therefore possibly without yaml).
    A config we cannot parse means default behavior, never an error.
    """
    cfg_path = root / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        import yaml  # noqa: PLC0415 — optional dependency, see docstring

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — best-effort by design
        return {}
    block = data.get("review_gate") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


def apps_dir_name(root: Path, override: str | None = None) -> str:
    """Resolve the apps directory name: flag > env > config > "apps"."""
    if override:
        return override
    env = os.environ.get("STREAMSNOW_APPS_DIR")
    if env:
        return env
    from_cfg = gate_config(root).get("apps_dir")
    if isinstance(from_cfg, str) and from_cfg:
        return from_cfg
    return DEFAULT_APPS_DIR


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


class GitError(RuntimeError):
    """Raised when a git invocation the gate depends on fails."""


def _git(repo_root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def repo_root(start: Path | None = None) -> Path:
    """Locate the repository root.

    An explicit ``start`` wins outright: the Stop hook passes the payload's
    ``cwd``, and a stale ``CLAUDE_PROJECT_DIR`` from another session must not
    redirect the gate to inspect (and notify about) a *different* repo than
    the one the turn actually ran in. The env var is only a fallback for
    callers with no better anchor (hooks run with an arbitrary process cwd,
    so a relative path is not safe there); the git fallback covers direct
    CLI use.
    """
    if start is not None:
        base = Path(start).resolve()
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=base,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return Path(proc.stdout.strip()).resolve()
        raise GitError(f"not a git repository: {base}")
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir and (Path(env_dir) / ".git").exists():
        return Path(env_dir).resolve()
    base = Path.cwd().resolve()
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=base,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(f"not a git repository: {base}")
    return Path(proc.stdout.strip()).resolve()


def current_branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()


def resolve_base_ref(root: Path, preferred: str = "origin/main") -> str:
    """First existing ref among preferred / origin-or-local main / master, else "".

    ``master`` is included because a repo whose default branch is `master` is a
    supported git state, and returning "" there used to make `changed_paths`
    silently drop every *committed* change — the gate would report "no changes"
    and disable itself. When nothing resolves, callers must fail toward review
    (see ``changed_paths``), never toward silence.
    """
    for ref in (preferred, "origin/main", "main", "origin/master", "master"):
        if not ref:
            continue
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return ref
    return ""


def _merge_base(root: Path, base_ref: str) -> str:
    """Merge-base of HEAD and base_ref, or "" when unavailable."""
    if not base_ref:
        return ""
    proc = subprocess.run(
        ["git", "merge-base", base_ref, "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def changed_paths(root: Path, base_ref: str) -> set[str]:
    """Repo-relative paths changed vs base_ref, plus anything uncommitted.

    Uses the merge base so a stale local main doesn't inflate the diff with
    commits that only exist upstream.

    **Fails toward review.** When no base ref resolves (unusual default branch,
    fresh repo, detached history), we cannot know which committed files are new,
    so every tracked file counts as changed rather than none. Reporting "no
    changes" there would silently switch the gate off, which is the one outcome
    this tool exists to prevent.
    """
    paths: set[str] = set()
    base = _merge_base(root, base_ref)
    if base:
        out = _git(root, "diff", "--name-only", base, "HEAD", check=False)
        paths.update(line.strip() for line in out.splitlines() if line.strip())
    else:
        out = _git(root, "ls-files", check=False)
        paths.update(line.strip() for line in out.splitlines() if line.strip())
    # Uncommitted: staged, unstaged, and untracked.
    out = _git(root, "status", "--porcelain", "--untracked-files=all", check=False)
    for line in out.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        # Renames are "old -> new"; take the destination.
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.add(entry.strip().strip('"'))
    return {p for p in paths if p}


def _blob_at(root: Path, rev: str, path: str) -> bytes | None:
    """File content at a revision, or None when absent there."""
    proc = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=root,
        capture_output=True,
    )
    return proc.stdout if proc.returncode == 0 else None


# ---------------------------------------------------------------------------
# Trivial-vs-substantive classification
# ---------------------------------------------------------------------------


_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _code_shape(source: bytes) -> str | None:
    """Structural fingerprint of a Python file, ignoring comments + docstrings.

    ``ast.parse`` already discards comments and all formatting, so the only
    documentation that survives into the tree is docstrings — strip those and
    the dump changes if and only if real code changed. A changed string literal
    that is *not* a docstring (a caption, a SQL fragment) does alter the dump,
    which is deliberate: the two are indistinguishable here and review is the
    safe default.

    Returns None when the source can't be parsed (a syntax error mid-edit),
    which the caller treats as "assume substantive".

    Docstrings are normally documentation, but they are also runtime data via
    ``__doc__`` — a module that renders ``__doc__`` in the UI changes behavior
    when its docstring changes. So if the file references ``__doc__`` at all,
    docstrings are kept and a docstring edit counts as a real change.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return None
    if b"__doc__" not in source:
        for node in ast.walk(tree):
            if not isinstance(node, _DOCSTRING_OWNERS):
                continue
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:]
    return ast.dump(tree)


def py_change_is_trivial(before: bytes | None, after: bytes | None) -> bool:
    """True when a .py edit changed only comments or docstrings.

    A new or deleted file is never trivial. An unparseable version is never
    trivial — we do not guess about broken syntax.
    """
    if before is None or after is None:
        return False
    if before == after:
        return True
    sb = _code_shape(before)
    sa = _code_shape(after)
    if sb is None or sa is None:
        return False
    return sb == sa


def _is_trivial_path(rel: str) -> bool:
    p = Path(rel)
    if p.name in TRIVIAL_NAMES:
        return True
    if p.suffix.lower() in TRIVIAL_SUFFIXES:
        return True
    return any(part in TRIVIAL_DIRS for part in p.parts)


def classify_files(
    root: Path,
    files: list[str],
    base_ref: str,
) -> tuple[str, str]:
    """Classify an app's changed files. Returns (verdict, reason)."""
    substantive = substantive_files(root, files, base_ref)
    if substantive:
        return VERDICT_LOOP, f"{len(substantive)} substantive file(s): " + ", ".join(
            sorted(substantive)[:5]
        )
    non_trivial_paths = [f for f in files if not _is_trivial_path(f)]
    if non_trivial_paths:
        return (
            VERDICT_TRIVIAL,
            f"{len(non_trivial_paths)} .py file(s) changed comments/docstrings only",
        )
    return VERDICT_TRIVIAL, "docs/screenshots/VERSION only"


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def compute_baseline(root: Path, slug: str, apps_dir: str = DEFAULT_APPS_DIR) -> str:
    """Digest of the app's reviewable tree state: committed sha + dirty files.

    ``.review/`` is deliberately excluded. Stamping an artifact writes *into*
    ``<apps_dir>/<slug>/.review/``, so counting it would change the very
    baseline the stamp just recorded — the review could never read as fresh.
    Review artifacts are outputs of the review, not part of what gets reviewed.

    For the committed half this means hashing the app's tracked tree with
    ``.review`` pruned, rather than taking the whole-directory tree sha.
    """
    app_rel = f"{apps_dir}/{slug}"
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "HEAD", "--", app_rel],
        cwd=root,
        capture_output=True,
        text=True,
    )
    tracked = [
        line
        for line in listing.stdout.splitlines()
        if line.strip() and f"{app_rel}/.review/" not in line
    ]

    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", app_rel],
        cwd=root,
        capture_output=True,
        text=True,
    ).stdout

    h = hashlib.sha256()
    for line in sorted(tracked):
        h.update(line.encode())
        h.update(b"\n")
    for line in sorted(ln for ln in dirty.splitlines() if ln.strip()):
        entry = line[3:].strip().strip('"')
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if _is_review_artifact(entry, slug, apps_dir):
            continue
        h.update(b"\n")
        h.update(entry.encode())
        fpath = root / entry
        if fpath.is_file():
            h.update(hashlib.sha256(fpath.read_bytes()).digest())
    return h.hexdigest()[:16]


def _is_review_artifact(rel: str, slug: str, apps_dir: str) -> bool:
    """True for anything under <apps_dir>/<slug>/.review/."""
    parts = Path(rel).parts
    return len(parts) >= 3 and parts[0] == apps_dir and parts[1] == slug and parts[2] == ".review"


def _artifact_texts(root: Path, slug: str, apps_dir: str) -> list[str]:
    review_dir = root / apps_dir / slug / ".review"
    if not review_dir.is_dir():
        return []
    texts: list[str] = []
    for path in sorted(review_dir.glob("*.md")):
        if not path.name.lower().startswith(ARTIFACT_PREFIXES):
            continue
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def stored_baselines(root: Path, slug: str, apps_dir: str) -> set[str]:
    """Every whole-tree baseline digest recorded under <apps_dir>/<slug>/.review/."""
    pattern = re.compile(rf"^{re.escape(BASELINE_HEADER)}\s*([0-9a-f]+)\s*$", re.MULTILINE)
    found: set[str] = set()
    for text in _artifact_texts(root, slug, apps_dir):
        found.update(m.group(1) for m in pattern.finditer(text))
    return found


#: Explicit terminator. Without a fence, an ordinary report-body line shaped
#: like ``<hex>  apps/<slug>/foo.py`` parses as coverage — which would let an
#: unreviewed change read as reviewed — and re-stamping could delete that body
#: line. Coverage is only ever read strictly between header and terminator.
FILES_END = "Reviewed-files-end."

_FILES_BLOCK_RE = re.compile(
    rf"^{re.escape(FILES_HEADER)}[ \t]*\n(.*?)^{re.escape(FILES_END)}[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
_COVERAGE_LINE_RE = re.compile(r"^[ \t]*([0-9a-f]{7,64})[ \t]+(\S.*?)[ \t]*$")


def stored_file_coverage(root: Path, slug: str, apps_dir: str) -> set[tuple[str, str]]:
    """Every ``(path, coverage_key)`` pair any artifact says it reviewed.

    Union across artifacts on purpose: a file whose current content was reviewed
    by *some* past run is covered, even if a later run reviewed different files.
    That makes reverting a file back to a reviewed state read as reviewed again,
    instead of demanding a fresh pass for content already looked at.

    Only fenced blocks count — see ``FILES_END``.
    """
    pairs: set[tuple[str, str]] = set()
    for text in _artifact_texts(root, slug, apps_dir):
        for block in _FILES_BLOCK_RE.finditer(text):
            for line in block.group(1).splitlines():
                m = _COVERAGE_LINE_RE.match(line)
                if m:
                    pairs.add((m.group(2), m.group(1)))
    return pairs


def blob_sha(root: Path, rel: str) -> str:
    """Git blob sha of a file's CURRENT content (committed or dirty).

    ``git hash-object`` hashes what is on disk, so this is the identity of the
    content the user is actually about to ship.
    """
    fpath = root / rel
    if not fpath.is_file():
        return DELETED_BLOB
    proc = subprocess.run(
        ["git", "hash-object", "--", str(fpath)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else DELETED_BLOB


def coverage_key(root: Path, rel: str) -> str:
    """Identity of a file for review-coverage purposes.

    Deliberately **not** the raw blob sha for Python: rewording a comment or a
    docstring in an already-reviewed file must not reopen review. The key is the
    AST shape (comments and docstrings stripped) — the same notion of "did the
    code actually change" that decides whether a diff is substantive at all.
    Keeping the two consistent is what stops the gate contradicting itself:
    a change that is too trivial to require review is also too trivial to
    invalidate one.

    Non-Python files fall back to the blob sha, since there is no comparable
    parse-and-normalize step for SQL or config.
    """
    fpath = root / rel
    if not fpath.is_file():
        return DELETED_BLOB
    if fpath.suffix.lower() == ".py":
        shape = _code_shape(fpath.read_bytes())
        if shape is not None:
            return hashlib.sha256(shape.encode()).hexdigest()
        # Unparseable: fall back to exact bytes rather than guessing.
    return blob_sha(root, rel)


def substantive_files(root: Path, files: list[str], base_ref: str) -> list[str]:
    """The subset of changed files that a reviewer would actually weigh."""
    base = _merge_base(root, base_ref) or base_ref
    out: list[str] = []
    for rel in files:
        if _is_trivial_path(rel):
            continue
        suffix = Path(rel).suffix.lower()
        if suffix == ".py" and base:
            before = _blob_at(root, base, rel)
            after_path = root / rel
            after = after_path.read_bytes() if after_path.exists() else None
            if py_change_is_trivial(before, after):
                continue
        out.append(rel)
    return out


def stamp_artifact(
    path: Path,
    baseline: str,
    file_blobs: dict[str, str] | None = None,
) -> None:
    """Insert or refresh the Reviewed-baseline (+ fenced Reviewed-files) headers.

    ``file_blobs`` maps repo-relative path → coverage key for every substantive
    file this run reviewed. It is what makes coverage per-change rather than
    per-app: a later edit to one of these files changes its key and that file
    alone reads as unreviewed.

    The files block is fenced by ``FILES_END``, and removal of a prior stamp only
    ever spans header → fence, so re-stamping cannot eat report body text that
    happens to look like a coverage line.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""

    block = f"{BASELINE_HEADER} {baseline}\n"
    if file_blobs:
        block += f"{FILES_HEADER}\n"
        for rel in sorted(file_blobs):
            block += f"  {file_blobs[rel]}  {rel}\n"
        block += f"{FILES_END}\n"

    # Drop any prior stamp (baseline line + optional fenced files block).
    # Deliberately line-scoped (`[^\n]*`, no DOTALL): with DOTALL the baseline
    # line's wildcard spans newlines and swallows the entire report body.
    text = re.sub(
        rf"^{re.escape(BASELINE_HEADER)}[^\n]*\n"
        rf"(?:{re.escape(FILES_HEADER)}[ \t]*\n"
        rf"(?:[^\n]*\n)*?"
        rf"{re.escape(FILES_END)}[ \t]*\n)?",
        "",
        text,
        count=1,
        flags=re.MULTILINE,
    )

    if text.startswith("# "):
        # Put it directly under the title so it survives casual editing.
        head, sep, rest = text.partition("\n")
        text = f"{head}{sep}{block}{rest.lstrip(chr(10))}"
    else:
        text = f"{block}\n{text.lstrip(chr(10))}"
    path.write_text(text, encoding="utf-8")


def app_substantive_blobs(
    root: Path, slug: str, base_ref: str, apps_dir: str = DEFAULT_APPS_DIR
) -> dict[str, str]:
    """{path: coverage_key} for every substantive changed file in an app right now."""
    changed = [rel for rel in changed_paths(root, base_ref) if app_slug_of(rel, apps_dir) == slug]
    return {rel: coverage_key(root, rel) for rel in substantive_files(root, changed, base_ref)}


# ---------------------------------------------------------------------------
# Classification driver
# ---------------------------------------------------------------------------


def app_slug_of(rel: str, apps_dir: str = DEFAULT_APPS_DIR) -> str | None:
    parts = Path(rel).parts
    if len(parts) >= 2 and parts[0] == apps_dir:
        return parts[1]
    return None


def classify(
    root: Path, slug: str | None, base_ref: str, apps_dir: str = DEFAULT_APPS_DIR
) -> list[AppVerdict]:
    changed = changed_paths(root, base_ref)
    by_app: dict[str, list[str]] = {}
    for rel in changed:
        app = app_slug_of(rel, apps_dir)
        if app and (slug is None or app == slug):
            by_app.setdefault(app, []).append(rel)

    if slug is not None:
        by_app.setdefault(slug, [])

    verdicts: list[AppVerdict] = []
    for app, files in sorted(by_app.items()):
        if not files:
            verdicts.append(
                AppVerdict(
                    slug=app,
                    verdict=VERDICT_TRIVIAL,
                    baseline=compute_baseline(root, app, apps_dir),
                    reviewed=True,
                    needs_review=False,
                    skipped=False,
                    changed_files=[],
                    reason="no changes under this app",
                )
            )
            continue
        verdict, reason = classify_files(root, files, base_ref)
        baseline = compute_baseline(root, app, apps_dir)
        skipped = (root / apps_dir / app / ".review" / SKIP_MARKER).exists()

        # Per-change coverage: a review counts only for the file CONTENT it saw.
        substantive = substantive_files(root, files, base_ref)
        coverage = stored_file_coverage(root, app, apps_dir)
        if coverage:
            mode = "per-file"
            covered = [f for f in substantive if (f, coverage_key(root, f)) in coverage]
            uncovered = [f for f in substantive if f not in set(covered)]
            reviewed = not uncovered
        else:
            # No artifact carries a per-file block (pre-per-file artifacts).
            # Fall back to the whole-tree digest.
            mode = "whole-tree"
            reviewed = baseline in stored_baselines(root, app, apps_dir)
            covered = list(substantive) if reviewed else []
            uncovered = [] if reviewed else list(substantive)

        if uncovered:
            reason = f"{len(uncovered)} unreviewed change(s): " + ", ".join(sorted(uncovered)[:5])
        elif verdict == VERDICT_LOOP and reviewed:
            reason = f"all {len(substantive)} changed file(s) already reviewed"

        verdicts.append(
            AppVerdict(
                slug=app,
                verdict=verdict,
                baseline=baseline,
                reviewed=reviewed,
                needs_review=(verdict == VERDICT_LOOP and not reviewed and not skipped),
                skipped=skipped,
                changed_files=sorted(files),
                reason=reason,
                unreviewed_files=sorted(uncovered),
                reviewed_files=sorted(covered),
                coverage_mode=mode,
            )
        )
    return verdicts


# ---------------------------------------------------------------------------
# Stop-hook state (dedupe)
# ---------------------------------------------------------------------------


#: Upper bound on the Stop-hook stdin payload we will read (1 MiB). The real
#: payload is a few KB; the cap keeps a pathological producer from stalling us
#: past the hook's configured timeout.
MAX_STDIN_BYTES = 1024 * 1024

#: Cap on remembered (slug, baseline) keys per session. Far above any real
#: session's churn; exists so a pathological loop can't grow the file forever.
MAX_NOTIFIED_KEYS = 500

#: Age after which another session's state file is swept on write.
STATE_TTL_SECONDS = 7 * 86400


def _state_dir() -> Path:
    return Path(os.environ.get("TMPDIR", "/tmp")) / "streamsnow-review-gate"


def _state_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession")
    return _state_dir() / f"{safe}.json"


def _prune_state_dir(now: float) -> None:
    """Delete state files older than the TTL. Best-effort, never raises."""
    try:
        for path in _state_dir().glob("*.json"):
            try:
                if now - path.stat().st_mtime > STATE_TTL_SECONDS:
                    path.unlink()
            except OSError:
                continue
    except OSError:
        pass


def load_notified(session_id: str) -> set[str]:
    path = _state_path(session_id)
    try:
        return set(json.loads(path.read_text()))
    except (OSError, ValueError):
        return set()


def save_notified(session_id: str, keys: set[str]) -> None:
    """Persist the dedupe set, bounded, and sweep stale sessions' files."""
    path = _state_path(session_id)
    trimmed = sorted(keys)[-MAX_NOTIFIED_KEYS:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _prune_state_dir(time.time())
        path.write_text(json.dumps(trimmed))
    except OSError:
        pass  # dedupe is best-effort; never fail the hook over it


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_classify(args: argparse.Namespace) -> int:
    root = repo_root()
    apps_dir = apps_dir_name(root, args.apps_dir)
    base_ref = resolve_base_ref(root, args.base_ref or _config_base_ref(root))
    verdicts = classify(root, args.slug, base_ref, apps_dir)
    needs = [v for v in verdicts if v.needs_review]

    if args.format == "json":
        print(
            json.dumps(
                {
                    "base_ref": base_ref,
                    "needs_review": [v.slug for v in needs],
                    "apps": [asdict(v) for v in verdicts],
                },
                indent=2,
            )
        )
    else:
        if not verdicts:
            print("No app changes detected.")
        for v in verdicts:
            mark = "REVIEW" if v.needs_review else "ok"
            extra = " (skip marker)" if v.skipped else ""
            print(f"[{mark}] {v.slug}: {v.verdict} — {v.reason}{extra}")
            for rel in v.unreviewed_files:
                print(f"         unreviewed: {rel}")
            if v.coverage_mode == "whole-tree" and v.verdict == VERDICT_LOOP:
                print("         (whole-tree coverage — artifacts predate per-file tracking)")
    return 1 if needs else 0


def _config_base_ref(root: Path) -> str:
    ref = gate_config(root).get("base_ref")
    return ref if isinstance(ref, str) and ref else "origin/main"


def cmd_baseline(args: argparse.Namespace) -> int:
    root = repo_root()
    apps_dir = apps_dir_name(root, args.apps_dir)
    print(compute_baseline(root, args.slug, apps_dir))
    return 0


def cmd_stamp(args: argparse.Namespace) -> int:
    root = repo_root()
    apps_dir = apps_dir_name(root, args.apps_dir)
    # A stamp on a filename classification will never discover is worse than
    # an error: the app reads as permanently unreviewed while the caller
    # believes it stamped. Fail loudly instead.
    basename = Path(args.artifact).name.lower()
    if not (basename.startswith(ARTIFACT_PREFIXES) and basename.endswith(".md")):
        print(
            f"error: artifact {Path(args.artifact).name!r} will not be discovered by "
            f"classify — name it <prefix><ts>.md with a prefix in "
            f"{', '.join(ARTIFACT_PREFIXES)} (case-insensitive)",
            file=sys.stderr,
        )
        return 2
    base_ref = resolve_base_ref(root, args.base_ref or _config_base_ref(root))
    baseline = compute_baseline(root, args.slug, apps_dir)
    blobs = app_substantive_blobs(root, args.slug, base_ref, apps_dir)
    path = Path(args.artifact)
    if not path.is_absolute():
        path = root / path
    stamp_artifact(path, baseline, blobs)
    print(
        json.dumps(
            {
                "artifact": str(path),
                "baseline": baseline,
                "reviewed_files": sorted(blobs),
            },
            indent=2,
        )
    )
    return 0


def _nudge_text(slugs: list[str]) -> str:
    apps = ", ".join(slugs)
    plural = "s" if len(slugs) > 1 else ""
    return (
        f"Substantive app-code change{plural} in {apps} with no review covering the "
        f"current tree state. The default close-out is `/review-app {slugs[0]} --auto` "
        "(review + auto-applied mechanical fixes, looped until clean). "
        "Skip with `REVIEW_GATE_OFF=1` or an `.review/SKIP` marker file in the app."
    )


def cmd_stop_hook(args: argparse.Namespace) -> int:
    """Warn-only Stop hook. Always exits 0; emits a payload only when warranted."""
    try:
        # Bounded read inside the fail-open block: a decode error or an
        # oversized/never-closed stdin must not escape as a traceback or burn
        # through the hook's 10s timeout.
        raw = sys.stdin.read(MAX_STDIN_BYTES)
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
    except Exception:  # noqa: BLE001 — malformed / unreadable stdin is not our problem
        return 0

    try:
        if payload.get("stop_hook_active"):
            return 0
        if os.environ.get("REVIEW_GATE_OFF") == "1":
            return 0

        root = repo_root(Path(payload.get("cwd") or Path.cwd()))

        # Repo gate: this hook ships in a plugin that loads in every session,
        # so it must be inert anywhere that isn't a StreamSnow repo.
        if not (root / CONFIG_FILENAME).is_file():
            return 0
        cfg = gate_config(root)
        if cfg.get("enabled") is False:
            return 0

        branch = current_branch(root)
        if branch in ("main", "master"):
            return 0

        apps_dir = apps_dir_name(root)
        base_ref = resolve_base_ref(root, _config_base_ref(root))
        verdicts = [v for v in classify(root, None, base_ref, apps_dir) if v.needs_review]
        if not verdicts:
            return 0

        session_id = payload.get("session_id", "")
        notified = load_notified(session_id)
        fresh = [v for v in verdicts if f"{v.slug}:{v.baseline}" not in notified]
        if not fresh:
            return 0

        slugs = [v.slug for v in fresh]
        text = _nudge_text(slugs)
        out: dict[str, object] = {}
        if args.payload in ("both", "system-only"):
            out["systemMessage"] = f"Review gate: {text}"
        if args.payload == "both":
            out["hookSpecificOutput"] = {
                "hookEventName": "Stop",
                "additionalContext": text,
            }
        print(json.dumps(out))

        notified.update(f"{v.slug}:{v.baseline}" for v in fresh)
        save_notified(session_id, notified)
    except Exception:  # noqa: BLE001 — fail-open is the whole point here
        return 0
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_gate",
        description="Decide whether an app change needs review, and how deep.",
    )
    parser.add_argument(
        "--apps-dir",
        default=None,
        help="Apps directory name (default: STREAMSNOW_APPS_DIR env, "
        "review_gate.apps_dir config, then 'apps').",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("classify", help="Classify app diffs as trivial or loop.")
    p.add_argument("slug", nargs="?", default=None, help="App slug (default: all changed)")
    p.add_argument("--base-ref", default=None)
    p.add_argument("--format", choices=("md", "json"), default="md")

    p = sub.add_parser("baseline", help="Print an app's current baseline digest.")
    p.add_argument("slug")

    p = sub.add_parser("stamp", help="Write/refresh Reviewed-baseline + Reviewed-files.")
    p.add_argument("artifact")
    p.add_argument("--slug", required=True)
    p.add_argument("--base-ref", default=None)

    # Default is system-only ON PURPOSE — see the module docstring's measured
    # finding about additionalContext starting an unrequested turn. A test
    # pins this default; do not change it without re-measuring.
    p = sub.add_parser("stop-hook", help="Warn-only Stop hook entrypoint.")
    p.add_argument("--payload", choices=("both", "system-only"), default="system-only")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {
        "classify": cmd_classify,
        "baseline": cmd_baseline,
        "stamp": cmd_stamp,
        "stop-hook": cmd_stop_hook,
    }
    handler = handlers[args.cmd]
    if args.cmd == "stop-hook":
        return handler(args)  # owns its own error handling (fail-open)
    try:
        return handler(args)
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
