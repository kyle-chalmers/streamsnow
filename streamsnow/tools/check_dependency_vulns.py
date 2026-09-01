"""Check app dependency manifests for known vulnerabilities via OSV.dev.

Why this check exists
=====================

App PRs can merge on green CI, and merges deploy straight to Snowflake — so
each app's dependency surface (``pyproject.toml`` for the container runtime,
``environment.yml`` for the warehouse runtime) gets no human security review by
default. This gate queries the OSV.dev database for every exact pin and fails
when a pin has a known vulnerability that is not allowlisted. A new CVE against
an existing pin fails the very next run, which is the point: the gate ages with
the ecosystem, not with the repo.

Range-pin policy (deliberate)
=============================

Only exact ``==`` pins are queried against OSV. Range specifiers (``>=``,
``<``, ``~=``, conda wildcards like ``2.*``, and bare names) have **no single
version to query** — they resolve at deploy time — so they are REPORTED in the
output as "unscanned (range pin)" for visibility but never fail the check.
Deterministic coverage for a ranged dependency means pinning it exactly.

What is scanned
===============

Given paths (default ``apps``) are widened to app roots (directories containing
``snowflake.yml``), then per app:

- ``pyproject.toml``: PEP 508 specs from ``project.dependencies`` and
  ``dependency-groups`` (environment markers stripped, extras ignored).
- ``environment.yml`` (warehouse runtime): conda-style ``name=1.2.3`` /
  ``name==1.2.3`` specs from ``dependencies``, plus any nested ``pip:`` list
  (PEP 508). The interpreter entry (``python=...``) is skipped — it is not a
  PyPI distribution. Conda pins are queried against the PyPI ecosystem: an
  approximation (conda channels rebuild packages) but version numbers track
  upstream releases, so advisory coverage carries over.

Queries go to the OSV querybatch API (one POST for all pins) via stdlib
``urllib`` with a short timeout and a few retries.

Allowlist
=========

``--allowlist PATH`` names a JSON file; by default the tool looks for
``osv_allowlist.json`` at the repo root — the directory containing
``streamsnow.config.yaml`` (walking up from the cwd), falling back to the cwd.
A missing file is an empty allowlist, not an error. The file is a JSON list of
entries::

    [
      {"id": "GHSA-xxxx-yyyy-zzzz", "package": "somepkg",
       "reason": "dated rationale", "expires": "2027-01-31"}
    ]

An entry suppresses exactly one vulnerability ID on one package — a NEW ID on
an allowlisted package still fails. Entries past their ``expires`` date (or
with a missing/unparseable one — the gate fails closed) are ignored, so the
vulnerability fails again, and they are reported as expired so the stale entry
gets cleaned up or re-justified.

Failure modes
=============

OSV unreachable after retries → exit 2 (tool error, fail closed) — unless
``--best-effort`` is passed, which downgrades it to a warning + exit 0 for
local pre-commit use where offline must not block; CI omits the flag so an
outage fails loudly.

Exit codes: 0 = clean, 1 = vulnerable pin found, 2 = tool error.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

import yaml

from ..config import CONFIG_FILENAME, find_config
from .check_artifacts import _app_dirs_for

OSV_URL = "https://api.osv.dev/v1/querybatch"
ALLOWLIST_FILENAME = "osv_allowlist.json"
_TIMEOUT_SECONDS = 10
_ATTEMPTS = 3

# Exact PEP 440 pin: name (optionally with extras) == version. Anything else
# (ranges, markers already stripped, URLs) has no single version to query.
_PINNED_SPEC = re.compile(r"^([A-Za-z0-9_.\[\]-]+)\s*==\s*([0-9][A-Za-z0-9.!+]*)$")
# Conda exact pin: name=1.2.3 (optionally =build). A wildcard version (2.*) is
# a range. environment.yml only.
_CONDA_SPEC = re.compile(r"^([A-Za-z0-9_.-]+)=([0-9][A-Za-z0-9.!+]*)(?:=[A-Za-z0-9_.*]+)?$")


def normalize(name: str) -> str:
    """PEP 503 name normalization, extras stripped (``foo[bar]`` -> ``foo``)."""
    return re.sub(r"[-_.]+", "-", name.split("[")[0]).lower()


class OSVUnreachableError(RuntimeError):
    """Raised when the OSV.dev API can't be reached after retries."""


# --------------------------------------------------------------------------- #
# Pin extraction
# --------------------------------------------------------------------------- #
def classify_pep440(spec: str, manifest: str, pins: dict, unscanned: list[dict]) -> None:
    """Sort one PEP 508 dependency string into exact pins or unscanned ranges."""
    # Strip environment markers ("pkg==1.0; python_version < '3.12'").
    base = spec.split(";")[0].strip()
    if not base:
        return
    m = _PINNED_SPEC.match(base.replace(" ", ""))
    if m:
        pins.setdefault((normalize(m.group(1)), m.group(2)), set()).add(manifest)
    else:
        unscanned.append({"file": manifest, "spec": base})


def classify_conda(spec: str, manifest: str, pins: dict, unscanned: list[dict]) -> None:
    """Sort one conda dependency string into exact pins or unscanned ranges."""
    base = spec.strip()
    if not base:
        return
    m = _CONDA_SPEC.match(base.replace(" ", "").replace("==", "="))
    name = normalize(m.group(1)) if m else ""
    if name == "python":
        return  # the interpreter, not a PyPI distribution
    if m:
        pins.setdefault((name, m.group(2)), set()).add(manifest)
    else:
        unscanned.append({"file": manifest, "spec": base})


def _extract_pyproject(path: Path, pins: dict, unscanned: list[dict]) -> None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (tomllib.TOMLDecodeError, OSError):
        return  # a malformed manifest is the manifest check's finding, not ours
    specs = [s for s in (data.get("project", {}).get("dependencies") or []) if isinstance(s, str)]
    for group in (data.get("dependency-groups") or {}).values():
        if isinstance(group, list):
            # Entries can be tables ({"include-group": ...}) — strings only.
            specs.extend(s for s in group if isinstance(s, str))
    for spec in specs:
        classify_pep440(spec, str(path), pins, unscanned)


def _extract_environment_yml(path: Path, pins: dict, unscanned: list[dict]) -> None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="ignore"))
    except (yaml.YAMLError, OSError):
        return
    deps = data.get("dependencies") if isinstance(data, dict) else None
    if not isinstance(deps, list):
        return
    for item in deps:
        if isinstance(item, str):
            classify_conda(item, str(path), pins, unscanned)
        elif isinstance(item, dict) and isinstance(item.get("pip"), list):
            for pip_spec in item["pip"]:
                if isinstance(pip_spec, str):
                    classify_pep440(pip_spec, str(path), pins, unscanned)


def collect_pins(app_dirs: list[Path]) -> tuple[dict, list[dict]]:
    """Return (``{(name, version): {manifest, ...}}``, unscanned range specs)."""
    pins: dict[tuple[str, str], set[str]] = {}
    unscanned: list[dict] = []
    for app_dir in app_dirs:
        pyproject = app_dir / "pyproject.toml"
        if pyproject.is_file():
            _extract_pyproject(pyproject, pins, unscanned)
        env = app_dir / "environment.yml"
        if env.is_file():
            _extract_environment_yml(env, pins, unscanned)
    return pins, unscanned


# --------------------------------------------------------------------------- #
# Allowlist
# --------------------------------------------------------------------------- #
def resolve_allowlist_path(explicit: str | None) -> Path:
    """Explicit ``--allowlist`` wins; else ``osv_allowlist.json`` at the repo
    root (the directory containing ``streamsnow.config.yaml``), else the cwd."""
    if explicit:
        return Path(explicit)
    cfg = find_config()
    root = cfg.parent if cfg else Path.cwd()
    return root / ALLOWLIST_FILENAME


def load_allowlist(path: Path, today: _dt.date | None = None) -> tuple[dict, list[dict]]:
    """Return (active ``{(package, vuln_id): entry}``, expired entries).

    A missing file is an empty allowlist. Entries whose ``expires`` date has
    passed — or is missing/unparseable (fail closed) — are returned as expired
    so the run can report them; they suppress nothing.
    """
    if not path.is_file():
        return {}, []
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"{path}: allowlist must be a JSON list of entries")
    today = today or _dt.date.today()
    active: dict[tuple[str, str], dict] = {}
    expired: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("package"):
            raise ValueError(f"{path}: each entry needs 'id' and 'package': {entry!r}")
        try:
            expires = _dt.date.fromisoformat(str(entry.get("expires", "")))
        except ValueError:
            expires = None  # missing/unparseable -> treated as already expired
        if expires is None or expires < today:
            expired.append(entry)
        else:
            active[(normalize(entry["package"]), entry["id"])] = entry
    return active, expired


# --------------------------------------------------------------------------- #
# OSV query + evaluation
# --------------------------------------------------------------------------- #
def query_osv(pins: list[tuple[str, str]]) -> list[list[str]]:
    """Return a vulnerability-ID list per pin (parallel to ``pins``).

    Module-level on purpose so tests monkeypatch it; nothing else in this
    module touches the network.
    """
    payload = json.dumps(
        {
            "queries": [
                {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
                for name, version in pins
            ]
        }
    ).encode()
    last_err: Exception | None = None
    for attempt in range(_ATTEMPTS):
        try:
            req = urllib.request.Request(
                OSV_URL, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
                results = json.load(resp)["results"]
            return [[v["id"] for v in (r.get("vulns") or [])] for r in results]
        except Exception as e:  # noqa: BLE001 — retry any transport failure
            last_err = e
            time.sleep(attempt + 1)
    raise OSVUnreachableError(last_err)


def evaluate(
    pins: dict[tuple[str, str], set[str]],
    vuln_ids_per_pin: list[list[str]],
    active_allowlist: dict[tuple[str, str], dict],
) -> tuple[list[dict], list[dict]]:
    """Split query results into (failing findings, suppressed-by-allowlist)."""
    findings: list[dict] = []
    allowlisted: list[dict] = []
    for (name, version), vuln_ids in zip(sorted(pins), vuln_ids_per_pin, strict=True):
        manifests = ", ".join(sorted(pins[(name, version)]))
        new_ids = []
        for vid in vuln_ids:
            entry = active_allowlist.get((name, vid))
            if entry:
                allowlisted.append(
                    {
                        "package": name,
                        "version": version,
                        "id": vid,
                        "reason": entry.get("reason", "no reason recorded"),
                        "expires": entry.get("expires"),
                    }
                )
            else:
                new_ids.append(vid)
        if new_ids:
            findings.append(
                {
                    "file": manifests,
                    "detail": f"{name}=={version} has known vulnerabilities: "
                    f"{', '.join(new_ids)} — bump the pin, or add a dated "
                    f"allowlist entry (with an expiry) per vulnerability ID",
                }
            )
    return findings, allowlisted


def scan_paths(paths: list[Path], allowlist_path: Path | None = None) -> dict:
    """Scan the apps under *paths*. Raises :class:`OSVUnreachableError` when
    OSV can't be reached — the caller decides whether that blocks."""
    active, expired = load_allowlist(allowlist_path or resolve_allowlist_path(None))
    pins, unscanned = collect_pins(_app_dirs_for(paths))
    vuln_ids_per_pin = query_osv(sorted(pins)) if pins else []
    findings, allowlisted = evaluate(pins, vuln_ids_per_pin, active)
    return {
        "ok": not findings,
        "findings": findings,
        "unscanned": unscanned,
        "allowlisted": allowlisted,
        "expired": expired,
        "checked": len(pins),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Check app dependency pins for known vulnerabilities (OSV.dev)."
    )
    ap.add_argument("paths", nargs="*", help="Apps, files, or directories (default: apps).")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument(
        "--allowlist",
        help=f"Allowlist JSON path (default: {ALLOWLIST_FILENAME} beside {CONFIG_FILENAME}).",
    )
    ap.add_argument(
        "--best-effort",
        action="store_true",
        help="Treat an unreachable OSV API as a pass (warn, exit 0) instead of a tool "
        "error. For local pre-commit where offline must not block; CI omits it.",
    )
    ap.add_argument(
        "--strict-pins",
        action="store_true",
        help="Fail on range/bare specs instead of reporting them unscanned. The "
        "generated CI uses this: a range pin resolves to a version OSV never saw, "
        "so 'unscanned' must not pass a fail-closed gate.",
    )
    args = ap.parse_args(argv)

    try:
        result = scan_paths(
            [Path(raw) for raw in (args.paths or ["apps"])],
            allowlist_path=Path(args.allowlist) if args.allowlist else None,
        )
    except OSVUnreachableError as exc:
        msg = f"OSV API unreachable after {_ATTEMPTS} attempts: {exc}"
        if args.best_effort:
            print(
                f"WARNING: {msg}\ndependency-vulns: SKIPPED (best-effort) — "
                "re-runs in CI where it is authoritative.",
                file=sys.stderr,
            )
            return 0
        print(f"ERROR: {msg}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.strict_pins:
        # Promote unscanned specs to findings: a range pin resolves to a
        # version OSV never checked, and a fail-closed gate must not pass it.
        for u in result["unscanned"]:
            result["findings"].append(
                {
                    "file": u["file"],
                    "detail": f"unpinned spec {u['spec']!r} cannot be scanned — pin an "
                    "exact version (==) so OSV checks what actually deploys "
                    "(--strict-pins)",
                }
            )
        result["ok"] = not result["findings"]

    if args.format == "json":
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    if not args.strict_pins:
        for u in result["unscanned"]:
            print(f"note: unscanned (range pin): {u['file']}: {u['spec']}")
    for e in result["expired"]:
        print(
            f"note: expired allowlist entry: {e.get('id')} on {e.get('package')} "
            f"(expires: {e.get('expires', 'missing')}) — no longer suppresses anything"
        )
    for a in result["allowlisted"]:
        print(
            f"allowlisted: {a['package']}=={a['version']} {a['id']} "
            f"(until {a['expires']}: {a['reason']})"
        )
    if result["ok"]:
        print(f"dependency-vulns: clean ({result['checked']} exact pins checked)")
    else:
        for f in result["findings"]:
            print(f"BLOCK {f['file']} {f['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
