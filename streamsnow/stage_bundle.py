"""Build the per-app bundle the stage-copy deploy uploads, without internal docs.

The generated stage-copy workflow used to run ``snow stage copy apps/ ...
--recursive``, which uploaded everything under ``apps/`` on every deploy: each
app's AGENTS.md, CLAUDE.md, REQUIREMENTS.md and README, plus ``sql_review/``
with its signed-off ``review_log/``. None of that is read by the running app,
but all of it landed on the stage, where anyone who can read the stage can
list and download it. The stage keeps every commit's copy, so a doc that once
held an internal note stays retrievable at that SHA.

``stage-bundle`` copies each app into ``--out/<slug>/`` minus the files a
deployed app never reads, and names every file it left out with the reason.
The workflow uploads the bundle instead of ``apps/``. What is excluded:

- root-level ``*.md`` files, unless an ``artifacts:`` entry in the app's
  ``snowflake.yml`` declares them (an app that renders its own help page from
  ``help.md`` keeps it);
- ``sql_review/``, the reviewers' audit trail (``check artifacts`` already
  treats it as non-runtime);
- dot-directories other than ``.streamlit``, and ``__pycache__``;
- everything in ``.streamlit/`` except ``config.toml``, so a local
  ``secrets.toml`` can never ship;
- ``.env`` and ``.env.*`` files anywhere in the app, even when declared:
  Streamlit in Snowflake never reads one, and a committed one usually holds
  credentials;
- any file named ``secrets.toml``, at any depth: Streamlit in Snowflake does
  not read it, and a local copy holds connection credentials. Matching by name
  also covers a real ``config/secrets.toml`` behind ``.streamlit -> config/``;
- symlinks whose target is excluded, judged at the path the target has in the
  repo (a ``runtime.txt`` link to ``../../shared/.env`` would upload ``.env``).

Symlinks that resolve inside the repo are followed, so an app can share a
helper module or ``.streamlit/config.toml`` from elsewhere in the repo, as it
could when the workflow copied ``apps/`` with ``snow stage copy --recursive``.
A symlink that resolves outside the repo fails the bundle (exit 2) before
anything is written: dropping it would deploy an app missing a file it
imports, and following it would copy a file from elsewhere on the runner into
the stage.

The same rules back the warn-only ``stage-files`` check in ``verify-deploy``,
so a repo still on the old workflow sees which files its stage holds that the
bundle would have left out.

The git-repository deploy builds from the committed repo folder, so it cannot
use the bundle; see docs/git-repository.md.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from .deploy import _safe_slug
from .tools.check_artifacts import _artifact_entries, _covers


class BundleError(ValueError):
    """A refusal that leaves nothing written: bad slug, unusable ``--out``, or a
    symlink that resolves outside the repo."""


_STREAMLIT_DIR = ".streamlit"
_STREAMLIT_CONFIG = ".streamlit/config.toml"
_ENV_REASON = "environment file (may hold secrets)"
_SECRETS_REASON = "Streamlit secrets file (never deployed)"


def _dir_reason(name: str) -> str | None:
    """Why a directory named ``name`` (one path segment) never ships."""
    if name == "__pycache__":
        return "Python bytecode cache"
    if name.startswith(".") and name != _STREAMLIT_DIR:
        return "tooling dot-directory (only .streamlit ships)"
    return None


def excluded_reason(rel: str, entries: list[str] | None) -> str | None:
    """Why the app-relative POSIX path ``rel`` stays out of the deploy, or None.

    ``entries`` is the app's ``snowflake.yml`` artifacts list (None when it has
    none); a root-level ``*.md`` an entry covers is shipped.
    """
    parts = rel.split("/")
    for name in parts[:-1]:
        reason = _dir_reason(name)
        if reason:
            return reason
    # Case-insensitive: macOS and Windows checkouts read `.ENV` as `.env`.
    base = parts[-1].casefold()
    if base == ".env" or base.startswith(".env."):
        return _ENV_REASON
    if base == "secrets.toml":
        return _SECRETS_REASON
    if _STREAMLIT_DIR in parts[:-1] and rel != _STREAMLIT_CONFIG:
        return "only .streamlit/config.toml ships from .streamlit/ (secrets stay local)"
    if parts[0] == "sql_review":
        return "sql_review/ is the reviewers' audit trail; the running app never reads it"
    if (
        len(parts) == 1
        and rel.lower().endswith(".md")
        and not any(_covers(e, rel) for e in entries or [])
    ):
        return (
            "root-level doc for people and agents; declare it under artifacts: in "
            "snowflake.yml to ship it"
        )
    return None


def app_artifact_entries(app_dir: Path) -> list[str] | None:
    """The app's ``snowflake.yml`` artifacts entries, or None when it declares none."""
    try:
        manifest = yaml.safe_load((app_dir / "snowflake.yml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return _artifact_entries(manifest) if isinstance(manifest, dict) else None


def _inside(path: Path, root: Path) -> str | None:
    """``path`` resolved, as a POSIX path relative to ``root``; None if it escapes."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        return None
    return resolved.relative_to(root).as_posix()


def _first_dir_reason(names: list[str]) -> str | None:
    return next((r for r in map(_dir_reason, names) if r), None)


def _repo_target_reason(repo_rel: str, repo: Path, is_dir: bool) -> str | None:
    """Why a link target outside the app, at ``repo_rel`` in the repo, stays out.

    A target inside another app is judged as that app's own file, so its
    AGENTS.md or ``sql_review/`` stays out. A shared file elsewhere is judged
    from the repo root (the repo's README.md is a root-level doc), except that
    a shared ``.streamlit/config.toml`` is the one ``.streamlit`` file that ships.
    """
    if repo_rel == ".":
        # The whole repo (and this app inside it) never ships. `.` would read
        # as a dot-directory below and blame a tooling folder that is not there.
        return "links to the repo root"
    parts = repo_rel.split("/")
    if is_dir:
        return _first_dir_reason(parts)
    if len(parts) > 2 and parts[0] == "apps":
        app_reason = _first_dir_reason(parts[:2])
        other = repo / "apps" / parts[1]
        return app_reason or excluded_reason("/".join(parts[2:]), app_artifact_entries(other))
    if parts[-2:] == [_STREAMLIT_DIR, "config.toml"]:
        return _first_dir_reason(parts[:-2])
    return excluded_reason(repo_rel, None)


def _escape_error(app_dir: Path, rel: str, is_dir: bool) -> BundleError:
    kind = "directory" if is_dir else "file"
    return BundleError(
        f"apps/{app_dir.name}/{rel} is a symlink to a {kind} outside the repo; the bundle "
        "cannot ship it, and leaving it out would deploy an app missing it. Move the "
        f"{kind} into the repo and point the link there, or replace the link with a copy"
    )


def select_app_files(app_dir: Path, repo: Path | None = None) -> tuple[dict[str, Path], list[dict]]:
    """Decide what the bundle ships for one app, without writing anything.

    Returns ``(selected, excluded)``: ``selected`` maps each shipped app-relative
    POSIX path to the file to copy, and ``excluded`` lists ``{"path", "reason"}``
    for what was left out (a pruned directory is reported once, with a trailing
    ``/``). ``bundle_app`` copies the selection and ``verify-deploy``'s
    ``stage-files`` check compares the stage with it, so both agree on what a
    deploy should hold.

    ``repo`` is the repo root (default: two levels above ``app_dir``, the
    parent of ``apps/``). Symlinks are judged twice: by the path they appear at
    and by the path they resolve to. A link named ``runtime.txt`` that points
    at ``.env`` is therefore left out with the ``.env`` reason, because copying
    follows the link and would upload the target. A link that resolves
    elsewhere in the repo is followed (a shared ``helpers.py``, or
    ``.streamlit -> config/``); a directory link that loops back to a directory
    already being walked is reported as a cycle. A link that resolves outside
    the repo raises :class:`BundleError`, naming the link.
    """
    root = app_dir.resolve()
    repo_root = (repo if repo is not None else app_dir.parent.parent).resolve()
    entries = app_artifact_entries(app_dir)
    selected: dict[str, Path] = {}
    excluded: list[dict] = []

    def skip(rel: str, reason: str) -> None:
        excluded.append({"path": rel, "reason": reason})

    def target_reason(child: Path, rel: str, is_dir: bool) -> str | None:
        """Why the place ``child`` resolves to stays out; raise if it leaves the repo."""
        real_rel = _inside(child, root)
        if real_rel is not None:
            if real_rel in (rel, "."):
                return None
            if is_dir:
                # A link into a pruned directory (`tools -> .git`) is pruned too.
                return _first_dir_reason(real_rel.split("/"))
            return excluded_reason(real_rel, entries)
        repo_rel = _inside(child, repo_root)
        if repo_rel is None:
            raise _escape_error(app_dir, rel, is_dir)
        return _repo_target_reason(repo_rel, repo_root, is_dir)

    def walk(here: Path, prefix: str, active: frozenset[Path]) -> None:
        for child in sorted(here.iterdir(), key=lambda p: p.name):
            rel = prefix + child.name
            if child.is_symlink() and not child.exists():
                skip(rel, "broken symlink")
                continue
            is_dir = child.is_dir()
            if is_dir:
                reason = _dir_reason(child.name) or target_reason(child, rel, True)
                real = child.resolve()
                if reason is None and real in active:
                    reason = "symlink cycle: points back at a directory already being walked"
                if reason:
                    skip(rel + "/", reason)
                    continue
                walk(child, rel + "/", active | {real})
                continue
            reason = excluded_reason(rel, entries) or target_reason(child, rel, False)
            if reason:
                skip(rel, reason)
                continue
            selected[rel] = child

    walk(app_dir, "", frozenset({root}))
    return selected, excluded


def bundle_app(
    app_dir: Path,
    dest: Path,
    selection: tuple[dict[str, Path], list[dict]] | None = None,
) -> dict:
    """Copy one app's shipped files into ``dest``.

    ``selection`` is a ``select_app_files`` result computed earlier (computed
    here when omitted). Returns ``{"slug", "files", "excluded"}``; ``excluded``
    items are ``{"path", "reason"}``. Paths are app-relative POSIX on every OS.
    """
    selected, excluded = selection if selection is not None else select_app_files(app_dir)
    for rel, src in selected.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
    return {"slug": app_dir.name, "files": sorted(selected), "excluded": excluded}


def _app_slugs(apps: Path) -> list[str]:
    if not apps.is_dir():
        return []
    return sorted(p.name for p in apps.iterdir() if p.is_dir() and _dir_reason(p.name) is None)


def build_bundle(repo: Path, out: Path, slugs: list[str] | None = None) -> dict:
    """Write ``out/<slug>/`` for each slug (default: every app under ``apps/``).

    Raises :class:`BundleError` before writing anything when a slug is invalid
    or has no app directory, when ``out`` is not empty or sits inside ``apps/``
    (the bundle would copy itself on the next run), or when an app holds a
    symlink that resolves outside ``repo``.
    """
    apps = repo / "apps"
    out_abs = out.resolve()
    apps_abs = apps.resolve()
    if out_abs == apps_abs or out_abs.is_relative_to(apps_abs):
        raise BundleError(f"--out {out.as_posix()} is inside apps/; pick a path outside it")
    if out.is_symlink() and not out.exists():
        raise BundleError(
            f"--out {out.as_posix()} is a broken symlink; remove it or pick a new path"
        )
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise BundleError(f"--out {out.as_posix()} is not empty; remove it or pick a new path")
    chosen = list(slugs) if slugs else _app_slugs(apps)
    for slug in chosen:
        try:
            _safe_slug(slug)
        except ValueError as exc:
            raise BundleError(str(exc)) from exc
        if not (apps / slug).is_dir():
            raise BundleError(f"no app directory apps/{slug}/")
    # Select every app before the first write, so an escaping symlink in the
    # last app still leaves --out untouched.
    selections = {slug: select_app_files(apps / slug, repo) for slug in chosen}
    out.mkdir(parents=True, exist_ok=True)
    results = [bundle_app(apps / slug, out / slug, selections[slug]) for slug in chosen]
    return {"ok": True, "out": out.as_posix(), "apps": results}


def render_md(result: dict) -> str:
    lines = [f"stage-bundle: wrote {len(result['apps'])} app(s) to {result['out']}"]
    for entry in result["apps"]:
        lines.append(
            f"  {entry['slug']}: {len(entry['files'])} file(s) shipped, "
            f"{len(entry['excluded'])} left out"
        )
        for item in entry["excluded"]:
            lines.append(f"      - {item['path']}: {item['reason']}")
    return "\n".join(lines)
