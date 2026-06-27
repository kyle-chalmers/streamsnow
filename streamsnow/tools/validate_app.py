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

from ..config import Config, ConfigError, load_config
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

# Dotted directories that hold tooling artifacts (review walkthroughs, git
# metadata, caches) — never real app source. The file-walk skips these so the
# governance checks don't fire on REVIEW-*.md notes or screenshots that quote
# denied schemas / dynamic-SQL examples. ``.streamlit`` is the one dotted dir
# that IS app source (config.toml lives there), so it is never skipped.
_KEEP_DOTTED = frozenset({".streamlit"})

# Container-runtime fields that must be ABSENT in warehouse mode.
_CONTAINER_ONLY = ("runtime_name", "compute_pool", "external_access_integrations")


def _walk_app_files(app_dir: Path) -> list[Path]:
    """Yield real app files under ``app_dir``, skipping dotted tooling dirs.

    ``.review/``, ``.git/``, ``.venv/``, ``__pycache__`` etc. are never app
    source; scanning them produced false positives on a clean repo. ``.streamlit``
    is kept because ``.streamlit/config.toml`` is a required app file.
    """
    files: list[Path] = []
    for p in app_dir.rglob("*"):
        rel_parts = p.relative_to(app_dir).parts
        if (
            any(part.startswith(".") and part not in _KEEP_DOTTED for part in rel_parts)
            or "__pycache__" in rel_parts
        ):
            continue
        if p.is_file():
            files.append(p)
    return files


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


def _env_yml_pins_python(app_dir: Path) -> bool:
    """True if a sibling ``environment.yml`` pins ``python`` (the CREATE STREAMLIT landmine)."""
    env = app_dir / "environment.yml"
    if not env.is_file():
        return False
    try:
        data = yaml.safe_load(env.read_text()) or {}
    except yaml.YAMLError:
        return False
    for dep in data.get("dependencies") or []:
        if isinstance(dep, str) and dep.split("=")[0].strip().lower() == "python":
            return True
    return False


def _check_manifest(app_dir: Path, cfg: Config) -> list[str]:
    """Return manifest problems (empty == valid snowflake.yml).

    Ports the runtime rules from the source monorepo's ``tools/validate_yaml.py``,
    but reads the literal runtime_name / allowed_warehouses from ``cfg`` instead of
    hardcoding them.
    """
    yml = app_dir / "snowflake.yml"
    if not yml.is_file():
        return ["snowflake.yml missing"]
    try:
        data = yaml.safe_load(yml.read_text()) or {}
    except yaml.YAMLError as exc:
        return [f"snowflake.yml is invalid YAML: {exc}"]

    problems: list[str] = []

    if data.get("definition_version") != 2:
        problems.append(f"definition_version must be 2, got {data.get('definition_version')!r}")

    entities = data.get("entities") or {}
    if not entities:
        return [*problems, "snowflake.yml has no entities"]

    allowed_warehouses = set(cfg.snowflake.objects.allowed_warehouses)
    expected_runtime = cfg.snowflake.objects.runtime_name

    for name, ent in entities.items():
        if not isinstance(ent, dict):
            problems.append(f"{name}: not a mapping")
            continue
        if ent.get("type") not in (None, "streamlit"):
            # non-streamlit entity — no Streamlit runtime rules apply.
            continue

        if ent.get("main_file") != "streamlit_app.py":
            problems.append(f"{name}: main_file must be 'streamlit_app.py'")

        wh = ent.get("query_warehouse")
        if not wh:
            problems.append(f"{name}: missing query_warehouse")
        elif wh not in allowed_warehouses:
            problems.append(
                f"{name}: query_warehouse {wh!r} is not in the allowed list "
                f"{sorted(allowed_warehouses)}"
            )

        identifier = ent.get("identifier")
        if not isinstance(identifier, dict) or not identifier:
            problems.append(f"{name}: missing 'identifier' mapping")

        # Runtime-mode rules (mirrors validate_yaml._validate_mode_fields).
        if ent.get("runtime_name"):  # container mode
            if ent.get("runtime_name") != expected_runtime:
                problems.append(
                    f"{name} (container): runtime_name must be {expected_runtime!r}, "
                    f"got {ent.get('runtime_name')!r}"
                )
            if not ent.get("compute_pool"):
                problems.append(f"{name} (container): compute_pool is required")
            eai = ent.get("external_access_integrations")
            if not isinstance(eai, list) or not eai:
                problems.append(
                    f"{name} (container): external_access_integrations must be a non-empty list"
                )
        else:  # warehouse mode
            present = [k for k in _CONTAINER_ONLY if k in ent]
            if present:
                problems.append(
                    f"{name} (warehouse): must not declare {present} — these are "
                    "container-only fields"
                )
            if _env_yml_pins_python(app_dir):
                problems.append(
                    f"{name} (warehouse): environment.yml pins `python` — this breaks "
                    "CREATE STREAMLIT (the Anaconda channel has no exact python==3.11 "
                    "build). Remove the python line."
                )

    return problems


def validate_app(app_dir: Path, policy: SchemaPolicy, cfg: Config) -> dict:
    checks: list[dict] = []

    runtime = _detect_runtime(app_dir, cfg.runtime)
    required = list(_BASE_REQUIRED) + (
        ["pyproject.toml"] if runtime == "container" else ["environment.yml"]
    )
    missing = [f for f in required if not (app_dir / f).exists()]
    checks.append({"name": "required-files", "ok": not missing, "findings": missing})

    manifest_problems = _check_manifest(app_dir, cfg)
    checks.append({"name": "manifest", "ok": not manifest_problems, "findings": manifest_problems})

    checks.append(
        {
            "name": "naming",
            "ok": bool(_SLUG_RE.match(app_dir.name)),
            "findings": [] if _SLUG_RE.match(app_dir.name) else [app_dir.name],
        }
    )

    files = _walk_app_files(app_dir)
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


def _format_finding(f: object) -> str:
    """Render a finding (string or dict) as a readable ``file:line — detail`` line."""
    if isinstance(f, str):
        return f
    if isinstance(f, dict):
        loc = str(f.get("file", "")).strip()
        line = f.get("line")
        if loc and line:
            loc = f"{loc}:{line}"
        elif line:
            loc = f"line {line}"
        # Prefer the most specific descriptor available.
        parts = [
            str(f[k]) for k in ("kind", "func", "schema", "detail") if f.get(k) not in (None, "")
        ]
        descriptor = " ".join(parts)
        if loc and descriptor:
            return f"{loc} — {descriptor}"
        return loc or descriptor or json.dumps(f, sort_keys=True)
    return str(f)


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
    result = validate_app(app_dir, policy, cfg)

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
                    print(f"      - {_format_finding(f)}")
        print(f"\n{'PASS' if result['ok'] else 'FAIL'}: {result['app']} ({result['runtime']})")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
