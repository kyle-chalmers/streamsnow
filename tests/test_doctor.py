"""Tests for the per-check environment doctor."""

from __future__ import annotations

import json
from pathlib import Path

from streamsnow.tools import doctor

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"


def _which_only(*names: str):
    """A shutil.which stand-in that resolves only the given tool names."""

    def fake(tool: str) -> str | None:
        return f"/opt/acme/bin/{tool}" if tool in names else None

    return fake


def test_result_contract_shape(tmp_path):
    results = doctor.run_checks(start=tmp_path)
    assert results  # python + 4 tools + config
    for r in results:
        assert set(r) == {"name", "ok", "level", "detail", "hint"}
        assert r["level"] in ("required", "optional")
    assert [r["name"] for r in results] == ["python", "git", "uv", "snow", "streamlit", "config"]


def test_python_check_passes_on_current_interpreter_and_fails_on_high_min():
    assert doctor.check_python()["ok"]  # the suite requires >= 3.11 itself
    res = doctor.check_python(minimum=(99, 0))
    assert not res["ok"]
    assert "99.0" in res["hint"]


def test_missing_required_tool_fails_run(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git"))  # no uv
    results = doctor.run_checks(start=tmp_path)
    by_name = {r["name"]: r for r in results}
    assert by_name["git"]["ok"]
    assert not by_name["uv"]["ok"] and by_name["uv"]["level"] == "required"
    assert not doctor.required_ok(results)


def test_missing_optional_tools_do_not_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv"))
    results = doctor.run_checks(start=tmp_path)
    by_name = {r["name"]: r for r in results}
    assert not by_name["snow"]["ok"] and by_name["snow"]["level"] == "optional"
    assert not by_name["streamlit"]["ok"] and by_name["streamlit"]["level"] == "optional"
    assert doctor.required_ok(results)  # config missing is optional too


def test_config_missing_is_optional_miss(tmp_path):
    res = doctor.check_config(start=tmp_path)
    assert not res["ok"] and res["level"] == "optional"
    assert "configure" in res["hint"]


def test_config_valid(tmp_path):
    (tmp_path / "streamsnow.config.yaml").write_text(EXAMPLE.read_text())
    res = doctor.check_config(start=tmp_path)
    assert res["ok"] and res["level"] == "required"
    assert res["detail"]["schema_version"] == 1


def test_config_invalid_is_required_failure(tmp_path):
    (tmp_path / "streamsnow.config.yaml").write_text("project:\n  name: Acme\n")  # no snowflake
    res = doctor.check_config(start=tmp_path)
    assert not res["ok"] and res["level"] == "required"
    assert res["detail"]["error"]


def test_main_exit_codes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv"))
    assert doctor.main([]) == 0
    (tmp_path / "streamsnow.config.yaml").write_text("project: {}\n")
    assert doctor.main([]) == 1
    monkeypatch.setattr(doctor.shutil, "which", _which_only())
    assert doctor.main([]) == 1


def test_main_json_output(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv"))
    assert doctor.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {c["name"] for c in payload["checks"]} >= {"python", "git", "uv", "config"}


def test_render_text_marks_and_hints(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git"))
    text = doctor.render_text(doctor.run_checks(start=tmp_path))
    assert "[MISSING] uv" in text and "astral.sh/uv" in text
    assert "[skip   ] snow" in text  # optional miss doesn't shout
    assert "[ok     ] git" in text
    assert text.endswith("doctor: FAIL")


def test_doctor_never_raises_from_checks(tmp_path, monkeypatch):
    def boom(_tool):
        raise OSError("PATH lookup exploded")

    monkeypatch.setattr(doctor.shutil, "which", boom)
    # main converts an internal crash into exit 2, not a traceback.
    assert doctor.main([]) == 2
