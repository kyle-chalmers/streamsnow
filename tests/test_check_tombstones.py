"""Tests for check_tombstones — the no-delete-path consent gate.

Each test drives the tool against a throwaway git repo: two Acme apps
committed as the base, then working-tree mutations simulate the PR under
review. The tool honors cwd for both git and relative paths, so the tests
chdir into the scratch repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from streamsnow.tools.check_tombstones import drop_sql, load_registry, main

CONFIG = """\
schema_version: 1
runtime: warehouse
project:
  name: "Acme Dashboards"
  slug: "acme-dashboards"
snowflake:
  account: "ab12345.us-east-1"
  connection_name: "acme"
  objects:
    app_database: "DATA_APPS"
    app_schema: "BI_APPS"
    stage_database: "DATA_APPS"
    stage_schema: "BI_APPS"
    default_warehouse: "STREAMLIT_WH"
  roles:
    ci_role: "STREAMLIT_CI_ROLE"
    viewer_role: "STREAMLIT_APP_ROLE"
governance:
  database: "ANALYTICS_DB"
  schema_allow: ["ANALYTICS", "REPORTING"]
"""

MANIFEST = """\
definition_version: 2
entities:
  {module}:
    type: streamlit
    identifier:
      name: {name}
      database: DATA_APPS
      schema: BI_APPS
    main_file: streamlit_app.py
"""

SALES_FQN = "DATA_APPS.BI_APPS.ACME_SALES_DASHBOARD"
CAMPAIGN_FQN = "DATA_APPS.BI_APPS.MARKETING_CAMPAIGN_DASHBOARD"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _write_app(repo: Path, slug: str) -> None:
    module = slug.replace("-", "_")
    app = repo / "apps" / slug
    app.mkdir(parents=True)
    (app / "snowflake.yml").write_text(MANIFEST.format(module=module, name=module.upper()))
    (app / "streamlit_app.py").write_text("import streamlit as st\nst.title('Acme')\n")


def _init_repo(tmp_path: Path) -> str:
    """Two committed Acme apps; returns the base commit sha."""
    (tmp_path / "streamsnow.config.yaml").write_text(CONFIG)
    _write_app(tmp_path, "acme-sales-dashboard")
    _write_app(tmp_path, "marketing-campaign-dashboard")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "ci@acme.example")
    _git(tmp_path, "config", "user.name", "Acme CI")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return _git(tmp_path, "rev-parse", "HEAD")


def _rename_sales(repo: Path) -> None:
    (repo / "apps" / "acme-sales-dashboard").rename(repo / "apps" / "acme-revenue-dashboard")


def _tombstone(repo: Path, body: str) -> Path:
    path = repo / "deploy" / "tombstones.yml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(body)
    return path


def test_clean_when_nothing_removed(tmp_path, monkeypatch, capsys):
    base = _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 0
    assert "clean" in capsys.readouterr().out


def test_rename_without_tombstone_blocks(tmp_path, monkeypatch, capsys):
    base = _init_repo(tmp_path)
    _rename_sales(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 1
    out = capsys.readouterr().out
    assert SALES_FQN in out
    assert "BLOCK" in out
    # The remediation must offer the non-destructive path too.
    assert "restore apps/acme-sales-dashboard/" in out


def test_rename_with_tombstone_is_clean(tmp_path, monkeypatch):
    base = _init_repo(tmp_path)
    _rename_sales(tmp_path)
    _tombstone(
        tmp_path,
        "tombstones:\n"
        f"  - identifier: {SALES_FQN}\n"
        "    reason: renamed to ACME_REVENUE_DASHBOARD\n"
        "    date: 2026-08-31\n",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 0


def test_tombstoned_but_still_declared_is_a_finding(tmp_path, monkeypatch, capsys):
    base = _init_repo(tmp_path)
    _tombstone(
        tmp_path,
        f"tombstones:\n  - identifier: {CAMPAIGN_FQN}\n    reason: retired\n    date: 2026-08-31\n",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base, "--format", "json"]) == 1
    out = capsys.readouterr().out
    assert "still declared" in out
    assert "marketing-campaign-dashboard" in out


def test_identifier_match_is_case_insensitive(tmp_path, monkeypatch):
    # Snowflake identifiers are case-insensitive; a lowercase tombstone still
    # covers the removal (and still contradicts a live app).
    base = _init_repo(tmp_path)
    _rename_sales(tmp_path)
    _tombstone(
        tmp_path,
        "tombstones:\n"
        f"  - identifier: {SALES_FQN.lower()}\n"
        "    reason: renamed to ACME_REVENUE_DASHBOARD\n"
        "    date: 2026-08-31\n",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 0


def test_malformed_registry_exits_2(tmp_path, monkeypatch, capsys):
    base = _init_repo(tmp_path)
    cases = [
        "tombstones: {not: a-list}\n",
        "tombstones:\n  - identifier: not..a..valid..fqn\n    reason: x\n    date: 2026-08-31\n",
        "tombstones:\n  - identifier: DATA_APPS.BI_APPS.GONE\n    reason: x\n    date: yesterday\n",
        "tombstones:\n  - identifier: DATA_APPS.BI_APPS.GONE\n    reason: x\n    data: 2026-08-31\n",
        f"tombstones:\n  - identifier: {SALES_FQN}\n    date: 2026-08-31\n",  # no reason
        "not: [valid\n",  # YAML syntax error
    ]
    monkeypatch.chdir(tmp_path)
    for body in cases:
        _tombstone(tmp_path, body)
        assert main(["--base-ref", base]) == 2, body
        assert "cannot verify" in capsys.readouterr().err


def test_duplicate_identifier_rejected(tmp_path):
    entry = f"  - identifier: {SALES_FQN}\n    reason: retired\n    date: 2026-08-31\n"
    path = _tombstone(
        tmp_path, "tombstones:\n" + entry + entry.replace(SALES_FQN, SALES_FQN.lower())
    )
    _, errors = load_registry(path)
    assert any("duplicate" in e for e in errors)


def test_missing_base_ref_exits_2(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)  # no origin remote, so the default origin/main is unresolvable
    monkeypatch.chdir(tmp_path)
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "cannot verify" in err
    assert "origin/main" in err


def test_two_part_identifier_rejected(tmp_path):
    path = _tombstone(
        tmp_path,
        "tombstones:\n  - identifier: BI_APPS.GONE\n    reason: retired\n    date: 2026-08-31\n",
    )
    _, errors = load_registry(path)
    assert any("fully-qualified" in e for e in errors)


def test_drop_sql_output(tmp_path, monkeypatch, capsys):
    import shutil

    _init_repo(tmp_path)
    # The tombstoned apps must no longer be declared — the live-app guard
    # refuses to emit a DROP for an app the deploy just created.
    shutil.rmtree(tmp_path / "apps" / "acme-sales-dashboard")
    _tombstone(
        tmp_path,
        "tombstones:\n"
        f"  - identifier: {SALES_FQN}\n"
        "    reason: renamed to ACME_REVENUE_DASHBOARD\n"
        "    date: 2026-08-31\n"
        "  - identifier: DATA_APPS.BI_APPS.OLD_INVENTORY_REPORT\n"
        "    reason: retired\n"
        "    date: 2026-08-30\n",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["--drop-sql"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        f"DROP STREAMLIT IF EXISTS {SALES_FQN};",
        "DROP STREAMLIT IF EXISTS DATA_APPS.BI_APPS.OLD_INVENTORY_REPORT;",
    ]


def test_drop_sql_refuses_malformed_registry(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _tombstone(
        tmp_path, "tombstones:\n  - identifier: DROP TABLE X\n    reason: r\n    date: 2026-08-31\n"
    )
    monkeypatch.chdir(tmp_path)
    assert main(["--drop-sql"]) == 2
    captured = capsys.readouterr()
    assert "DROP STREAMLIT" not in captured.out  # never render SQL from an invalid registry


def test_drop_sql_empty_registry_prints_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["--drop-sql"]) == 0  # registry file absent: optional until first rename
    assert capsys.readouterr().out == ""
    assert drop_sql([]) == ""


def test_git_mv_that_keeps_slug_contents_changed_is_clean(tmp_path, monkeypatch):
    # Edits inside an app (or re-scaffolds that keep the slug) never trip the
    # rule — identity is the slug, not the manifest content.
    base = _init_repo(tmp_path)
    yml = tmp_path / "apps" / "acme-sales-dashboard" / "snowflake.yml"
    yml.write_text(yml.read_text() + "    # comment\n")
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 0


def test_json_format_payload(tmp_path, monkeypatch, capsys):
    import json as _json

    base = _init_repo(tmp_path)
    _rename_sales(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base, "--format", "json"]) == 1
    payload = _json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["findings"][0]["file"] == "apps/acme-sales-dashboard/snowflake.yml"
    assert SALES_FQN in payload["findings"][0]["detail"]


def test_drop_sql_refuses_live_tombstone(tmp_path, monkeypatch, capsys):
    """The reconcile step is the last hand on the DROP: a tombstone naming a
    still-declared app must refuse, even though the PR check exists — a
    direct push to main never went through it."""
    _init_repo(tmp_path)
    _tombstone(
        tmp_path,
        "tombstones:\n"
        f"  - identifier: {SALES_FQN}\n"
        "    reason: mistake — app is live\n"
        "    date: 2026-08-31\n",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["--drop-sql"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""  # zero DROP statements rendered
    assert "still a declared app" in captured.err


def test_namespace_move_requires_tombstones_for_old_fqns(tmp_path, monkeypatch, capsys):
    """A PR that changes app_database/app_schema orphans every previously
    deployed object: the base inventory must derive from the BASE commit's
    config, so the old FQNs demand tombstones."""
    _init_repo(tmp_path)
    cfg_path = tmp_path / "streamsnow.config.yaml"
    cfg_path.write_text(
        cfg_path.read_text()
        .replace('app_database: "DATA_APPS"', 'app_database: "NEW_APPS"')
        .replace('app_schema: "BI_APPS"', 'app_schema: "NEW_SCHEMA"')
    )
    monkeypatch.chdir(tmp_path)
    code = main(["--base-ref", "main"])
    out = capsys.readouterr().out
    assert code == 1
    assert SALES_FQN in out  # the OLD namespace's identifier needs a tombstone


def test_drop_sql_from_wrong_cwd_fails_closed(tmp_path, monkeypatch, capsys):
    """Run from a cwd where apps/ doesn't resolve, the live guard must refuse
    (exit 2, zero DROPs) — an empty glob must never read as 'no live apps'."""
    _init_repo(tmp_path)
    _tombstone(
        tmp_path,
        "tombstones:\n"
        f"  - identifier: {SALES_FQN}\n"
        "    reason: retired\n"
        "    date: 2026-08-31\n",
    )
    monkeypatch.chdir(tmp_path / "apps")  # apps/apps does not exist
    assert main(["--drop-sql", "--registry", str(tmp_path / "deploy" / "tombstones.yml")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not found from cwd" in captured.err
