"""Tests for the background preview lifecycle (``streamsnow.tools.preview_app``).

No real Streamlit and no network beyond localhost: ``build_command`` is
monkeypatched to launch tiny stand-in scripts — an http.server that answers
``/_stcore/health`` for the happy path, and scripts that emit classifiable
launch failures for the timeout/death paths.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

from streamsnow.tools import preview_app

SLUG = "acme-sales-dashboard"

# Stand-in "streamlit": binds the port and answers the health endpoint.
FAKE_SERVER = """\
import http.server
import sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200 if self.path == "/_stcore/health" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

print("You can now view your Streamlit app in your browser.", flush=True)
print("Local URL: http://127.0.0.1:" + sys.argv[1], flush=True)
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""

# Stand-in that hangs without ever serving health (secrets misconfiguration).
FAKE_HANG = """\
import time

print("FileNotFoundError: No secrets files found. Valid paths for a "
      "secrets.toml file are ...", flush=True)
time.sleep(120)
"""

# Stand-in that dies during startup (missing dependency).
FAKE_DIE = """\
import sys

print("ModuleNotFoundError: No module named 'plotly'", flush=True)
sys.exit(1)
"""


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _repo(tmp_path: Path) -> Path:
    app_dir = tmp_path / "apps" / SLUG
    app_dir.mkdir(parents=True)
    (app_dir / "streamlit_app.py").write_text("import streamlit as st\n")
    return tmp_path


def _fake_launcher(tmp_path: Path, script_body: str, monkeypatch) -> None:
    script = tmp_path / "fake_streamlit.py"
    script.write_text(script_body)
    monkeypatch.setattr(
        preview_app,
        "build_command",
        lambda entrypoint, port: [sys.executable, str(script), str(port)],
    )


def _start_args(repo: Path, port: int, timeout: float = 15.0) -> list[str]:
    return [
        "start",
        SLUG,
        "--dir",
        str(repo),
        "--port",
        str(port),
        "--timeout",
        str(timeout),
        "--poll-interval",
        "0.05",
    ]


def test_start_ready_status_logs_stop_lifecycle(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _fake_launcher(tmp_path, FAKE_SERVER, monkeypatch)
    port = _free_port()
    try:
        assert preview_app.main(_start_args(repo, port) + ["--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ready"
        assert payload["url"] == f"http://127.0.0.1:{port}"
        state_path = repo / ".streamsnow" / "preview" / f"{SLUG}.json"
        state = json.loads(state_path.read_text())
        assert state["port"] == port and state["slug"] == SLUG

        # A second start is a no-op report, not a double launch.
        assert preview_app.main(_start_args(repo, port)) == 0
        assert "already running" in capsys.readouterr().out

        assert preview_app.main(["status", SLUG, "--dir", str(repo), "--json"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["status"] == "running" and status["healthy"] is True

        assert preview_app.main(["logs", SLUG, "--dir", str(repo)]) == 0
        assert "You can now view your Streamlit app" in capsys.readouterr().out

        pid = state["pid"]
        assert preview_app.main(["stop", SLUG, "--dir", str(repo)]) == 0
        capsys.readouterr()
        assert not state_path.exists()
        assert not preview_app._pid_alive(pid)
        # Log survives stop for post-mortems; status now reports not running.
        assert (repo / ".streamsnow" / "preview" / f"{SLUG}.log").is_file()
        assert preview_app.main(["status", SLUG, "--dir", str(repo)]) == 1
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])


def test_start_timeout_kills_and_classifies_missing_secrets(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _fake_launcher(tmp_path, FAKE_HANG, monkeypatch)
    port = _free_port()
    try:
        rc = preview_app.main(_start_args(repo, port, timeout=0.8))
        out = capsys.readouterr().out
        assert rc == 1
        assert "missing_secrets" in out
        assert "secrets.toml" in out
        # State cleaned up so the next start isn't wedged.
        assert not (repo / ".streamsnow" / "preview" / f"{SLUG}.json").exists()
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])


def test_start_early_death_classifies_missing_package(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _fake_launcher(tmp_path, FAKE_DIE, monkeypatch)
    port = _free_port()
    rc = preview_app.main(_start_args(repo, port))
    out = capsys.readouterr().out
    assert rc == 1
    assert "process exited during startup" in out
    assert "missing_package" in out


def test_start_fails_fast_when_port_busy(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _fake_launcher(tmp_path, FAKE_SERVER, monkeypatch)
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        rc = preview_app.main(_start_args(repo, port))
        out = capsys.readouterr().out
        assert rc == 1
        assert "already in use" in out
        assert not (repo / ".streamsnow" / "preview" / f"{SLUG}.json").exists()
    finally:
        holder.close()


def test_missing_entrypoint_is_tool_error(tmp_path, capsys):
    (tmp_path / "apps").mkdir()
    rc = preview_app.main(["start", SLUG, "--dir", str(tmp_path)])
    assert rc == 2
    assert "not found" in capsys.readouterr().out


def test_stale_state_cleaned_up_not_an_error(tmp_path, capsys):
    repo = _repo(tmp_path)
    # A genuinely dead PID: spawn a trivial process and wait for it to exit.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    state_dir = repo / ".streamsnow" / "preview"
    state_dir.mkdir(parents=True)
    state_path = state_dir / f"{SLUG}.json"
    state_path.write_text(
        json.dumps({"slug": SLUG, "pid": proc.pid, "port": 8599, "log": str(state_dir / "x.log")})
    )

    assert preview_app.main(["status", SLUG, "--dir", str(repo)]) == 1
    assert "stale state" in capsys.readouterr().out
    assert not state_path.exists()

    # stop on a missing/stale preview is idempotent success.
    assert preview_app.main(["stop", SLUG, "--dir", str(repo)]) == 0


def test_stale_state_does_not_block_restart(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _fake_launcher(tmp_path, FAKE_SERVER, monkeypatch)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    state_dir = repo / ".streamsnow" / "preview"
    state_dir.mkdir(parents=True)
    (state_dir / f"{SLUG}.json").write_text(json.dumps({"slug": SLUG, "pid": proc.pid, "port": 1}))
    port = _free_port()
    try:
        assert preview_app.main(_start_args(repo, port)) == 0
        assert "serving at" in capsys.readouterr().out
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])


def test_classify_log_patterns():
    ready = preview_app.classify_log("You can now view your Streamlit app in your browser.")
    assert ready["status"] == "ready"

    double_suffix = preview_app.classify_log(
        "snowflake.connector.errors.OperationalError: 250001 (08001): Failed to connect to DB: "
        "acme123.snowflakecomputing.com.snowflakecomputing.com:443."
    )
    assert double_suffix["status"] == "bad_account"
    assert "account locator" in double_suffix["hint"]

    session = preview_app.classify_log(
        "SnowparkSessionException: (1403): get_active_session() is not supported "
        "outside of Snowflake"
    )
    assert session["status"] == "session_outside_snowflake"

    old_streamlit = preview_app.classify_log(
        "AttributeError: module 'streamlit' has no attribute 'connection'"
    )
    assert old_streamlit["status"] == "connection_attr_missing"

    unknown = preview_app.classify_log("something entirely novel happened")
    assert unknown["status"] == "unknown"


def test_logs_missing_file(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert preview_app.main(["logs", SLUG, "--dir", str(repo)]) == 1
    assert "no preview log" in capsys.readouterr().err


def test_probe_health_refused_port_is_false():
    # Nothing bound on this port; the probe must swallow the refusal.
    assert preview_app.probe_health(_free_port(), timeout=0.2) is False


def test_start_launcher_missing_is_tool_error(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        preview_app,
        "build_command",
        lambda entrypoint, port: [str(tmp_path / "no-such-binary"), str(port)],
    )
    rc = preview_app.main(_start_args(repo, _free_port()))
    assert rc == 2
    assert "not found on PATH" in capsys.readouterr().out
