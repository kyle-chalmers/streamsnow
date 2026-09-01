"""Tests for streamsnow.tools.review_gate.

The gate is the single decision function for "does this change need review".
These tests pin the behaviors the whole review loop depends on:

- triviality is AST-shape-based (comments/docstrings never reopen review);
- coverage stamps are fenced and re-stampable without eating report body;
- both artifact filename dialects (lowercase ``review-``, uppercase
  ``REVIEW-``) are honored;
- the stop-hook is fail-open, repo-gated, deduped, and defaults to
  ``--payload=system-only`` (measured: ``additionalContext`` from a Stop hook
  starts an unrequested turn — the default must not regress).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from streamsnow.tools import review_gate as rg

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


PAGE_V1 = '''"""Overview page."""

import streamlit as st


def render() -> None:
    # KPI row
    st.metric("Revenue", "$1,234")
'''

# Same code shape: docstring + comment changed only.
PAGE_V1_TRIVIAL_EDIT = '''"""Overview page (reworded docstring)."""

import streamlit as st


def render() -> None:
    # KPI row, now with a better comment
    st.metric("Revenue", "$1,234")
'''

# Real change: the metric label literal changed.
PAGE_V2 = '''"""Overview page."""

import streamlit as st


def render() -> None:
    # KPI row
    st.metric("Orders", "1,234")
'''


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return proc.stdout


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scratch StreamSnow-shaped repo with one committed Acme app."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("STREAMSNOW_APPS_DIR", raising=False)
    root = tmp_path / "repo"
    app = root / "apps" / "acme-sales-dashboard"
    (app / "pages").mkdir(parents=True)
    (app / "pages" / "overview.py").write_text(PAGE_V1)
    (app / "streamlit_app.py").write_text("import streamlit as st\n")
    (root / "streamsnow.config.yaml").write_text("project:\n  name: Acme\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    return root


SLUG = "acme-sales-dashboard"


def _overview(root: Path) -> Path:
    return root / "apps" / SLUG / "pages" / "overview.py"


# ---------------------------------------------------------------------------
# Triviality (AST shape)
# ---------------------------------------------------------------------------


def test_comment_and_docstring_edit_is_trivial() -> None:
    assert rg.py_change_is_trivial(PAGE_V1.encode(), PAGE_V1_TRIVIAL_EDIT.encode())


def test_string_literal_change_is_substantive() -> None:
    # A caption/label literal and a SQL literal are indistinguishable at the
    # AST level — review is the safe default.
    assert not rg.py_change_is_trivial(PAGE_V1.encode(), PAGE_V2.encode())


def test_new_file_is_never_trivial() -> None:
    assert not rg.py_change_is_trivial(None, PAGE_V1.encode())


def test_unparseable_version_is_never_trivial() -> None:
    assert not rg.py_change_is_trivial(PAGE_V1.encode(), b"def broken(:\n")


def test_docstring_kept_when_dunder_doc_referenced() -> None:
    before = '"""v1"""\nx = __doc__\n'
    after = '"""v2"""\nx = __doc__\n'
    # The module renders its docstring at runtime, so the edit is real.
    assert not rg.py_change_is_trivial(before.encode(), after.encode())


def test_classify_trivial_paths() -> None:
    assert rg._is_trivial_path("apps/x/README.md")
    assert rg._is_trivial_path("apps/x/screenshots/overview.png")
    assert rg._is_trivial_path("apps/x/.review/review-1.md")
    assert rg._is_trivial_path("apps/x/VERSION")
    assert not rg._is_trivial_path("apps/x/pages/overview.py")
    assert not rg._is_trivial_path("apps/x/queries/revenue_daily.sql")


# ---------------------------------------------------------------------------
# classify end-to-end
# ---------------------------------------------------------------------------


def test_classify_flags_substantive_change(repo: Path) -> None:
    _overview(repo).write_text(PAGE_V2)
    verdicts = rg.classify(repo, SLUG, "main")
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.verdict == rg.VERDICT_LOOP
    assert v.needs_review
    assert v.unreviewed_files == [f"apps/{SLUG}/pages/overview.py"]


def test_classify_trivial_for_comment_edit(repo: Path) -> None:
    _overview(repo).write_text(PAGE_V1_TRIVIAL_EDIT)
    v = rg.classify(repo, SLUG, "main")[0]
    assert v.verdict == rg.VERDICT_TRIVIAL
    assert not v.needs_review


def test_classify_no_changes(repo: Path) -> None:
    v = rg.classify(repo, SLUG, "main")[0]
    assert v.verdict == rg.VERDICT_TRIVIAL
    assert v.reviewed
    assert v.reason == "no changes under this app"


def test_deleted_file_reads_unreviewed(repo: Path) -> None:
    _overview(repo).unlink()
    v = rg.classify(repo, SLUG, "main")[0]
    assert v.verdict == rg.VERDICT_LOOP
    assert v.needs_review


def test_skip_marker_suppresses_needs_review(repo: Path) -> None:
    _overview(repo).write_text(PAGE_V2)
    marker = repo / "apps" / SLUG / ".review" / "SKIP"
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    v = rg.classify(repo, SLUG, "main")[0]
    assert v.skipped
    assert not v.needs_review


# ---------------------------------------------------------------------------
# Stamp + coverage
# ---------------------------------------------------------------------------


def _stamp_current(repo: Path, artifact_name: str) -> Path:
    review_dir = repo / "apps" / SLUG / ".review"
    review_dir.mkdir(parents=True, exist_ok=True)
    artifact = review_dir / artifact_name
    if not artifact.exists():
        artifact.write_text("# Review report\n\n## SQL\n\n### BLOCK\n- _none_\n")
    baseline = rg.compute_baseline(repo, SLUG)
    blobs = rg.app_substantive_blobs(repo, SLUG, "main")
    rg.stamp_artifact(artifact, baseline, blobs)
    return artifact


def test_stamped_change_reads_reviewed(repo: Path) -> None:
    _overview(repo).write_text(PAGE_V2)
    _stamp_current(repo, "review-20260831-120000.md")
    v = rg.classify(repo, SLUG, "main")[0]
    assert v.reviewed
    assert not v.needs_review
    assert v.coverage_mode == "per-file"


def test_uppercase_artifact_dialect_also_counts(repo: Path) -> None:
    # Artifacts from other tooling use REVIEW-<ts>.md; coverage must match
    # case-insensitively or stamping silently never applies.
    _overview(repo).write_text(PAGE_V2)
    _stamp_current(repo, "REVIEW-20260831-120000.md")
    v = rg.classify(repo, SLUG, "main")[0]
    assert v.reviewed


def test_comment_edit_after_review_stays_reviewed(repo: Path) -> None:
    _overview(repo).write_text(PAGE_V2)
    _stamp_current(repo, "review-1.md")
    # Now reword a comment in the reviewed file: coverage key is AST-shaped,
    # so the review must NOT reopen.
    _overview(repo).write_text(PAGE_V2.replace("# KPI row", "# KPI row (top)"))
    v = rg.classify(repo, SLUG, "main")[0]
    assert v.reviewed


def test_real_edit_after_review_reopens_only_that_file(repo: Path) -> None:
    _overview(repo).write_text(PAGE_V2)
    (repo / "apps" / SLUG / "queries").mkdir()
    (repo / "apps" / SLUG / "queries" / "revenue_daily.sql").write_text("SELECT 1\n")
    _stamp_current(repo, "review-1.md")
    _overview(repo).write_text(PAGE_V2.replace('"Orders"', '"Units"'))
    v = rg.classify(repo, SLUG, "main")[0]
    assert not v.reviewed
    assert v.unreviewed_files == [f"apps/{SLUG}/pages/overview.py"]
    assert f"apps/{SLUG}/queries/revenue_daily.sql" in v.reviewed_files


def test_restamp_is_idempotent_and_fenced(repo: Path) -> None:
    _overview(repo).write_text(PAGE_V2)
    artifact = _stamp_current(repo, "review-1.md")
    # Add a body line that LOOKS like a coverage line; re-stamping must not
    # eat it, and it must not count as coverage (it is outside the fence).
    body_line = "deadbeefdeadbeef  apps/acme-sales-dashboard/pages/other.py"
    artifact.write_text(artifact.read_text() + f"\n{body_line}\n")
    baseline = rg.compute_baseline(repo, SLUG)
    rg.stamp_artifact(artifact, baseline, rg.app_substantive_blobs(repo, SLUG, "main"))
    text = artifact.read_text()
    assert text.count(rg.BASELINE_HEADER) == 1
    assert text.count(rg.FILES_END) == 1
    assert body_line in text
    coverage = rg.stored_file_coverage(repo, SLUG, "apps")
    assert ("apps/acme-sales-dashboard/pages/other.py", "deadbeefdeadbeef") not in coverage


def test_reverting_to_reviewed_content_reads_reviewed(repo: Path) -> None:
    _overview(repo).write_text(PAGE_V2)
    _stamp_current(repo, "review-1.md")
    _overview(repo).write_text(PAGE_V2.replace('"Orders"', '"Units"'))
    _overview(repo).write_text(PAGE_V2)  # revert
    v = rg.classify(repo, SLUG, "main")[0]
    assert v.reviewed


# ---------------------------------------------------------------------------
# apps_dir override
# ---------------------------------------------------------------------------


def test_apps_dir_from_env(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STREAMSNOW_APPS_DIR", "dashboards")
    assert rg.apps_dir_name(repo) == "dashboards"


def test_apps_dir_from_config(tmp_path: Path) -> None:
    root = tmp_path
    (root / "streamsnow.config.yaml").write_text("review_gate:\n  apps_dir: dashboards\n")
    assert rg.apps_dir_name(root) == "dashboards"


def test_apps_dir_default(tmp_path: Path) -> None:
    assert rg.apps_dir_name(tmp_path) == "apps"


# ---------------------------------------------------------------------------
# stop-hook
# ---------------------------------------------------------------------------


def _run_stop_hook(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    payload: dict | str | None,
    argv_payload: str | None = None,
) -> tuple[int, str]:
    import io

    # Isolate the dedupe state dir — the real TMPDIR persists across test
    # runs, and a leftover session file makes the hook silently dedupe.
    monkeypatch.setenv("TMPDIR", str(repo / ".gate-state"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    raw = json.dumps(payload) if isinstance(payload, dict) else payload or ""
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    argv = ["stop-hook"]
    if argv_payload:
        argv += ["--payload", argv_payload]
    code = rg.main(argv)
    return code, capsys.readouterr().out


def _hook_payload(repo: Path) -> dict:
    return {"cwd": str(repo), "session_id": "s1"}


def test_stop_hook_emits_system_message_only_by_default(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _overview(repo).write_text(PAGE_V2)
    code, out = _run_stop_hook(repo, monkeypatch, capsys, _hook_payload(repo))
    assert code == 0
    data = json.loads(out)
    assert "systemMessage" in data
    # The measured lesson: additionalContext from a Stop hook starts an
    # unrequested turn. The default payload must never include it.
    assert "hookSpecificOutput" not in data
    assert "/review-app" in data["systemMessage"]


def test_stop_hook_dedupes_within_session(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path / "state"))
    _overview(repo).write_text(PAGE_V2)
    code, out = _run_stop_hook(repo, monkeypatch, capsys, _hook_payload(repo))
    assert json.loads(out)
    code, out = _run_stop_hook(repo, monkeypatch, capsys, _hook_payload(repo))
    assert code == 0
    assert out == ""


def test_stop_hook_silent_outside_streamsnow_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    code, out = _run_stop_hook(root, monkeypatch, capsys, {"cwd": str(root)})
    assert code == 0
    assert out == ""


def test_stop_hook_silent_when_disabled_in_config(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    (repo / "streamsnow.config.yaml").write_text("review_gate:\n  enabled: false\n")
    _overview(repo).write_text(PAGE_V2)
    code, out = _run_stop_hook(repo, monkeypatch, capsys, _hook_payload(repo))
    assert code == 0
    assert out == ""


def test_stop_hook_silent_on_main_branch(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _git(repo, "checkout", "-q", "main")
    _overview(repo).write_text(PAGE_V2)
    code, out = _run_stop_hook(repo, monkeypatch, capsys, _hook_payload(repo))
    assert code == 0
    assert out == ""


def test_stop_hook_respects_env_off_switch(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("REVIEW_GATE_OFF", "1")
    _overview(repo).write_text(PAGE_V2)
    code, out = _run_stop_hook(repo, monkeypatch, capsys, _hook_payload(repo))
    assert code == 0
    assert out == ""


def test_stop_hook_fail_open_on_garbage_stdin(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    code, out = _run_stop_hook(repo, monkeypatch, capsys, "not json {{{")
    assert code == 0
    assert out == ""


def test_stop_hook_fail_open_when_stop_hook_active(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    payload = {**_hook_payload(repo), "stop_hook_active": True}
    _overview(repo).write_text(PAGE_V2)
    code, out = _run_stop_hook(repo, monkeypatch, capsys, payload)
    assert code == 0
    assert out == ""


def test_stop_hook_default_payload_is_system_only() -> None:
    # Pins the argparse default itself: `both` re-introduces the unrequested
    # extra turn. Do not change without re-measuring (see module docstring).
    parser = rg._build_parser()
    args = parser.parse_args(["stop-hook"])
    assert args.payload == "system-only"


# ---------------------------------------------------------------------------
# Self-containment (the plugin hook executes this file by path, no pip)
# ---------------------------------------------------------------------------


def test_module_runs_standalone_by_path(repo: Path) -> None:
    """The Stop hook runs review_gate.py from ${CLAUDE_PLUGIN_ROOT} on machines
    without the streamsnow package installed — the file must work as a plain
    script with no package imports."""
    tool = Path(rg.__file__)
    proc = subprocess.run(
        ["python3", str(tool), "baseline", SLUG],
        cwd=repo,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()
