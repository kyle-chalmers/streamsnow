#!/usr/bin/env python3
"""Stop hook — warn-only review-gate nudge.

When a turn ends with a substantive app-code change that no review artifact
covers, this emits a one-line ``systemMessage`` pointing at
``/review-app <slug> --auto``. It never blocks a turn.

This wrapper exists so the gate works on **plugin-only installs**: the
marketplace clones the whole repository, so the gate's self-contained,
stdlib-only implementation is always present at
``<plugin root>/streamsnow/tools/review_gate.py`` and is executed **by
path** — no ``streamsnow`` pip package required, no import of anything.
The pip CLI (``streamsnow review-gate``) wraps the same file, so there is
exactly one implementation to drift.

Trust bar (matches deploy_safety.py):
- repo-gated — exits 0 instantly unless ``streamsnow.config.yaml`` exists at
  the repo root of the turn's cwd (the gate re-checks this itself too);
- stdlib-only, no network;
- fail-open — ANY error exits 0 silently; a broken gate must never wedge a
  turn;
- ``--payload=system-only`` is deliberate and measured: emitting
  ``additionalContext`` from a Stop hook starts a fresh assistant turn with
  no user input, costing an unrequested turn per change. Do not switch to
  ``both`` without re-measuring (see the gate's module docstring).

Off-switches: ``REVIEW_GATE_OFF=1``, ``apps/<slug>/.review/SKIP``, or
``review_gate: {enabled: false}`` in streamsnow.config.yaml.

Dedupe is best-effort: the gate remembers (repo, slug, baseline) per session
in ``$TMPDIR`` so the same unreviewed state nudges once, not every turn. When
that state cannot be written (read-only TMPDIR), the nudge may repeat — a
worse failure mode than silence would be, so it is accepted rather than
made load-bearing.
"""

from __future__ import annotations

import contextlib
import os
import runpy
import sys
from pathlib import Path


def main() -> int:
    try:
        gate = (
            Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))
            / "streamsnow"
            / "tools"
            / "review_gate.py"
        )
        if not gate.is_file():
            return 0  # nothing to run — never an error
        # Execute by path with argv set for the stop-hook subcommand. runpy
        # keeps this a single process (a subprocess would double the hook's
        # startup cost inside its 10s budget).
        sys.argv = [str(gate), "stop-hook", "--payload=system-only"]
        with contextlib.suppress(SystemExit):
            # The gate's stop-hook always intends 0; clamp regardless.
            runpy.run_path(str(gate), run_name="__main__")
        # ALWAYS 0, whatever the delegated gate did — a non-zero exit from a
        # Stop hook is interpreted by the harness, and this wrapper's whole
        # contract is "never influence the turn".
        return 0
    except Exception:  # noqa: BLE001 — fail-open is the contract
        return 0


if __name__ == "__main__":
    sys.exit(main())
