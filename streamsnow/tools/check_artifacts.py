"""Cross-check ``snowflake.yml`` ``artifacts:`` against the files on disk.

Local development reads files straight from disk, but a manifest-driven deploy
(``snow streamlit deploy``) uploads only what ``artifacts:`` lists — so a new
helper module or query file that was never added to the list works locally and
404s deployed. The reverse drift (an entry pointing at a deleted file) breaks
the deploy outright. Both recur whenever multi-file changes land without
touching the manifest, and neither is visible until deploy time.

Rules (only when an ``artifacts:`` list is present — StreamSnow's generated
stage-copy/git deploys upload the whole app dir and don't read the list):

- every deployable file on disk (``*.py``, ``*.sql``, ``pyproject.toml``,
  ``environment.yml``, ``.streamlit/config.toml``) must be covered by an entry
  (exact path, parent-directory entry, or glob);
- every entry must resolve to at least one existing path.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
from pathlib import Path

import yaml

_DEPLOYABLE_SUFFIXES = (".py", ".sql")
_DEPLOYABLE_NAMES = ("pyproject.toml", "environment.yml")
_DEPLOYABLE_EXACT = (".streamlit/config.toml",)
# Never demanded as artifacts even though they sit in the app dir.
_EXCLUDED = ("snowflake.yml",)
_GLOB_CHARS = ("*", "?", "[")


def _artifact_entries(manifest: dict) -> list[str] | None:
    """Collect artifact entries across all streamlit entities.

    Returns ``None`` when no entity declares ``artifacts:`` (check passes
    trivially). Dict entries (``{src: ..., dest: ...}``) contribute their
    ``src``.
    """
    entities = manifest.get("entities")
    if not isinstance(entities, dict):
        return None
    entries: list[str] = []
    declared = False
    for ent in entities.values():
        if not isinstance(ent, dict) or ent.get("type") not in (None, "streamlit"):
            continue
        artifacts = ent.get("artifacts")
        if not isinstance(artifacts, list):
            continue
        declared = True
        for item in artifacts:
            if isinstance(item, str):
                entries.append(item)
            elif isinstance(item, dict) and isinstance(item.get("src"), str):
                entries.append(item["src"])
    return entries if declared else None


def _deployable_files(app_dir: Path) -> list[str]:
    """App-relative paths of files a deployed app needs, skipping tooling dirs."""
    out: list[str] = []
    for p in app_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(app_dir)
        parts = rel.parts
        # Same dotted-dir policy as validate_app._walk_app_files: only .streamlit
        # is real app source among dotted dirs.
        if any(part.startswith(".") and part != ".streamlit" for part in parts[:-1]):
            continue
        if "__pycache__" in parts:
            continue
        rel_str = rel.as_posix()
        if rel_str in _EXCLUDED:
            continue
        if (
            rel_str in _DEPLOYABLE_EXACT
            or p.name in _DEPLOYABLE_NAMES
            or (p.suffix in _DEPLOYABLE_SUFFIXES and not p.name.startswith("."))
        ):
            out.append(rel_str)
    return sorted(out)


def _normalize_entry(entry: str) -> str:
    """Strip whitespace and a literal ``./`` prefix (NOT ``lstrip("./")`` — that
    would eat the leading dot of ``.streamlit/config.toml``)."""
    entry = entry.strip()
    return entry.removeprefix("./")


def _covers(entry: str, rel_path: str) -> bool:
    entry = _normalize_entry(entry)
    if not entry:
        return False
    if entry == rel_path:
        return True
    prefix = entry if entry.endswith("/") else entry + "/"
    if rel_path.startswith(prefix):
        return True
    if any(c in entry for c in _GLOB_CHARS):
        return fnmatch.fnmatch(rel_path, entry)
    return False


def _entry_exists(app_dir: Path, entry: str) -> bool:
    entry = _normalize_entry(entry).rstrip("/")
    if not entry:
        return True
    if any(c in entry for c in _GLOB_CHARS):
        return any(app_dir.glob(entry))
    return (app_dir / entry).exists()


def check_app(app_dir: Path) -> dict:
    yml = app_dir / "snowflake.yml"
    findings: list[dict] = []
    if not yml.is_file():
        # Manifest presence is the required-files/manifest checks' concern.
        return {"ok": True, "findings": []}
    try:
        manifest = yaml.safe_load(yml.read_text()) or {}
    except yaml.YAMLError:
        return {"ok": True, "findings": []}  # invalid YAML is the manifest check's finding

    entries = _artifact_entries(manifest) if isinstance(manifest, dict) else None
    if entries is None:
        return {"ok": True, "findings": []}

    for rel in _deployable_files(app_dir):
        if not any(_covers(e, rel) for e in entries):
            findings.append(
                {
                    "file": str(yml),
                    "detail": f"{rel} exists on disk but no artifacts entry covers it — "
                    "local dev reads disk while a manifest-driven deploy reads this list, "
                    "so the file silently goes missing in the deployed app",
                }
            )
    for entry in entries:
        if not _entry_exists(app_dir, entry):
            findings.append(
                {
                    "file": str(yml),
                    "detail": f"artifacts entry {entry!r} does not match anything on disk "
                    "(stale after a delete/rename?)",
                }
            )
    return {"ok": not findings, "findings": findings}


def _app_dirs_for(paths: list[Path]) -> list[Path]:
    """Map arbitrary file/dir paths to their app roots (dir containing snowflake.yml)."""
    roots: set[Path] = set()
    for p in paths:
        if p.is_dir():
            if (p / "snowflake.yml").is_file():
                roots.add(p)
            else:
                roots.update(y.parent for y in p.rglob("snowflake.yml"))
            continue
        for parent in p.resolve().parents:
            if (parent / "snowflake.yml").is_file():
                roots.add(parent)
                break
    return sorted(roots)


def scan_paths(paths: list[Path]) -> dict:
    findings = []
    for app_dir in _app_dirs_for(paths):
        findings.extend(check_app(app_dir)["findings"])
    return {"ok": not findings, "findings": findings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Cross-check snowflake.yml artifacts against files on disk."
    )
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    result = scan_paths([Path(raw) for raw in (args.paths or ["apps"])])
    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print("artifacts: clean")
    else:
        for f in result["findings"]:
            print(f"BLOCK {f['file']} {f['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
