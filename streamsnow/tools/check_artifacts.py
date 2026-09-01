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

``--fix`` repairs the drift instead of reporting it, as a minimal edit rather
than a wholesale regeneration:

- entries that no longer match anything on disk are **dropped** (the stale
  half of the drift);
- deployable files no entry covers are **appended** as explicit paths (the
  missing half) — directory and glob entries the manifest already uses are
  kept, so ``pages/`` keeps auto-covering future pages;
- image assets (``.png``/``.jpg``/``.jpeg``/``.svg``/``.ico``) and files under
  ``data/`` are also appended when uncovered. The check doesn't *demand* them
  (an app may intentionally not ship a scratch CSV), but an asset the code
  references and the manifest omits renders locally and 404s deployed, so the
  fixer errs toward shipping them; remove the entry to opt out.

Round-trip fidelity: only the ``artifacts:`` block's own lines are rewritten —
every other byte of ``snowflake.yml`` (comments, ordering, quoting) is
preserved verbatim. Known limitation: comment lines *inside* the artifacts
block are dropped by a rewrite (a full round-trip parser was deliberately not
added as a dependency); comments on the ``artifacts:`` key line and everywhere
else survive. ``--fix`` refuses (with a finding) manifests it can't rewrite
safely: multiple entities declaring artifacts, or ``{src:, dest:}`` mapping
entries.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
from pathlib import Path

import yaml

_DEPLOYABLE_SUFFIXES = (".py", ".sql")
_DEPLOYABLE_NAMES = ("pyproject.toml", "environment.yml")
_DEPLOYABLE_EXACT = (".streamlit/config.toml",)
# Never demanded as artifacts even though they sit in the app dir.
_EXCLUDED = ("snowflake.yml",)
_GLOB_CHARS = ("*", "?", "[")
# --fix also ships these when uncovered (see module docstring): a referenced
# logo/data file missing from the manifest renders locally and 404s deployed.
_ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".svg", ".ico")


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
        # sql_review/ is the repo-side audit trail for REVIEWERS — the running
        # app never reads it, snowflake.yml must not declare it, and --fix must
        # never "repair" it into the deploy set. (A fresh scaffold generates a
        # companion there before its first validate run.)
        if parts[0] == "sql_review":
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


def _asset_files(app_dir: Path) -> list[str]:
    """Image assets anywhere in the app plus files under ``data/`` (--fix only).

    Same dotted-dir / ``__pycache__`` policy as :func:`_deployable_files`.
    """
    out: list[str] = []
    for p in app_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(app_dir)
        parts = rel.parts
        if any(part.startswith(".") and part != ".streamlit" for part in parts[:-1]):
            continue
        if "__pycache__" in parts or p.name.startswith("."):
            continue
        if p.suffix.lower() in _ASSET_SUFFIXES or parts[0] == "data":
            out.append(rel.as_posix())
    return sorted(out)


def _yaml_entry(entry: str) -> str:
    """Render one artifacts entry as a plain YAML scalar, quoting only when
    plain style would change the value (defensive — entries are normally
    simple relative paths)."""
    try:
        if yaml.safe_load(entry) == entry:
            return entry
    except yaml.YAMLError:
        pass
    return json.dumps(entry)


_ARTIFACTS_KEY_RE = re.compile(r"^(\s*)artifacts\s*:(.*)$")


def _splice_artifacts(text: str, new_entries: list[str]) -> str | None:
    """Return ``text`` with the (single) ``artifacts:`` block replaced by
    ``new_entries``, leaving every other line byte-identical. Returns None when
    the block can't be located unambiguously (zero or several key lines)."""
    lines = text.splitlines(keepends=True)
    hits = [i for i, ln in enumerate(lines) if _ARTIFACTS_KEY_RE.match(ln.rstrip("\r\n"))]
    if len(hits) != 1:
        return None
    i = hits[0]
    key_match = _ARTIFACTS_KEY_RE.match(lines[i].rstrip("\r\n"))
    assert key_match is not None
    key_indent = key_match.group(1)
    remainder = key_match.group(2).strip()

    # Find the end of the block: consume lines that are blank or indented
    # deeper than the key, then hand back any trailing blank lines (they
    # separate the block from what follows, they aren't part of it).
    j = i + 1
    item_indent: str | None = None
    while j < len(lines):
        raw = lines[j]
        stripped = raw.strip()
        if stripped:
            cur_indent = len(raw) - len(raw.lstrip(" "))
            if cur_indent <= len(key_indent):
                break
            if item_indent is None and stripped.startswith("- "):
                item_indent = " " * cur_indent
        j += 1
    while j > i + 1 and not lines[j - 1].strip():
        j -= 1

    if item_indent is None:
        item_indent = key_indent + "  "
    items = [f"{item_indent}- {_yaml_entry(e)}\n" for e in new_entries]

    if remainder and not remainder.startswith("#"):
        # Flow style (`artifacts: [a, b]`): replace the key line with block form.
        return "".join(lines[:i] + [f"{key_indent}artifacts:\n"] + items + lines[i + 1 :])
    # Block style: keep the key line verbatim (incl. any trailing comment).
    return "".join(lines[: i + 1] + items + lines[j:])


def fix_app(app_dir: Path) -> dict:
    """Repair one app's artifacts drift in place (see module docstring).

    Returns ``{"ok", "changed", "detail", "artifacts"}``. ``ok`` is False only
    when a fix was needed but couldn't be applied safely; a manifest with
    nothing to fix (no ``artifacts:`` list, invalid YAML — other checks own
    those) is ``ok`` with ``changed`` False.
    """
    yml = app_dir / "snowflake.yml"
    if not yml.is_file():
        return {"ok": True, "changed": False, "detail": "no snowflake.yml", "artifacts": []}
    old_text = yml.read_text()
    try:
        manifest = yaml.safe_load(old_text) or {}
    except yaml.YAMLError:
        return {"ok": True, "changed": False, "detail": "invalid YAML", "artifacts": []}

    entries = _artifact_entries(manifest) if isinstance(manifest, dict) else None
    if entries is None:
        return {"ok": True, "changed": False, "detail": "no artifacts list", "artifacts": []}

    # Safety gates: refuse shapes the text splice can't rewrite unambiguously.
    entities = manifest.get("entities", {})
    declaring = [
        k
        for k, ent in entities.items()
        if isinstance(ent, dict)
        and ent.get("type") in (None, "streamlit")
        and isinstance(ent.get("artifacts"), list)
    ]
    if len(declaring) != 1:
        return {
            "ok": False,
            "changed": False,
            "detail": f"{len(declaring)} entities declare artifacts — fix manually",
            "artifacts": entries,
        }
    raw_items = entities[declaring[0]]["artifacts"]
    if any(not isinstance(item, str) for item in raw_items):
        return {
            "ok": False,
            "changed": False,
            "detail": "artifacts contains {src:, dest:} mapping entries — fix manually",
            "artifacts": entries,
        }

    kept = [e for e in entries if _entry_exists(app_dir, e)]
    files = _deployable_files(app_dir) + _asset_files(app_dir)
    additions = sorted({f for f in files if not any(_covers(e, f) for e in kept)})
    new_entries = kept + additions
    if new_entries == entries:
        return {"ok": True, "changed": False, "detail": "already in sync", "artifacts": entries}

    new_text = _splice_artifacts(old_text, new_entries)
    if new_text is None:
        return {
            "ok": False,
            "changed": False,
            "detail": "could not locate a single artifacts: block to rewrite — fix manually",
            "artifacts": entries,
        }
    # Verify the splice round-trips to exactly the intended list before writing.
    try:
        reparsed = yaml.safe_load(new_text)
        new_list = reparsed["entities"][declaring[0]]["artifacts"]
    except (yaml.YAMLError, KeyError, TypeError):
        new_list = None
    if new_list != new_entries:
        return {
            "ok": False,
            "changed": False,
            "detail": "rewrite failed round-trip verification — not written; fix manually",
            "artifacts": entries,
        }
    yml.write_text(new_text)
    dropped = [e for e in entries if e not in kept]
    return {
        "ok": True,
        "changed": True,
        "detail": f"added {len(additions)} entr(y/ies), dropped {len(dropped)} stale",
        "artifacts": new_entries,
    }


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


def scan_paths(paths: list[Path], fix: bool = False) -> dict:
    findings: list[dict] = []
    fixed: list[dict] = []
    for app_dir in _app_dirs_for(paths):
        if fix:
            fix_result = fix_app(app_dir)
            if not fix_result["ok"]:
                findings.append(
                    {"file": str(app_dir / "snowflake.yml"), "detail": fix_result["detail"]}
                )
            elif fix_result["changed"]:
                fixed.append(
                    {"file": str(app_dir / "snowflake.yml"), "detail": fix_result["detail"]}
                )
        findings.extend(check_app(app_dir)["findings"])
    result: dict = {"ok": not findings, "findings": findings}
    if fixed:
        result["fixed"] = fixed
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Cross-check snowflake.yml artifacts against files on disk."
    )
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument(
        "--fix",
        action="store_true",
        help="Rewrite each app's artifacts list to match disk (drop stale entries, "
        "append uncovered files) before checking.",
    )
    args = ap.parse_args(argv)

    result = scan_paths([Path(raw) for raw in (args.paths or ["apps"])], fix=args.fix)
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        for f in result.get("fixed", []):
            print(f"FIXED {f['file']} {f['detail']}")
        for f in result["findings"]:
            print(f"BLOCK {f['file']} {f['detail']}")
        if result["ok"]:
            print("artifacts: clean")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
