"""Environment doctor — per-check prerequisite detection for StreamSnow.

The CLI's original ``doctor`` printed a flat pass/fail transcript, which is
fine for a human at a terminal but opaque to everything else: ``/start-app``'s
preflight, CI bootstrap steps, and "fix then re-check" loops all need to know
*which* prerequisite failed and what to do about it, without scraping console
text. This module restructures the same coverage into per-check subresults so
those callers get machine-readable state and the CLI keeps a human rendering.

Contract — every check returns one dict::

    {"name": str, "ok": bool, "level": "required" | "optional",
     "detail": {...}, "hint": str}

``level`` is per-result, not per-check: the config check is *optional* when no
``streamsnow.config.yaml`` exists (a machine can be healthy outside any repo)
but *required* when one exists and fails validation — a malformed config must
never be masked as "not configured yet".

Checks (the same set the CLI covered): Python >= 3.11 (the running
interpreter — the one that would run the tools), ``git`` and ``uv`` on PATH
(required), ``snow`` and ``streamlit`` on PATH (optional, preview/deploy
conveniences), and config presence + validity.

Detection only: no prompts, no fix execution — hints name the fix, callers own
the UX. Checks never raise; an unexpected error inside the doctor itself is a
tool error.

Exit codes: 0 = every required check passes, 1 = a required prerequisite is
missing or broken, 2 = the doctor itself failed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from ..config import ConfigError, find_config, load_config

REQUIRED = "required"
OPTIONAL = "optional"

_MIN_PYTHON = (3, 11)

# name -> (level, hint when missing)
_PATH_TOOLS: tuple[tuple[str, str, str], ...] = (
    ("git", REQUIRED, "install git"),
    ("uv", REQUIRED, "install uv — https://docs.astral.sh/uv/"),
    ("snow", OPTIONAL, "uv tool install snowflake-cli-labs (for preview/deploy diagnostics)"),
    (
        "streamlit",
        OPTIONAL,
        "uv pip install streamlit (in your app environment, for local preview)",
    ),
)


def _result(name: str, ok: bool, level: str, detail: dict, hint: str = "") -> dict:
    return {"name": name, "ok": ok, "level": level, "detail": detail, "hint": hint}


def check_python(minimum: tuple[int, int] = _MIN_PYTHON) -> dict:
    """The *running* interpreter, matching the CLI: it is the one that executes
    the governance tools, so probing some other ``python3`` on PATH would pass
    a machine that still fails in practice."""
    version = (sys.version_info.major, sys.version_info.minor)
    ok = version >= minimum
    return _result(
        "python",
        ok,
        REQUIRED,
        {"version": f"{version[0]}.{version[1]}", "min": f"{minimum[0]}.{minimum[1]}"},
        "" if ok else f"need Python >= {minimum[0]}.{minimum[1]}",
    )


def check_path_tool(name: str, level: str, hint: str) -> dict:
    path = shutil.which(name)
    return _result(
        name,
        path is not None,
        level,
        {"found": path is not None, "path": path or ""},
        "" if path else hint,
    )


def check_config(start: Path | None = None) -> dict:
    """Config presence + validity, walking up from ``start`` (default: cwd).

    Missing is an *optional* miss (just not a configured repo); present but
    invalid is a *required* failure with the validation message as detail.
    """
    cfg_path = find_config(start)
    if cfg_path is None:
        return _result(
            "config",
            False,
            OPTIONAL,
            {"found": False},
            "no streamsnow.config.yaml here — run 'streamsnow configure'",
        )
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        return _result(
            "config",
            False,
            REQUIRED,
            {"found": True, "path": str(cfg_path), "error": str(exc)},
            "fix streamsnow.config.yaml (or re-run 'streamsnow configure')",
        )
    return _result(
        "config",
        True,
        REQUIRED,
        {"found": True, "path": str(cfg_path), "schema_version": cfg.schema_version},
    )


def run_checks(start: Path | None = None) -> list[dict]:
    """Run every check; never raises from an individual check."""
    checks = [check_python()]
    checks += [check_path_tool(name, level, hint) for name, level, hint in _PATH_TOOLS]
    checks.append(check_config(start))
    return checks


def required_ok(results: list[dict]) -> bool:
    return all(r["ok"] or r["level"] == OPTIONAL for r in results)


def render_text(results: list[dict]) -> str:
    """Plain-text rendering the CLI can print (or wrap in color itself).

    Marks: ``ok`` passed; ``MISSING`` a required prerequisite is absent/broken;
    ``skip`` an optional one is absent (informational, does not gate).
    """
    lines = []
    for r in results:
        if r["ok"]:
            mark = "ok     "
        elif r["level"] == OPTIONAL:
            mark = "skip   "
        else:
            mark = "MISSING"
        summary = _summarize(r["detail"])
        hint = f" — {r['hint']}" if r["hint"] and not r["ok"] else ""
        lines.append(f"[{mark}] {r['name']}{f' ({summary})' if summary else ''}{hint}")
    lines.append("doctor: " + ("all required checks passed" if required_ok(results) else "FAIL"))
    return "\n".join(lines)


def _summarize(detail: dict) -> str:
    if "version" in detail:
        return f"v{detail['version']}"
    if detail.get("path"):
        return str(detail["path"])
    if "error" in detail:
        return str(detail["error"])[:80]
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Check the local environment for the prerequisites StreamSnow needs."
    )
    ap.add_argument("--json", action="store_true", help="Emit per-check results as JSON.")
    args = ap.parse_args(argv)

    try:
        results = run_checks()
    except Exception as exc:  # noqa: BLE001 — the doctor itself must not crash opaquely
        print(f"doctor: tool error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"ok": required_ok(results), "checks": results}, indent=2))
    else:
        print(render_text(results))
    return 0 if required_ok(results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
