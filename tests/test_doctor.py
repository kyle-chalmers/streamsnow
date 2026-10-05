"""Tests for the per-check environment doctor."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from streamsnow.tools import doctor

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"


def _which_only(*names: str):
    """A shutil.which stand-in that resolves only the given tool names."""

    def fake(tool: str) -> str | None:
        return f"/opt/acme/bin/{tool}" if tool in names else None

    return fake


def test_result_contract_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run({}))
    results = doctor.run_checks(start=tmp_path)
    assert results  # python + tools + config + connection + container python
    for r in results:
        assert set(r) == {"name", "ok", "level", "detail", "hint"}
        assert r["level"] in ("required", "optional")
    assert [r["name"] for r in results] == [
        "python",
        "git",
        "uv",
        "snow",
        "streamlit",
        "gh",
        "pre-commit",
        "config",
        "snow-connection",
        "snow-key-file",
        "container-python",
        "repo-files",
        "git-identity",
        "pre-commit-hook",
        "node",
        "ci-secrets",
        *(["platform"] if sys.platform == "win32" else []),
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
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    res = doctor.check_config(start=tmp_path)
    assert res["ok"] and res["level"] == "required"
    assert res["detail"]["schema_version"] == 1


def test_config_invalid_is_required_failure(tmp_path):
    (tmp_path / "streamsnow.config.yaml").write_text(
        "project:\n  name: Acme\n", encoding="utf-8"
    )  # no snowflake
    res = doctor.check_config(start=tmp_path)
    assert not res["ok"] and res["level"] == "required"
    assert res["detail"]["error"]


def test_main_exit_codes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv"))
    assert doctor.main([]) == 0
    (tmp_path / "streamsnow.config.yaml").write_text("project: {}\n", encoding="utf-8")
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
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    results = doctor.run_checks(start=tmp_path)
    by_name = {r["name"]: r for r in results}
    assert not by_name["pre-commit"]["ok"] and by_name["pre-commit"]["level"] == "required"
    assert "pre-commit install" in by_name["pre-commit"]["hint"]
    assert not doctor.required_ok(results)  # a scaffolded repo's first commit would fail


def test_config_detail_carries_runtime_and_connection_name(tmp_path):
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    res = doctor.check_config(start=tmp_path)
    assert res["detail"]["runtime"] == "container"
    assert res["detail"]["connection_name"] == "acme"


def test_snow_connection_skipped_without_config_or_snow(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow"))
    res = doctor.check_snow_connection(doctor.check_config(start=tmp_path))
    assert not res["ok"] and res["level"] == "optional" and "skipped" in res["hint"]
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv"))
    res = doctor.check_snow_connection(doctor.check_config(start=tmp_path))
    assert not res["ok"] and res["level"] == "optional" and "snow CLI" in res["hint"]
    # Skips never gate a healthy machine.
    assert doctor.required_ok([res])


def test_snow_connection_found_and_missing(tmp_path, monkeypatch):
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
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
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow"))
    seen: list[list[str]] = []

    def spy(cmd, **_kwargs):
        seen.append(list(cmd))
        return _Proc(0, "[]")

    monkeypatch.setattr(doctor.subprocess, "run", spy)
    doctor.check_snow_connection(doctor.check_config(start=tmp_path))
    assert seen and all("test" not in c for c in seen)


def test_snow_checks_never_raise_on_timeout_or_garbage(tmp_path, monkeypatch):
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow", "pre-commit"))
    monkeypatch.chdir(tmp_path)

    def boom(*_a, **_k):
        raise doctor.subprocess.TimeoutExpired(cmd="snow", timeout=5)

    monkeypatch.setattr(doctor.subprocess, "run", boom)
    assert doctor.main([]) in (0, 1)  # never 2: the doctor itself did not crash
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run({_LIST: (0, "not json")}))
    res = doctor.check_snow_connection(doctor.check_config(start=tmp_path))
    # Unreadable output is "not checked", never "you have no such connection".
    assert not res["ok"] and "known" not in res["detail"]
    assert res["hint"].startswith("not checked:")
    assert "snow connection add" not in res["hint"]


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
        REPO_ROOT / "skills" / "onboard" / "setup.md",
    ]
    offenders = [str(p) for p in targets if "snowflake-cli-labs" in p.read_text(encoding="utf-8")]
    assert not offenders, offenders


# --------------------------------------------------------------------------- #
# 0.7.1: snow must run (not just exist), gh, container Python 3.11, cold snow
# --------------------------------------------------------------------------- #
_VERSION = ("snow", "--version")
_FIND_311 = ("uv", "python", "find")


def test_snow_on_path_but_crashing_is_a_failure(tmp_path, monkeypatch):
    """A Homebrew snow on a newer Python crashed on import (a pyOpenSSL
    mismatch) and doctor still printed [ok] snow: PATH presence is not a
    working install."""
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow"))
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run({_VERSION: (1, "")}))
    res = doctor.check_snow()
    assert not res["ok"] and res["level"] == "required"
    assert res["detail"]["broken"] is True
    assert "uv tool install snowflake-cli" in res["hint"]
    text = doctor.render_text([res])
    assert "[BROKEN ] snow" in text


def _scripted_run(script: dict[tuple[str, ...], object], seen: list | None = None):
    """subprocess.run stand-in keyed on the first three argv tokens: a value is
    (returncode, stdout), or an exception to raise (e.g. TimeoutExpired)."""

    def fake(cmd, **kwargs):
        key = tuple(cmd[:3])
        if seen is not None:
            seen.append((key, kwargs.get("timeout")))
        outcome = script.get(key, (127, ""))
        if isinstance(outcome, Exception):
            raise outcome
        return _Proc(*outcome)

    return fake


# A bare tmp_path repo is correctly not onboarded (no governed files, git
# repo or hook); tests about other checks gate on everything but these rows.
_ONBOARDING_ROWS = {"repo-files", "git-identity", "pre-commit-hook"}


def _required_ok_ignoring_onboarding(results: list[dict]) -> bool:
    return doctor.required_ok([r for r in results if r["name"] not in _ONBOARDING_ROWS])


def _cold_start():
    return doctor.subprocess.TimeoutExpired(cmd="snow", timeout=doctor._SNOW_TIMEOUT_S)


def test_snow_version_outcomes_ok_timeout_and_import_error(monkeypatch):
    """Observed 2026-09-27: the first `snow --version` after the Mac idled took
    24.8 s (about 1 s of CPU), past 0.7.1's 15 s timeout, and doctor printed
    `[BROKEN ] snow` with reinstall advice for a healthy install. A timeout is
    its own state: a warning to re-run. A crash on import stays BROKEN."""
    monkeypatch.setattr(doctor.shutil, "which", _which_only("snow"))
    outcomes = {
        "ok": (0, "Snowflake CLI version: 3.27.0\n"),
        "timeout": _cold_start(),
        "import-error": (1, ""),  # e.g. a Homebrew snow whose pyOpenSSL import fails
    }
    results = {}
    for label, outcome in outcomes.items():
        monkeypatch.setattr(doctor.subprocess, "run", _scripted_run({_VERSION: outcome}))
        results[label] = doctor.check_snow()

    ok = results["ok"]
    assert ok["ok"] and ok["detail"]["version"] == "3.27.0"

    slow = results["timeout"]
    assert not slow["ok"] and slow["level"] == "optional"
    assert slow["detail"]["timed_out"] is True and "broken" not in slow["detail"]
    assert "did not answer within 45s" in slow["hint"] and "re-run" in slow["hint"]
    assert "reinstall" not in slow["hint"] and "uv tool install" not in slow["hint"]
    assert doctor.render_text([slow]).startswith("[warn   ] snow")
    assert doctor.required_ok([slow])  # a slow snow never fails the doctor

    broken = results["import-error"]
    assert not broken["ok"] and broken["level"] == "required"
    assert broken["detail"]["broken"] is True
    assert "uv tool install snowflake-cli" in broken["hint"]
    assert doctor.render_text([broken]).startswith("[BROKEN ] snow")
    assert not doctor.required_ok([broken])


def test_snow_timeout_still_runs_the_connection_checks(tmp_path, monkeypatch):
    """A `snow --version` that only timed out must not cascade into skipped
    connection checks: the listing usually answers once the cold start is over."""
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow", "pre-commit"))
    rows = json.dumps(_key_rows(authenticator="SNOWFLAKE_JWT", private_key_file="/k.p8"))
    seen: list = []
    script = {_VERSION: _cold_start(), _LIST: (0, rows)}
    monkeypatch.setattr(doctor.subprocess, "run", _scripted_run(script, seen))
    results = doctor.run_checks(start=tmp_path)
    by_name = {r["name"]: r for r in results}
    assert by_name["snow"]["detail"]["timed_out"] is True
    assert by_name["snow-connection"]["ok"], by_name["snow-connection"]
    assert by_name["snow-key-file"]["ok"], by_name["snow-key-file"]
    assert [k for k, _ in seen].count(_LIST) == 1
    assert _required_ok_ignoring_onboarding(results)


def test_snow_that_stays_silent_says_the_connection_checks_were_not_checked(tmp_path, monkeypatch):
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow", "pre-commit"))
    seen: list = []
    script = {_VERSION: _cold_start(), _LIST: _cold_start()}
    monkeypatch.setattr(doctor.subprocess, "run", _scripted_run(script, seen))
    results = doctor.run_checks(start=tmp_path)
    by_name = {r["name"]: r for r in results}
    for name in ("snow-connection", "snow-key-file"):
        hint = by_name[name]["hint"]
        assert hint.startswith("not checked: snow connection list did not answer"), (name, hint)
        assert "snow connection add" not in hint, name
    # One listing attempt, not one per dependent check (each can wait 45 s).
    assert [k for k, _ in seen].count(_LIST) == 1
    assert _required_ok_ignoring_onboarding(results)
    text = doctor.render_text(results)
    assert "[warn   ] snow " in text and "BROKEN" not in text


def test_snow_probes_wait_out_a_cold_start_and_local_probes_stay_short(tmp_path, monkeypatch):
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow", "pre-commit"))
    seen: list = []
    script = {_VERSION: (0, "Snowflake CLI version: 3.27.0"), _LIST: (0, "[]")}
    monkeypatch.setattr(doctor.subprocess, "run", _scripted_run(script, seen))
    doctor.run_checks(start=tmp_path)
    timeouts = dict(seen)
    assert timeouts[_VERSION] >= 45  # the observed cold start took 24.8 s
    assert timeouts[_LIST] >= timeouts[_VERSION]
    assert timeouts[_FIND_311] <= 15  # uv python find is local; no need to wait long


def test_snow_missing_stays_optional(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv"))
    res = doctor.check_snow()
    assert not res["ok"] and res["level"] == "optional"


def test_snow_probe_timeout_absorbs_a_cold_start():
    """A cold `snow` start took longer than 5 s, so the first doctor run
    reported no connections and a re-run found one; later one took 24.8 s."""
    assert doctor._SNOW_TIMEOUT_S >= 45


def test_gh_is_an_optional_check_that_names_ship_app(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv"))
    by_name = {r["name"]: r for r in doctor.run_checks(start=tmp_path)}
    assert not by_name["gh"]["ok"] and by_name["gh"]["level"] == "optional"
    assert "/ship-app" in by_name["gh"]["hint"]


def _container_cfg(tmp_path):
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return doctor.check_config(start=tmp_path)


def test_container_repo_warns_without_a_python_311(tmp_path, monkeypatch):
    """Doctor passed Python 3.12 while container apps pin >=3.11,<3.12."""
    cfg = _container_cfg(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _which_only("uv"))
    monkeypatch.setattr(doctor.subprocess, "run", _fake_run({_FIND_311: (2, "")}))
    res = doctor.check_container_python(cfg)
    assert not res["ok"] and res["level"] == "optional"
    assert "uv python install 3.11" in res["hint"]
    assert "[warn   ] container-python" in doctor.render_text([res])
    monkeypatch.setattr(
        doctor.subprocess, "run", _fake_run({_FIND_311: (0, "/opt/acme/python3.11\n")})
    )
    res = doctor.check_container_python(cfg)
    assert res["ok"] and res["detail"]["path"] == "/opt/acme/python3.11"


def test_container_python_skipped_outside_container_repos(tmp_path, monkeypatch):
    res = doctor.check_container_python(doctor.check_config(start=tmp_path))
    assert not res["ok"] and "skipped" in res["detail"]
    data = EXAMPLE.read_text(encoding="utf-8").replace("runtime: container", "runtime: warehouse")
    data = data.replace('compute_pool: "SYSTEM_COMPUTE_POOL_CPU"', 'compute_pool: ""')
    (tmp_path / "streamsnow.config.yaml").write_text(data, encoding="utf-8")
    res = doctor.check_container_python(doctor.check_config(start=tmp_path))
    assert "skipped" in res["detail"]


def _key_rows(**params):
    return [{"connection_name": "acme", "is_default": True, "parameters": params}]


_OK_SNOW = {"name": "snow", "ok": True, "level": "optional", "detail": {"found": True}}


def test_snow_key_file_warns_on_key_pair_without_a_connector_readable_key():
    """snow reads private_key_path (a legacy alias it rewrites to private_key_file);
    the Python connector behind st.connection drops it and the first page load dies
    with TypeError: Expected bytes, RSAPrivateKey, ... got NoneType. snow connection
    list never prints private_key_path, so key-pair auth with no private_key_file IS
    the signature."""
    rows = _key_rows(account="x", user="y", authenticator="SNOWFLAKE_JWT")
    res = doctor.check_snow_key_file(_OK_SNOW, rows)
    assert not res["ok"] and res["level"] == "optional" and res["detail"]["warn"]
    assert "private_key_path to private_key_file" in res["hint"]
    assert "private_key_file_pwd" in res["hint"]
    assert doctor.required_ok([res])  # a warning, never a gate
    assert "[warn   ] snow-key-file" in doctor.render_text([res])
    # private_key_raw is snow-only too: the connector has no such parameter.
    raw = _key_rows(authenticator="snowflake_jwt", private_key_raw="****")
    assert not doctor.check_snow_key_file(_OK_SNOW, raw)["ok"]


def test_snow_key_file_passes_when_the_connector_can_load_the_key():
    ok = doctor.check_snow_key_file(
        _OK_SNOW, _key_rows(authenticator="SNOWFLAKE_JWT", private_key_file="/k.p8")
    )
    assert ok["ok"] and ok["detail"]["key_fields"] == ["private_key_file"]
    sso = doctor.check_snow_key_file(_OK_SNOW, _key_rows(authenticator="externalbrowser"))
    assert sso["ok"] and sso["detail"]["key_pair"] is False


def test_snow_key_file_reads_names_never_values():
    secret = "acme-raw-key-material"
    rows = _key_rows(authenticator="SNOWFLAKE_JWT", private_key_raw=secret, user="acme-user")
    res = doctor.check_snow_key_file(_OK_SNOW, rows)
    assert secret not in json.dumps(res) and "acme-user" not in json.dumps(res)


def test_snow_key_file_skips_without_snow_or_a_default_connection(monkeypatch):
    missing = {"name": "snow", "ok": False, "level": "optional", "detail": {"found": False}}
    assert "skipped" in doctor.check_snow_key_file(missing, None)["detail"]
    broken = {"name": "snow", "ok": False, "level": "required", "detail": {"broken": True}}
    assert "skipped" in doctor.check_snow_key_file(broken, None)["detail"]
    no_default = [{"connection_name": "acme", "is_default": False}]
    res = doctor.check_snow_key_file(_OK_SNOW, no_default)
    assert "skipped" in res["detail"] and doctor.required_ok([res])


def test_run_checks_lists_snow_connections_once(tmp_path, monkeypatch):
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git", "uv", "snow"))
    seen: list[tuple[str, ...]] = []
    rows = json.dumps(_key_rows(authenticator="SNOWFLAKE_JWT", private_key_file="/k.p8"))

    def spy(cmd, **_kwargs):
        seen.append(tuple(cmd[:3]))
        return _Proc(0, rows if tuple(cmd[:3]) == _LIST else "Snowflake CLI version: 3.27.0")

    monkeypatch.setattr(doctor.subprocess, "run", spy)
    by_name = {r["name"]: r for r in doctor.run_checks(start=tmp_path)}
    assert seen.count(_LIST) == 1
    assert by_name["snow-connection"]["ok"] and by_name["snow-key-file"]["ok"]


def test_snow_connection_hint_points_at_an_existing_default_before_adding_one(
    tmp_path, monkeypatch
):
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    cfg = doctor.check_config(start=tmp_path)
    rows = [{"connection_name": "tutorial", "is_default": True}]
    monkeypatch.setattr(doctor.shutil, "which", _which_only("snow"))
    res = doctor.check_snow_connection(cfg, rows=rows)
    assert not res["ok"]
    assert "snowflake.connection_name: tutorial" in res["hint"]
    assert "snow connection add --connection-name acme" in res["hint"]


# --------------------------------------------------------------------------- #
# 0.7.6: onboarding checks. A teammate cloning a configured repo, or a stranger
# setting one up, must not be told "ok" while governance is silently off.
# --------------------------------------------------------------------------- #
_IDENTITY_NAME = ("git", "config", "user.name")
_IDENTITY_EMAIL = ("git", "config", "user.email")
_HOOK_PATH = ("git", "rev-parse", "--path-format=absolute")
_SECRETS = ("gh", "secret", "list")
_NODE = ("node", "--version")
_ALL_SECRETS = [{"name": n} for n in doctor.CI_SECRET_NAMES]


def _configured(tmp_path: Path) -> dict:
    (tmp_path / "streamsnow.config.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return doctor.check_config(start=tmp_path)


def test_repo_files_skipped_without_config(tmp_path):
    res = doctor.check_repo_files(start=tmp_path)
    assert not res["ok"] and res["level"] == "optional" and "skipped" in res["detail"]


def test_repo_files_missing_is_required_and_names_the_fix(tmp_path):
    _configured(tmp_path)
    res = doctor.check_repo_files(start=tmp_path)
    assert not res["ok"] and res["level"] == "required"
    assert ".gitignore" in res["detail"]["missing"]
    assert "streamsnow init --no-starter-app" in res["hint"]
    for rel in res["detail"]["missing"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    assert doctor.check_repo_files(start=tmp_path)["ok"]


def test_git_identity_reports_presence_only_never_values(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git"))
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _scripted_run({_IDENTITY_NAME: (0, "Acme Dev\n"), _IDENTITY_EMAIL: (0, "dev@acme.test\n")}),
    )
    res = doctor.check_git_identity(config_present=True)
    assert res["ok"] and res["detail"] == {"name_set": True, "email_set": True}
    blob = json.dumps(res)
    assert "Acme Dev" not in blob and "dev@acme.test" not in blob


def test_git_identity_missing_is_required_only_in_a_configured_repo(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git"))
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        _scripted_run({_IDENTITY_NAME: (0, "Acme Dev\n"), _IDENTITY_EMAIL: (1, "")}),
    )
    res = doctor.check_git_identity(config_present=True)
    assert not res["ok"] and res["level"] == "required"
    assert "git config user.email" in res["hint"] and "user.name" not in res["hint"]
    assert "Acme Dev" not in json.dumps(res)
    bare = doctor.check_git_identity(config_present=False)
    assert not bare["ok"] and bare["level"] == "optional" and bare["detail"]["warn"]


def test_git_identity_skipped_without_git_and_not_checked_on_timeout(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only())
    res = doctor.check_git_identity(config_present=True)
    assert not res["ok"] and res["level"] == "optional" and "skipped" in res["detail"]
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git"))
    timeout = doctor.subprocess.TimeoutExpired(cmd="git", timeout=15)
    monkeypatch.setattr(doctor.subprocess, "run", _scripted_run({_IDENTITY_NAME: timeout}))
    res = doctor.check_git_identity(config_present=True)
    assert not res["ok"] and res["level"] == "optional" and "not checked" in res["hint"]


def _hook_run(monkeypatch, out: str, code: int = 0):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("git"))
    monkeypatch.setattr(doctor.subprocess, "run", _scripted_run({_HOOK_PATH: (code, out)}))


def test_pre_commit_hook_skipped_without_config(tmp_path):
    res = doctor.check_pre_commit_hook(doctor.check_config(start=tmp_path))
    assert not res["ok"] and res["level"] == "optional" and "skipped" in res["detail"]


def test_pre_commit_hook_missing_is_required(tmp_path, monkeypatch):
    cfg = _configured(tmp_path)
    _hook_run(monkeypatch, str(tmp_path / ".git" / "hooks" / "pre-commit") + "\n")
    res = doctor.check_pre_commit_hook(cfg)
    assert not res["ok"] and res["level"] == "required"
    assert "pre-commit install" in res["hint"]


def test_pre_commit_hook_must_be_pre_commits_own(tmp_path, monkeypatch):
    cfg = _configured(tmp_path)
    hook = tmp_path / "hooks" / "pre-commit"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\necho some other hook\n", encoding="utf-8")
    _hook_run(monkeypatch, str(hook))
    res = doctor.check_pre_commit_hook(cfg)
    assert not res["ok"] and res["level"] == "required"
    assert "pre-commit.legacy" in res["hint"]  # install keeps and still runs the old hook
    hook.write_text(
        "#!/usr/bin/env bash\n# File generated by pre-commit: https://pre-commit.com\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    assert doctor.check_pre_commit_hook(cfg)["ok"]


def test_pre_commit_hook_resolves_a_relative_hooks_path_against_the_repo(tmp_path, monkeypatch):
    cfg = _configured(tmp_path)
    (tmp_path / ".githooks").mkdir()
    (tmp_path / ".githooks" / "pre-commit").write_text(
        "# File generated by pre-commit\n", encoding="utf-8"
    )
    (tmp_path / ".githooks" / "pre-commit").chmod(0o755)
    _hook_run(monkeypatch, ".githooks/pre-commit\n")  # core.hooksPath, older git
    assert doctor.check_pre_commit_hook(cfg)["ok"]


def test_pre_commit_hook_not_checked_outside_a_git_repo(tmp_path, monkeypatch):
    cfg = _configured(tmp_path)
    _hook_run(monkeypatch, "", code=128)
    res = doctor.check_pre_commit_hook(cfg)
    assert not res["ok"] and res["level"] == "optional" and "not checked" in res["hint"]


def test_node_ok_old_missing_and_no_npx(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("node", "npx"))
    monkeypatch.setattr(doctor.subprocess, "run", _scripted_run({_NODE: (0, "v20.11.1\n")}))
    res = doctor.check_node()
    assert res["ok"] and res["level"] == "optional" and res["detail"]["version"] == "20.11.1"
    monkeypatch.setattr(doctor.subprocess, "run", _scripted_run({_NODE: (0, "v18.19.1\n")}))
    res = doctor.check_node()
    assert not res["ok"] and res["detail"]["warn"] and "20" in res["hint"]
    monkeypatch.setattr(doctor.shutil, "which", _which_only("node"))
    monkeypatch.setattr(doctor.subprocess, "run", _scripted_run({_NODE: (0, "v20.11.1\n")}))
    res = doctor.check_node()
    assert not res["ok"] and "npx" in res["hint"]
    monkeypatch.setattr(doctor.shutil, "which", _which_only())
    res = doctor.check_node()
    assert not res["ok"] and res["level"] == "optional" and res["detail"]["warn"]
    assert doctor.required_ok([res])  # the UI walk is advisory: never gates


def test_ci_secrets_skipped_without_config_or_gh(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", _which_only("gh"))
    res = doctor.check_ci_secrets(doctor.check_config(start=tmp_path))
    assert not res["ok"] and "skipped" in res["detail"]
    cfg = _configured(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _which_only())
    res = doctor.check_ci_secrets(cfg)
    assert not res["ok"] and "skipped" in res["detail"] and doctor.required_ok([res])


def test_ci_secrets_all_present_and_missing(tmp_path, monkeypatch):
    cfg = _configured(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _which_only("gh"))
    monkeypatch.setattr(
        doctor.subprocess, "run", _scripted_run({_SECRETS: (0, json.dumps(_ALL_SECRETS))})
    )
    assert doctor.check_ci_secrets(cfg)["ok"]
    partial = [s for s in _ALL_SECRETS if s["name"] != "SNOWFLAKE_PRIVATE_KEY_RAW"]
    monkeypatch.setattr(
        doctor.subprocess, "run", _scripted_run({_SECRETS: (0, json.dumps(partial))})
    )
    res = doctor.check_ci_secrets(cfg)
    assert not res["ok"] and res["level"] == "optional" and res["detail"]["warn"]
    assert res["detail"]["missing"] == ["SNOWFLAKE_PRIVATE_KEY_RAW"]
    assert "SNOWFLAKE_PRIVATE_KEY_RAW" in res["hint"]


def test_ci_secrets_access_denied_is_not_checked_never_missing(tmp_path, monkeypatch):
    cfg = _configured(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", _which_only("gh"))
    for outcome in [
        (1, ""),
        (0, "not json"),
        doctor.subprocess.TimeoutExpired(cmd="gh", timeout=15),
    ]:
        monkeypatch.setattr(doctor.subprocess, "run", _scripted_run({_SECRETS: outcome}))
        res = doctor.check_ci_secrets(cfg)
        assert not res["ok"] and "not checked" in res["hint"]
        assert "missing" not in res["detail"]


def test_ci_secret_names_match_the_deploy_workflow_templates():
    import re

    templates = REPO_ROOT / "streamsnow" / "_templates" / "repo"
    for name in ("deploy.yml.j2", "deploy.git.yml.j2"):
        used = set(
            re.findall(r"secrets\.(SNOWFLAKE_\w+)", (templates / name).read_text(encoding="utf-8"))
        )
        assert used == set(doctor.CI_SECRET_NAMES) | set(doctor.OPTIONAL_CI_SECRET_NAMES), name


def test_platform_row_only_on_native_windows():
    res = doctor.check_platform("win32")
    assert res is not None and not res["ok"] and res["detail"]["warn"]
    assert "WSL" in res["hint"] and res["level"] == "optional"
    assert doctor.check_platform("linux") is None and doctor.check_platform("darwin") is None


def test_pre_commit_hook_must_be_executable(tmp_path, monkeypatch):
    """Git silently ignores a hook without the executable bit, so a restored or
    copied hook can carry pre-commit's marker and still run nothing."""
    cfg = _configured(tmp_path)
    hook = tmp_path / "hooks" / "pre-commit"
    hook.parent.mkdir()
    hook.write_text(
        "#!/usr/bin/env bash\n# File generated by pre-commit: https://pre-commit.com\n",
        encoding="utf-8",
    )
    hook.chmod(0o644)
    _hook_run(monkeypatch, str(hook))
    res = doctor.check_pre_commit_hook(cfg)
    if os.name == "nt":
        # No executable bit on Windows; git runs the hook regardless.
        assert res["ok"]
        return
    assert not res["ok"] and res["level"] == "required"
    assert "pre-commit install" in res["hint"]
    hook.chmod(0o755)
    assert doctor.check_pre_commit_hook(cfg)["ok"]
