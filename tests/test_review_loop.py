"""Tests for streamsnow.tools.review_loop — the /review-app --auto primitives."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from streamsnow.tools import review_loop as rl

REPORT = """# Review report — acme-sales-dashboard

## SQL

### BLOCK
- [apps/acme-sales-dashboard/queries/revenue_daily.sql:12] SELECT * over a wide view — name the columns; wastes compute and breaks on schema changes.

### FLAG
- [apps/acme-sales-dashboard/pages/overview.py:40] query function missing @st.cache_data ttl — every rerun refetches.

### NICE-TO-HAVE
- _none_

## UI

### BLOCK
- _none_

### FLAG
- [apps/acme-sales-dashboard/pages/overview.py:88] table numbers lack thousand separators — hard to scan.

### NICE-TO-HAVE
- unlabeled chart axis on the trend chart
"""


def test_parse_findings_schema() -> None:
    findings = rl.parse_findings(REPORT)
    assert len(findings) == 4
    block = [f for f in findings if f.severity == "BLOCK"]
    assert len(block) == 1
    assert block[0].dimension == "SQL"
    assert block[0].citation == "apps/acme-sales-dashboard/queries/revenue_daily.sql:12"
    assert block[0].summary.startswith("SELECT *")
    assert "wastes compute" in block[0].why
    # A bullet with no [citation] still parses.
    nth = [f for f in findings if f.severity == "NICE-TO-HAVE"]
    assert nth[0].citation == ""


def test_parse_skips_resolutions_section() -> None:
    text = REPORT + "\n## Resolutions\n\n### Applied (Bucket A)\n- [x.py:1] already fixed\n"
    findings = rl.parse_findings(text)
    assert all(f.dimension.lower() != "resolutions" for f in findings)
    assert len(findings) == 4


def test_normalize_summary() -> None:
    assert rl.normalize_summary("  SELECT *   over a\twide view ") == "select * over a wide view"


# ---------------------------------------------------------------------------
# Resolutions + dedup
# ---------------------------------------------------------------------------


def _write_report_with_resolutions(path: Path) -> None:
    path.write_text(
        REPORT + "\n## Resolutions\n\n### Applied (Bucket A)\n"
        "- [apps/acme-sales-dashboard/queries/revenue_daily.sql:12] SELECT * over a wide view — fixed.\n"
        "\n### Deferred — judgment required (Bucket B)\n"
        "- [apps/acme-sales-dashboard/pages/overview.py:40] query function missing @st.cache_data ttl — needs TTL decision.\n"
    )


def test_dedup_filters_previously_resolved(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    session = tmp_path / ".review"
    session.mkdir()
    _write_report_with_resolutions(session / "review-20260830-090000.md")
    new_report = session / "review-20260831-090000.md"
    new_report.write_text(REPORT)

    code = rl.main(["dedup-findings", str(session), "--new", str(new_report)])
    assert code == 0
    kept = json.loads(capsys.readouterr().out)
    citations = {f["citation"] for f in kept}
    # Both resolved findings are filtered; the un-resolved UI ones remain.
    assert "apps/acme-sales-dashboard/queries/revenue_daily.sql:12" not in citations
    assert "apps/acme-sales-dashboard/pages/overview.py:40" not in citations
    assert "apps/acme-sales-dashboard/pages/overview.py:88" in citations


def test_dedup_reads_uppercase_artifact_dialect(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    session = tmp_path / ".review"
    session.mkdir()
    _write_report_with_resolutions(session / "REVIEW-20260830-090000.md")
    new_report = session / "review-new.md"
    new_report.write_text(REPORT)
    rl.main(["dedup-findings", str(session), "--new", str(new_report)])
    kept = json.loads(capsys.readouterr().out)
    assert "apps/acme-sales-dashboard/queries/revenue_daily.sql:12" not in {
        f["citation"] for f in kept
    }


def test_stale_resolutions_outside_window_do_not_dedup(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    import os
    import time

    session = tmp_path / ".review"
    session.mkdir()
    old = session / "review-old.md"
    _write_report_with_resolutions(old)
    stale = time.time() - 8 * 86400
    os.utime(old, (stale, stale))
    new_report = session / "review-new.md"
    new_report.write_text(REPORT)
    rl.main(["dedup-findings", str(session), "--new", str(new_report)])
    kept = json.loads(capsys.readouterr().out)
    assert "apps/acme-sales-dashboard/queries/revenue_daily.sql:12" in {f["citation"] for f in kept}


def test_write_resolutions_appends_block(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    report = tmp_path / "review-1.md"
    report.write_text(REPORT)
    applied = tmp_path / "applied.json"
    applied.write_text(
        json.dumps(
            [
                {
                    "citation": "apps/acme-sales-dashboard/queries/revenue_daily.sql:12",
                    "summary": "SELECT * over a wide view",
                    "why": "columns now named",
                }
            ]
        )
    )
    code = rl.main(["write-resolutions", str(report), "--applied", str(applied)])
    assert code == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts == {"applied": 1, "deferred_b": 0, "bucket_c": 0, "out_of_scope": 0}
    text = report.read_text()
    assert "## Resolutions" in text
    assert "### Applied (Bucket A)" in text
    # Round-trip: the block we wrote is what dedup reads.
    tuples = rl.parse_resolution_tuples(text)
    assert (
        "apps/acme-sales-dashboard/queries/revenue_daily.sql:12",
        "select * over a wide view",
    ) in tuples


# ---------------------------------------------------------------------------
# exit-condition matrix
# ---------------------------------------------------------------------------


def _exit(capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, dict]:
    code = rl.main(["exit-condition", *argv])
    return code, json.loads(capsys.readouterr().out)


def test_exit_max_iterations_checked_first(capsys: pytest.CaptureFixture) -> None:
    # Even a clean-looking cycle at the ceiling must report max-iterations,
    # not clean — the loop ran out of budget, not out of findings.
    code, verdict = _exit(
        capsys, "--iter", "5", "--max-iter", "5", "--applied", "3", "--block", "0", "--flag", "0"
    )
    assert code == 1
    assert verdict["reason"] == "max-iterations"


def test_exit_clean(capsys: pytest.CaptureFixture) -> None:
    code, verdict = _exit(capsys, "--iter", "2", "--max-iter", "5", "--applied", "1")
    assert code == 1
    assert verdict["reason"] == "clean"


def test_exit_plateau(capsys: pytest.CaptureFixture) -> None:
    code, verdict = _exit(
        capsys, "--iter", "2", "--max-iter", "5", "--applied", "0", "--block", "1"
    )
    assert code == 1
    assert verdict["reason"] == "plateau"


def test_continue_when_fixes_applied_and_findings_remain(capsys: pytest.CaptureFixture) -> None:
    code, verdict = _exit(capsys, "--iter", "2", "--max-iter", "5", "--applied", "2", "--flag", "3")
    assert code == 0
    assert verdict["reason"] == "continue"


def test_walk_degraded_is_terminal(capsys: pytest.CaptureFixture) -> None:
    code, verdict = _exit(
        capsys,
        "--iter",
        "1",
        "--max-iter",
        "5",
        "--applied",
        "0",
        "--walk-status",
        "DEGRADED",
        "--walk-findings-new",
        "3",
    )
    assert code == 1
    assert verdict["reason"] == "walk-degraded"


def test_unknown_walk_status_treated_as_degraded(capsys: pytest.CaptureFixture) -> None:
    # Allow-list, not a DEGRADED check: --walk-status=FAILED must not report
    # the UI as verified.
    code, verdict = _exit(
        capsys, "--iter", "1", "--max-iter", "5", "--applied", "0", "--walk-status", "FAILED"
    )
    assert verdict["walk_status"] == "DEGRADED"
    assert verdict["reason"] == "walk-degraded"


def test_walk_reentry_when_new_findings_within_cap(capsys: pytest.CaptureFixture) -> None:
    code, verdict = _exit(
        capsys,
        "--iter",
        "1",
        "--max-iter",
        "5",
        "--applied",
        "0",
        "--walk-status",
        "CLEAN",
        "--walk-not-clean",
        "--walk-findings-new",
        "2",
        "--walk-reentries",
        "0",
        "--max-walk-reentries",
        "2",
    )
    assert code == 0
    assert verdict["reason"] == "walk-reentry"


def test_walk_reentry_cap_exhausted_plateaus(capsys: pytest.CaptureFixture) -> None:
    code, verdict = _exit(
        capsys,
        "--iter",
        "1",
        "--max-iter",
        "5",
        "--applied",
        "0",
        "--walk-status",
        "CLEAN",
        "--walk-not-clean",
        "--walk-findings-new",
        "2",
        "--walk-reentries",
        "2",
        "--max-walk-reentries",
        "2",
    )
    assert code == 1
    assert verdict["reason"] == "plateau"


# ---------------------------------------------------------------------------
# merge-findings
# ---------------------------------------------------------------------------


def test_merge_findings_consensus_tags(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    a = tmp_path / "claude.md"
    a.write_text(REPORT)
    b = tmp_path / "other.md"
    b.write_text(
        "## SQL\n\n### BLOCK\n"
        "- [apps/acme-sales-dashboard/queries/revenue_daily.sql:12] SELECT * over a wide view — same finding, different reviewer.\n"
        "\n### FLAG\n"
        "- [apps/acme-sales-dashboard/sql_loader.py:5] token substitution unguarded — could render empty fragment.\n"
    )
    code = rl.main(["merge-findings", "--inputs", f"claude:{a},other:{b}"])
    assert code == 0
    out = capsys.readouterr().out
    assert "(also flagged by Other)" in out
    assert "token substitution unguarded" in out
    # The duplicate BLOCK appears once.
    assert out.count("SELECT * over a wide view") == 1


def test_merge_findings_missing_report_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    a = tmp_path / "claude.md"
    a.write_text(REPORT)
    code = rl.main(["merge-findings", "--inputs", f"claude:{a},other:{tmp_path / 'nope.md'}"])
    assert code == 2


def test_dedup_with_repeats_surfaces_no_convergence(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A finding that returns after its own applied fix must be surfaced as a
    repeat (no-convergence signal), even though plain dedup filters it."""
    session = tmp_path / ".review"
    session.mkdir()
    _write_report_with_resolutions(session / "review-20260830-090000.md")
    new_report = session / "review-20260831-090000.md"
    new_report.write_text(REPORT)  # the SELECT * BLOCK is back after being Applied
    code = rl.main(["dedup-findings", str(session), "--new", str(new_report), "--with-repeats"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    kept_cites = {f["citation"] for f in out["kept"]}
    repeat_cites = {f["citation"] for f in out["repeats_of_applied"]}
    assert "apps/acme-sales-dashboard/queries/revenue_daily.sql:12" in repeat_cites
    # The deferred (Bucket B) finding is deduped but NOT a repeat-of-applied.
    assert "apps/acme-sales-dashboard/pages/overview.py:40" not in repeat_cites
    assert "apps/acme-sales-dashboard/queries/revenue_daily.sql:12" not in kept_cites
