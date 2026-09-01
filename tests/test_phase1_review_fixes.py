"""Regression tests for the Phase 1 external-review findings (Codex pass).

Each test pins one reviewed-and-fixed failure scenario; the comment names it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from streamsnow.tools import check_requirements, preview_app, review_gate
from streamsnow.tools import doctor as doctor_mod


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)


def _make_repo(root: Path, slug: str = "acme-sales-dashboard") -> Path:
    app = root / "apps" / slug
    (app / "pages").mkdir(parents=True)
    (app / "pages" / "overview.py").write_text("import streamlit as st\nst.metric('Revenue', 1)\n")
    (root / "streamsnow.config.yaml").write_text("project:\n  name: Acme\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    return root


# --------------------------------------------------------------------------- #
# review_gate: stamp must reject names classify will never discover
# --------------------------------------------------------------------------- #


def test_stamp_rejects_undiscoverable_artifact_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    repo = _make_repo(tmp_path / "repo")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    review_dir = repo / "apps" / "acme-sales-dashboard" / ".review"
    review_dir.mkdir(parents=True)
    bad = review_dir / "notes.md"  # no review-/loop-/walk- prefix
    code = review_gate.main(["stamp", str(bad), "--slug", "acme-sales-dashboard"])
    assert code == 2
    assert not bad.exists()  # nothing written — the caller must rename, not believe it stamped


# --------------------------------------------------------------------------- #
# review_gate: the hook payload's cwd must beat a stale CLAUDE_PROJECT_DIR
# --------------------------------------------------------------------------- #


def test_stop_hook_uses_payload_cwd_over_stale_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import io

    # Repo A has an unreviewed substantive change; repo B (the stale env var)
    # is clean. A hook firing for a turn in B must not notify about A.
    repo_a = _make_repo(tmp_path / "a")
    (repo_a / "apps" / "acme-sales-dashboard" / "pages" / "overview.py").write_text(
        "import streamlit as st\nst.metric('Orders', 2)\n"
    )
    repo_b = _make_repo(tmp_path / "b")

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo_a))  # stale — another session's repo
    monkeypatch.setenv("TMPDIR", str(tmp_path / "state"))
    payload = {"cwd": str(repo_b), "session_id": "s-x"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    code = review_gate.main(["stop-hook"])
    assert code == 0
    assert capsys.readouterr().out == ""  # repo B is clean; no cross-repo nudge


# --------------------------------------------------------------------------- #
# preview_app: stop must not signal a reused PID
# --------------------------------------------------------------------------- #


def test_stop_refuses_pid_that_is_not_our_process(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    repo = tmp_path / "repo"
    slug = "acme-sales-dashboard"
    state_dir = repo / ".streamsnow" / "preview"
    state_dir.mkdir(parents=True)
    # A live PID that is definitely not a preview we launched: this test's
    # own interpreter. The recorded cmd names a fake entrypoint that the live
    # command line cannot contain.
    (state_dir / f"{slug}.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "pid": os.getpid(),
                "port": 8599,
                "cmd": ["streamlit", "run", str(repo / "apps" / slug / "streamlit_app.py")],
                "entrypoint": str(repo / "apps" / slug / "streamlit_app.py"),
            }
        )
    )
    code = preview_app.main(["stop", slug, "--dir", str(repo), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["status"] == "stale_state"
    assert not (state_dir / f"{slug}.json").exists()
    # ...and we are, observably, still alive.


def test_preview_rejects_traversal_slug(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = preview_app.main(["stop", "../evil", "--dir", str(tmp_path)])
    assert code == 2


# --------------------------------------------------------------------------- #
# check_requirements: a deeper-level next heading must terminate §11
# --------------------------------------------------------------------------- #


def test_requirements_section_terminated_by_level3_heading(tmp_path: Path) -> None:
    req = tmp_path / "REQUIREMENTS.md"
    req.write_text(
        "## 11. Build Progress\n\n"
        "**Current phase:** build\n\n"
        "### 12. Appendix\n\n"
        "### Sessions\n- 2026-08-31 built the overview page. Next: /preview-app\n"
    )
    result = check_requirements.check_file(req)
    # The Sessions block lives under §12, not §11 — §11 has no session log and
    # must FAIL rather than borrow the neighbor's content.
    assert not result["ok"]
    assert any("session" in f["detail"].lower() for f in result["findings"])


# --------------------------------------------------------------------------- #
# doctor: package-wide --format contract
# --------------------------------------------------------------------------- #


def test_doctor_accepts_format_json(capsys: pytest.CaptureFixture) -> None:
    code = doctor_mod.main(["--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert "checks" in data
    assert code in (0, 1)


# NOTE: the export-gate org-name regression test lives in test_export_clean.py
# (that file is exempt from the gate's own scan; spelling the terms here would
# trip the gate on this very file).


# --------------------------------------------------------------------------- #
# validate_app: --dir anchors config discovery
# --------------------------------------------------------------------------- #


def test_validate_app_dir_flag_anchors_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from streamsnow.tools import validate_app as va

    # No config anywhere near cwd; --dir points at a repo-less directory too.
    monkeypatch.chdir(tmp_path)
    empty = tmp_path / "elsewhere"
    (empty / "apps" / "acme-sales-dashboard").mkdir(parents=True)
    code = va.main(["acme-sales-dashboard", "--dir", str(empty)])
    assert code == 2  # clear config error anchored on --dir, not a cwd surprise


# --------------------------------------------------------------------------- #
# Second-review findings
# --------------------------------------------------------------------------- #


def test_stop_refuses_pid_of_a_different_apps_preview(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """PID reused by a DIFFERENT app's preview (shares this tool's generic
    launch flags) must read stale — identity is the path-bearing tokens, never
    flags like --server.headless."""
    repo = tmp_path / "repo"
    slug_a = "acme-sales-dashboard"
    state_dir = repo / ".streamsnow" / "preview"
    state_dir.mkdir(parents=True)

    # A live process that LOOKS like a preview of app B: generic streamlit-ish
    # flags in argv, but app B's path — not app A's.
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time; time.sleep(30)\n")
    other_entry = str(tmp_path / "apps" / "acme-inventory-dashboard" / "streamlit_app.py")
    proc = subprocess.Popen(
        [sys.executable, str(sleeper), other_entry, "--server.headless", "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        (state_dir / f"{slug_a}.json").write_text(
            json.dumps(
                {
                    "slug": slug_a,
                    "pid": proc.pid,
                    "port": 8599,
                    "cmd": [
                        "streamlit",
                        "run",
                        str(repo / "apps" / slug_a / "streamlit_app.py"),
                        "--server.port",
                        "8599",
                        "--server.headless",
                        "true",
                    ],
                    "entrypoint": str(repo / "apps" / slug_a / "streamlit_app.py"),
                }
            )
        )
        code = preview_app.main(["stop", slug_a, "--dir", str(repo), "--json"])
        out = json.loads(capsys.readouterr().out)
        assert code == 0
        assert out["status"] == "stale_state"
        assert proc.poll() is None  # the other app's process was NOT signaled
    finally:
        proc.kill()
        proc.wait()


def test_tombstones_honors_custom_apps_dir_on_both_sides(tmp_path: Path) -> None:
    """--apps-dir must apply to the base-ref inventory, not just the worktree:
    a rename under a custom dir with no tombstone must still BLOCK."""
    from streamsnow.tools import check_tombstones as ct

    root = tmp_path / "repo"
    dash = root / "dashboards" / "acme-sales-dashboard"
    dash.mkdir(parents=True)
    (dash / "snowflake.yml").write_text("definition_version: 2\n")
    (root / "streamsnow.config.yaml").write_text(
        "schema_version: 1\n"
        "runtime: warehouse\n"
        "project: {name: Acme, slug: acme}\n"
        "snowflake:\n"
        "  account: ab12345\n"
        "  connection_name: acme\n"
        "  objects:\n"
        "    app_database: DATA_APPS\n"
        "    app_schema: BI_APPS\n"
        "    stage_database: DATA_APPS\n"
        "    stage_schema: BI_APPS\n"
        "    default_warehouse: STREAMLIT_WH\n"
        "  roles: {ci_role: STREAMLIT_CI_ROLE, viewer_role: STREAMLIT_APP_ROLE}\n"
        "governance: {database: ANALYTICS_DB, schema_allow: [ANALYTICS]}\n"
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    # Rename with no tombstone.
    _git(root, "mv", "dashboards/acme-sales-dashboard", "dashboards/acme-revenue-dashboard")
    _git(root, "commit", "-q", "-m", "rename")

    import os as _os

    cwd = _os.getcwd()
    _os.chdir(root)
    try:
        code = ct.main(["--base-ref", "main", "--apps-dir", "dashboards"])
    finally:
        _os.chdir(cwd)
    assert code == 1  # abandoned identifier detected under the custom dir


def test_cli_preview_shim_routes_flag_first_invocation() -> None:
    """`streamsnow preview --port N slug` must route to `start`, not argparse-error."""
    from typer.testing import CliRunner

    from streamsnow.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["preview", "--port", "8599", "no-such-app"])
    # Routed to `start`, which then fails on the missing entrypoint (exit 2) —
    # NOT the argparse "invalid choice" error the unroutable form produced.
    assert "invalid choice" not in result.output
    assert result.exit_code == 2


def test_cli_doctor_accepts_format_json() -> None:
    from typer.testing import CliRunner

    from streamsnow.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["doctor", "--format", "json"])
    assert result.exit_code in (0, 1)
    assert json.loads(result.output)["checks"]


if sys.platform == "win32":  # pragma: no cover
    pytest.skip("POSIX process semantics", allow_module_level=True)
