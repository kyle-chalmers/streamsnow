"""Tests for check_artifacts --fix (drift repair with round-trip fidelity)."""

from __future__ import annotations

from pathlib import Path

import yaml

from streamsnow.config import Config
from streamsnow.scaffolder import scaffold
from streamsnow.tools import check_artifacts

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"


def _cfg() -> Config:
    return Config.from_dict(yaml.safe_load(EXAMPLE.read_text()))


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _scaffold_app(tmp_path: Path) -> Path:
    scaffold(_cfg(), tmp_path, "acme-sales-dashboard")
    return tmp_path / "apps/acme-sales-dashboard"


def _artifacts(app: Path) -> list[str]:
    data = yaml.safe_load((app / "snowflake.yml").read_text())
    (entity,) = data["entities"].values()
    return entity["artifacts"]


def test_fix_appends_uncovered_file_and_keeps_dir_entries(tmp_path):
    app = _scaffold_app(tmp_path)
    _write(app / "helpers.py", "X = 1\n")
    res = check_artifacts.fix_app(app)
    assert res["ok"] and res["changed"]
    arts = _artifacts(app)
    assert "helpers.py" in arts
    assert "pages/" in arts  # directory entries survive the fix
    assert check_artifacts.check_app(app)["ok"]


def test_fix_drops_stale_entry(tmp_path):
    app = _scaffold_app(tmp_path)
    (app / "branding.py").unlink()
    res = check_artifacts.fix_app(app)
    assert res["ok"] and res["changed"]
    assert "branding.py" not in _artifacts(app)
    assert check_artifacts.check_app(app)["ok"]


def test_fix_is_idempotent(tmp_path):
    app = _scaffold_app(tmp_path)
    _write(app / "helpers.py", "X = 1\n")
    assert check_artifacts.fix_app(app)["changed"]
    before = (app / "snowflake.yml").read_text()
    res = check_artifacts.fix_app(app)
    assert res["ok"] and not res["changed"]
    assert (app / "snowflake.yml").read_text() == before


def test_fix_preserves_lines_outside_the_artifacts_block(tmp_path):
    app = _scaffold_app(tmp_path)
    yml = app / "snowflake.yml"
    original = yml.read_text()
    yml.write_text("# deploy notes: reviewed by the platform team\n" + original)
    _write(app / "helpers.py", "X = 1\n")
    assert check_artifacts.fix_app(app)["changed"]
    fixed = yml.read_text()
    assert fixed.startswith("# deploy notes: reviewed by the platform team\n")
    # Every pre-existing non-artifacts line survives byte-for-byte.
    art_start = original.index("    artifacts:")
    assert ("# deploy notes: reviewed by the platform team\n" + original[:art_start]) in fixed


def test_fix_ships_image_and_data_assets(tmp_path):
    app = _scaffold_app(tmp_path)
    _write(app / "assets/logo.png", "png-bytes")
    _write(app / "data/regions.csv", "region\nnorth\n")
    assert check_artifacts.fix_app(app)["changed"]
    arts = _artifacts(app)
    assert "assets/logo.png" in arts
    assert "data/regions.csv" in arts


def test_fix_converts_flow_style_list(tmp_path):
    app = tmp_path / "apps/acme-sales-dashboard"
    _write(app / "streamlit_app.py", "import streamlit as st\n")
    _write(app / "helpers.py", "X = 1\n")
    _write(
        app / "snowflake.yml",
        "definition_version: 2\n"
        "entities:\n"
        "  acme_app:\n"
        "    type: streamlit\n"
        "    main_file: streamlit_app.py\n"
        "    artifacts: [streamlit_app.py]\n",
    )
    res = check_artifacts.fix_app(app)
    assert res["ok"] and res["changed"]
    assert set(_artifacts(app)) == {"streamlit_app.py", "helpers.py"}
    assert check_artifacts.check_app(app)["ok"]


def test_fix_refuses_mapping_entries(tmp_path):
    app = tmp_path / "apps/acme-sales-dashboard"
    _write(app / "streamlit_app.py", "import streamlit as st\n")
    _write(
        app / "snowflake.yml",
        "definition_version: 2\n"
        "entities:\n"
        "  acme_app:\n"
        "    type: streamlit\n"
        "    main_file: streamlit_app.py\n"
        "    artifacts:\n"
        "      - src: streamlit_app.py\n"
        "        dest: app/\n",
    )
    before = (app / "snowflake.yml").read_text()
    res = check_artifacts.fix_app(app)
    assert not res["ok"] and not res["changed"]
    assert "manually" in res["detail"]
    assert (app / "snowflake.yml").read_text() == before


def test_fix_refuses_multiple_declaring_entities(tmp_path):
    app = tmp_path / "apps/acme-sales-dashboard"
    _write(app / "streamlit_app.py", "import streamlit as st\n")
    _write(
        app / "snowflake.yml",
        "definition_version: 2\n"
        "entities:\n"
        "  one:\n"
        "    type: streamlit\n"
        "    artifacts:\n"
        "      - streamlit_app.py\n"
        "  two:\n"
        "    type: streamlit\n"
        "    artifacts:\n"
        "      - streamlit_app.py\n",
    )
    res = check_artifacts.fix_app(app)
    assert not res["ok"]
    assert "entities" in res["detail"]


def test_fix_noop_without_artifacts_list(tmp_path):
    app = tmp_path / "apps/acme-sales-dashboard"
    _write(app / "streamlit_app.py", "import streamlit as st\n")
    _write(
        app / "snowflake.yml",
        "definition_version: 2\nentities:\n  acme_app:\n    type: streamlit\n",
    )
    res = check_artifacts.fix_app(app)
    assert res["ok"] and not res["changed"]


def test_scan_paths_fix_reports_unfixable_as_finding(tmp_path):
    app = tmp_path / "apps/acme-sales-dashboard"
    _write(app / "streamlit_app.py", "import streamlit as st\n")
    _write(
        app / "snowflake.yml",
        "definition_version: 2\n"
        "entities:\n"
        "  acme_app:\n"
        "    type: streamlit\n"
        "    artifacts:\n"
        "      - src: streamlit_app.py\n",
    )
    res = check_artifacts.scan_paths([tmp_path / "apps"], fix=True)
    assert not res["ok"]
    assert any("manually" in f["detail"] for f in res["findings"])


def test_main_fix_repairs_and_exits_clean(tmp_path, capsys):
    app = _scaffold_app(tmp_path)
    _write(app / "helpers.py", "X = 1\n")
    assert check_artifacts.main([str(app)]) == 1  # drift detected without --fix
    capsys.readouterr()
    assert check_artifacts.main([str(app), "--fix"]) == 0
    out = capsys.readouterr().out
    assert "FIXED" in out and "clean" in out
    assert check_artifacts.main([str(app), "--fix", "--format", "json"]) == 0
    assert '"ok": true' in capsys.readouterr().out
