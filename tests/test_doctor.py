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
    assert results  # python + 5 tools + config + connection
    for r in results:
        assert set(r) == {"name", "ok", "level", "detail", "hint"}
        assert r["level"] in ("required", "optional")
    assert [r["name"] for r in results] == [
        "python",
        "git",
        "uv",
        "snow",
        "streamlit",
        "pre-commit",
        "config",
        "snow-connection",
    ]


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


# --------------------------------------------------------------------------- #
# 0.7 additions: pre-commit level flip, snow-connection, no `-labs` anywhere
# --------------------------------------------------------------------------- #
class _Proc:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _fake_run(outputs: dict[tuple[str, ...], tuple[int, str]]):
    """A subprocess.run stand-in keyed on the first three argv tokens."""

    def fake(cmd, **_kwargs):
        code, out = outputs.get(tuple(cmd[:3]), (127, ""))
        return _Proc(code, out)

    return fake


_LIST = ("snow", "connection", "list")


def test_pre_commit_optional_without_config_required_with(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv"))
    by_name = {r["name"]: r for r in doctor.run_checks(start=tmp_path)}
    assert not by_name["pre-commit"]["ok"] and by_name["pre-commit"]["level"] == "optional"
    assert doctor.required_ok(doctor.run_checks(start=tmp_path))
    (tmp_path / "streamsnow.config.yaml").write_text(EXAMPLE.read_text())
    results = doctor.run_checks(start=tmp_path)
    by_name = {r["name"]: r for r in results}
    assert not by_name["pre-commit"]["ok"] and by_name["pre-commit"]["level"] == "required"
    assert "pre-commit install" in by_name["pre-commit"]["hint"]
    assert not doctor.required_ok(results)  # a scaffolded repo's first commit would fail


def test_config_detail_carries_runtime_and_connection_name(tmp_path):
    (tmp_path / "streamsnow.config.yaml").write_text(EXAMPLE.read_text())
    res = doctor.check_config(start=tmp_path)
    assert res["detail"]["runtime"] == "container"
    assert res["detail"]["connection_name"] == "acme"


def test_snow_connection_skipped_without_config_or_snow(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow"))
    res = doctor.check_snow_connection(doctor.check_config(start=tmp_path))
    assert not res["ok"] and res["level"] == "optional" and "skipped" in res["hint"]
    (tmp_path / "streamsnow.config.yaml").write_text(EXAMPLE.read_text())
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv"))
    res = doctor.check_snow_connection(doctor.check_config(start=tmp_path))
    assert not res["ok"] and res["level"] == "optional" and "snow CLI" in res["hint"]
    # Skips never gate a healthy machine.
    assert doctor.required_ok([res])


def test_snow_connection_found_and_missing(tmp_path, monkeypatch):
    (tmp_path / "streamsnow.config.yaml").write_text(EXAMPLE.read_text())
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow"))
    cfg = doctor.check_config(start=tmp_path)
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _fake_run({_LIST: (0, json.dumps([{"connection_name": "acme", "is_default": True}]))}),
    )
    res = doctor.check_snow_connection(cfg)
    assert res["ok"] and res["detail"]["known"] == ["acme"]
    monkeypatch.setattr(
        doctor.subprocess, "run", _fake_run({_LIST: (0, json.dumps([{"name": "other"}]))})
    )
    res = doctor.check_snow_connection(cfg)
    assert not res["ok"]
    assert "snow connection add --connection-name acme" in res["hint"]
    assert "--default" in res["hint"]


def test_snow_connection_never_runs_connection_test(tmp_path, monkeypatch):
    (tmp_path / "streamsnow.config.yaml").write_text(EXAMPLE.read_text())
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow"))
    seen: list[list[str]] = []

    def spy(cmd, **_kwargs):
        seen.append(list(cmd))
        return _Proc(0, "[]")

    monkeypatch.setattr(doctor.subprocess, "run", spy)
    doctor.check_snow_connection(doctor.check_config(start=tmp_path))
    assert seen and all("test" not in c for c in seen)


def test_snow_checks_never_raise_on_timeout_or_garbage(tmp_path, monkeypatch):
    (tmp_path / "streamsnow.config.yaml").write_text(EXAMPLE.read_text())
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow", "pre-commit"))
    monkeypatch.chdir(tmp_path)

    def boom(*_a, **_k):
        raise doctor.subprocess.TimeoutExpired(cmd="snow", timeout=5)

    monkeypatch.setattr(doctor.subprocess, "run", boom)
    assert doctor.main([]) in (0, 1)  # never 2: the doctor itself did not crash
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run({_LIST: (0, "not json")}))
    res = doctor.check_snow_connection(doctor.check_config(start=tmp_path))
    assert not res["ok"] and res["detail"]["known"] == []


def test_healthy_machine_without_config_still_exits_zero(tmp_path, monkeypatch):
    """Exit-code contract pin: the new optional checks must not turn a healthy
    machine outside any repo into a doctor FAIL."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv"))
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run({}))
    assert doctor.main([]) == 0


def test_no_snowflake_cli_labs_anywhere():
    """`snowflake-cli` is the canonical PyPI name; the repo used both for a while."""
    targets = [
        REPO_ROOT / "streamsnow" / "tools" / "doctor.py",
        *sorted((REPO_ROOT / "streamsnow" / "_templates" / "repo").glob("deploy*.yml.j2")),
        REPO_ROOT / "skills" / "start-app" / "setup.md",
    ]
    offenders = [str(p) for p in targets if "snowflake-cli-labs" in p.read_text()]
    assert not offenders, offenders
