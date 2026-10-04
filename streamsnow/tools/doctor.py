"""Environment doctor — per-check prerequisite detection for StreamSnow.

The CLI's original ``doctor`` printed a flat pass/fail transcript, which is
fine for a human at a terminal but opaque to everything else: ``/start-app``'s
preflight, CI bootstrap steps, and "fix then re-check" loops all need to know
*which* prerequisite failed and what to do about it, without scraping console
text. This module restructures the same coverage into per-check subresults so
those callers get machine-readable state and the CLI keeps a human rendering.

Contract — every check returns one dict::

    {"name": str, "ok": bool, "level": "required" | "optional",
     "detail": {...}, "hint": str}

``level`` is per-result, not per-check: the config check is *optional* when no
``streamsnow.config.yaml`` exists (a machine can be healthy outside any repo)
but *required* when one exists and fails validation — a malformed config must
never be masked as "not configured yet".

Checks: Python >= 3.11 (the running interpreter — the one that would run the
tools), ``git`` and ``uv`` on PATH (required), ``snow`` (optional when absent,
but a ``snow`` on PATH must answer ``snow --version``: a Homebrew install that
crashed on import used to report ``ok`` because only PATH presence was
checked, so a broken one is a required failure; one that does not answer
within the timeout is a warning to re-run, since a cold start alone can take
25 s), ``streamlit`` on PATH
(optional), ``gh`` (optional; ``/ship-app`` needs it), ``pre-commit`` on PATH (optional
outside a repo, *required* once a ``streamsnow.config.yaml`` exists — the
generated hooks are ``language: system`` and a scaffolded repo's first commit
fails without the executable), config presence + validity, and — only when
both a config and ``snow`` exist — whether the configured ``snow`` connection
name has been created (``snow connection list``; never ``connection test``,
which can open a browser). The connection check is what turns "preview can't
connect", the most common first-run failure, into a named result. In a
container-runtime repo, a Python 3.11 interpreter must be findable (the apps
pin ``>=3.11,<3.12``, so a machine with only 3.12 passed the old doctor and
then failed ``uv pip install -e``); a miss is a warning with the fix.

``snow-key-file`` reads the *default* ``snow`` connection, the one
``st.connection("snowflake")`` opens locally when an app has no
``secrets.toml``. Key-pair auth with no ``private_key_file`` listed means the
key is named some way only the ``snow`` CLI understands (in practice the legacy
``private_key_path`` alias, which ``snow connection list`` never prints): ``snow
sql`` works, and the first page load of a local preview dies inside the Python
connector with ``TypeError: Expected bytes, RSAPrivateKey, ... got NoneType``.
The check reads parameter NAMES only, never values, and never edits the
connection; the hint names the rename that works for both tools.

Onboarding checks (0.7.6), each closing a way a new user or a teammate cloning
a configured repo could be told "ok" while governance was silently off:
``repo-files`` (required once a config exists: ``configure`` alone writes the
config but no hooks, CI or ``.gitignore``, so config presence never means
"onboarded"); ``git-identity`` (required once a config exists; reads whether
``user.name``/``user.email`` are set and never records their values);
``pre-commit-hook`` (required once a config exists: ``pre-commit install`` is
per clone, and a teammate's commits skip every governance check until it runs;
the hook must be pre-commit's own, not just any file at that path); ``node``
(optional warning: the bundled Playwright MCP runs through ``npx``, and without
it every UI walkthrough is skipped); ``ci-secrets`` (optional: the deploy
workflow does nothing until its ``SNOWFLAKE_*`` secrets exist; reads secret
NAMES via ``gh``, and a listing that fails for lack of access says "not
checked", never "missing"). On native Windows only, a ``platform`` warning says
StreamSnow runs inside WSL today.

Detection only: no prompts, no fix execution — hints name the fix, callers own
the UX. Checks never raise; an unexpected error inside the doctor itself is a
tool error.

Exit codes: 0 = every required check passes, 1 = a required prerequisite is
missing or broken, 2 = the doctor itself failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import ConfigError, find_config, load_config
from ..scaffolder import missing_repo_files

REQUIRED = "required"
OPTIONAL = "optional"

_MIN_PYTHON = (3, 11)

# name -> (level, hint when missing)
_PATH_TOOLS: tuple[tuple[str, str, str], ...] = (
    ("git", REQUIRED, "install git"),
    ("uv", REQUIRED, "install uv — https://docs.astral.sh/uv/"),
    (
        "streamlit",
        OPTIONAL,
        "uv pip install streamlit (in your app environment, for local preview)",
    ),
    (
        "gh",
        OPTIONAL,
        "install the GitHub CLI (brew install gh, then gh auth login); /ship-app requires it",
    ),
)
_SNOW_HINT = "uv tool install snowflake-cli (for preview/deploy diagnostics)"
_SNOW_BROKEN_HINT = (
    "snow is on PATH but `snow --version` fails: reinstall it with "
    "`uv tool install snowflake-cli` (a Homebrew snow can break on a newer system Python)"
)
_PRE_COMMIT_HINT = "uv tool install pre-commit, then `pre-commit install` in the repo"
# `snow` probes (`--version`, `connection list`). The first `snow --version`
# after a Mac sat idle took 24.8 s of wall time on about 1 s of CPU (observed
# 2026-09-27; 5.9 s and 3.5 s warm), so the 15 s timeout of 0.7.1 reported a
# healthy snow as BROKEN. At 5 s, before that, the first run reported "no
# connections". 45 s absorbs the cold start with room to spare.
_SNOW_TIMEOUT_S = 45.0
# Every other probe (`uv python find`) is local and fast.
_PROBE_TIMEOUT_S = 15.0
_TIMEOUT_CODE = 124
_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


def _result(name: str, ok: bool, level: str, detail: dict, hint: str = "") -> dict:
    return {"name": name, "ok": ok, "level": level, "detail": detail, "hint": hint}


def check_python(minimum: tuple[int, int] = _MIN_PYTHON) -> dict:
    """The *running* interpreter, matching the CLI: it is the one that executes
    the governance tools, so probing some other ``python3`` on PATH would pass
    a machine that still fails in practice."""
    version = (sys.version_info.major, sys.version_info.minor)
    ok = version >= minimum
    return _result(
        "python",
        ok,
        REQUIRED,
        {"version": f"{version[0]}.{version[1]}", "min": f"{minimum[0]}.{minimum[1]}"},
        "" if ok else f"need Python >= {minimum[0]}.{minimum[1]}",
    )


def check_path_tool(name: str, level: str, hint: str) -> dict:
    path = shutil.which(name)
    return _result(
        name,
        path is not None,
        level,
        {"found": path is not None, "path": path or ""},
        "" if path else hint,
    )


def check_pre_commit(config_present: bool) -> dict:
    """``pre-commit`` on PATH. Optional on a bare machine; required in a
    configured repo, whose generated ``.pre-commit-config.yaml`` hooks all run
    ``streamsnow check …`` via ``language: system``."""
    res = check_path_tool("pre-commit", REQUIRED if config_present else OPTIONAL, _PRE_COMMIT_HINT)
    return res


def _run(
    cmd: list[str], timeout: float = _PROBE_TIMEOUT_S, cwd: Path | None = None
) -> tuple[int, str]:
    """Run a short diagnostic command; never raises (124 on timeout, 127 on any
    other failure to run, with empty output)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd
        )
        return proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired:
        return _TIMEOUT_CODE, ""
    except (OSError, ValueError):
        return 127, ""


def _snow_slow_hint(what: str) -> str:
    return (
        f"{what} did not answer within {_SNOW_TIMEOUT_S:.0f}s (a cold start can be slow); "
        "re-run streamsnow doctor"
    )


def check_snow() -> dict:
    """``snow`` on PATH *and* answering ``snow --version``.

    Absent is an optional miss. Present but crashing is a required failure:
    every preview/deploy diagnostic and skill that shells to ``snow`` would fail
    with a traceback instead of a hint. Present but silent past the timeout is
    a warning, not BROKEN: a healthy ``snow`` on a cold machine is that slow,
    and reinstalling it would not help.
    """
    path = shutil.which("snow")
    if path is None:
        return _result("snow", False, OPTIONAL, {"found": False, "path": ""}, _SNOW_HINT)
    code, out = _run(["snow", "--version"], timeout=_SNOW_TIMEOUT_S)
    if code == _TIMEOUT_CODE:
        return _result(
            "snow",
            False,
            OPTIONAL,
            {"found": True, "path": path, "timed_out": True, "warn": True},
            _snow_slow_hint("snow --version"),
        )
    if code != 0:
        return _result(
            "snow",
            False,
            REQUIRED,
            {"found": True, "path": path, "broken": True, "exit_code": code},
            _SNOW_BROKEN_HINT,
        )
    m = _VERSION_RE.search(out)
    return _result(
        "snow",
        True,
        OPTIONAL,
        {"found": True, "path": path, "version": m.group(1) if m else ""},
    )


def check_container_python(cfg_result: dict) -> dict:
    """In a container-runtime repo: is a Python matching the app pin findable?

    Container apps pin ``>=3.11,<3.12`` (Snowflake's container runtime is 3.11
    only). A warning, never a gate: ``uv venv --python 3.11`` can download one.
    """
    detail = cfg_result.get("detail", {})
    if not cfg_result.get("ok") or detail.get("runtime") != "container":
        return _result(
            "container-python",
            False,
            OPTIONAL,
            {"skipped": "not a container-runtime repo"},
            "skipped: not a container-runtime repo",
        )
    want = str(detail.get("container_python") or "3.11")
    found = ""
    if shutil.which("uv") is not None:
        code, out = _run(["uv", "python", "find", want], timeout=_PROBE_TIMEOUT_S)
        if code == 0 and out.strip():
            found = out.strip().splitlines()[-1]
    if not found:
        found = shutil.which(f"python{want}") or ""
    if found:
        return _result("container-python", True, OPTIONAL, {"want": want, "path": found})
    return _result(
        "container-python",
        False,
        OPTIONAL,
        {"want": want, "path": "", "warn": True},
        f"container apps pin Python {want} and none was found: uv python install {want} "
        f"(or let `uv venv --python {want}` download it)",
    )


def _list_connections() -> tuple[list[dict] | None, str]:
    """``(rows, "")`` from ``snow connection list --format json``, or ``(None,
    why)`` when ``snow`` is absent, fails, times out, or prints anything that is
    not a JSON list. Never raises."""
    if shutil.which("snow") is None:
        return None, "snow CLI not installed"
    code, out = _run(["snow", "connection", "list", "--format", "json"], timeout=_SNOW_TIMEOUT_S)
    if code == _TIMEOUT_CODE:
        return None, _snow_slow_hint("snow connection list")
    if code != 0:
        return None, f"snow connection list failed (exit {code})"
    try:
        parsed = json.loads(out or "[]")
    except ValueError:
        parsed = None
    if not isinstance(parsed, list):
        return None, "snow connection list printed something other than a JSON list"
    return [row for row in parsed if isinstance(row, dict)], ""


def snow_connections() -> list[dict] | None:
    """Rows of ``snow connection list --format json``, or None when ``snow`` is
    absent, fails, times out, or prints anything that is not a JSON list.

    Callers read connection names, ``is_default`` and parameter NAMES; nothing
    here prints a parameter value. Never raises.
    """
    return _list_connections()[0]


def _not_checked(name: str, why: str, detail: dict | None = None) -> dict:
    """A dependent check that could not run: said plainly, never a false finding."""
    return _result(name, False, OPTIONAL, {"skipped": why, **(detail or {})}, f"not checked: {why}")


def _connection_name(row: dict) -> str:
    candidate = row.get("connection_name") or row.get("name")
    return candidate if isinstance(candidate, str) else ""


def default_connection(rows: list[dict] | None) -> dict | None:
    """The row ``snow`` marks ``is_default``, or None."""
    for row in rows or []:
        if row.get("is_default") is True and _connection_name(row):
            return row
    return None


def default_connection_name(rows: list[dict] | None) -> str | None:
    """Name of the default ``snow`` connection, or None when there is none."""
    row = default_connection(rows)
    return _connection_name(row) if row else None


def check_snow_connection(
    cfg_result: dict,
    snow_result: dict | None = None,
    rows: list[dict] | None = None,
    list_error: str = "",
) -> dict:
    """Does the ``snow`` connection the config names exist on this machine?

    Optional, and a not-ok "skipped" result (never an omission) when there is
    no valid config or no ``snow`` — the result list keeps a stable shape so
    callers iterating it need no special cases. Reads ``snow connection list``
    only; it never runs ``snow connection test`` (browser/MFA side effects).
    ``rows``/``list_error`` carry a listing the caller already ran; a listing
    that failed or timed out reports "not checked", never "no such connection".
    """
    name = str(cfg_result.get("detail", {}).get("connection_name") or "")
    if not cfg_result.get("ok") or not name:
        return _result(
            "snow-connection",
            False,
            OPTIONAL,
            {"skipped": "no valid config"},
            "skipped — no config",
        )
    if snow_result is not None and snow_result.get("detail", {}).get("broken"):
        return _result(
            "snow-connection",
            False,
            OPTIONAL,
            {"skipped": "snow is broken", "connection_name": name},
            "skipped: fix the snow CLI first",
        )
    if shutil.which("snow") is None:
        return _result(
            "snow-connection",
            False,
            OPTIONAL,
            {"skipped": "snow not on PATH", "connection_name": name},
            "skipped — snow CLI not installed",
        )
    if rows is None and not list_error:
        rows, list_error = _list_connections()
    if rows is None:
        return _not_checked("snow-connection", list_error, {"connection_name": name})
    names = [n for n in (_connection_name(row) for row in rows) if n]
    ok = name in names
    add = (
        f"snow connection add --connection-name {name} --account <locator> --user <you> "
        "--authenticator externalbrowser --default"
    )
    existing_default = default_connection_name(rows)
    if ok:
        hint = ""
    elif existing_default:
        # A working default connection (a prior tutorial, another project) is the
        # common case. Telling that user to add a second --default connection
        # silently repoints every other tool that reads the default.
        hint = (
            f"no snow connection named {name!r}, but your default snow connection is "
            f"{existing_default!r}: set snowflake.connection_name: {existing_default} in "
            "streamsnow.config.yaml to use it (st.connection('snowflake') reads the default "
            f"locally), or create a new one: {add}"
        )
    else:
        hint = f"no snow connection named {name!r}; run: {add}"
    return _result(
        "snow-connection",
        ok,
        OPTIONAL,
        {"connection_name": name, "found": ok, "known": names},
        hint,
    )


# Parameter names under which the Python connector (and so st.connection) can
# load a key-pair key from a connection definition. snow additionally accepts
# private_key_path (a legacy alias it rewrites itself) and private_key_raw,
# which the connector silently drops.
_CONNECTOR_KEY_FIELDS = ("private_key_file",)
_KEY_FILE_HINT = (
    "rename private_key_path to private_key_file in that connection's entry (a passphrase "
    "goes in private_key_file_pwd). st.connection('snowflake') loads the default connection "
    "through the Python connector, which ignores private_key_path and fails with "
    "'TypeError: Expected bytes, RSAPrivateKey, ... got NoneType'; snow reads private_key_file too"
)


def check_snow_key_file(
    snow_result: dict | None = None, rows: list[dict] | None = None, list_error: str = ""
) -> dict:
    """Can the Python connector load the default connection's key-pair key?

    Optional and never gating: a warning with the rename, or a not-ok "skipped"
    result when there is no working ``snow`` or no default connection. Reads
    parameter NAMES from ``snow connection list`` only. A ``snow --version``
    that only timed out does not skip it: the listing may still answer.
    """
    snow_detail = (snow_result or {}).get("detail", {})
    if snow_result is not None and not snow_result.get("ok") and not snow_detail.get("timed_out"):
        broken = bool(snow_detail.get("broken"))
        return _result(
            "snow-key-file",
            False,
            OPTIONAL,
            {"skipped": "snow is broken" if broken else "snow not on PATH"},
            "skipped: fix the snow CLI first" if broken else "skipped: snow CLI not installed",
        )
    if rows is None and not list_error:
        rows, list_error = _list_connections()
    if rows is None:
        return _not_checked("snow-key-file", list_error)
    row = default_connection(rows)
    if row is None:
        return _result(
            "snow-key-file",
            False,
            OPTIONAL,
            {"skipped": "no default snow connection"},
            "skipped: no default snow connection",
        )
    name = _connection_name(row)
    params = row.get("parameters") if isinstance(row.get("parameters"), dict) else {}
    keys = {str(k).lower() for k in params}
    key_pair = str(params.get("authenticator") or "").upper() == "SNOWFLAKE_JWT"
    loadable = any(field in keys for field in _CONNECTOR_KEY_FIELDS)
    detail: dict = {
        "connection_name": name,
        "key_pair": key_pair,
        "key_fields": sorted(k for k in keys if k.startswith("private_key")),
    }
    if key_pair and not loadable:
        detail["warn"] = True
        return _result(
            "snow-key-file",
            False,
            OPTIONAL,
            detail,
            f"default snow connection {name!r} uses key-pair auth but lists no "
            f"private_key_file: {_KEY_FILE_HINT}",
        )
    return _result("snow-key-file", True, OPTIONAL, detail)


def check_config(start: Path | None = None) -> dict:
    """Config presence + validity, walking up from ``start`` (default: cwd).

    Missing is an *optional* miss (just not a configured repo); present but
    invalid is a *required* failure with the validation message as detail.
    """
    cfg_path = find_config(start)
    if cfg_path is None:
        return _result(
            "config",
            False,
            OPTIONAL,
            {"found": False},
            "no streamsnow.config.yaml here — run 'streamsnow configure'",
        )
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        return _result(
            "config",
            False,
            REQUIRED,
            {"found": True, "path": str(cfg_path), "error": str(exc)},
            f"invalid streamsnow.config.yaml — {exc} (fix it or re-run 'streamsnow configure')",
        )
    return _result(
        "config",
        True,
        REQUIRED,
        {
            "found": True,
            "path": str(cfg_path),
            "schema_version": cfg.schema_version,
            "runtime": cfg.runtime,
            "connection_name": cfg.snowflake.connection_name,
            "container_python": cfg.snowflake.objects.container_python,
        },
    )


def _config_dir(cfg_result: dict) -> Path | None:
    path = cfg_result.get("detail", {}).get("path")
    return Path(path).parent if path else None


def check_repo_files(start: Path | None = None) -> dict:
    """Are the governed repo files this config calls for on disk?

    Skipped without a valid config. Required when any is missing: a repo with a
    config but no ``.pre-commit-config.yaml``/CI/``.gitignore`` has its
    governance off, and the fix (``init --no-starter-app``) reuses the config
    and writes only what is missing.
    """
    cfg_path = find_config(start)
    if cfg_path is None:
        return _result(
            "repo-files", False, OPTIONAL, {"skipped": "no config"}, "skipped: no config"
        )
    try:
        cfg = load_config(cfg_path)
    except ConfigError:
        return _result(
            "repo-files", False, OPTIONAL, {"skipped": "invalid config"}, "skipped: fix the config"
        )
    missing = missing_repo_files(cfg, cfg_path.parent)
    if not missing:
        return _result("repo-files", True, REQUIRED, {"missing": []})
    return _result(
        "repo-files",
        False,
        REQUIRED,
        {"missing": missing},
        f"missing {', '.join(missing)}: run `streamsnow init --no-starter-app` "
        "(it reuses the config and writes only the missing files)",
    )


def check_git_identity(config_present: bool, cwd: Path | None = None) -> dict:
    """Are ``user.name`` and ``user.email`` set where commits happen?

    Records only whether each is set, never the values. Required once a config
    exists (every commit in a governed repo needs them); a warning on a bare
    machine. A probe that times out says "not checked".
    """
    level = REQUIRED if config_present else OPTIONAL
    if shutil.which("git") is None:
        return _result(
            "git-identity", False, OPTIONAL, {"skipped": "git not installed"}, "skipped: no git"
        )
    present: dict[str, bool] = {}
    for key in ("name", "email"):
        code, out = _run(["git", "config", f"user.{key}"], cwd=cwd)
        if code == _TIMEOUT_CODE:
            return _not_checked("git-identity", "git config did not answer")
        present[key] = code == 0 and bool(out.strip())
    detail = {"name_set": present["name"], "email_set": present["email"]}
    unset = [k for k, ok in present.items() if not ok]
    if not unset:
        return _result("git-identity", True, level, detail)
    if level == OPTIONAL:
        detail["warn"] = True
    fix = " and ".join(f'git config user.{k} "<your {k}>"' for k in unset)
    return _result(
        "git-identity",
        False,
        level,
        detail,
        f"commits need a name and email: {fix} (add --global to use them in every repo)",
    )


_PRE_COMMIT_MARKER = "generated by pre-commit"


def check_pre_commit_hook(cfg_result: dict) -> dict:
    """Is pre-commit's own git hook installed in this clone?

    ``pre-commit install`` runs per clone, so a teammate who skips it commits
    with every governance check off while the executable check still passes.
    Resolves the hook path through git, so ``core.hooksPath`` and worktrees are
    honored, and resolves a relative answer (older git) against the repo. Any
    other hook at that path does not count. Skipped without a config; "not
    checked" when git cannot resolve the path (not a git repository).
    """
    repo = _config_dir(cfg_result)
    if repo is None:
        return _result(
            "pre-commit-hook", False, OPTIONAL, {"skipped": "no config"}, "skipped: no config"
        )
    if shutil.which("git") is None:
        return _not_checked("pre-commit-hook", "git not installed")
    code, out = _run(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-commit"], cwd=repo
    )
    lines = out.strip().splitlines()
    if code != 0 or not lines:
        return _not_checked("pre-commit-hook", "git could not resolve the hooks path here")
    hook = Path(lines[-1])
    if not hook.is_absolute():
        hook = repo / hook
    install = "run `pre-commit install` in the repo"
    if not hook.is_file():
        return _result(
            "pre-commit-hook",
            False,
            REQUIRED,
            {"path": str(hook), "installed": False},
            f"the governance hooks are not installed in this clone: {install} "
            "(if it refuses because core.hooksPath is set, ask before unsetting it)",
        )
    try:
        ours = _PRE_COMMIT_MARKER in hook.read_text(errors="replace").lower()
    except OSError:
        return _not_checked("pre-commit-hook", "could not read the hook file")
    if not ours:
        return _result(
            "pre-commit-hook",
            False,
            REQUIRED,
            {"path": str(hook), "installed": False, "foreign": True},
            f"another tool's hook is installed, not pre-commit's: {install} "
            "(it keeps the existing hook as pre-commit.legacy and still runs it)",
        )
    # Git silently skips a hook without the executable bit (Windows has none).
    if os.name != "nt" and not os.access(hook, os.X_OK):
        return _result(
            "pre-commit-hook",
            False,
            REQUIRED,
            {"path": str(hook), "installed": False, "executable": False},
            f"the hook is there but not executable, so git skips it: {install} again",
        )
    return _result("pre-commit-hook", True, REQUIRED, {"path": str(hook), "installed": True})


# @playwright/mcp itself declares node >=18, but the playwright-core it pins
# declares >=20 and exits on anything older, so 18 would pass here and then fail.
_NODE_MIN_MAJOR = 20
_NODE_HINT = (
    "install Node.js 20+ (brew install node, or nvm on Linux/WSL, where the distro package "
    "is often older): the bundled "
    "Playwright browser tool runs through npx, and without it UI walkthroughs are skipped"
)


def check_node() -> dict:
    """Node >= 20 with ``npx``, which the plugin's bundled Playwright MCP needs.

    Optional and never gating: the UI walkthrough is advisory. A warning (not a
    quiet skip) when absent, because the walkthrough otherwise degrades silently.
    """
    npx = shutil.which("npx") is not None
    path = shutil.which("node")
    if path is None:
        return _result(
            "node",
            False,
            OPTIONAL,
            {"found": False, "path": "", "npx": npx, "warn": True},
            _NODE_HINT,
        )
    code, out = _run(["node", "--version"])
    m = _VERSION_RE.search(out) if code == 0 else None
    version = m.group(1) if m else ""
    detail = {"found": True, "path": path, "version": version, "npx": npx}
    if not version or int(version.split(".")[0]) < _NODE_MIN_MAJOR:
        detail["warn"] = True
        return _result(
            "node", False, OPTIONAL, detail, f"Node {version or '?'} found; {_NODE_HINT}"
        )
    if not npx:
        detail["warn"] = True
        return _result(
            "node", False, OPTIONAL, detail, "node is installed but npx is not: reinstall Node.js"
        )
    return _result("node", True, OPTIONAL, detail)


# The deploy workflows' secrets (streamsnow/_templates/repo/deploy*.yml.j2; a test
# pins this list to the templates). The passphrase only exists for encrypted keys.
CI_SECRET_NAMES = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PRIVATE_KEY_RAW",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_ROLE",
)
OPTIONAL_CI_SECRET_NAMES = ("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",)


def check_ci_secrets(cfg_result: dict) -> dict:
    """Do the GitHub secrets the deploy workflow reads exist on this repo?

    The deploy job is gated on ``SNOWFLAKE_ACCOUNT``, so without the secrets a
    merge deploys nothing and nothing says so. Reads secret NAMES only (``gh``
    cannot return values). Listing needs repo access and a token scope a
    teammate may not have, so any failure is "not checked", never "missing";
    secrets set at the org or environment level do not appear here either.
    """
    repo = _config_dir(cfg_result)
    if repo is None or not cfg_result.get("ok"):
        return _result(
            "ci-secrets", False, OPTIONAL, {"skipped": "no valid config"}, "skipped: no config"
        )
    if shutil.which("gh") is None:
        return _result(
            "ci-secrets", False, OPTIONAL, {"skipped": "gh not installed"}, "skipped: no gh CLI"
        )
    code, out = _run(["gh", "secret", "list", "--json", "name"], cwd=repo)
    if code == _TIMEOUT_CODE:
        return _not_checked("ci-secrets", "gh secret list did not answer")
    try:
        rows = json.loads(out) if code == 0 else None
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        return _not_checked(
            "ci-secrets",
            "gh could not list this repo's secrets (run `gh auth login`; listing needs repo "
            "access, and the repo needs a GitHub remote)",
        )
    names = {str(r.get("name")) for r in rows if isinstance(r, dict)}
    missing = [n for n in CI_SECRET_NAMES if n not in names]
    if not missing:
        return _result("ci-secrets", True, OPTIONAL, {"missing": []})
    return _result(
        "ci-secrets",
        False,
        OPTIONAL,
        {"missing": missing, "warn": True},
        f"CI deploys stay off until these GitHub secrets exist: {', '.join(missing)} "
        "(see docs/deploy-setup.md; secrets set at the org or environment level do not "
        "show here)",
    )


def check_platform(system: str | None = None) -> dict | None:
    """Native Windows only: StreamSnow's preview and safety hooks are POSIX-only
    today, so it runs inside WSL. Returns None everywhere else (no row)."""
    system = sys.platform if system is None else system
    if system != "win32":
        return None
    return _result(
        "platform",
        False,
        OPTIONAL,
        {"system": system, "warn": True},
        "StreamSnow runs inside WSL (Windows Subsystem for Linux) on Windows today: "
        "run `wsl --install` in an administrator PowerShell, restart, then set up inside WSL",
    )


def run_checks(start: Path | None = None) -> list[dict]:
    """Run every check; never raises from an individual check."""
    checks = [check_python()]
    tools = {name: check_path_tool(name, level, hint) for name, level, hint in _PATH_TOOLS}
    snow = check_snow()
    checks += [tools["git"], tools["uv"], snow, tools["streamlit"], tools["gh"]]
    config = check_config(start)
    checks.append(check_pre_commit(config_present=bool(config["detail"].get("found"))))
    checks.append(config)
    # One `snow connection list` (a cold start takes seconds) feeds both checks.
    # A `snow --version` that timed out still gets the listing: the cold start
    # it waited on has usually finished, and a listing that also fails makes
    # both checks say "not checked" instead of skipping silently.
    rows, list_error = None, ""
    if snow["ok"] or snow["detail"].get("timed_out"):
        rows, list_error = _list_connections()
    checks.append(check_snow_connection(config, snow, rows, list_error))
    checks.append(check_snow_key_file(snow, rows, list_error))
    checks.append(check_container_python(config))
    config_present = bool(config["detail"].get("found"))
    checks.append(check_repo_files(start))
    checks.append(check_git_identity(config_present, cwd=_config_dir(config)))
    checks.append(check_pre_commit_hook(config))
    checks.append(check_node())
    checks.append(check_ci_secrets(config))
    platform = check_platform()
    if platform is not None:
        checks.append(platform)
    return checks


def required_ok(results: list[dict]) -> bool:
    return all(r["ok"] or r["level"] == OPTIONAL for r in results)


def render_text(results: list[dict]) -> str:
    """Plain-text rendering the CLI can print (or wrap in color itself).

    Marks: ``ok`` passed; ``MISSING`` a required prerequisite is absent;
    ``BROKEN`` one is present but does not run; ``warn`` an optional check found
    a problem worth fixing; ``skip`` an optional one is absent (informational).
    Only MISSING and BROKEN gate.
    """
    lines = []
    for r in results:
        detail = r.get("detail") or {}
        if r["ok"]:
            mark = "ok     "
        elif detail.get("broken"):
            mark = "BROKEN "
        elif r["level"] == OPTIONAL:
            mark = "warn   " if detail.get("warn") else "skip   "
        else:
            mark = "MISSING"
        summary = _summarize(r["detail"])
        hint = f" — {r['hint']}" if r["hint"] and not r["ok"] else ""
        lines.append(f"[{mark}] {r['name']}{f' ({summary})' if summary else ''}{hint}")
    lines.append("doctor: " + ("all required checks passed" if required_ok(results) else "FAIL"))
    return "\n".join(lines)


def _summarize(detail: dict) -> str:
    if "version" in detail:
        return f"v{detail['version']}"
    if detail.get("path"):
        return str(detail["path"])
    if "error" in detail:
        return str(detail["error"])[:80]
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Check the local environment for the prerequisites StreamSnow needs."
    )
    ap.add_argument("--json", action="store_true", help="Emit per-check results as JSON.")
    # The package-wide check contract is `--format md|json`; honor it here too
    # so automation doesn't need a doctor-specific flag (--json stays an alias).
    ap.add_argument("--format", choices=("md", "json"), default="md", dest="output_format")
    args = ap.parse_args(argv)

    try:
        results = run_checks()
    except Exception as exc:  # noqa: BLE001 — the doctor itself must not crash opaquely
        print(f"doctor: tool error: {exc}", file=sys.stderr)
        return 2

    if args.json or args.output_format == "json":
        print(json.dumps({"ok": required_ok(results), "checks": results}, indent=2))
    else:
        print(render_text(results))
    return 0 if required_ok(results) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
