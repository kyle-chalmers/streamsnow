"""Detect ``branding.py`` version skew across apps.

Every app ships its **own copy** of ``branding.py`` (deploy runtimes can't
import a shared module — see the scaffold template), so a branding change
never propagates by itself: the app you re-scaffolded gets the new colors and
API while every sibling keeps the old copy. Nothing fails — dashboards just
quietly stop matching each other, and a helper signature change surfaces as a
runtime error only in the apps left behind.

Content comparison can't catch this (copies legitimately differ — per-app
brand values are rendered into each file), so the contract is a semantic
stamp: the scaffold template carries ``_BRANDING_VERSION = "<n>"`` and every
generated copy inherits it. Bump the stamp whenever colors / template / API
change, re-scaffold or copy the file into each app, and this check enumerates
the stragglers.

What it reports:

- **finding** — an app whose stamp is older than the newest stamp found across
  apps (version skew: someone updated branding and this app was left behind).
- **note (never a failure)** — an app copy with no ``_BRANDING_VERSION`` stamp
  (scaffolded before the convention existed), or apps lagging the stamp in the
  *installed StreamSnow template* (that lag appears on every StreamSnow
  upgrade and is expected until the next re-scaffold, so it informs rather
  than blocks).

Versions compare via ``packaging.version`` where they parse; unparseable
stamps fall back to string comparison and simply must all match.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from packaging.version import InvalidVersion, Version

# App-root resolution is already solved (marker: snowflake.yml); don't grow a second one.
from .check_artifacts import _app_dirs_for

_KIND = "branding-parity"

_VERSION_RE = re.compile(r"""^_BRANDING_VERSION\s*=\s*["']([^"']+)["']""", re.MULTILINE)
# The stamp's source of truth: the branding template packaged with StreamSnow.
_TEMPLATE = Path(__file__).resolve().parent.parent / "_templates" / "app" / "branding.py.j2"


def _extract_version(path: Path) -> tuple[str, int] | None:
    """Return ``(version, line)`` of the ``_BRANDING_VERSION`` stamp, or None."""
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    match = _VERSION_RE.search(text)
    if not match:
        return None
    return match.group(1), text.count("\n", 0, match.start()) + 1


def _sort_key(version: str) -> tuple[int, Version | str]:
    """Order versions semantically when possible; parseable versions outrank
    unparseable ones so a typo'd stamp never becomes the reference."""
    try:
        return (1, Version(version))
    except InvalidVersion:
        return (0, version)


def scan_paths(paths: list[Path]) -> dict:
    findings: list[dict] = []
    notes: list[str] = []

    stamped: list[tuple[Path, str, int]] = []  # (branding.py, version, stamp line)
    for app_root in _app_dirs_for(paths):
        branding = app_root / "branding.py"
        if not branding.is_file():
            continue  # presence is the required-files check's concern, not parity's
        extracted = _extract_version(branding)
        if extracted is None:
            notes.append(
                f"{branding}: no _BRANDING_VERSION stamp (copy predates the stamp "
                "convention) — re-scaffold branding.py to opt into parity checking"
            )
            continue
        stamped.append((branding, extracted[0], extracted[1]))

    if stamped:
        reference = max((v for _, v, _ in stamped), key=_sort_key)
        for branding, version, line in stamped:
            if version != reference:
                findings.append(
                    {
                        "file": str(branding),
                        "line": line,
                        "detail": f"_BRANDING_VERSION {version!r} lags the newest app copy "
                        f"({reference!r}) — this app was left behind by a branding change; "
                        "regenerate branding.py (or copy it from an up-to-date app)",
                    }
                )
        template_version = _extract_version(_TEMPLATE)
        if template_version is not None:
            if _sort_key(template_version[0]) > _sort_key(reference):
                notes.append(
                    f"installed StreamSnow branding template is {template_version[0]!r} but the "
                    f"newest app copy is {reference!r} — re-scaffold when convenient to pick "
                    "up the new branding"
                )
        else:
            notes.append(f"{_TEMPLATE}: packaged branding template has no _BRANDING_VERSION stamp")

    result: dict = {"ok": not findings, "findings": findings}
    if notes:
        result["notes"] = notes
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Detect branding.py _BRANDING_VERSION skew across apps."
    )
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    result = scan_paths([Path(p) for p in (args.paths or ["apps"])])
    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        for f in result["findings"]:
            print(f"BLOCK {f['file']}:{f['line']} {f['detail']}")
        for note in result.get("notes", []):
            print(f"NOTE {note}")
        if result["ok"]:
            print(f"{_KIND}: clean")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
