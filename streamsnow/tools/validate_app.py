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
import tomllib
from pathlib import Path

import yaml

try:  # packaging is a declared dependency; stay defensive if it is somehow absent.
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
except ImportError:  # pragma: no cover
    SpecifierSet = None  # type: ignore[assignment]
    InvalidSpecifier = Exception  # type: ignore[assignment,misc]

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

# Every Snowflake Streamlit app needs these in its dependency manifest, in either
# runtime. (Ported from the source monorepo's tools/validate_yaml.REQUIRED_DEPS.)
_REQUIRED_APP_DEPS = ("streamlit", "snowflake-snowpark-python")
# Leading distribution name of a dependency spec ("streamlit==1.50.0" -> "streamlit",
# conda "streamlit=1.50.0" -> "streamlit"). Case-folded by the caller.
_DEP_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")
# Any "X.Y" version token inside a PEP 440 specifier (">=3.11,<3.12" -> 3.11, 3.12).
_PYVER_RE = re.compile(r"(\d+\.\d+)")


def _dep_name(spec: str) -> str | None:
    match = _DEP_NAME_RE.match(spec.strip())
    return match.group(1).lower() if match else None


def _requires_python_allows(spec: str, version: str) -> bool:
    """True if a ``requires-python`` specifier admits ``version`` (e.g. '3.11').

    Uses PEP 440 specifier semantics when ``packaging`` is available, so '>=3.10'
    correctly *allows* 3.11 while '<3.11' / '==3.10.*' correctly do not. Falls back
    to a token-presence heuristic only if the specifier cannot be parsed (or
    packaging is absent) — the heuristic is lossy at range boundaries, which is
    exactly why the parsed path is preferred.
    """
    if SpecifierSet is not None:
        try:
            return SpecifierSet(spec).contains(version)
        except InvalidSpecifier:
            pass
    return version in _PYVER_RE.findall(spec)


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
            entities = data.get("entities")
            if isinstance(entities, dict) and entities:
                for entity in entities.values():
                    if isinstance(entity, dict) and entity.get("runtime_name"):
                        return "container"
                return "warehouse"
        except yaml.YAMLError:
            pass
    return default


def _check_pyproject(app_dir: Path, container_python: str) -> list[str]:
    """Validate container-mode ``pyproject.toml`` *contents* (empty == valid/absent).

    Ports ``validate_yaml.validate_pyproject_toml`` but reads the required Python
    version from ``cfg`` (``container_python``) instead of a hardcoded constant.
    Absence is reported by the ``required-files`` check, so a missing file is a
    no-op here (avoid a duplicate finding).
    """
    path = app_dir / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return [f"pyproject.toml is invalid TOML: {exc}"]

    project = data.get("project")
    if not isinstance(project, dict):
        return ["pyproject.toml: missing [project] table"]

    problems: list[str] = []
    name = project.get("name")
    if not isinstance(name, str) or not name:
        problems.append("pyproject.toml: [project].name must be a non-empty string")

    requires_python = project.get("requires-python")
    if not requires_python or not isinstance(requires_python, str):
        problems.append(
            "pyproject.toml: [project].requires-python is required and must allow Python "
            f"{container_python} (container runtime is {container_python} only)."
        )
    elif not _requires_python_allows(requires_python, container_python):
        problems.append(
            f"pyproject.toml: [project].requires-python={requires_python!r} does not allow "
            f"Python {container_python}. The container runtime is {container_python} only."
        )

    deps = project.get("dependencies")
    if not isinstance(deps, list) or not deps:
        problems.append("pyproject.toml: [project].dependencies must be a non-empty list")
    else:
        declared = {_dep_name(d) for d in deps if isinstance(d, str)}
        missing = [d for d in _REQUIRED_APP_DEPS if d not in declared]
        if missing:
            problems.append(
                f"pyproject.toml: [project].dependencies is missing required packages: "
                f"{missing}. Every Snowflake Streamlit app needs "
                "streamlit + snowflake-snowpark-python."
            )
    return problems


def _check_environment_yml(app_dir: Path) -> list[str]:
    """Validate warehouse-mode ``environment.yml`` *contents* (empty == valid/absent).

    Ports ``validate_yaml.validate_environment_yml``: required ``name`` + deps, and
    the CREATE STREAMLIT landmine (a pinned ``python`` has no exact Anaconda build).
    Absence is reported by ``required-files``, so a missing file is a no-op here.
    """
    path = app_dir / "environment.yml"
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        return [f"environment.yml is invalid YAML: {exc}"]
    if not isinstance(data, dict):
        return ["environment.yml must be a mapping"]

    problems: list[str] = []
    if not data.get("name"):
        problems.append("environment.yml: missing required key: name")

    deps = data.get("dependencies")
    if not isinstance(deps, list) or not deps:
        return [*problems, "environment.yml: dependencies must be a non-empty list"]

    declared: set[str] = set()
    python_specs: list[str] = []
    for dep in deps:
        if not isinstance(dep, str):
            continue
        # conda deps look like "streamlit=1.50.0", "streamlit>=1.50", or "python=3.11".
        # _dep_name extracts the leading distribution name across all operator forms
        # ("streamlit>=1.50" -> "streamlit"), unlike a naive split on "=".
        name = _dep_name(dep)
        if name is None:
            continue
        declared.add(name)
        if name == "python":
            python_specs.append(dep)

    missing = [d for d in _REQUIRED_APP_DEPS if d not in declared]
    if missing:
        problems.append(
            f"environment.yml: dependencies is missing required packages: {missing}. "
            "Every Snowflake Streamlit app needs streamlit + snowflake-snowpark-python."
        )
    if python_specs:
        problems.append(
            f"environment.yml: do not pin `python` in dependencies (found {python_specs}). "
            "The warehouse Anaconda channel has no exact python==3.11 build; the runtime "
            "supplies Python via default_packages (python==3.11.*). Pinning breaks CREATE "
            "STREAMLIT. Remove the python line."
        )
    return problems


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

    entities = data.get("entities")
    if not isinstance(entities, dict) or not entities:
        return [*problems, "snowflake.yml has no entities (must be a mapping)"]

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
        else:
            for key in ("name", "database", "schema"):
                if not identifier.get(key):
                    problems.append(f"{name}: identifier.{key} is required")

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
            # environment.yml contents (required deps + the python-pin landmine)
            # are validated once per app by _check_environment_yml, called from
            # validate_app alongside the matching runtime.

    return problems


def validate_app(app_dir: Path, policy: SchemaPolicy, cfg: Config) -> dict:
    checks: list[dict] = []

    runtime = _detect_runtime(app_dir, cfg.runtime)
    required = list(_BASE_REQUIRED) + (
        ["pyproject.toml"] if runtime == "container" else ["environment.yml"]
    )
    missing = [f for f in required if not (app_dir / f).exists()]
    checks.append({"name": "required-files", "ok": not missing, "findings": missing})

    # The manifest check covers snowflake.yml AND the matching sibling dependency
    # manifest (pyproject.toml for container, environment.yml for warehouse) so a
    # mistyped runtime/dep set fails the gate, not just a missing file.
    manifest_problems = _check_manifest(app_dir, cfg)
    if runtime == "container":
        manifest_problems += _check_pyproject(app_dir, cfg.snowflake.objects.container_python)
    else:
        manifest_problems += _check_environment_yml(app_dir)
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
