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
  elsewhere on the runner into the stage.

The same rules back the warn-only ``stage-files`` check in ``verify-deploy``,
so a repo still on the old workflow sees which files its stage holds that the
bundle would have left out.

The git-repository deploy builds from the committed repo folder, so it cannot
use the bundle; see docs/git-repository.md.
"""

from __future__ import annotations

import os
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


def _escapes(path: Path, root: Path) -> bool:
    return not path.resolve().is_relative_to(root)


def bundle_app(app_dir: Path, dest: Path) -> dict:
    """Copy one app's shipped files into ``dest``.

    Returns ``{"slug", "files", "excluded"}``; ``excluded`` items are
    ``{"path", "reason"}``. Paths are app-relative POSIX on every OS.
    """
    root = app_dir.resolve()
    entries = app_artifact_entries(app_dir)
    files: list[str] = []
    excluded: list[dict] = []

    def skip(rel: str, reason: str) -> None:
        excluded.append({"path": rel, "reason": reason})

    for current, dirnames, filenames in os.walk(app_dir, followlinks=False):
        here = Path(current)
        rel_here = here.relative_to(app_dir)
        dirnames.sort()
        for name in list(dirnames):
            sub = here / name
            rel = (rel_here / name).as_posix()
            reason = _dir_reason(name)
            if sub.is_symlink():
                reason = (
                    "symlink escapes the app"
                    if _escapes(sub, root)
                    else "symlinked directory is not followed; move the files into the app"
                )
            if reason:
                dirnames.remove(name)
                skip(rel + "/", reason)
        for name in sorted(filenames):
            src = here / name
            rel = (rel_here / name).as_posix()
            if src.is_symlink():
                if not src.exists():
                    skip(rel, "broken symlink")
                    continue
                if _escapes(src, root):
                    skip(rel, "symlink escapes the app")
                    continue
            reason = excluded_reason(rel, entries)
            if reason:
                skip(rel, reason)
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            files.append(rel)
    return {"slug": app_dir.name, "files": sorted(files), "excluded": excluded}


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
