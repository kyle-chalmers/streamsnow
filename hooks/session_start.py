#!/usr/bin/env python3
"""SessionStart hook: one line of orientation, and ONLY where it helps.

A StreamSnow repo (config present) gets the banner, with a nudge when this clone has no
pre-commit hook. A repo that has Streamlit apps, or has the plugin enabled, but no config yet
gets the /onboard nudge. Everywhere else it prints nothing, so the token cost is zero in
unrelated repos.

Python, launched through ``uv run`` like the other hooks, rather than a bash script: it runs
on native Windows without bash or WSL (a 1.0 gate in RELEASING.md), and the Claude plugin
directory holds for review any hook shell script it cannot follow into the programs it runs.

Trust bar (matches the other hooks): stdlib-only, no network, read-only, and fail-open, so
ANY error prints nothing and exits 0. A broken banner must never wedge a session.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_ENABLED = re.compile(r'"streamsnow@streamsnow"\s*:\s*true')


def _version(plugin_root: Path) -> str:
    try:
        manifest = plugin_root / ".claude-plugin" / "plugin.json"
        return str(json.loads(manifest.read_text(encoding="utf-8"))["version"]) or "?"
    except Exception:  # noqa: BLE001 - a missing version shows as "?"
        return "?"


def _clone_nudge(root: Path) -> str:
    """The same path doctor's pre-commit-hook row checks; worktrees resolve to the main repo."""
    git = shutil.which("git")
    if not git:
        return ""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            git,
            "-C",
            str(root),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "hooks/pre-commit",
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    hook = proc.stdout.strip()
    if proc.returncode == 0 and hook and not Path(hook).is_file():
        return " This clone isn't set up yet (no pre-commit hook): run /onboard."
    return ""


def banner() -> str:
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".")
    plugin_root = Path(
        os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent
    )
    ver = _version(plugin_root)
    cli = "" if shutil.which("streamsnow") else " CLI not on PATH: uv tool install streamsnow."

    if not (root / "streamsnow.config.yaml").is_file():
        # Streamlit apps without StreamSnow config: point at /onboard (adopt), once.
        if any((root / "apps").glob("*/streamlit_app.py")):
            return (
                f"StreamSnow {ver}: this repo has apps/*/streamlit_app.py but no "
                "streamsnow.config.yaml: run /onboard (it maps onto existing apps and never "
                f"scaffolds over them).{cli}"
            )
        # The plugin is enabled for this repo, but the repo isn't set up yet.
        settings = root / ".claude" / "settings.json"
        if settings.is_file() and _ENABLED.search(settings.read_text(encoding="utf-8")):
            return f"StreamSnow {ver}: StreamSnow is enabled here but the repo isn't set up yet: run /onboard.{cli}"
        return ""

    return (
        f"StreamSnow {ver} repo. Governance: AGENTS.md. Skills: /onboard (setup) /build-app "
        "(spec/build; runs preview, validate and review itself) /preview-app /validate-app "
        "/review-app /sql-review /ship-app /migrate-app. Deploy-safety guard is ACTIVE "
        "(destructive snow/SQL commands pause; /ship-app is the deploy path). Key guard is ACTIVE "
        "(Claude's tools can't open ~/.streamsnow-ci). Review gate is ACTIVE (warn-only; "
        f"REVIEW_GATE_OFF=1 or apps/<slug>/.review/SKIP silences it).{_clone_nudge(root)}{cli}"
    )


def main() -> int:
    try:
        line = banner()
        if line:
            print(line)
    except Exception:  # noqa: BLE001 - fail-open is the contract
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
