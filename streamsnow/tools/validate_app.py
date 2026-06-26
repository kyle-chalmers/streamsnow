"""Aggregate PASS/FAIL preflight for one app — the deterministic ship gate.

Runs the governance checks (required files, naming, runtime-matched manifest,
schema-refs, app-security, bind-predicates, caching) over ``apps/<slug>/`` and
returns a single PASS/FAIL. No database, no network. This is what the
``/validate-app`` skill and ``streamsnow ship-app`` call as the hard gate.

Exit codes: 0 = PASS, 1 = FAIL, 2 = tool error.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml

from ..config import ConfigError, load_config
from ..policy import SchemaPolicy
from . import check_app_security, check_bind_predicates, check_caching, check_schema_refs

_BASE_REQUIRED = (
    "streamlit_app.py",
    "snowflake.yml",
    "branding.py",
    "sql_loader.py",
    "AGENTS.md",
    ".streamlit/config.toml",
)
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _detect_runtime(app_dir: Path, default: str) -> str:
    yml = app_dir / "snowflake.yml"
    if yml.is_file():
        try:
            data = yaml.safe_load(yml.read_text()) or {}
            for entity in (data.get("entities") or {}).values():
                if isinstance(entity, dict) and entity.get("runtime_name"):
                    return "container"
            if data.get("entities") or {}:
                return "warehouse"
        except yaml.YAMLError:
            pass
    return default


def validate_app(app_dir: Path, policy: SchemaPolicy, default_runtime: str) -> dict:
    checks: list[dict] = []

    runtime = _detect_runtime(app_dir, default_runtime)
    required = list(_BASE_REQUIRED) + (
        ["pyproject.toml"] if runtime == "container" else ["environment.yml"]
    )
    missing = [f for f in required if not (app_dir / f).exists()]
    checks.append({"name": "required-files", "ok": not missing, "findings": missing})

    checks.append(
        {
            "name": "naming",
            "ok": bool(_SLUG_RE.match(app_dir.name)),
            "findings": [] if _SLUG_RE.match(app_dir.name) else [app_dir.name],
        }
    )

    files = [p for p in app_dir.rglob("*") if p.is_file()]
    sr = check_schema_refs.check_paths(files, policy)
    checks.append({"name": "schema-refs", "ok": sr["ok"], "findings": sr["findings"]})
    sec = check_app_security.scan_paths(files)
    checks.append({"name": "app-security", "ok": sec["ok"], "findings": sec["findings"]})
    bind = check_bind_predicates.scan_paths(files)
    checks.append({"name": "bind-predicates", "ok": bind["ok"], "findings": bind["findings"]})
    cache = check_caching.scan_paths(files)
    checks.append({"name": "caching", "ok": cache["ok"], "findings": cache["findings"]})

    return {
        "app": app_dir.name,
        "runtime": runtime,
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PASS/FAIL preflight for one StreamSnow app.")
    ap.add_argument("slug", help="App slug (directory name under apps/).")
    ap.add_argument("--dir", default=".", help="Repo root (default: cwd).")
    ap.add_argument("--config", help="Path to streamsnow.config.yaml (default: discover).")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(Path(args.config) if args.config else None)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 2

    app_dir = Path(args.dir) / "apps" / args.slug
    if not app_dir.is_dir():
        print(f"no app at {app_dir}")
        return 2

    policy = SchemaPolicy.from_governance(cfg.governance)
    result = validate_app(app_dir, policy, cfg.runtime)

    if args.format == "json":
        print(json.dumps(result, indent=2))
    else:
        for c in result["checks"]:
            mark = "✓" if c["ok"] else "✗"
            print(
                f"  {mark} {c['name']}" + ("" if c["ok"] else f"  ({len(c['findings'])} issue(s))")
            )
            if not c["ok"]:
                for f in c["findings"][:10]:
                    print(f"      - {f}")
        print(f"\n{'PASS' if result['ok'] else 'FAIL'}: {result['app']} ({result['runtime']})")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
