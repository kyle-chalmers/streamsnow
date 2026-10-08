"""Tests for check_tombstones — the no-delete-path consent gate.

Each test drives the tool against a throwaway git repo: two Acme apps
committed as the base, then working-tree mutations simulate the PR under
review. The tool honors cwd for both git and relative paths, so the tests
chdir into the scratch repo.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from streamsnow.tools.check_tombstones import Tombstone, drop_sql, load_registry, main

CONFIG_V1 = """\
schema_version: 1
runtime: warehouse
project:
  name: "Acme Dashboards"
  slug: "acme-dashboards"
snowflake:
  account: "ab12345.us-east-1"
  connection_name: "acme"
  objects:
    app_database: "STREAMSNOW_APPS"
    app_schema: "DASHBOARDS"
    stage_database: "STREAMSNOW_APPS"
    stage_schema: "DASHBOARDS"
    default_warehouse: "STREAMSNOW_WH"
  roles:
    ci_role: "STREAMSNOW_DEPLOY_ROLE"
    viewer_role: "STREAMSNOW_VIEWER_ROLE"
governance:
  database: "ANALYTICS_DB"
  schema_allow: ["ANALYTICS", "REPORTING"]
"""

CONFIG = CONFIG_V1.replace("schema_version: 1", "schema_version: 2").replace(
    '  database: "ANALYTICS_DB"\n  schema_allow: ["ANALYTICS", "REPORTING"]\n',
    '  sources: ["ANALYTICS_DB.ANALYTICS", "ANALYTICS_DB.REPORTING"]\n',
)

MANIFEST = """\
definition_version: 2
entities:
  {module}:
    type: streamlit
    identifier:
      name: {name}
      database: STREAMSNOW_APPS
      schema: DASHBOARDS
    main_file: streamlit_app.py
"""

SALES_FQN = "STREAMSNOW_APPS.DASHBOARDS.ACME_SALES_DASHBOARD"
CAMPAIGN_FQN = "STREAMSNOW_APPS.DASHBOARDS.MARKETING_CAMPAIGN_DASHBOARD"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip()


def _write_app(repo: Path, slug: str) -> None:
    module = slug.replace("-", "_")
    app = repo / "apps" / slug
    app.mkdir(parents=True)
    (app / "snowflake.yml").write_text(
        MANIFEST.format(module=module, name=module.upper()), encoding="utf-8"
    )
    (app / "streamlit_app.py").write_text(
        "import streamlit as st\nst.title('Acme')\n", encoding="utf-8"
    )


def _init_repo(tmp_path: Path, config: str = CONFIG) -> str:
    """Two committed Acme apps; returns the base commit sha."""
    (tmp_path / "streamsnow.config.yaml").write_text(config, encoding="utf-8")
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
    path.write_text(body, encoding="utf-8")
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
        "tombstones:\n  - identifier: STREAMSNOW_APPS.DASHBOARDS.GONE\n    reason: x\n    date: yesterday\n",
        "tombstones:\n  - identifier: STREAMSNOW_APPS.DASHBOARDS.GONE\n    reason: x\n    data: 2026-08-31\n",
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
        "tombstones:\n  - identifier: DASHBOARDS.GONE\n    reason: retired\n    date: 2026-08-31\n",
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
        "  - identifier: STREAMSNOW_APPS.DASHBOARDS.OLD_INVENTORY_REPORT\n"
        "    reason: retired\n"
        "    date: 2026-08-30\n",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["--drop-sql"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        f"DROP STREAMLIT IF EXISTS {SALES_FQN};",
        "DROP STREAMLIT IF EXISTS STREAMSNOW_APPS.DASHBOARDS.OLD_INVENTORY_REPORT;",
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
    yml.write_text(yml.read_text(encoding="utf-8") + "    # comment\n", encoding="utf-8")
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
        cfg_path.read_text(encoding="utf-8")
        .replace('app_database: "STREAMSNOW_APPS"', 'app_database: "NEW_APPS"')
        .replace('app_schema: "DASHBOARDS"', 'app_schema: "NEW_SCHEMA"'),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    code = main(["--base-ref", "main"])
    out = capsys.readouterr().out
    assert code == 1
    assert SALES_FQN in out  # the OLD namespace's identifier needs a tombstone


def test_base_identity_survives_a_base_config_this_streamsnow_cannot_read(
    tmp_path, monkeypatch, capsys
):
    """C17: only snowflake.objects and deploy are read from the base commit. A base
    whose governance block does not load today (here: missing) must still yield the
    BASE FQNs, so moving app_database in the same PR still demands tombstones."""
    _init_repo(tmp_path, config=CONFIG.split("governance:")[0])
    (tmp_path / "streamsnow.config.yaml").write_text(
        CONFIG.replace('app_database: "STREAMSNOW_APPS"', 'app_database: "NEW_APPS"'),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    code = main(["--base-ref", "main"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert SALES_FQN in out and CAMPAIGN_FQN in out
    assert "unparseable" not in out


def test_v1_base_commit_still_yields_base_fqns(tmp_path, monkeypatch, capsys):
    """C17 after the config change: a base commit still on schema_version 1 must
    yield its own FQNs, so an app_database move in the upgrade PR needs tombstones."""
    _init_repo(tmp_path, config=CONFIG_V1)
    (tmp_path / "streamsnow.config.yaml").write_text(
        CONFIG.replace('app_database: "STREAMSNOW_APPS"', 'app_database: "NEW_APPS"'),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", "main"]) == 1
    out = capsys.readouterr().out
    assert SALES_FQN in out and "unparseable" not in out


def test_drop_sql_from_wrong_cwd_fails_closed(tmp_path, monkeypatch, capsys):
    """Run from a cwd where apps/ doesn't resolve, the live guard must refuse
    (exit 2, zero DROPs) — an empty glob must never read as 'no live apps'."""
    _init_repo(tmp_path)
    _tombstone(
        tmp_path,
        f"tombstones:\n  - identifier: {SALES_FQN}\n    reason: retired\n    date: 2026-08-31\n",
    )
    monkeypatch.chdir(tmp_path / "apps")  # apps/apps does not exist
    assert main(["--drop-sql", "--registry", str(tmp_path / "deploy" / "tombstones.yml")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not found from cwd" in captured.err


DT_FQN = "STREAMSNOW_APPS.STREAMSNOW_REPORTING.DAILY_REVENUE"
DT_DDL = (
    f"CREATE OR ALTER DYNAMIC TABLE {DT_FQN}\n"
    "  TARGET_LAG = '1 hour' WAREHOUSE = STREAMSNOW_WH INITIALIZE = ON_CREATE\n"
    "AS SELECT order_date, SUM(revenue) AS revenue FROM ANALYTICS_DB.REPORTING.ORDERS\n"
    "GROUP BY order_date;\n"
)


def _declare_object(repo: Path, ddl: str = DT_DDL, slug: str = "acme-sales-dashboard") -> None:
    review = repo / "apps" / slug / "sql_review"
    (review / "app_specific_reporting_objects").mkdir(parents=True, exist_ok=True)
    (review / "app_specific_reporting_objects" / f"{DT_FQN}.sql").write_text(ddl, encoding="utf-8")
    index = {
        "schema_version": 2,
        "app": slug,
        "pages": [],
        "objects": [{"name": DT_FQN, "grants": [], "reason": "performance"}],
    }
    (review / "index.yaml").write_text(yaml.safe_dump(index), encoding="utf-8")


def _retire_object(repo: Path, slug: str = "acme-sales-dashboard") -> None:
    shutil.rmtree(repo / "apps" / slug / "sql_review")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _object_stone(kind: str | None) -> str:
    line = f"    kind: {kind}\n" if kind else ""
    return (
        "tombstones:\n"
        f"  - identifier: {DT_FQN}\n"
        f"{line}"
        "    reason: replaced by a view\n"
        "    date: 2026-10-06\n"
    )


def _base_with_object(tmp_path: Path) -> str:
    _init_repo(tmp_path)
    _declare_object(tmp_path)
    return _commit(tmp_path, "declare an app-data object")


def test_removed_app_data_object_needs_a_tombstone_with_its_kind(tmp_path, monkeypatch, capsys):
    base = _base_with_object(tmp_path)
    _retire_object(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 1
    out = capsys.readouterr().out
    assert DT_FQN in out and "kind: dynamic_table" in out


def test_object_tombstone_with_the_right_kind_is_clean(tmp_path, monkeypatch):
    base = _base_with_object(tmp_path)
    _retire_object(tmp_path)
    _tombstone(tmp_path, _object_stone("dynamic_table"))
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 0


@pytest.mark.parametrize(
    ("kind", "needle"),
    [("view", "would run the wrong DROP"), (None, "needs kind: view or kind: dynamic_table")],
)
def test_object_tombstone_with_the_wrong_kind_is_a_finding(
    tmp_path, monkeypatch, capsys, kind, needle
):
    base = _base_with_object(tmp_path)
    _retire_object(tmp_path)
    _tombstone(tmp_path, _object_stone(kind))
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 1
    assert needle in capsys.readouterr().out


def test_a_tombstone_naming_a_declared_object_is_a_finding(tmp_path, monkeypatch, capsys):
    base = _base_with_object(tmp_path)
    _tombstone(tmp_path, _object_stone("dynamic_table"))
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 1
    assert "still declared as an app-data object" in capsys.readouterr().out


def test_changing_an_objects_kind_in_place_is_a_finding(tmp_path, monkeypatch, capsys):
    base = _base_with_object(tmp_path)
    _declare_object(
        tmp_path,
        f"CREATE OR ALTER VIEW {DT_FQN} AS\nSELECT order_date, SUM(revenue) AS revenue\n"
        "FROM ANALYTICS_DB.REPORTING.ORDERS\nGROUP BY order_date;\n",
    )
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 1
    assert "cannot change an object's kind" in capsys.readouterr().out


def test_a_v1_base_declares_no_app_data_objects(tmp_path, monkeypatch):
    _init_repo(tmp_path, config=CONFIG_V1)
    _declare_object(tmp_path)
    base = _commit(tmp_path, "review-only object under a v1 config")
    (tmp_path / "streamsnow.config.yaml").write_text(CONFIG, encoding="utf-8")
    _retire_object(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 0


def test_drop_sql_uses_the_recorded_kind():
    stones = [
        Tombstone(identifier="STREAMSNOW_APPS.DASHBOARDS.OLD_APP", reason="r", date="2026-10-06"),
        Tombstone(
            identifier="STREAMSNOW_APPS.STREAMSNOW_REPORTING.V",
            reason="r",
            date="2026-10-06",
            kind="view",
        ),
        Tombstone(identifier=DT_FQN, reason="r", date="2026-10-06", kind="dynamic_table"),
    ]
    assert drop_sql(stones).splitlines() == [
        "DROP STREAMLIT IF EXISTS STREAMSNOW_APPS.DASHBOARDS.OLD_APP;",
        "DROP VIEW IF EXISTS STREAMSNOW_APPS.STREAMSNOW_REPORTING.V;",
        f"DROP DYNAMIC TABLE IF EXISTS {DT_FQN};",
    ]


def test_an_unknown_kind_is_a_registry_error(tmp_path):
    path = _tombstone(tmp_path, _object_stone("table"))
    _stones, errors = load_registry(path)
    assert any("kind must be view or dynamic_table" in e for e in errors)


def test_a_removed_object_of_unknown_kind_still_needs_a_kind(tmp_path, monkeypatch, capsys):
    """The base DDL file shows no deployable kind (here a table, which the loader would
    have refused): the tombstone still needs kind: view or dynamic_table, because a
    tombstone without one renders DROP STREAMLIT."""
    _init_repo(tmp_path)
    _declare_object(tmp_path, f"CREATE TABLE {DT_FQN} (x INT);\n")
    base = _commit(tmp_path, "an object whose kind cannot be told")
    _retire_object(tmp_path)
    _tombstone(tmp_path, _object_stone(None))
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 1
    assert "needs kind: view or kind: dynamic_table" in capsys.readouterr().out
    _tombstone(tmp_path, _object_stone("dynamic_table"))
    assert main(["--base-ref", base]) == 0


def test_an_unreadable_base_index_fails_closed(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _declare_object(tmp_path)
    index = tmp_path / "apps" / "acme-sales-dashboard" / "sql_review" / "index.yaml"
    index.write_text("objects: [\n", encoding="utf-8")
    base = _commit(tmp_path, "a broken index at base")
    _retire_object(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 1
    out = capsys.readouterr().out
    assert "cannot tell which app-data objects" in out and "(note)" not in out


def _config_with(**changes) -> dict:
    data = yaml.safe_load(CONFIG)
    data.update(changes)
    return data


def _malformed_base(tmp_path: Path, *, config: dict | None = None, objects=None) -> str:
    """A base commit with one malformed shape, then a working tree that removed the
    object under a valid config."""
    _init_repo(tmp_path)
    _declare_object(tmp_path)
    if config is not None:
        (tmp_path / "streamsnow.config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    if objects is not None:
        index = tmp_path / "apps" / "acme-sales-dashboard" / "sql_review" / "index.yaml"
        data = yaml.safe_load(index.read_text(encoding="utf-8"))
        data["objects"] = objects
        index.write_text(yaml.safe_dump(data), encoding="utf-8")
    base = _commit(tmp_path, "a malformed base")
    (tmp_path / "streamsnow.config.yaml").write_text(CONFIG, encoding="utf-8")
    _retire_object(tmp_path)
    return base


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(
            {"config": _config_with(governance="not a mapping")}, id="governance-not-a-mapping"
        ),
        pytest.param(
            {"config": _config_with(snowflake=["STREAMSNOW_APPS"])}, id="snowflake-is-a-list"
        ),
        pytest.param({"config": _config_with(schema_version="two")}, id="unknown-schema-version"),
        pytest.param({"objects": [{"name": "NOT_THREE_PARTS"}]}, id="name-not-three-parts"),
        pytest.param({"objects": "not a list"}, id="objects-not-a-list"),
        pytest.param({"objects": [DT_FQN]}, id="entry-not-a-mapping"),
    ],
)
def test_a_malformed_base_inventory_fails_closed(tmp_path, monkeypatch, capsys, shape):
    """Each shape used to read as "no app-data objects at base" (or crash): a removed
    object would then need no tombstone. Unverifiable is a finding, never empty."""
    base = _malformed_base(tmp_path, **shape)
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 1
    out = capsys.readouterr().out
    assert "cannot tell which app-data objects" in out


def test_drop_sql_refuses_when_a_live_entry_did_not_load(tmp_path, monkeypatch, capsys):
    """--drop-sql reads only the live inventory: an entry the index loader rejects makes
    it incomplete, and the guard refuses rather than trust it."""
    _init_repo(tmp_path)
    _declare_object(tmp_path)
    index = tmp_path / "apps" / "acme-sales-dashboard" / "sql_review" / "index.yaml"
    data = yaml.safe_load(index.read_text(encoding="utf-8"))
    data["objects"] = [{"name": "NOT_THREE_PARTS"}]
    index.write_text(yaml.safe_dump(data), encoding="utf-8")
    _tombstone(tmp_path, _object_stone("dynamic_table"))
    monkeypatch.chdir(tmp_path)
    assert main(["--drop-sql"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "incomplete" in captured.err


def test_an_incomplete_live_inventory_fails_the_check_and_blocks_drops(
    tmp_path, monkeypatch, capsys
):
    base = _base_with_object(tmp_path)
    index = tmp_path / "apps" / "acme-sales-dashboard" / "sql_review" / "index.yaml"
    index.write_text("objects: [\n", encoding="utf-8")
    _tombstone(tmp_path, _object_stone("dynamic_table"))
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 1
    assert "cannot tell which app-data objects" in capsys.readouterr().out
    assert main(["--drop-sql"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "incomplete" in captured.err


def test_drop_sql_never_drops_an_app_data_name_as_a_streamlit(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _tombstone(tmp_path, _object_stone(None))
    monkeypatch.chdir(tmp_path)
    assert main(["--drop-sql"]) == 2
    captured = capsys.readouterr()
    assert "DROP STREAMLIT" not in captured.out
    assert "needs kind: view or kind: dynamic_table" in captured.err


def test_drop_sql_refuses_a_tombstone_for_a_declared_object(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _declare_object(tmp_path)
    _tombstone(tmp_path, _object_stone("dynamic_table"))
    monkeypatch.chdir(tmp_path)
    assert main(["--drop-sql"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refusing --drop-sql" in captured.err and DT_FQN in captured.err


def test_retired_object_is_dropped_by_kind_on_a_later_unrelated_deploy(
    tmp_path, monkeypatch, capsys
):
    """Retire the object with a tombstone (commit B), then merge an unrelated change
    (commit C): C's PR check is clean, and C's deploy still emits the right DROP from
    the tombstone alone, with the DDL file long gone."""
    _base_with_object(tmp_path)
    _retire_object(tmp_path)
    _tombstone(tmp_path, _object_stone("dynamic_table"))
    retired = _commit(tmp_path, "retire DAILY_REVENUE")
    app_py = tmp_path / "apps" / "marketing-campaign-dashboard" / "streamlit_app.py"
    app_py.write_text("import streamlit as st\nst.title('Acme campaigns')\n", encoding="utf-8")
    _commit(tmp_path, "unrelated change")
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", retired]) == 0
    capsys.readouterr()
    assert main(["--drop-sql"]) == 0
    assert capsys.readouterr().out.strip() == f"DROP DYNAMIC TABLE IF EXISTS {DT_FQN};"
    assert not list(tmp_path.rglob(f"{DT_FQN}.sql"))


def _kind_stone(identifier: str, kind: str = "view") -> str:
    return (
        "tombstones:\n"
        f"  - identifier: {identifier}\n"
        f"    kind: {kind}\n"
        "    reason: probe\n"
        "    date: 2026-10-06\n"
    )


def test_a_kinded_tombstone_outside_app_data_is_a_finding(tmp_path, monkeypatch, capsys):
    base = _init_repo(tmp_path)
    _tombstone(tmp_path, _kind_stone("ANALYTICS_DB.REPORTING.ORDERS"))
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 1
    out = capsys.readouterr().out
    assert "retires objects in governance.app_data only" in out


def test_drop_sql_refuses_a_kinded_tombstone_outside_app_data(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _tombstone(tmp_path, _kind_stone("ANALYTICS_DB.REPORTING.ORDERS"))
    monkeypatch.chdir(tmp_path)
    assert main(["--drop-sql"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "retires objects in governance.app_data only" in captured.err


def test_a_lower_case_app_data_name_is_still_accepted(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _tombstone(tmp_path, _kind_stone("streamsnow_apps.streamsnow_reporting.old_view"))
    monkeypatch.chdir(tmp_path)
    assert main(["--drop-sql"]) == 0
    assert "DROP VIEW IF EXISTS" in capsys.readouterr().out


@pytest.mark.parametrize("version", [None, "2"], ids=["key-omitted", "string-two"])
def test_a_base_config_schema_version_is_read_like_the_loader(tmp_path, monkeypatch, version):
    data = yaml.safe_load(CONFIG)
    if version is None:
        del data["schema_version"]
    else:
        data["schema_version"] = version
    _init_repo(tmp_path, config=yaml.safe_dump(data))
    base = _git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 0


def test_a_tombstone_identifier_with_a_trailing_newline_renders_one_clean_line(tmp_path):
    """The identifier is rendered into --drop-sql: a trailing newline must not survive."""
    path = tmp_path / "t.yml"
    path.write_text(
        "tombstones:\n"
        '  - identifier: "STREAMSNOW_APPS.DASHBOARDS.X\\n"\n'
        "    reason: r\n    date: 2026-08-31\n",
        encoding="utf-8",
    )
    stones, errors = load_registry(path)
    assert errors == []
    assert [s.identifier for s in stones] == ["STREAMSNOW_APPS.DASHBOARDS.X"]
    assert drop_sql(stones) == "DROP STREAMLIT IF EXISTS STREAMSNOW_APPS.DASHBOARDS.X;"


def test_a_boolean_base_schema_version_is_unverifiable_not_version_one(
    tmp_path, monkeypatch, capsys
):
    """int(True) is 1, which would read the base as a pre-app-data config and report that
    nothing was removed. A boolean is not a version: the inventory is unverifiable."""
    data = yaml.safe_load(CONFIG)
    data["schema_version"] = True
    base = _init_repo(tmp_path, config=yaml.safe_dump(data))
    (tmp_path / "streamsnow.config.yaml").write_text(CONFIG, encoding="utf-8")  # live is valid
    monkeypatch.chdir(tmp_path)
    assert main(["--base-ref", base]) == 1
    out = capsys.readouterr().out
    assert "cannot tell which app-data objects" in out and "True" in out
