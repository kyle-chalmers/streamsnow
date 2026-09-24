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
checked, so a broken one is a required failure), ``streamlit`` on PATH
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

Detection only: no prompts, no fix execution — hints name the fix, callers own
the UX. Checks never raise; an unexpected error inside the doctor itself is a
tool error.

Exit codes: 0 = every required check passes, 1 = a required prerequisite is
missing or broken, 2 = the doctor itself failed.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import ConfigError, find_config, load_config

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
# A cold `snow` start was observed above 5 s; at 5 s the first doctor run
# reported "no connections" and a re-run found one.
_SNOW_TIMEOUT_S = 15.0
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


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run a short diagnostic command; never raises (124 on timeout, 127 on any
    other failure to run, with empty output)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_SNOW_TIMEOUT_S, check=False
        )
        return proc.returncode, proc.stdout or ""
    except subprocess.TimeoutExpired:
        return _TIMEOUT_CODE, ""
    except (OSError, ValueError):
        return 127, ""


def check_snow() -> dict:
    """``snow`` on PATH *and* answering ``snow --version``.

    Absent is an optional miss. Present but crashing (or hanging past the
    timeout) is a required failure: every preview/deploy diagnostic and skill
    that shells to ``snow`` would fail with a traceback instead of a hint.
    """
    path = shutil.which("snow")
    if path is None:
        return _result("snow", False, OPTIONAL, {"found": False, "path": ""}, _SNOW_HINT)
    code, out = _run(["snow", "--version"])
    if code != 0:
        return _result(
            "snow",
            False,
            REQUIRED,
            {
                "found": True,
                "path": path,
                "broken": True,
                "exit_code": code,
                "timed_out": code == _TIMEOUT_CODE,
            },
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
        code, out = _run(["uv", "python", "find", want])
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


def check_snow_connection(cfg_result: dict, snow_result: dict | None = None) -> dict:
    """Does the ``snow`` connection the config names exist on this machine?

    Optional, and a not-ok "skipped" result (never an omission) when there is
    no valid config or no ``snow`` — the result list keeps a stable shape so
    callers iterating it need no special cases. Reads ``snow connection list``
    only; it never runs ``snow connection test`` (browser/MFA side effects).
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
    code, out = _run(["snow", "connection", "list", "--format", "json"])
    names: list[str] = []
    if code == 0:
        try:
            parsed = json.loads(out or "[]")
        except ValueError:
            parsed = []
        for row in parsed if isinstance(parsed, list) else []:
            if isinstance(row, dict):
                candidate = row.get("connection_name") or row.get("name")
                if isinstance(candidate, str):
                    names.append(candidate)
    ok = name in names
    hint = (
        ""
        if ok
        else (
            f"no snow connection named {name!r} — run: snow connection add "
            f"--connection-name {name} --account <locator> --user <you> "
            "--authenticator externalbrowser --default"
        )
    )
    return _result(
        "snow-connection",
        ok,
        OPTIONAL,
        {"connection_name": name, "found": ok, "known": names},
        hint,
    )


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


def run_checks(start: Path | None = None) -> list[dict]:
    """Run every check; never raises from an individual check."""
    checks = [check_python()]
    tools = {name: check_path_tool(name, level, hint) for name, level, hint in _PATH_TOOLS}
    snow = check_snow()
    checks += [tools["git"], tools["uv"], snow, tools["streamlit"], tools["gh"]]
    config = check_config(start)
    checks.append(check_pre_commit(config_present=bool(config["detail"].get("found"))))
    checks.append(config)
    checks.append(check_snow_connection(config, snow))
    checks.append(check_container_python(config))
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
