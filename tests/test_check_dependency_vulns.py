"""Tests for the OSV dependency-vulnerability check.

No network: every test monkeypatches ``query_osv`` (the only function in the
module that touches the network). Fixtures are minimal Acme-style app dirs —
``snowflake.yml`` marks the app root, manifests carry the pins under test.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from streamsnow.tools import check_dependency_vulns as cdv

TODAY = dt.date(2026, 8, 31)
FUTURE = "2027-01-01"
PAST = "2025-01-01"


def _app(tmp_path: Path, slug: str, pyproject: str = "", env_yml: str = "") -> Path:
    app = tmp_path / "apps" / slug
    app.mkdir(parents=True)
    (app / "snowflake.yml").write_text("definition_version: '2'\n")
    if pyproject:
        (app / "pyproject.toml").write_text(pyproject)
    if env_yml:
        (app / "environment.yml").write_text(env_yml)
    return app


def _allowlist(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "osv_allowlist.json"
    p.write_text(json.dumps(entries))
    return p


def _fake_osv(mapping: dict[tuple[str, str], list[str]]):
    """A query_osv stand-in returning ``mapping``'s IDs per pin ([] default)."""

    def fake(pins: list[tuple[str, str]]) -> list[list[str]]:
        return [mapping.get(pin, []) for pin in pins]

    return fake


# --------------------------------------------------------------------------- #
# Spec classification
# --------------------------------------------------------------------------- #
def test_normalize_pep503_and_extras():
    assert cdv.normalize("Some_Wid.Get-Kit") == "some-wid-get-kit"
    assert cdv.normalize("uvicorn[standard]") == "uvicorn"


def test_classify_pep440_pin_range_and_marker():
    pins, unscanned = {}, []
    cdv.classify_pep440("streamlit==1.59.2", "m", pins, unscanned)
    cdv.classify_pep440("pandas>=2,<3", "m", pins, unscanned)
    cdv.classify_pep440("snowflake-snowpark-python", "m", pins, unscanned)
    cdv.classify_pep440("tomli==2.0.1; python_version < '3.11'", "m", pins, unscanned)
    assert set(pins) == {("streamlit", "1.59.2"), ("tomli", "2.0.1")}
    assert [u["spec"] for u in unscanned] == ["pandas>=2,<3", "snowflake-snowpark-python"]


def test_classify_conda_pin_wildcard_and_python_skip():
    pins, unscanned = {}, []
    cdv.classify_conda("plotly=5.22.0", "e", pins, unscanned)
    cdv.classify_conda("pandas=2.*", "e", pins, unscanned)
    cdv.classify_conda("python=3.11", "e", pins, unscanned)
    assert set(pins) == {("plotly", "5.22.0")}
    assert [u["spec"] for u in unscanned] == ["pandas=2.*"]


def test_collect_pins_covers_pyproject_and_environment_yml(tmp_path):
    _app(
        tmp_path,
        "acme-sales-dashboard",
        pyproject='[project]\nname = "x"\ndependencies = ["streamlit==1.59.2", "pandas>=2,<3"]\n',
    )
    _app(
        tmp_path,
        "marketing-campaign-dashboard",
        env_yml="dependencies:\n  - plotly=5.22.0\n  - python=3.11\n  - pip:\n      - requests==2.31.0\n",
    )
    pins, unscanned = cdv.collect_pins(sorted((tmp_path / "apps").iterdir()))
    assert set(pins) == {
        ("streamlit", "1.59.2"),
        ("plotly", "5.22.0"),
        ("requests", "2.31.0"),
    }
    assert [u["spec"] for u in unscanned] == ["pandas>=2,<3"]


# --------------------------------------------------------------------------- #
# Allowlist semantics
# --------------------------------------------------------------------------- #
def test_load_allowlist_missing_file_is_empty(tmp_path):
    active, expired = cdv.load_allowlist(tmp_path / "nope.json", today=TODAY)
    assert active == {} and expired == []


def test_load_allowlist_splits_active_and_expired(tmp_path):
    path = _allowlist(
        tmp_path,
        [
            {"id": "GHSA-live", "package": "Some_Pkg", "reason": "ok", "expires": FUTURE},
            {"id": "GHSA-old", "package": "somepkg", "reason": "stale", "expires": PAST},
            {"id": "GHSA-nodate", "package": "somepkg", "reason": "no expiry"},
        ],
    )
    active, expired = cdv.load_allowlist(path, today=TODAY)
    assert set(active) == {("some-pkg", "GHSA-live")}  # package name normalized
    assert {e["id"] for e in expired} == {"GHSA-old", "GHSA-nodate"}


def test_load_allowlist_rejects_malformed(tmp_path):
    with pytest.raises(ValueError):
        cdv.load_allowlist(_allowlist(tmp_path, [{"reason": "no id/package"}]), today=TODAY)


# --------------------------------------------------------------------------- #
# End-to-end scan_paths / main (query_osv monkeypatched)
# --------------------------------------------------------------------------- #
def test_clean_scan_exit_0(tmp_path, monkeypatch, capsys):
    _app(
        tmp_path,
        "acme-sales-dashboard",
        pyproject='[project]\nname = "x"\ndependencies = ["streamlit==1.59.2"]\n',
    )
    monkeypatch.setattr(cdv, "query_osv", _fake_osv({}))
    monkeypatch.chdir(tmp_path)
    assert cdv.main(["apps"]) == 0
    assert "clean (1 exact pins checked)" in capsys.readouterr().out


def test_vulnerable_pin_fails(tmp_path, monkeypatch, capsys):
    _app(
        tmp_path,
        "acme-sales-dashboard",
        pyproject='[project]\nname = "x"\ndependencies = ["requests==2.19.0"]\n',
    )
    monkeypatch.setattr(
        cdv, "query_osv", _fake_osv({("requests", "2.19.0"): ["GHSA-aaaa", "GHSA-bbbb"]})
    )
    monkeypatch.chdir(tmp_path)
    assert cdv.main(["apps"]) == 1
    out = capsys.readouterr().out
    assert "BLOCK" in out and "GHSA-aaaa, GHSA-bbbb" in out


def test_allowlisted_vuln_passes_but_new_id_still_fails(tmp_path, monkeypatch):
    _app(
        tmp_path,
        "acme-sales-dashboard",
        pyproject='[project]\nname = "x"\ndependencies = ["requests==2.19.0"]\n',
    )
    allow = _allowlist(
        tmp_path, [{"id": "GHSA-known", "package": "requests", "reason": "r", "expires": FUTURE}]
    )
    monkeypatch.setattr(cdv, "query_osv", _fake_osv({("requests", "2.19.0"): ["GHSA-known"]}))
    res = cdv.scan_paths([tmp_path / "apps"], allowlist_path=allow)
    assert res["ok"]
    assert [a["id"] for a in res["allowlisted"]] == ["GHSA-known"]

    # A NEW ID on the same allowlisted package still fails.
    monkeypatch.setattr(
        cdv, "query_osv", _fake_osv({("requests", "2.19.0"): ["GHSA-known", "GHSA-new"]})
    )
    res = cdv.scan_paths([tmp_path / "apps"], allowlist_path=allow)
    assert not res["ok"]
    assert "GHSA-new" in res["findings"][0]["detail"]
    assert "GHSA-known" not in res["findings"][0]["detail"]


def test_expired_allowlist_entry_fails_again_and_is_reported(tmp_path, monkeypatch, capsys):
    _app(
        tmp_path,
        "acme-sales-dashboard",
        pyproject='[project]\nname = "x"\ndependencies = ["requests==2.19.0"]\n',
    )
    allow = _allowlist(
        tmp_path, [{"id": "GHSA-known", "package": "requests", "reason": "r", "expires": PAST}]
    )
    monkeypatch.setattr(cdv, "query_osv", _fake_osv({("requests", "2.19.0"): ["GHSA-known"]}))
    monkeypatch.chdir(tmp_path)
    assert cdv.main(["apps", "--allowlist", str(allow)]) == 1
    out = capsys.readouterr().out
    assert "expired allowlist entry: GHSA-known" in out
    assert "BLOCK" in out


def test_range_pins_reported_unscanned_never_failed(tmp_path, monkeypatch, capsys):
    _app(
        tmp_path,
        "acme-sales-dashboard",
        pyproject='[project]\nname = "x"\ndependencies = ["pandas>=2,<3"]\n',
    )
    called = []
    monkeypatch.setattr(cdv, "query_osv", lambda pins: called.append(pins) or [[] for _ in pins])
    monkeypatch.chdir(tmp_path)
    assert cdv.main(["apps"]) == 0
    assert "unscanned (range pin)" in capsys.readouterr().out
    assert called == []  # nothing pinned -> OSV never queried


def _raise_unreachable(_pins):
    raise cdv.OSVUnreachableError("simulated outage")


def test_network_failure_is_tool_error_exit_2(tmp_path, monkeypatch, capsys):
    _app(
        tmp_path,
        "acme-sales-dashboard",
        pyproject='[project]\nname = "x"\ndependencies = ["streamlit==1.59.2"]\n',
    )
    monkeypatch.setattr(cdv, "query_osv", _raise_unreachable)
    monkeypatch.chdir(tmp_path)
    assert cdv.main(["apps"]) == 2
    assert "unreachable" in capsys.readouterr().err


def test_network_failure_with_best_effort_warns_and_passes(tmp_path, monkeypatch, capsys):
    _app(
        tmp_path,
        "acme-sales-dashboard",
        pyproject='[project]\nname = "x"\ndependencies = ["streamlit==1.59.2"]\n',
    )
    monkeypatch.setattr(cdv, "query_osv", _raise_unreachable)
    monkeypatch.chdir(tmp_path)
    assert cdv.main(["apps", "--best-effort"]) == 0
    assert "SKIPPED (best-effort)" in capsys.readouterr().err


def test_default_allowlist_resolves_beside_config(tmp_path, monkeypatch):
    # Repo root = the directory containing streamsnow.config.yaml, found by
    # walking up from the cwd; the allowlist sits beside it.
    (tmp_path / "streamsnow.config.yaml").write_text("schema_version: 1\n")
    _allowlist(tmp_path, [])
    nested = tmp_path / "apps"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert cdv.resolve_allowlist_path(None) == tmp_path / "osv_allowlist.json"


def test_json_format_carries_full_result(tmp_path, monkeypatch, capsys):
    _app(
        tmp_path,
        "acme-sales-dashboard",
        pyproject='[project]\nname = "x"\ndependencies = ["requests==2.19.0", "plotly>=5,<6"]\n',
    )
    monkeypatch.setattr(cdv, "query_osv", _fake_osv({("requests", "2.19.0"): ["GHSA-aaaa"]}))
    monkeypatch.chdir(tmp_path)
    assert cdv.main(["apps", "--format", "json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert not payload["ok"]
    assert payload["checked"] == 1
    assert payload["unscanned"][0]["spec"] == "plotly>=5,<6"
