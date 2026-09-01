"""Tests for the §11 Build Progress resume-contract check."""

from __future__ import annotations

from pathlib import Path

from streamsnow.tools import check_requirements

_GOOD = """\
# Acme Sales Dashboard — Requirements
**Source:** Local-only
**Status:** Draft · **Last updated:** 2026-08-01

## 1. Identity
acme-sales-dashboard — daily revenue overview for the retail team.

## 10. Open Questions
_None_

## 11. Build Progress
**Current phase:** build
### Sessions
- 2026-08-01T10:00Z — spec written (/start-app). Next: scaffold (`/start-app acme-sales-dashboard`).
- 2026-08-02T09:30Z — page overview scaffolded. Next: fill stubs, then /preview-app acme-sales-dashboard.
"""


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_conforming_file_is_clean(tmp_path):
    p = _write(tmp_path / "apps/acme-sales-dashboard/REQUIREMENTS.md", _GOOD)
    assert check_requirements.check_file(p)["ok"]


def test_missing_section_11(tmp_path):
    p = _write(
        tmp_path / "apps/a/REQUIREMENTS.md",
        "# App — Requirements\n\n## 1. Identity\nstuff\n",
    )
    res = check_requirements.check_file(p)
    assert not res["ok"]
    assert "Build Progress" in res["findings"][0]["detail"]


def test_missing_current_phase_line(tmp_path):
    text = _GOOD.replace("**Current phase:** build\n", "")
    p = _write(tmp_path / "apps/a/REQUIREMENTS.md", text)
    res = check_requirements.check_file(p)
    assert not res["ok"]
    assert any("Current phase" in f["detail"] for f in res["findings"])


def test_unknown_phase_flagged(tmp_path):
    text = _GOOD.replace("**Current phase:** build", "**Current phase:** polishing")
    p = _write(tmp_path / "apps/a/REQUIREMENTS.md", text)
    res = check_requirements.check_file(p)
    assert not res["ok"]
    assert any("not a recognized phase" in f["detail"] for f in res["findings"])


def test_backfilled_phase_accepted(tmp_path):
    text = _GOOD.replace(
        "**Current phase:** build", "**Current phase:** in-production (backfilled)"
    )
    p = _write(tmp_path / "apps/a/REQUIREMENTS.md", text)
    assert check_requirements.check_file(p)["ok"]


def test_missing_sessions_log(tmp_path):
    text = _GOOD[: _GOOD.index("### Sessions")]
    p = _write(tmp_path / "apps/a/REQUIREMENTS.md", text)
    res = check_requirements.check_file(p)
    assert not res["ok"]
    assert any("Sessions" in f["detail"] for f in res["findings"])


def test_empty_sessions_log(tmp_path):
    text = _GOOD[: _GOOD.index("- 2026-08-01")]
    p = _write(tmp_path / "apps/a/REQUIREMENTS.md", text)
    res = check_requirements.check_file(p)
    assert not res["ok"]
    assert any("no entries" in f["detail"] for f in res["findings"])


def test_last_session_line_needs_timestamp(tmp_path):
    text = _GOOD + "- fixed the region filter. Next: /preview-app acme-sales-dashboard.\n"
    p = _write(tmp_path / "apps/a/REQUIREMENTS.md", text)
    res = check_requirements.check_file(p)
    assert not res["ok"]
    assert any("ISO timestamp" in f["detail"] for f in res["findings"])


def test_earlier_session_lines_not_validated(tmp_path):
    # Only the LAST line is the resume contract; malformed history is harmless.
    text = _GOOD.replace(
        "- 2026-08-01T10:00Z — spec written",
        "- (hand note, no timestamp) spec written",
    )
    p = _write(tmp_path / "apps/a/REQUIREMENTS.md", text)
    assert check_requirements.check_file(p)["ok"]


def test_last_line_needs_next_hint_when_not_terminal(tmp_path):
    text = _GOOD.replace(
        "Next: fill stubs, then /preview-app acme-sales-dashboard.", "stubs filled."
    )
    p = _write(tmp_path / "apps/a/REQUIREMENTS.md", text)
    res = check_requirements.check_file(p)
    assert not res["ok"]
    assert any("Next:" in f["detail"] for f in res["findings"])


def test_terminal_phase_needs_no_next_hint(tmp_path):
    text = _GOOD.replace("**Current phase:** build", "**Current phase:** done").replace(
        "Next: fill stubs, then /preview-app acme-sales-dashboard.", "PR merged."
    )
    p = _write(tmp_path / "apps/a/REQUIREMENTS.md", text)
    assert check_requirements.check_file(p)["ok"]


def test_date_only_timestamp_accepted(tmp_path):
    text = _GOOD + "- 2026-08-03 — chart polish. Next: /preview-app acme-sales-dashboard.\n"
    p = _write(tmp_path / "apps/a/REQUIREMENTS.md", text)
    assert check_requirements.check_file(p)["ok"]


def test_scan_paths_finds_files_under_dir_and_skips_missing(tmp_path):
    _write(tmp_path / "apps/good-app/REQUIREMENTS.md", _GOOD)
    _write(tmp_path / "apps/bad-app/REQUIREMENTS.md", "# nothing here\n")
    (tmp_path / "apps/no-spec-app").mkdir()  # no REQUIREMENTS.md — not a finding
    res = check_requirements.scan_paths([tmp_path / "apps"])
    assert not res["ok"]
    assert len(res["findings"]) == 1
    assert "bad-app" in res["findings"][0]["file"]


def test_scan_paths_ignores_non_requirements_file(tmp_path):
    p = _write(tmp_path / "apps/a/README.md", "# not a spec\n")
    assert check_requirements.scan_paths([p])["ok"]


def test_main_exit_codes_and_json(tmp_path, capsys):
    _write(tmp_path / "apps/bad-app/REQUIREMENTS.md", "# nothing\n")
    assert check_requirements.main([str(tmp_path / "apps"), "--format", "json"]) == 1
    out = capsys.readouterr().out
    assert '"ok": false' in out
    _write(tmp_path / "apps/bad-app/REQUIREMENTS.md", _GOOD)
    assert check_requirements.main([str(tmp_path / "apps")]) == 0
