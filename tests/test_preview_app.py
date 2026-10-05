"""Tests for the background preview lifecycle (``streamsnow.tools.preview_app``).

No real Streamlit and no network beyond localhost: ``build_command`` is
monkeypatched to launch tiny stand-in scripts — an http.server that answers
``/_stcore/health`` for the happy path, and scripts that emit classifiable
launch failures for the timeout/death paths.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from streamsnow.tools import preview_app

SLUG = "acme-sales-dashboard"

# Stand-in "streamlit": binds the port and answers the health endpoint.
# socketserver.TCPServer, not http.server.HTTPServer: HTTPServer calls
# socket.getfqdn() between bind and listen, a reverse-DNS lookup that stalls on
# GitHub's macOS runners, leaving the port bound but refusing connections.
FAKE_SERVER = """\
import http.server
import socketserver
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

socketserver.TCPServer.allow_reuse_address = True  # as HTTPServer does
server = socketserver.TCPServer(("127.0.0.1", int(sys.argv[1])), Handler)
print("You can now view your Streamlit app in your browser.", flush=True)
print("Local URL: http://127.0.0.1:" + sys.argv[1], flush=True)
server.serve_forever()
"""

# Stand-in like streamlit.exe on Windows: a launcher whose CHILD process is the
# real server. Stop must take the whole tree, or the child keeps the port.
FAKE_LAUNCHER_WITH_CHILD = """\
import subprocess
import sys
from pathlib import Path

server = Path(sys.argv[0]).with_name("fake_streamlit_server.py")
child = subprocess.Popen([sys.executable, str(server), sys.argv[1]])
Path(sys.argv[0]).with_name("child.pid").write_text(str(child.pid), encoding="utf-8")
child.wait()
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
    (app_dir / "streamlit_app.py").write_text("import streamlit as st\n", encoding="utf-8")
    return tmp_path


def _fake_launcher(tmp_path: Path, script_body: str, monkeypatch) -> None:
    script = tmp_path / "fake_streamlit.py"
    script.write_text(script_body, encoding="utf-8")
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
        state = json.loads(state_path.read_text(encoding="utf-8"))
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
        json.dumps({"slug": SLUG, "pid": proc.pid, "port": 8599, "log": str(state_dir / "x.log")}),
        encoding="utf-8",
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
    (state_dir / f"{SLUG}.json").write_text(
        json.dumps({"slug": SLUG, "pid": proc.pid, "port": 1}), encoding="utf-8"
    )
    port = _free_port()
    try:
        assert preview_app.main(_start_args(repo, port)) == 0
        assert "serving at" in capsys.readouterr().out
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])


def test_stop_takes_the_whole_process_tree(tmp_path, monkeypatch, capsys):
    """streamlit.exe on Windows is a launcher whose child python.exe holds the
    port; stopping only the recorded PID would leave the server running."""
    repo = _repo(tmp_path)
    (tmp_path / "fake_streamlit_server.py").write_text(FAKE_SERVER, encoding="utf-8")
    _fake_launcher(tmp_path, FAKE_LAUNCHER_WITH_CHILD, monkeypatch)
    port = _free_port()
    try:
        assert preview_app.main(_start_args(repo, port)) == 0, capsys.readouterr().out
        child = int((tmp_path / "child.pid").read_text(encoding="utf-8"))
        assert preview_app._pid_alive(child)
        assert preview_app.main(["stop", SLUG, "--dir", str(repo)]) == 0
        deadline = time.monotonic() + 10
        while preview_app._pid_alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not preview_app._pid_alive(child), "the server child outlived stop"
        assert not preview_app._port_in_use(port)
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])


# --- the Windows (psutil) branches, exercised on every OS ------------------
def test_windows_liveness_and_command_line_via_psutil(monkeypatch):
    monkeypatch.setattr(preview_app, "_WINDOWS", True)
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert preview_app._pid_alive(sleeper.pid)
        assert "time.sleep(60)" in preview_app._process_command(sleeper.pid)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert not preview_app._pid_alive(sleeper.pid)
    assert preview_app._process_command(sleeper.pid) == ""


def test_windows_pid_alive_never_signals(monkeypatch):
    """On Windows os.kill(pid, 0) TERMINATES the process; liveness must not
    go anywhere near it."""
    monkeypatch.setattr(preview_app, "_WINDOWS", True)

    def _boom(*_args):
        raise AssertionError("os.kill called on the Windows path")

    monkeypatch.setattr(preview_app.os, "kill", _boom)
    assert preview_app._pid_alive(os.getpid())


def test_windows_tree_kill_takes_children(monkeypatch, tmp_path):
    monkeypatch.setattr(preview_app, "_WINDOWS", True)
    pidfile = tmp_path / "child.pid"
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time; "
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(pidfile)!r}, 'w').write(str(c.pid)); time.sleep(60)",
        ]
    )
    deadline = time.monotonic() + 10
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child = int(pidfile.read_text(encoding="utf-8") or 0)
    assert preview_app._kill(parent.pid, grace=5) is True
    parent.wait(timeout=10)
    deadline = time.monotonic() + 10
    while preview_app._pid_alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not preview_app._pid_alive(child)


def test_windows_owner_check_is_case_and_separator_blind(monkeypatch):
    monkeypatch.setattr(preview_app, "_WINDOWS", True)
    monkeypatch.setattr(preview_app, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        preview_app,
        "_process_command",
        lambda _pid: (
            "C:\\Repo\\.venv\\Scripts\\streamlit.exe run C:\\Repo\\apps\\x\\streamlit_app.py"
        ),
    )
    state = {"pid": 4242, "entrypoint": "c:/repo/apps/x/streamlit_app.py", "cmd": []}
    assert preview_app._state_owns_pid(state)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows creation flags")
def test_windows_launch_falls_back_when_the_job_forbids_breakaway(monkeypatch, tmp_path):
    calls = []
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        calls.append(kwargs["creationflags"])
        if kwargs["creationflags"] & subprocess.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Access is denied")
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(preview_app.subprocess, "Popen", fake_popen)
    with (tmp_path / "log").open("wb") as log_fh:
        proc = preview_app._launch_detached([sys.executable, "-c", "pass"], log_fh, tmp_path)
    proc.wait(timeout=30)
    assert len(calls) == 2
    assert not calls[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert calls[1] & subprocess.DETACHED_PROCESS


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


# The tail of a real preview log (streamlit 1.59.2, snowflake-connector-python 4.7.5) from a
# default snow connection that names its key-pair key `private_key_path`. The app served
# first; the error arrived with the first page load.
KEYPAIR_LOG = """\
  You can now view your Streamlit app in your browser.

  Local URL: http://localhost:8501

Traceback (most recent call last):
  File ".venv/lib/python3.11/site-packages/streamlit/connections/snowflake_connection.py", \
line 667, in _connect
    return snowflake.connector.connect()
  File ".venv/lib/python3.11/site-packages/snowflake/connector/auth/keypair.py", line 155, \
in prepare
    raise TypeError(
TypeError: Expected bytes, RSAPrivateKey, or EllipticCurvePrivateKey, got <class 'NoneType'>
"""


def test_classify_keypair_key_not_loaded():
    hit = preview_app.classify_log(
        "TypeError: Expected bytes, RSAPrivateKey, or EllipticCurvePrivateKey, "
        "got <class 'NoneType'>"
    )
    assert hit["status"] == "keypair_key_not_loaded"
    assert "private_key_path" in hit["hint"] and "private_key_file" in hit["hint"]
    assert "private_key_file_pwd" in hit["hint"]
    # Older connector wording.
    older = preview_app.classify_log(
        "TypeError: Expected bytes or RSAPrivateKey, got <class 'NoneType'>"
    )
    assert older["status"] == "keypair_key_not_loaded"
    # A launch log still reads as ready: classify_log answers "did it start".
    assert preview_app.classify_log(KEYPAIR_LOG)["status"] == "ready"
    # The failure classifier looks past the ready banner.
    assert preview_app.classify_failure(KEYPAIR_LOG)["status"] == "keypair_key_not_loaded"
    assert preview_app.classify_failure("You can now view your Streamlit app") is None


def test_logs_prints_the_remedy_for_a_first_page_load_failure(tmp_path, capsys):
    """start reports ready before any page runs, so a connection error raised by the
    first page load was only ever visible as a raw traceback in `preview logs`."""
    repo = _repo(tmp_path)
    log = repo / ".streamsnow" / "preview" / f"{SLUG}.log"
    log.parent.mkdir(parents=True)
    log.write_text(KEYPAIR_LOG, encoding="utf-8")
    assert preview_app.main(["logs", SLUG, "--dir", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "TypeError: Expected bytes" in out  # the raw tail is still printed
    assert "cause: keypair_key_not_loaded: " in out
    assert "Rename private_key_path to private_key_file" in out

    log.write_text("  You can now view your Streamlit app in your browser.\n", encoding="utf-8")
    assert preview_app.main(["logs", SLUG, "--dir", str(repo)]) == 0
    assert "cause:" not in capsys.readouterr().out


def test_logs_missing_file(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert preview_app.main(["logs", SLUG, "--dir", str(repo)]) == 1
    assert "no preview log" in capsys.readouterr().err


def test_probe_health_ignores_proxy_settings(tmp_path, monkeypatch):
    """A proxy in the environment (corporate machines, CI runners) must not
    capture the localhost probe; it once made a serving app read unhealthy."""
    monkeypatch.setenv("HTTP_PROXY", "http://10.255.255.1:9")
    monkeypatch.setenv("http_proxy", "http://10.255.255.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    port = _free_port()
    script = tmp_path / "fake_streamlit.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, str(script), str(port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    try:
        assert server.stdout is not None
        server.stdout.readline()  # the "You can now view" line: about to bind
        deadline = time.monotonic() + 10
        while not preview_app.probe_health(port, timeout=0.5):
            assert time.monotonic() < deadline, "probe never reached the local server"
            time.sleep(0.05)
    finally:
        server.kill()
        server.wait(timeout=10)


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


def test_build_command_prefers_repo_venv_streamlit(tmp_path):
    """The documented CLI-only setup (`uv venv && uv pip install -e apps/<slug>`)
    never activates the venv, so PATH `streamlit` is the wrong (or a missing)
    interpreter. The launcher must find the venv the user just built."""
    repo = _repo(tmp_path)
    entry = repo / "apps" / SLUG / "streamlit_app.py"
    assert preview_app.build_command(entry, 8501)[0] == "streamlit"
    fake = repo / ".venv" / "bin" / "streamlit"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    assert preview_app.build_command(entry, 8501)[0] == str(fake)
    # An app-local venv wins over nothing but loses to the repo venv.
    app_fake = repo / "apps" / SLUG / ".venv" / "bin" / "streamlit"
    app_fake.parent.mkdir(parents=True)
    app_fake.write_text("#!/bin/sh\n", encoding="utf-8")
    assert preview_app.build_command(entry, 8501)[0] == str(fake)
    fake.unlink()
    assert preview_app.build_command(entry, 8501)[0] == str(app_fake)


def test_missing_secrets_hint_names_both_connection_stores():
    hint = preview_app.classify_log("No secrets files found")["hint"]
    assert "connections.toml" in hint and "secrets.toml" in hint


# --------------------------------------------------------------------------- #
# 0.7.1: runtime-aware local install hint (warehouse apps have no pyproject)
# --------------------------------------------------------------------------- #
_ENV_YML = """\
name: sf_env
channels:
  - snowflake
dependencies:
  - streamlit=1.50.0
  - pandas
  - plotly>=5
  - snowflake-snowpark-python
"""


def test_local_install_command_container_app(tmp_path):
    repo = _repo(tmp_path)
    (repo / "apps" / SLUG / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.11,<3.12"\n', encoding="utf-8"
    )
    cmd = preview_app.local_install_command(repo / "apps" / SLUG)
    assert cmd == f"uv venv --python 3.11 && uv pip install -e apps/{SLUG}"


def test_local_install_command_warehouse_app(tmp_path):
    """`uv pip install -e apps/<slug>` fails on a warehouse app (no pyproject):
    the hint installs environment.yml's packages, conda pins translated to pip."""
    repo = _repo(tmp_path)
    (repo / "apps" / SLUG / "environment.yml").write_text(_ENV_YML, encoding="utf-8")
    cmd = preview_app.local_install_command(repo / "apps" / SLUG)
    assert "-e apps/" not in cmd
    assert cmd.startswith("uv venv --python 3.11 && uv pip install ")
    assert "'streamlit==1.50.0'" in cmd
    assert "'plotly>=5'" in cmd
    assert "snowflake-snowpark-python" in cmd and "pandas" in cmd


def test_start_warehouse_app_without_streamlit_prints_install_hint(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    (repo / "apps" / SLUG / "environment.yml").write_text(_ENV_YML, encoding="utf-8")
    monkeypatch.setattr(
        preview_app,
        "build_command",
        lambda entrypoint, port: [str(tmp_path / "no-such-streamlit"), str(port)],
    )
    assert preview_app.main(_start_args(repo, _free_port())) == 2
    out = capsys.readouterr().out
    assert "uv pip install" in out and "'streamlit==1.50.0'" in out
    assert "-e apps/" not in out


# --------------------------------------------------------------------------- #
# Review preview mode (--review-capture) and --port 0
# --------------------------------------------------------------------------- #
# Stand-in server that records the capture flag it was started with.
FAKE_SERVER_REPORTING_ENV = (
    "import os, pathlib, sys\n"
    "pathlib.Path(sys.argv[0]).with_name('env.txt').write_text("
    "os.environ.get('STREAMSNOW_REVIEW_CAPTURE', '<unset>'), encoding='utf-8')\n"
) + FAKE_SERVER

# Stand-in that loses its port once (another process took it), then serves.
FAKE_PORT_RACE = (
    "import pathlib, sys\n"
    "marker = pathlib.Path(sys.argv[0]).with_name('raced')\n"
    "if not marker.exists():\n"
    "    marker.write_text('1', encoding='utf-8')\n"
    "    print('Port ' + sys.argv[1] + ' is already in use', flush=True)\n"
    "    sys.exit(1)\n"
) + FAKE_SERVER


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_review_capture_reaches_the_child_and_nothing_else_does(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _fake_launcher(tmp_path, FAKE_SERVER_REPORTING_ENV, monkeypatch)
    monkeypatch.setenv("STREAMSNOW_REVIEW_CAPTURE", "/stray/from/the/shell")
    run_dir = ".streamsnow/sql-review/acme-sales-dashboard/20261005-120000-abc1234"
    try:
        args = _start_args(repo, 0) + ["--review-capture", f"{run_dir}/capture", "--json"]
        assert preview_app.main(args) == 0
        payload = _json_out(capsys)
        capture = (repo / run_dir / "capture").resolve()
        assert payload["review_capture"] == str(capture)
        assert capture.is_dir()
        # Inside the run directory the sql-review .gitignore covers it already.
        assert not (capture / ".gitignore").exists()
        assert (tmp_path / "env.txt").read_text(encoding="utf-8") == str(capture)
        state = json.loads(
            (repo / ".streamsnow/preview" / f"{SLUG}.json").read_text(encoding="utf-8")
        )
        assert state["review_capture"] == str(capture)
        assert preview_app.main(["status", SLUG, "--dir", str(repo), "--json"]) == 0
        assert _json_out(capsys)["review_capture"] == str(capture)
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])
    capsys.readouterr()

    # A normal preview never inherits a stray shell export.
    try:
        assert preview_app.main(_start_args(repo, 0) + ["--json"]) == 0
        assert _json_out(capsys)["review_capture"] is None
        assert (tmp_path / "env.txt").read_text(encoding="utf-8") == "<unset>"
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])


def test_review_capture_elsewhere_gets_its_own_gitignore(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _fake_launcher(tmp_path, FAKE_SERVER, monkeypatch)
    try:
        assert preview_app.main(_start_args(repo, 0) + ["--review-capture", "my capture"]) == 0
        ignore = repo / "my capture" / ".gitignore"
        assert ignore.read_text(encoding="utf-8").splitlines()[-1] == "*"
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])


def test_port_zero_picks_a_free_port(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _fake_launcher(tmp_path, FAKE_SERVER, monkeypatch)
    try:
        assert preview_app.main(_start_args(repo, 0) + ["--json"]) == 0
        payload = _json_out(capsys)
        port = payload["port"]
        assert port > 0 and payload["url"] == f"http://127.0.0.1:{port}"
        state = json.loads(
            (repo / ".streamsnow/preview" / f"{SLUG}.json").read_text(encoding="utf-8")
        )
        assert state["port"] == port
        assert preview_app.probe_health(port)
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])


def test_port_zero_retries_once_when_the_free_port_is_taken(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _fake_launcher(tmp_path, FAKE_PORT_RACE, monkeypatch)
    try:
        assert preview_app.main(_start_args(repo, 0) + ["--json"]) == 0
        assert _json_out(capsys)["status"] == "ready"
        assert (tmp_path / "raced").exists()
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])


def test_a_fixed_port_is_not_retried(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _fake_launcher(tmp_path, FAKE_PORT_RACE, monkeypatch)
    try:
        assert preview_app.main(_start_args(repo, _free_port())) == 1
        assert "port_in_use" in capsys.readouterr().out
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])


def test_running_preview_with_other_capture_is_a_mismatch(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _fake_launcher(tmp_path, FAKE_SERVER, monkeypatch)
    try:
        assert preview_app.main(_start_args(repo, 0)) == 0
        capsys.readouterr()
        rc = preview_app.main(_start_args(repo, 0) + ["--review-capture", "cap", "--json"])
        payload = _json_out(capsys)
        assert rc == 1 and payload["status"] == "capture_mismatch"
        assert payload["review_capture"] is None
        assert "preview stop" in payload["message"]
        # The same (absent) capture setting is the normal already-running report.
        assert preview_app.main(_start_args(repo, 0) + ["--json"]) == 0
        assert _json_out(capsys)["status"] == "already_running"
    finally:
        preview_app.main(["stop", SLUG, "--dir", str(repo)])


@pytest.mark.parametrize("port", ["-1", "65536"])
def test_port_out_of_range_is_a_tool_error(tmp_path, capsys, port):
    repo = _repo(tmp_path)
    args = _start_args(repo, 0)
    args[args.index("--port") + 1] = port
    assert preview_app.main(args) == 2
    assert "0-65535" in capsys.readouterr().out


def test_launch_passes_an_all_str_environment(monkeypatch, tmp_path):
    """Windows' CreateProcess refuses a non-str env value; POSIX would not notice."""
    seen = {}

    class Done:
        pid = 1

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        return Done()

    monkeypatch.setattr(preview_app.subprocess, "Popen", fake_popen)
    env = preview_app._child_env(tmp_path / "capture")
    with (tmp_path / "log").open("wb") as log_fh:
        preview_app._launch_detached([sys.executable, "-c", "pass"], log_fh, tmp_path, env)
    assert seen["env"]["STREAMSNOW_REVIEW_CAPTURE"] == str(tmp_path / "capture")
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in seen["env"].items())
