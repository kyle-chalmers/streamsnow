"""Background local-preview lifecycle for one StreamSnow app.

``streamlit run`` blocks its terminal, which makes it awkward for skills and
scripts that need to launch an app, walk its pages, and tear it down. This tool
owns the whole lifecycle so callers never hand-roll ``nohup``/PID bookkeeping:

    start <slug> [--port N] [--review-capture DIR] [--dir REPO] [--timeout SECS]
        Verify the entrypoint exists and the port is free (``--port 0`` picks
        any free one), launch
        ``streamlit run`` detached with output captured to a log file, then
        poll ``http://127.0.0.1:<port>/_stcore/health`` until it answers 200
        or the timeout expires. On timeout (or early process death) the
        process is killed and the log tail is *classified* — the known launch
        failures (missing secrets.toml, bad account locator, missing package,
        port collision, session-outside-Snowflake) each map to an actionable
        hint instead of a raw traceback. A key-pair connection whose key the
        Python connector cannot load is classified too, but it only fails on
        the first page load, so it surfaces through ``logs``.

        ``--review-capture DIR`` is review preview mode for ``/sql-review``:
        the app's ``review_value`` calls record what each visual received
        (aggregates, never rows) as JSON in DIR, which ``streamsnow sql-review
        compare`` holds against the reviewed SQL. The flag reaches only this
        child process; a normal preview strips it, so a stray shell export
        never turns capture on. A preview already running with a different
        capture setting is reported (``capture_mismatch``), never reused.

    status <slug> [--dir REPO]
        Running / not-running plus a live health probe. Stale state (the
        recorded PID is dead) is cleaned up silently — a crashed preview
        must not wedge the next ``start``.

    stop <slug> [--dir REPO]
        SIGTERM the recorded process (whole process group when possible),
        escalate to SIGKILL after a grace period, remove the state file.
        Idempotent: stopping a preview that isn't running succeeds.

    logs <slug> [--lines N] [--dir REPO]
        Tail the captured launch log (kept after ``stop`` for post-mortems),
        then classify it. Some failures only happen once a browser opens the
        page (a connection opened by the first script run), after ``start``
        already reported ready, so ``logs`` is where their hint surfaces.

Native Windows has no process groups, signals or ``ps``, so the process
control there goes through psutil (a Windows-only dependency), for three
reasons. ``os.kill(pid, 0)``, the POSIX liveness probe, TERMINATES the process
on Windows instead of probing it. ``streamlit.exe`` is a launcher that starts a
separate ``python.exe``, so stop must take the whole tree, not the PID it
recorded. And reading another process's command line (the PID-reuse guard
below) has no stdlib route there. The preview launches detached in a new
process group and first tries to break away from any job object it was started
in: a tool call that runs inside a kill-on-close job would otherwise take the
preview down with it the moment the call returns. macOS and Linux keep the
original POSIX path unchanged.

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
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_WINDOWS = sys.platform == "win32"
_DEFAULT_PORT = 8501
#: Read once by the scaffolded apps/<slug>/review.py; set only here.
REVIEW_CAPTURE_ENV = "STREAMSNOW_REVIEW_CAPTURE"
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
        "Local runs need a Snowflake connection: either a default `snow connection add "
        "... --default` (read from connections.toml) or apps/<slug>/.streamlit/secrets.toml "
        "(gitignored) with a [connections.snowflake] block — set one up before previewing",
    ),
    (
        "keypair_key_not_loaded",
        # Raised by snowflake-connector-python's AuthByKeyPair.prepare when a
        # SNOWFLAKE_JWT connection reaches it with no key. The usual cause: the
        # default snow connection names its key `private_key_path`, a snow-CLI
        # alias the connector silently drops. `snow sql` works on the very same
        # connection, which is what makes this one confusing. Older connectors
        # word it "Expected bytes or RSAPrivateKey".
        r"Expected bytes,? (?:or )?RSAPrivateKey[^\n]*got <class 'NoneType'>",
        "Key-pair connection with no key the Python connector can load: st.connection"
        "('snowflake') reads your default snow connection through snowflake-connector-python, "
        "which ignores `private_key_path` (a snow-CLI-only alias). Rename private_key_path to "
        "private_key_file in that connection's entry (a passphrase goes in private_key_file_pwd); "
        "snow reads private_key_file too. `streamsnow doctor` flags this as snow-key-file",
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

# Slugs land in state/log file paths — reject anything that could traverse.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _validate_slug(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug))


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


def classify_failure(text: str) -> dict[str, Any] | None:
    """The first known FAILURE in ``text``, ignoring the "ready" banner.

    ``classify_log`` answers "did the launch succeed", so the ready banner wins
    there. A log read after launch usually has that banner at the top and the
    real problem (raised on the first page load) below it, so ``logs`` needs
    the failure patterns alone. None when nothing known matched.
    """
    for status, pattern, hint in _LOG_PATTERNS:
        if status == "ready":
            continue
        m = re.search(pattern, text)
        if m:
            return {"status": status, "hint": hint, "excerpt": m.group(0)}
    return None


def tail_lines(path: Path, n: int) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]


# --------------------------------------------------------------------------- #
# Process / port / state helpers
# --------------------------------------------------------------------------- #
def streamlit_executable(entrypoint: Path) -> str:
    """The ``streamlit`` to launch: a repo- or app-local ``.venv`` first, then PATH.

    The documented CLI-only setup is ``uv venv && uv pip install -e apps/<slug>``,
    which creates ``.venv`` without activating it — so a bare ``streamlit`` on
    PATH is either missing or some other interpreter's. Prefer the venv the
    user just built (repo root, then the app dir), falling back to PATH so an
    activated environment or a tool install still works.
    """
    app_dir = entrypoint.parent
    repo = app_dir.parent.parent
    for root in (repo, app_dir):
        for candidate in (
            root / ".venv" / "bin" / "streamlit",
            root / ".venv" / "Scripts" / "streamlit.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return "streamlit"


# Container apps are Python 3.11 only; the warehouse runtime tops out at 3.11
# too, and a newer default interpreter can outrun snowflake-snowpark-python.
_LOCAL_PYTHON = "3.11"
_REQUIRES_PY_FLOOR = re.compile(r">=\s*(3\.\d+)")
_CONDA_PIN = re.compile(r"^([A-Za-z0-9_.\-]+)\s*=\s*([^=<>!~\s]+)$")


def _pip_spec(conda_dep: str) -> str:
    """Translate one environment.yml dependency into a pip requirement.

    Conda pins with a single ``=`` (``streamlit=1.50.0``) become ``==``; other
    operators pass through. Quoted when it carries an operator, for the shell.
    """
    dep = conda_dep.strip()
    m = _CONDA_PIN.match(dep)
    if m:
        dep = f"{m.group(1)}=={m.group(2)}"
    return f"'{dep}'" if re.search(r"[<>=!~]", dep) else dep


def local_install_command(app_dir: Path) -> str:
    """The one-line local environment setup for an app, matched to its runtime.

    Container apps ship a ``pyproject.toml`` and install editable. Warehouse
    apps ship only a conda ``environment.yml`` (Snowflake's Anaconda channel),
    so the documented ``uv pip install -e apps/<slug>`` fails on them with no
    project file to build; install the listed packages instead.
    """
    rel = f"apps/{app_dir.name}"
    pyproject = app_dir / "pyproject.toml"
    if pyproject.is_file():
        py = _LOCAL_PYTHON
        try:
            spec = (
                tomllib.loads(pyproject.read_text(encoding="utf-8"))
                .get("project", {})
                .get("requires-python", "")
            )
            m = _REQUIRES_PY_FLOOR.search(str(spec))
            if m:
                py = m.group(1)
        except (tomllib.TOMLDecodeError, OSError):
            pass
        return f"uv venv --python {py} && uv pip install -e {rel}"
    env = app_dir / "environment.yml"
    if env.is_file():
        import yaml

        try:
            data = yaml.safe_load(env.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            data = {}
        deps = [
            _pip_spec(d)
            for d in (data.get("dependencies") or [])
            # The runtime supplies Python; a `python` line is not a package.
            if isinstance(d, str) and re.split(r"[\s=<>!~]", d.strip(), maxsplit=1)[0] != "python"
        ]
        if deps:
            return f"uv venv --python {_LOCAL_PYTHON} && uv pip install {' '.join(deps)}"
    return f"uv venv --python {_LOCAL_PYTHON} && uv pip install streamlit (plus the app's deps)"


def build_command(entrypoint: Path, port: int) -> list[str]:
    """The launch argv. A module-level function so tests can substitute a fake
    server without touching a real Streamlit install."""
    return [
        streamlit_executable(entrypoint),
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
    if _WINDOWS:
        import psutil

        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:  # exists, owned by someone else
            return True
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


def _process_command(pid: int) -> str:
    """The live command line of ``pid`` (``ps`` on POSIX, psutil on Windows), or ""
    when unknown."""
    if _WINDOWS:
        import psutil

        try:
            return subprocess.list2cmdline(psutil.Process(pid).cmdline())
        except psutil.Error:
            return ""
    proc = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _state_owns_pid(state: dict[str, Any]) -> bool:
    """True when the state file's PID is alive AND still looks like OUR process.

    PIDs are reused: a state file left behind by a crash (or written by
    something else entirely) can name a live PID that belongs to an unrelated
    process, and ``stop`` would TERM/KILL it. Before trusting a PID, verify
    the live command line still carries a distinctive token of the command we
    recorded at launch (the entrypoint path in practice). When the command
    line cannot be read, or matches nothing we recorded, the state is treated
    as stale — refusing to signal an unverified PID is the safe direction.
    """
    pid = int(state.get("pid", 0))
    if not _pid_alive(pid):
        return False
    # cmd[0] is the launcher (the venv's streamlit), which every app previewed
    # from the same venv shares, so it identifies the tool, not this app.
    recorded: list[str] = [str(t) for t in (state.get("cmd") or [])[1:]]
    if entry := state.get("entrypoint"):
        recorded.append(str(entry))
    # Identity tokens are the PATH-bearing arguments (the entrypoint / script
    # path) — those differ per launch. Generic tokens must never count:
    # matching on e.g. a shared launcher flag would claim ownership of a PID
    # reused by a DIFFERENT app's preview started by this same tool, which is
    # the most plausible reuse collision of all.
    tokens = [t for t in recorded if "/" in t or "\\" in t]
    if not tokens:
        # Nothing distinctive was recorded (hand-written state) — do not
        # claim ownership of an arbitrary PID.
        return False
    live = _process_command(pid)
    if _WINDOWS:  # case-blind paths, either separator
        live = live.lower().replace("/", "\\")
        tokens = [t.lower().replace("/", "\\") for t in tokens]
    return bool(live) and any(t in live for t in tokens)


def _port_in_use(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.2)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


#: Never route the localhost probe through a proxy. ``urlopen`` honors
#: HTTP_PROXY and, on macOS and Windows, the system proxy settings; behind a
#: proxy that does not exempt 127.0.0.1 the probe went to the proxy and
#: ``start`` reported a serving app as "not healthy" after the full timeout.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe_health(port: int, timeout: float = 1.0) -> bool:
    """One GET against Streamlit's liveness endpoint. 200 = serving."""
    url = f"http://127.0.0.1:{port}/_stcore/health"
    try:
        with _DIRECT_OPENER.open(url, timeout=timeout) as resp:  # noqa: S310 - localhost only
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _kill_tree_windows(pid: int, grace: float) -> bool:
    """Terminate ``pid`` and every descendant, then kill what survives ``grace``.

    Windows has no SIGTERM a detached console process can catch, so terminate is
    the honest equivalent. The tree matters: streamlit.exe is a launcher whose
    child python.exe is the actual server holding the port.
    """
    import psutil

    try:
        parent = psutil.Process(pid)
        procs = [*parent.children(recursive=True), parent]
    except psutil.NoSuchProcess:
        return True
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.terminate()
    _, alive = psutil.wait_procs(procs, timeout=grace)
    for p in alive:
        with contextlib.suppress(psutil.Error):
            p.kill()
    psutil.wait_procs(alive, timeout=grace)
    return not _pid_alive(pid)


def _kill(pid: int, grace: float = _STOP_GRACE_SECONDS) -> bool:
    """SIGTERM (whole process group when the PID leads one), escalate to
    SIGKILL after ``grace`` seconds. Returns True when the process is gone."""
    if _WINDOWS:
        return _kill_tree_windows(pid, grace)

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


def _launch_detached(
    cmd: list[str], log_fh: Any, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.Popen:
    """Start the preview so it outlives this CLI and can be stopped as a unit."""
    common: dict[str, Any] = {
        "stdout": log_fh,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": cwd,
        "env": env,
    }
    if not _WINDOWS:
        # detach: survives this CLI's exit; killable as a group
        return subprocess.Popen(cmd, start_new_session=True, **common)  # noqa: S603
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        # Leave the caller's job object, if it allows that, so a tool call
        # running in a kill-on-close job does not take the preview with it.
        return subprocess.Popen(  # noqa: S603
            cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common
        )
    except PermissionError:  # the job forbids breakaway
        return subprocess.Popen(cmd, creationflags=flags, **common)  # noqa: S603


def _free_port() -> int:
    """A port nothing listens on right now (``--port 0``)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _capture_dir(repo: Path, raw: str | None) -> Path | None:
    """``--review-capture``: absolute, relative to the repo, created, git-ignored.

    Inside the run directory the sql-review ``.gitignore`` already applies; a
    folder anywhere else gets its own, since captures are review evidence.
    """
    if raw is None:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = repo / path
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    runs = (repo / ".streamsnow" / "sql-review").resolve()
    if not path.is_relative_to(runs) and not (path / ".gitignore").exists():
        (path / ".gitignore").write_text(
            "# streamsnow review capture: aggregates only, never committed.\n*\n",
            encoding="utf-8",
            newline="\n",
        )
    return path


def _same_capture(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a is b
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _child_env(capture: Path | None) -> dict[str, str]:
    """The child's environment, always explicit and all ``str`` (Windows refuses
    a Path): capture on only when asked for, never inherited from the shell."""
    env = {k: v for k, v in os.environ.items() if k != REVIEW_CAPTURE_ENV}
    if capture is not None:
        env[REVIEW_CAPTURE_ENV] = str(capture)
    return env


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
    if not 0 <= args.port <= 65535:
        _emit(
            {"status": "error", "message": f"error: --port {args.port} is not 0-65535"}, args.json
        )
        return 2
    capture = _capture_dir(repo, args.review_capture)
    wanted = str(capture) if capture else None

    # A live previous preview is fine — report it instead of double-launching,
    # unless it captures differently than asked (review mode must not silently
    # reuse a preview that records nothing, or one that records elsewhere).
    state = _read_state(repo, slug)
    if state and _state_owns_pid(state):
        port = int(state.get("port", 0))
        running = state.get("review_capture")
        if not _same_capture(running, wanted):
            _emit(
                {
                    "status": "capture_mismatch",
                    "pid": state["pid"],
                    "port": port,
                    "review_capture": running,
                    "requested_capture": wanted,
                    "message": f"error: {slug} is already running (pid {state['pid']}, port "
                    f"{port}) with review capture {running or 'off'}, not "
                    f"{wanted or 'off'}; run `streamsnow preview stop {slug}` first",
                },
                args.json,
            )
            return 1
        healthy = probe_health(port)
        _emit(
            {
                "status": "already_running",
                "pid": state["pid"],
                "port": port,
                "url": f"http://127.0.0.1:{port}",
                "healthy": healthy,
                "review_capture": running,
                "message": f"{slug} already running (pid {state['pid']}, port {port}, "
                f"{'healthy' if healthy else 'not yet healthy'})",
            },
            args.json,
        )
        return 0

    port = _free_port() if args.port == 0 else args.port
    if _port_in_use(port):
        _emit(
            {
                "status": "port_in_use",
                "port": port,
                "message": f"error: port {port} is already in use — stop the process "
                f"holding it or pass --port with a free one (0 picks one)",
            },
            args.json,
        )
        return 1

    state_dir = _state_dir(repo)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = _log_path(repo, slug)
    # --port 0 can lose the free port to another process before Streamlit binds
    # it; one retry on a fresh port covers that race.
    attempts = 2 if args.port == 0 else 1
    for attempt in range(attempts):
        retry_port = attempt < attempts - 1
        outcome = _launch_and_wait(args, repo, entrypoint, port, log_path, capture, retry_port)
        if outcome is None:
            return 0
        if outcome == "tool_error":
            return 2
        if outcome != "port_in_use" or not retry_port:
            return 1
        port = _free_port()
    return 1


def _launch_and_wait(
    args: argparse.Namespace,
    repo: Path,
    entrypoint: Path,
    port: int,
    log_path: Path,
    capture: Path | None,
    retry_port: bool,
) -> str | None:
    """Launch on ``port`` and wait for health. None when serving; otherwise the
    failure's classification (reported, except a ``port_in_use`` the caller
    will retry) or ``tool_error``."""
    slug = args.slug
    cmd = build_command(entrypoint, port)
    try:
        with log_path.open("wb") as log_fh:
            proc = _launch_detached(cmd, log_fh, repo, _child_env(capture))
    except FileNotFoundError:
        _emit(
            {
                "status": "error",
                "message": f"error: {cmd[0]!r} not found on PATH. Install the app's "
                "environment before previewing:\n  "
                f"{local_install_command(entrypoint.parent)}",
                "install": local_install_command(entrypoint.parent),
            },
            args.json,
        )
        return "tool_error"

    _state_path(repo, slug).write_text(
        json.dumps(
            {
                "slug": slug,
                "pid": proc.pid,
                "port": port,
                "log": str(log_path),
                "entrypoint": str(entrypoint),
                # Recorded so stop/status can verify the PID still belongs to
                # this launch before signaling it (PID reuse — _state_owns_pid).
                "cmd": cmd,
                "review_capture": str(capture) if capture else None,
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
        if probe_health(port):
            _emit(
                {
                    "status": "ready",
                    "pid": proc.pid,
                    "port": port,
                    "url": f"http://127.0.0.1:{port}",
                    "log": str(log_path),
                    "review_capture": str(capture) if capture else None,
                    "message": f"{slug} serving at http://127.0.0.1:{port} "
                    f"(pid {proc.pid}, log {log_path})"
                    + (f", capturing review values to {capture}" if capture else ""),
                },
                args.json,
            )
            return None
        time.sleep(args.poll_interval)

    # Timed out or the process died: kill, clean state, classify the log tail.
    if not died:
        _kill(proc.pid)
    _state_path(repo, slug).unlink(missing_ok=True)
    tail = "\n".join(tail_lines(log_path, 100))
    classified = classify_log(tail)
    if retry_port and classified["status"] == "port_in_use":
        return "port_in_use"
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
    return classified["status"]


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.dir).resolve()
    slug = args.slug
    state = _read_state(repo, slug)
    if state is None:
        _emit({"status": "not_running", "message": f"{slug}: not running"}, args.json)
        return 1
    pid = int(state.get("pid", 0))
    port = int(state.get("port", 0))
    if not _state_owns_pid(state):
        # Stale state (dead PID, or a reused PID that is no longer our
        # process) — clean it up, not an error.
        _state_path(repo, slug).unlink(missing_ok=True)
        _emit(
            {
                "status": "not_running",
                "stale_state_cleaned": True,
                "message": f"{slug}: not running (stale state for pid {pid} cleaned up)",
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
            "review_capture": state.get("review_capture"),
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
    if _pid_alive(pid) and not _state_owns_pid(state):
        # A live PID that is no longer (verifiably) our launch: signaling it
        # could kill an unrelated process that inherited the PID. Drop the
        # stale state and refuse.
        _state_path(repo, slug).unlink(missing_ok=True)
        _emit(
            {
                "status": "stale_state",
                "pid": pid,
                "message": f"{slug}: state file named pid {pid}, but that process is not "
                "this preview (PID reuse) — state cleaned, nothing signaled",
            },
            args.json,
        )
        return 0
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
    lines = tail_lines(log_path, args.lines)
    for line in lines:
        print(line)
    failure = classify_failure("\n".join(lines))
    if failure:
        print(f"\ncause: {failure['status']}: {failure['hint']}")
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
    p.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help=f"Port to serve on (default {_DEFAULT_PORT}; 0 picks any free port).",
    )
    p.add_argument(
        "--review-capture",
        default=None,
        metavar="DIR",
        help="Review preview mode for /sql-review: review_value records what each visual "
        "received (aggregates only) in DIR, relative to --dir.",
    )
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
    if not _validate_slug(args.slug):
        print(
            f"error: app slug {args.slug!r} must be kebab-case (^[a-z][a-z0-9-]*$)",
            file=sys.stderr,
        )
        return 2
    dispatch = {"start": cmd_start, "status": cmd_status, "stop": cmd_stop, "logs": cmd_logs}
    return dispatch[args.cmd](args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
