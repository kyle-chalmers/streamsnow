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
- symlinks that point outside the app, which would otherwise copy a file from
  elsewhere on the runner into the stage, and symlinks inside the app whose
  target is excluded (a ``runtime.txt`` link to ``.env`` would upload ``.env``).
  Directory symlinks that stay inside the app are followed.

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
    """A refusal that leaves nothing written: bad slug, unusable ``--out``."""


_STREAMLIT_DIR = ".streamlit"
_STREAMLIT_CONFIG = ".streamlit/config.toml"
_ENV_REASON = "environment file (may hold secrets)"


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
    if parts[-1] == ".env" or parts[-1].startswith(".env."):
        return _ENV_REASON
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


def select_app_files(app_dir: Path) -> tuple[dict[str, Path], list[dict]]:
    """Decide what the bundle ships for one app, without writing anything.

    Returns ``(selected, excluded)``: ``selected`` maps each shipped app-relative
    POSIX path to the file to copy, and ``excluded`` lists ``{"path", "reason"}``
    for what was left out (a pruned directory is reported once, with a trailing
    ``/``). ``bundle_app`` copies the selection and ``verify-deploy``'s
    ``stage-files`` check compares the stage with it, so both agree on what a
    deploy should hold.

    Symlinks are judged twice: by the path they appear at and by the path they
    resolve to inside the app. A link named ``runtime.txt`` that points at
    ``.env`` is therefore left out with the ``.env`` reason, because copying
    follows the link and would upload the target. A directory link that stays
    inside the app is followed (``.streamlit -> config/`` must still ship
    ``.streamlit/config.toml``); one that loops back to a directory already
    being walked is reported as a cycle, and one that escapes the app is
    reported and never read.
    """
    root = app_dir.resolve()
    entries = app_artifact_entries(app_dir)
    selected: dict[str, Path] = {}
    excluded: list[dict] = []

    def skip(rel: str, reason: str) -> None:
        excluded.append({"path": rel, "reason": reason})

    def reason_for(rel: str, real_rel: str) -> str | None:
        reason = excluded_reason(rel, entries)
        if reason is None and real_rel != rel:
            reason = excluded_reason(real_rel, entries)
        return reason

    def walk(here: Path, prefix: str, active: frozenset[Path]) -> None:
        for child in sorted(here.iterdir(), key=lambda p: p.name):
            rel = prefix + child.name
            linked = child.is_symlink()
            if linked and not child.exists():
                skip(rel, "broken symlink")
                continue
            real_rel = _inside(child, root)
            is_dir = child.is_dir()
            if real_rel is None:
                skip(rel + "/" if is_dir else rel, "symlink escapes the app")
                continue
            if is_dir:
                reason = _dir_reason(child.name)
                if reason is None and linked and real_rel != ".":
                    # A link into a pruned directory (`tools -> .git`) is pruned too.
                    reason = next((r for r in map(_dir_reason, real_rel.split("/")) if r), None)
                real = child.resolve()
                if reason is None and real in active:
                    reason = "symlink cycle: points back at a directory already being walked"
                if reason:
                    skip(rel + "/", reason)
                    continue
                walk(child, rel + "/", active | {real})
                continue
            reason = reason_for(rel, real_rel)
            if reason:
                skip(rel, reason)
                continue
            selected[rel] = child

    walk(app_dir, "", frozenset({root}))
    return selected, excluded


def bundle_app(app_dir: Path, dest: Path) -> dict:
    """Copy one app's shipped files into ``dest``.

    Returns ``{"slug", "files", "excluded"}``; ``excluded`` items are
    ``{"path", "reason"}``. Paths are app-relative POSIX on every OS.
    """
    selected, excluded = select_app_files(app_dir)
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
    or has no app directory, or when ``out`` is not empty or sits inside
    ``apps/`` (the bundle would copy itself on the next run).
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
    out.mkdir(parents=True, exist_ok=True)
    results = [bundle_app(apps / slug, out / slug) for slug in chosen]
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
