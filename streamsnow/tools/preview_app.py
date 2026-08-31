"""Background local-preview lifecycle for one StreamSnow app.

``streamlit run`` blocks its terminal, which makes it awkward for skills and
scripts that need to launch an app, walk its pages, and tear it down. This tool
owns the whole lifecycle so callers never hand-roll ``nohup``/PID bookkeeping:

    start <slug> [--port N] [--dir REPO] [--timeout SECS]
        Verify the entrypoint exists and the port is free, launch
        ``streamlit run`` detached with output captured to a log file, then
        poll ``http://127.0.0.1:<port>/_stcore/health`` until it answers 200
        or the timeout expires. On timeout (or early process death) the
        process is killed and the log tail is *classified* — the known launch
        failures (missing secrets.toml, bad account locator, missing package,
        port collision, session-outside-Snowflake) each map to an actionable
        hint instead of a raw traceback.

    status <slug> [--dir REPO]
        Running / not-running plus a live health probe. Stale state (the
        recorded PID is dead) is cleaned up silently — a crashed preview
        must not wedge the next ``start``.

    stop <slug> [--dir REPO]
        SIGTERM the recorded process (whole process group when possible),
        escalate to SIGKILL after a grace period, remove the state file.
        Idempotent: stopping a preview that isn't running succeeds.

    logs <slug> [--lines N] [--dir REPO]
        Tail the captured launch log (kept after ``stop`` for post-mortems).

State lives per-repo under ``<repo>/.streamsnow/preview/<slug>.json`` next to
``<slug>.log`` — no global state, nothing outside the repo. Recommend adding
``.streamsnow/`` to the repo's ``.gitignore`` (runtime artifacts, never
committed); this tool does not edit .gitignore itself.

The health endpoint (``/_stcore/health``) is Streamlit's own liveness probe
and returns before the app's first script run completes, so "ready" means
"serving", not "queries succeeded" — data errors surface in the browser and
in ``logs``.

Exit codes: ``start`` 0 = serving, 1 = launch failed (port busy, died, or
timed out — with a classified reason), 2 = tool error (missing entrypoint,
streamlit not installed). ``status`` 0 = running, 1 = not running.
``stop``/``logs`` 0 = done, 1 = problem, 2 = tool error.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_DEFAULT_PORT = 8501
_DEFAULT_TIMEOUT = 60.0
_STOP_GRACE_SECONDS = 5.0

# Known ``streamlit run`` launch failures → (status, pattern, actionable hint).
# Matched in order against the log tail; first hit wins.
_LOG_PATTERNS: list[tuple[str, str, str]] = [
    (
        "ready",
        r"You can now view your Streamlit app in your browser",
        "Streamlit launched and is serving",
    ),
    (
        "port_in_use",
        r"Port (\d+) is already in use",
        "Port is taken — stop the process holding it or retry with --port",
    ),
    (
        "missing_secrets",
        r"No secrets files found|st\.secrets has no key",
        "Local runs need .streamlit/secrets.toml (gitignored) with a [connections.snowflake] "
        "block — create it before previewing",
    ),
    (
        "bad_account",
        # The connector appends .snowflakecomputing.com itself; a full hostname
        # in secrets double-suffixes and 404s. 250001 is the connector's
        # can't-reach-account error code.
        r"\.snowflakecomputing\.com\.snowflakecomputing\.com|250001|"
        r"Failed to connect to DB.*Verify the connection|"
        r"could not be reached.*snowflakecomputing",
        "Snowflake account unreachable — use the bare account locator in secrets.toml "
        "(no https://, no .snowflakecomputing.com suffix) and check VPN/network",
    ),
    (
        "missing_package",
        r"ModuleNotFoundError: No module named '([^']+)'",
        "A dependency is missing from the local venv — add it to the app's manifest "
        "and re-sync the environment",
    ),
    (
        "session_outside_snowflake",
        # The exact error get_active_session raises outside Snowflake. Forgiving
        # substring so SnowparkSessionException wrapping variants still hit.
        r"get_active_session\(\) is not supported outside of Snowflake|"
        r"SnowparkSessionException.*active session",
        "Warehouse-mode app needs the local-parity fallback "
        "(try/except around get_active_session with an st.connection fallback)",
    ),
    (
        "connection_attr_missing",
        r"module 'streamlit' has no attribute 'connection'",
        "Streamlit < 1.22 in the venv — re-sync dependencies to pick up the pinned version",
    ),
]

_URL_RE = re.compile(r"Local URL:\s*(https?://\S+)")


# --------------------------------------------------------------------------- #
# Log classification (free functions so tests don't go through argparse)
# --------------------------------------------------------------------------- #
def classify_log(text: str) -> dict[str, Any]:
    """Classify ``streamlit run`` output into one of the known statuses.

    Returns ``{status, hint, excerpt, url}``; ``status`` is ``"unknown"`` when
    nothing matched — the caller should then show the raw log tail.
    """
    url_match = _URL_RE.search(text)
    url = url_match.group(1) if url_match else ""
    for status, pattern, hint in _LOG_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return {"status": status, "hint": hint, "excerpt": m.group(0), "url": url}
    return {
        "status": "unknown",
        "hint": "No known Streamlit launch pattern matched — inspect the log",
        "excerpt": "",
        "url": url,
    }


def tail_lines(path: Path, n: int) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]


# --------------------------------------------------------------------------- #
# Process / port / state helpers
# --------------------------------------------------------------------------- #
def build_command(entrypoint: Path, port: int) -> list[str]:
    """The launch argv. A module-level function so tests can substitute a fake
    server without touching a real Streamlit install."""
    return [
        "streamlit",
        "run",
        str(entrypoint),
        "--server.port",
        str(port),
        "--server.headless",
        "true",
    ]


def _state_dir(repo: Path) -> Path:
    return repo / ".streamsnow" / "preview"


def _state_path(repo: Path, slug: str) -> Path:
    return _state_dir(repo) / f"{slug}.json"


def _log_path(repo: Path, slug: str) -> Path:
    return _state_dir(repo) / f"{slug}.log"


def _read_state(repo: Path, slug: str) -> dict[str, Any] | None:
    path = _state_path(repo, slug)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    # When the caller is also the process that launched the preview (one
    # session doing start→stop), the dead child lingers as a zombie that
    # os.kill(pid, 0) still "sees". Reap it if it's ours; WNOHANG leaves a
    # live child untouched and a non-child raises ChildProcessError.
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def _port_in_use(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def probe_health(port: int, timeout: float = 1.0) -> bool:
    """One GET against Streamlit's liveness endpoint. 200 = serving."""
    url = f"http://127.0.0.1:{port}/_stcore/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost only
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _kill(pid: int, grace: float = _STOP_GRACE_SECONDS) -> bool:
    """SIGTERM (whole process group when the PID leads one), escalate to
    SIGKILL after ``grace`` seconds. Returns True when the process is gone."""

    def _signal(sig: int) -> None:
        try:
            os.killpg(pid, sig)  # start() launches with start_new_session=True
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(pid, sig)

    _signal(signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    _signal(signal.SIGKILL)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(payload.get("message", ""))


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_start(args: argparse.Namespace) -> int:
    repo = Path(args.dir).resolve()
    slug = args.slug
    entrypoint = repo / "apps" / slug / "streamlit_app.py"
    if not entrypoint.is_file():
        _emit({"status": "error", "message": f"error: {entrypoint} not found"}, args.json)
        return 2

    # A live previous preview is fine — report it instead of double-launching.
    state = _read_state(repo, slug)
    if state and _pid_alive(int(state.get("pid", 0))):
        port = int(state.get("port", 0))
        healthy = probe_health(port)
        _emit(
            {
                "status": "already_running",
                "pid": state["pid"],
                "port": port,
                "url": f"http://127.0.0.1:{port}",
                "healthy": healthy,
                "message": f"{slug} already running (pid {state['pid']}, port {port}, "
                f"{'healthy' if healthy else 'not yet healthy'})",
            },
            args.json,
        )
        return 0

    if _port_in_use(args.port):
        _emit(
            {
                "status": "port_in_use",
                "port": args.port,
                "message": f"error: port {args.port} is already in use — stop the process "
                f"holding it or pass --port with a free one",
            },
            args.json,
        )
        return 1

    state_dir = _state_dir(repo)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = _log_path(repo, slug)

    cmd = build_command(entrypoint, args.port)
    try:
        with log_path.open("wb") as log_fh:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=repo,
                start_new_session=True,  # detach: survives this CLI's exit; killable as a group
            )
    except FileNotFoundError:
        _emit(
            {
                "status": "error",
                "message": f"error: {cmd[0]!r} not found on PATH — install it in the local "
                "environment before previewing",
            },
            args.json,
        )
        return 2

    _state_path(repo, slug).write_text(
        json.dumps(
            {
                "slug": slug,
                "pid": proc.pid,
                "port": args.port,
                "log": str(log_path),
                "entrypoint": str(entrypoint),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    deadline = time.monotonic() + args.timeout
    died = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            died = True  # no point polling a corpse's port
            break
        if probe_health(args.port):
            _emit(
                {
                    "status": "ready",
                    "pid": proc.pid,
                    "port": args.port,
                    "url": f"http://127.0.0.1:{args.port}",
                    "log": str(log_path),
                    "message": f"{slug} serving at http://127.0.0.1:{args.port} "
                    f"(pid {proc.pid}, log {log_path})",
                },
                args.json,
            )
            return 0
        time.sleep(args.poll_interval)

    # Timed out or the process died: kill, clean state, classify the log tail.
    if not died:
        _kill(proc.pid)
    _state_path(repo, slug).unlink(missing_ok=True)
    tail = "\n".join(tail_lines(log_path, 100))
    classified = classify_log(tail)
    reason = "process exited during startup" if died else f"not healthy after {args.timeout:g}s"
    lines = [f"error: {slug} failed to start ({reason})"]
    if classified["status"] != "unknown":
        lines.append(f"cause: {classified['status']} — {classified['hint']}")
        if classified["excerpt"]:
            lines.append(f"log: {classified['excerpt']}")
    else:
        lines.append(f"{classified['hint']}: {log_path}")
    _emit(
        {
            "status": "failed",
            "reason": reason,
            "classification": classified,
            "log": str(log_path),
            "message": "\n".join(lines),
        },
        args.json,
    )
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.dir).resolve()
    slug = args.slug
    state = _read_state(repo, slug)
    if state is None:
        _emit({"status": "not_running", "message": f"{slug}: not running"}, args.json)
        return 1
    pid = int(state.get("pid", 0))
    port = int(state.get("port", 0))
    if not _pid_alive(pid):
        # Stale state from a crashed/killed preview — clean it up, not an error.
        _state_path(repo, slug).unlink(missing_ok=True)
        _emit(
            {
                "status": "not_running",
                "stale_state_cleaned": True,
                "message": f"{slug}: not running (stale state for dead pid {pid} cleaned up)",
            },
            args.json,
        )
        return 1
    healthy = probe_health(port)
    _emit(
        {
            "status": "running",
            "pid": pid,
            "port": port,
            "url": f"http://127.0.0.1:{port}",
            "healthy": healthy,
            "log": state.get("log", ""),
            "message": f"{slug}: running (pid {pid}, port {port}, "
            f"{'healthy' if healthy else 'health probe failed'})",
        },
        args.json,
    )
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    repo = Path(args.dir).resolve()
    slug = args.slug
    state = _read_state(repo, slug)
    if state is None:
        _emit({"status": "not_running", "message": f"{slug}: nothing to stop"}, args.json)
        return 0
    pid = int(state.get("pid", 0))
    if _pid_alive(pid) and not _kill(pid):
        _emit(
            {
                "status": "error",
                "message": f"error: pid {pid} survived SIGTERM and SIGKILL — kill it manually",
            },
            args.json,
        )
        return 1
    _state_path(repo, slug).unlink(missing_ok=True)
    # The log file is kept on purpose: post-mortems outlive the process.
    _emit({"status": "stopped", "pid": pid, "message": f"{slug}: stopped (pid {pid})"}, args.json)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    repo = Path(args.dir).resolve()
    state = _read_state(repo, args.slug)
    # Fall back to the conventional location so logs work after stop/crash.
    log_path = Path(state["log"]) if state and state.get("log") else _log_path(repo, args.slug)
    if not log_path.is_file():
        print(f"error: no preview log at {log_path}", file=sys.stderr)
        return 1
    for line in tail_lines(log_path, args.lines):
        print(line)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="preview_app",
        description="Background local-preview lifecycle for one app.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("slug", help="App slug (directory name under apps/).")
        p.add_argument("--dir", default=".", help="Repo root (default: cwd).")
        p.add_argument("--json", action="store_true", help="Emit structured JSON.")

    p = sub.add_parser("start", help="launch streamlit detached and wait for health")
    common(p)
    p.add_argument("--port", type=int, default=_DEFAULT_PORT)
    p.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT,
        help=f"Seconds to wait for the health endpoint (default {_DEFAULT_TIMEOUT:g}).",
    )
    p.add_argument("--poll-interval", type=float, default=0.5, help=argparse.SUPPRESS)

    p = sub.add_parser("status", help="running/not-running + health probe")
    common(p)

    p = sub.add_parser("stop", help="kill the preview process and clear state")
    common(p)

    p = sub.add_parser("logs", help="tail the preview log")
    common(p)
    p.add_argument("--lines", type=int, default=50)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    dispatch = {"start": cmd_start, "status": cmd_status, "stop": cmd_stop, "logs": cmd_logs}
    return dispatch[args.cmd](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
