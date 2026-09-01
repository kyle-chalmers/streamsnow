"""Tests for the branding.py _BRANDING_VERSION parity check."""

from __future__ import annotations

from pathlib import Path

from streamsnow.tools import check_branding_parity

_BRANDING = '''\
"""Branding for Acme dashboards."""

_BRANDING_VERSION = "{version}"

BRAND_COLORS = {{"primary": "#336699"}}
'''


def _app(tmp_path: Path, slug: str, version: str | None) -> Path:
    app = tmp_path / "apps" / slug
    app.mkdir(parents=True)
    (app / "snowflake.yml").write_text("definition_version: 2\nentities: {}\n")
    if version is None:
        (app / "branding.py").write_text('"""Branding, pre-stamp era."""\nCOLOR = "#123456"\n')
    else:
        (app / "branding.py").write_text(_BRANDING.format(version=version))
    return app


def test_matching_versions_clean(tmp_path):
    _app(tmp_path, "acme-sales-dashboard", "1.2.0")
    _app(tmp_path, "marketing-campaign-dashboard", "1.2.0")
    res = check_branding_parity.scan_paths([tmp_path / "apps"])
    assert res["ok"]
    assert res["findings"] == []


def test_lagging_app_flagged(tmp_path):
    _app(tmp_path, "acme-sales-dashboard", "1.2.0")
    behind = _app(tmp_path, "marketing-campaign-dashboard", "1.1.0")
    res = check_branding_parity.scan_paths([tmp_path / "apps"])
    assert not res["ok"]
    assert len(res["findings"]) == 1
    f = res["findings"][0]
    assert str(behind / "branding.py") == f["file"]
    assert "'1.1.0'" in f["detail"] and "'1.2.0'" in f["detail"]


def test_semantic_version_ordering(tmp_path):
    # 1.10.0 > 1.9.0 semantically even though it sorts lower as a string.
    _app(tmp_path, "acme-sales-dashboard", "1.10.0")
    _app(tmp_path, "marketing-campaign-dashboard", "1.9.0")
    res = check_branding_parity.scan_paths([tmp_path / "apps"])
    assert not res["ok"]
    assert "marketing-campaign-dashboard" in res["findings"][0]["file"]


def test_missing_stamp_is_note_not_failure(tmp_path):
    _app(tmp_path, "acme-sales-dashboard", "1.2.0")
    _app(tmp_path, "marketing-campaign-dashboard", None)
    res = check_branding_parity.scan_paths([tmp_path / "apps"])
    assert res["ok"]
    assert any("no _BRANDING_VERSION stamp" in n for n in res["notes"])


def test_single_app_clean(tmp_path):
    _app(tmp_path, "acme-sales-dashboard", "1.0.0")
    assert check_branding_parity.scan_paths([tmp_path / "apps"])["ok"]


def test_app_without_branding_file_skipped(tmp_path):
    app = tmp_path / "apps" / "acme-sales-dashboard"
    app.mkdir(parents=True)
    (app / "snowflake.yml").write_text("definition_version: 2\nentities: {}\n")
    res = check_branding_parity.scan_paths([tmp_path / "apps"])
    assert res["ok"]
    assert res["findings"] == []


def test_unparseable_version_never_becomes_reference(tmp_path):
    # A typo'd stamp must not outrank real versions; it is itself the skew.
    _app(tmp_path, "acme-sales-dashboard", "1.2.0")
    _app(tmp_path, "marketing-campaign-dashboard", "one-point-three")
    res = check_branding_parity.scan_paths([tmp_path / "apps"])
    assert not res["ok"]
    assert "marketing-campaign-dashboard" in res["findings"][0]["file"]


def test_template_lag_is_note_not_failure(tmp_path):
    # The packaged template is 1.0.0; an older app copy lags it but matches its
    # sibling — cross-app parity holds, so only a note may appear, never a finding.
    _app(tmp_path, "acme-sales-dashboard", "0.9.0")
    _app(tmp_path, "marketing-campaign-dashboard", "0.9.0")
    res = check_branding_parity.scan_paths([tmp_path / "apps"])
    assert res["ok"]
    assert any("template" in n for n in res.get("notes", []))


def test_main_exit_codes_and_json(tmp_path, capsys):
    _app(tmp_path, "acme-sales-dashboard", "1.2.0")
    _app(tmp_path, "marketing-campaign-dashboard", "1.0.0")
    assert check_branding_parity.main([str(tmp_path / "apps"), "--format", "json"]) == 1
    assert '"ok": false' in capsys.readouterr().out
    # Bring the straggler up to date -> clean.
    (tmp_path / "apps/marketing-campaign-dashboard/branding.py").write_text(
        _BRANDING.format(version="1.2.0")
    )
    assert check_branding_parity.main([str(tmp_path / "apps")]) == 0
