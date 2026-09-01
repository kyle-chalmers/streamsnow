"""Validate the §11 Build Progress block in each app's ``REQUIREMENTS.md``.

§11 is the resume contract between sessions: ``/start-app`` reads the
``**Current phase:**`` line to jump back into the lifecycle after a context
reset, and the *last* line of the ``### Sessions`` log to know the exact next
command. ``/feedback-app`` and the build phase append to the same log. When a
hand edit drops the phase line, mangles the timestamp, or deletes the session
log, those skills don't error — they silently forget where the build was and
either restart a phase that already ran or stall waiting for state that isn't
there. A structural check turns that silent amnesia into a named finding.

This validates exactly what the skills rely on to resume — nothing more:

1. a ``## 11. Build Progress`` section exists;
2. it carries a ``**Current phase:**`` line whose value is a recognized
   lifecycle phase (``spec``/``scaffold``/``build``/``preview``/``verify``/
   ``ship``/``done``, or ``in-production (backfilled)`` from spec backfill) —
   an unknown phase can't be routed to any resume target;
3. a ``Sessions`` heading follows, with at least one ``- `` bullet;
4. the **last** session line starts with an ISO 8601 timestamp
   (``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM[:SS][Z|±HH:MM]``);
5. for a non-terminal phase, the last session line names the next step
   (contains ``Next:``) — that hint is what the resume flow hands the user.

Earlier session lines are history: the log is append-only and resume only reads
the last line, so older lines are deliberately not validated (strictness there
would punish hand-written history that harms nothing).

Missing ``REQUIREMENTS.md`` files are not findings — presence is the concern of
the spec phase, not this check; adopted apps grow the file when first spec'd.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_KIND = "requirements"

# The /start-app lifecycle plus the backfill terminal state. Matching is
# case-insensitive; trailing punctuation and markdown emphasis are stripped.
_PHASES = frozenset(
    {
        "spec",
        "scaffold",
        "build",
        "preview",
        "verify",
        "ship",
        "done",
        "in-production",
        "in-production (backfilled)",
    }
)
# Phases where the app is live and no "next command" is owed.
_TERMINAL_PHASES = frozenset({"done", "in-production", "in-production (backfilled)"})

# Section header: lenient about heading depth and the separator after "11"
# (``## 11. Build Progress`` is canonical; hand edits drift).
_SECTION_RE = re.compile(r"^#{1,4}\s*11[.\s].*build\s+progress.*$", re.IGNORECASE | re.MULTILINE)
# Matches the same heading depths _SECTION_RE accepts: a `### 12. …` heading
# must terminate §11, or its content leaks in and satisfies a malformed §11.
_NEXT_SECTION_RE = re.compile(r"^#{1,4}\s*\d", re.MULTILINE)

_PHASE_RE = re.compile(r"\*\*Current phase:\*\*\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_SESSIONS_RE = re.compile(r"^(?:#{2,4}\s*Sessions\s*:?|\*\*Sessions:?\*\*)\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*-\s+(.*\S)\s*$")
_HEADING_RE = re.compile(r"^#{1,6}\s")
# A leading ISO timestamp, tolerating decoration (backticks, bold, brackets).
_TS_RE = re.compile(r"^[\s`*<\[]*\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})?)?\b")


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _normalize_phase(raw: str) -> str:
    """Strip markdown emphasis / code ticks / trailing punctuation from a phase value."""
    return raw.strip().strip("`*_").rstrip(".").strip().lower()


def check_file(path: Path) -> dict:
    """Validate one REQUIREMENTS.md. Findings name the missing/malformed piece."""
    findings: list[dict] = []
    text = path.read_text(errors="ignore")

    section_match = _SECTION_RE.search(text)
    if not section_match:
        return {
            "ok": False,
            "findings": [
                {
                    "file": str(path),
                    "line": 1,
                    "detail": "missing `## 11. Build Progress` section — /start-app "
                    "cannot resume this app without it (it holds the current phase "
                    "and the session log)",
                }
            ],
        }

    start = section_match.end()
    next_section = _NEXT_SECTION_RE.search(text, pos=start)
    end = next_section.start() if next_section else len(text)
    section = text[start:end]
    section_line = _line_of(text, section_match.start())

    phase_match = _PHASE_RE.search(section)
    phase = ""
    if not phase_match or not _normalize_phase(phase_match.group(1)):
        findings.append(
            {
                "file": str(path),
                "line": section_line,
                "detail": "§11 has no `**Current phase:** <phase>` line — resume reads "
                "this to pick which lifecycle phase to re-enter",
            }
        )
    else:
        phase = _normalize_phase(phase_match.group(1))
        if phase not in _PHASES:
            findings.append(
                {
                    "file": str(path),
                    "line": section_line + section.count("\n", 0, phase_match.start()),
                    "detail": f"`**Current phase:** {phase_match.group(1).strip()}` is not a "
                    f"recognized phase — resume routes on it; use one of: "
                    f"{', '.join(sorted(_PHASES))}",
                }
            )

    sessions_match = _SESSIONS_RE.search(section)
    if not sessions_match:
        findings.append(
            {
                "file": str(path),
                "line": section_line,
                "detail": "§11 has no `### Sessions` log — the append-only session log's "
                "last line is what tells resume the next command",
            }
        )
        return {"ok": not findings, "findings": findings}

    bullets: list[tuple[int, str]] = []  # (line offset within section, bullet text)
    pos = sessions_match.end()
    for raw in section[pos:].splitlines():
        if _HEADING_RE.match(raw):
            break
        m = _BULLET_RE.match(raw)
        if m:
            bullets.append((pos, m.group(1)))
        pos += len(raw) + 1

    if not bullets:
        findings.append(
            {
                "file": str(path),
                "line": section_line + section.count("\n", 0, sessions_match.start()),
                "detail": "§11 Sessions log has no entries — every phase transition "
                "appends one `- <timestamp> — <what happened>. Next: <command>` line",
            }
        )
        return {"ok": not findings, "findings": findings}

    # Only the LAST line matters to resume; earlier lines are unread history.
    last_offset, last = bullets[-1]
    last_line = section_line + section.count("\n", 0, last_offset)
    if not _TS_RE.match(last):
        findings.append(
            {
                "file": str(path),
                "line": last_line,
                "detail": f"last Sessions line does not start with an ISO timestamp "
                f"(`YYYY-MM-DDTHH:MMZ`): {last[:60]!r}",
            }
        )
    if phase and phase not in _TERMINAL_PHASES and "next:" not in last.casefold():
        findings.append(
            {
                "file": str(path),
                "line": last_line,
                "detail": f"current phase is {phase!r} (not terminal) but the last Sessions "
                "line names no `Next:` step — resume hands that hint to the user",
            }
        )

    return {"ok": not findings, "findings": findings}


def _targets_for(paths: list[Path]) -> list[Path]:
    """Map file/dir arguments to REQUIREMENTS.md files.

    A directory is searched recursively; an explicit file argument counts only
    when it *is* a REQUIREMENTS.md (pre-commit hands whole changesets).
    """
    out: set[Path] = set()
    for p in paths:
        if p.is_dir():
            out.update(f for f in p.rglob("REQUIREMENTS.md") if f.is_file())
        elif p.name == "REQUIREMENTS.md" and p.is_file():
            out.add(p)
    return sorted(out)


def scan_paths(paths: list[Path]) -> dict:
    findings: list[dict] = []
    for target in _targets_for(paths):
        findings.extend(check_file(target)["findings"])
    return {"ok": not findings, "findings": findings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Validate the §11 Build Progress resume contract in REQUIREMENTS.md."
    )
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    result = scan_paths([Path(p) for p in (args.paths or ["apps"])])
    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print(f"{_KIND}: clean")
    else:
        for f in result["findings"]:
            print(f"BLOCK {f['file']}:{f['line']} {f['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
