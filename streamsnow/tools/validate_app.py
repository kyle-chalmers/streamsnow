"""Aggregate PASS/FAIL preflight for one app — the deterministic ship gate.

Runs the governance checks (required files, naming, runtime-matched manifest,
artifacts, schema-refs, app-security, bind-predicates, caching, sql-tokens,
session-fallback, page-imports, path-leaks, requirements-§11, and the offline
``sql-review check``) over ``apps/<slug>/`` and returns a single PASS/FAIL. A
``placeholders`` check fails while any authored app file still carries the
scaffold's ``YOUR_TABLE`` or the starter page's sample metric and chart. A warn-only
``starter-text`` check covers the prose that scan skips: an app ``AGENTS.md`` still
describing the starter page and ``example_metric.sql``, and a repo README whose Apps
table has no row for the app.
No database, no network, which is why ``check_dependency_vulns`` (OSV.dev) is
deliberately NOT in this aggregate: it runs as its own pre-commit hook
(``--best-effort``) and CI job, as does the CI-only ``tombstones`` check. This is
what the ``/validate-app`` skill and ``/ship-app`` call as the hard gate.

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
    from packaging.utils import canonicalize_name
except ImportError:  # pragma: no cover
    SpecifierSet = None  # type: ignore[assignment]
    InvalidSpecifier = Exception  # type: ignore[assignment,misc]

    def canonicalize_name(name: str) -> str:
        """PEP 503 name normalization fallback (packaging.utils.canonicalize_name)."""
        return re.sub(r"[-_.]+", "-", name).lower()


from ..config import Config, ConfigError, find_config, load_config
from ..policy import SchemaPolicy
from . import (
    check_app_security,
    check_artifacts,
    check_bind_predicates,
    check_caching,
    check_page_imports,
    check_path_leaks,
    check_requirements,
    check_schema_refs,
    check_session_fallback,
    check_sql_tokens,
    sql_review,
)

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

# The scaffold's starter trio (queries/example_metric.sql, the review window in
# sql_review/index.yaml, and pages/overview.py with sample numbers) carry this token until
# replaced. 0.7.1 made it a warning so a fresh scaffold passed its own gate; the
# placeholder app then validated clean and deployed beside the real one (CI
# deploys every apps/*/). It FAILS now: an unfinished scaffold must not ship.
_PLACEHOLDER_RE = re.compile(r"\bYOUR_TABLE\b")
# The starter page's metric and chart are hard-coded samples that never read the query,
# so replacing YOUR_TABLE everywhere still left "1,234" and alpha/beta/gamma on a page
# that validated PASS. The template marks that block STREAMSNOW_STARTER_PLACEHOLDER; the
# sample values match too, which also covers pages scaffolded before the marker existed.
_STARTER_SAMPLE_RE = re.compile(
    r'\bSTREAMSNOW_STARTER_PLACEHOLDER\b|"1,234"\)?, delta="\+5\.3%"|\["alpha", "beta", "gamma"\]'
)
# Authored files only. Generated sql_review page files (NN_<page>.sql) repeat
# their query's text, so scanning them would report every placeholder twice.
_PLACEHOLDER_SUFFIXES = (".py", ".sql", ".json", ".yaml")
_GENERATED_PAGE_FILE_RE = re.compile(r"^\d{2}_[a-z0-9_]+\.sql$")

# Prose the placeholders scan cannot see. The app AGENTS.md is skipped there on purpose,
# because it legitimately names YOUR_TABLE, so a real app shipped with an AGENTS.md that
# still described the starter page and example_metric.sql, and only a human docs review
# caught it. These are Jinja-free lines copied from _templates/app/AGENTS.md.j2; a test
# renders a scaffold and asserts each is still there, so rewording the template cannot
# silently turn the check off.
_STARTER_AGENTS_MARKERS = (
    (
        "starter page with sample numbers; replace it with your real pages.",
        "still describes the starter page (Pages section): list the app's real pages",
    ),
    (
        "`queries/example_metric.sql`: placeholder;",
        "still describes the placeholder example_metric.sql (Queries section): "
        "list the app's real queries",
    ),
    (
        "_None recorded yet._",
        "Data notes still say none recorded: write the grain, metric definitions, "
        "quirks and freshness a reviewer needs",
    ),
)
_README_NONE_YET = "_(none yet)_"
_APPS_HEADING_RE = re.compile(r"^##[ \t]+Apps[ \t]*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^#{1,6}[ \t]+\S", re.MULTILINE)

# Container-runtime fields that must be ABSENT in warehouse mode.
_CONTAINER_ONLY = ("runtime_name", "compute_pool", "external_access_integrations")

# Every Snowflake Streamlit app needs these in its dependency manifest, in either
# runtime. Stored in PEP 503 canonical form so a manifest that spells the package
# differently but equivalently (underscores, dots, case) still matches.
# (Ported from the source monorepo's tools/validate_yaml.REQUIRED_DEPS.)
_REQUIRED_APP_DEPS = (
    canonicalize_name("streamlit"),
    canonicalize_name("snowflake-snowpark-python"),
)
# Leading distribution name of a dependency spec ("streamlit==1.50.0" -> "streamlit",
# conda "streamlit=1.50.0" -> "streamlit"). Canonicalized by _dep_name.
_DEP_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")


def _dep_name(spec: str) -> str | None:
    """Return the PEP 503 canonical distribution name from a dependency spec.

    "streamlit==1.50.0" / "streamlit>=1.50" -> "streamlit"; the equivalent forms
    "snowflake_snowpark_python", "Snowflake.Snowpark.Python" all normalize to
    "snowflake-snowpark-python", so a valid manifest is never reported as missing
    a required package over a spelling difference.
    """
    match = _DEP_NAME_RE.match(spec.strip())
    return canonicalize_name(match.group(1)) if match else None


def _requires_python_allows(spec: str, version: str) -> bool:
    """True if a ``requires-python`` specifier admits ``version`` (e.g. '3.11').

    Uses PEP 440 specifier semantics (``packaging.SpecifierSet``), so '>=3.10'
    correctly *allows* 3.11 while '<3.11' / '==3.10.*' correctly do not — a naive
    token match gets both boundaries wrong, so it is intentionally NOT used as a
    fallback. ``packaging`` is a declared dependency; if it is somehow absent we
    cannot evaluate the specifier, so we assume it is fine (fail open) rather than
    emit a wrong finding. A malformed specifier fails closed (not a valid pin).
    """
    if SpecifierSet is None:  # pragma: no cover - packaging is a hard dependency
        return True
    try:
        return SpecifierSet(spec).contains(version)
    except InvalidSpecifier:
        return False


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
            data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
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
        data = tomllib.loads(path.read_text(encoding="utf-8"))
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
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
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
        data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
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


def _is_generated_review_sql(app_dir: Path, path: Path) -> bool:
    rel = path.relative_to(app_dir).parts
    return len(rel) == 2 and rel[0] == "sql_review" and bool(_GENERATED_PAGE_FILE_RE.match(rel[1]))


def _check_placeholders(app_dir: Path) -> list[dict]:
    """Authored app files (queries, pages, manifests) still carrying starter content.

    Two kinds, one finding per file and kind, at the first occurrence: the
    ``YOUR_TABLE`` token, and the starter page's sample metric and chart. The
    sample values never read the query, so a page that no longer mentions
    ``YOUR_TABLE`` can still show them: repointing the query does not clear the gate.
    """
    kinds = (
        (
            _PLACEHOLDER_RE,
            "scaffold placeholder YOUR_TABLE: replace the starter content "
            "(repoint the query and the review window in sql_review/index.yaml at a real "
            "table, or delete the starter query, its index.yaml entry and pages/overview.py "
            "once real pages exist). CI deploys every app under apps/",
        ),
        (
            _STARTER_SAMPLE_RE,
            "starter page sample values (the hard-coded metric and chart marked "
            "STREAMSNOW_STARTER_PLACEHOLDER): render real query results in their place, or "
            "replace the page with a real one. CI deploys every app under apps/",
        ),
    )
    found: list[dict] = []
    for path in sorted(_walk_app_files(app_dir)):
        if path.suffix not in _PLACEHOLDER_SUFFIXES or _is_generated_review_sql(app_dir, path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern, detail in kinds:
            m = pattern.search(text)
            if m:
                found.append(
                    {
                        "file": path.relative_to(app_dir).as_posix(),
                        "line": text.count("\n", 0, m.start()) + 1,
                        "detail": detail,
                    }
                )
    return found


def _first_table(section: str) -> str:
    """The first markdown table in ``section`` (consecutive lines starting with ``|``).

    The README checks read only table rows: a prose line that names ``apps/<slug>/`` is not
    a row, and a ``_(none yet)_`` in a later table or note is not the Apps table.
    """
    rows: list[str] = []
    for raw in section.splitlines():
        text = raw.strip()
        if text.startswith("|"):
            rows.append(text)
        elif rows:
            break  # a blank line or prose ends the table
    return "\n".join(rows)


def _check_starter_text(app_dir: Path, repo_root: Path) -> list[dict]:
    """Warnings for starter prose the placeholders gate does not scan (warn-only).

    Two sources. The app's ``AGENTS.md`` still carrying the scaffold's lines about the
    starter page, ``example_metric.sql`` or empty data notes. And the repo ``README.md``
    whose ``## Apps`` table still reads ``_(none yet)_`` or has no ``apps/<slug>/`` row,
    which is what ``streamsnow new`` leaves behind when the README was written before the
    app existed. Both read as finished docs while describing an app that is not there.

    Warn-only because a stricter check must not fail a repo that passed before. A repo
    with no README, or a README with no ``## Apps`` heading, is silent: the README is not
    ours to demand.
    """
    warnings: list[dict] = []
    agents = app_dir / "AGENTS.md"
    if agents.is_file():
        try:
            text = agents.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for marker, detail in _STARTER_AGENTS_MARKERS:
            idx = text.find(marker)
            if idx != -1:
                warnings.append(
                    {
                        "file": "AGENTS.md",
                        "line": text.count("\n", 0, idx) + 1,
                        "detail": f"scaffold text, {detail}",
                    }
                )

    readme = repo_root / "README.md"
    if readme.is_file():
        try:
            body = readme.read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = ""
        heading = _APPS_HEADING_RE.search(body)
        if heading:
            rest = body[heading.end() :]
            nxt = _NEXT_HEADING_RE.search(rest)
            section = rest[: nxt.start()] if nxt else rest
            table = _first_table(section)
            line = body.count("\n", 0, heading.start()) + 1
            slug_row = re.search(rf"apps/{re.escape(app_dir.name)}(?![\w-])", table)
            if _README_NONE_YET in table:
                detail = (
                    f"README.md Apps table still says {_README_NONE_YET}: "
                    f"add a row for `apps/{app_dir.name}/`"
                )
            elif not slug_row:
                detail = f"README.md Apps table has no row for `apps/{app_dir.name}/`"
            else:
                detail = ""
            if detail:
                warnings.append({"file": "README.md", "line": line, "detail": detail})
    return warnings


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

    arts = check_artifacts.check_app(app_dir, cfg.deploy.artifact_exclude)
    checks.append({"name": "artifacts", "ok": arts["ok"], "findings": arts["findings"]})

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
    tokens = check_sql_tokens.scan_paths(files)
    checks.append({"name": "sql-tokens", "ok": tokens["ok"], "findings": tokens["findings"]})
    session = check_session_fallback.scan_paths(files)
    checks.append(
        {"name": "session-fallback", "ok": session["ok"], "findings": session["findings"]}
    )
    # Same family as session-fallback: resolves under `streamlit run`, fails deployed.
    # Takes the app dir, not `files` — a violation can live in a file the caller never
    # passed (see check_page_imports.scan_paths).
    imports = check_page_imports.check_app(app_dir)
    checks.append({"name": "page-imports", "ok": imports["ok"], "findings": imports["findings"]})
    cache = check_caching.scan_paths(files)
    checks.append({"name": "caching", "ok": cache["ok"], "findings": cache["findings"]})
    leaks = check_path_leaks.scan_paths(files)
    checks.append({"name": "path-leaks", "ok": leaks["ok"], "findings": leaks["findings"]})
    # §11 build-state contract — what /build-app resumes from. The check is a
    # no-op for apps without a REQUIREMENTS.md (spec presence is a build-phase
    # concern, not a ship gate).
    reqs = check_requirements.scan_paths([app_dir])
    checks.append({"name": "requirements", "ok": reqs["ok"], "findings": reqs["findings"]})

    # sql_review: index, provenance, markers, objects, lint, comments. Those
    # always fail: they mean the committed review SQL does not match what the
    # app runs. Coverage (a nav page or query that index.yaml does not account
    # for) follows `sql_review.coverage` in config: `warn` (default) reports
    # it, `fail` gates on it, so an adopting fleet backfills on its own schedule.
    # `advisory` never gates; split_by_policy is the one place those rules live.
    # `check` is import-free by design, so it is safe inside this gate.
    sqlr = sql_review._check_app(app_dir.parent.parent, app_dir)
    policy = cfg.sql_review.coverage
    hard, soft = sql_review.split_by_policy(sqlr, policy)
    checks.append(
        {
            "name": f"sql-review (coverage policy: {policy})",
            "ok": not hard,
            "findings": hard,
            "warnings": soft,
        }
    )

    placeholders = _check_placeholders(app_dir)
    checks.append({"name": "placeholders", "ok": not placeholders, "findings": placeholders})

    # Warn-only: never sets ok=False, so the exit code and PASS/FAIL are unchanged.
    starter = _check_starter_text(app_dir, app_dir.parent.parent)
    checks.append({"name": "starter-text", "ok": True, "findings": [], "warnings": starter})

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
            str(f[k])
            for k in ("kind", "func", "schema", "token", "detail")
            if f.get(k) not in (None, "")
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
        # Config discovery is anchored on --dir, not the process cwd: running
        # `validate-app x --dir /repo` from elsewhere must apply /repo's
        # policy, not whatever config the caller's cwd happens to sit under.
        cfg_path = Path(args.config) if args.config else find_config(Path(args.dir).resolve())
        if cfg_path is None:
            print(f"config error: no streamsnow.config.yaml found under {Path(args.dir).resolve()}")
            return 2
        cfg = load_config(cfg_path)
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
            warnings = c.get("warnings") or []
            mark = "✓" if c["ok"] else "✗"
            if c["ok"] and warnings:
                mark = "!"
            suffix = "" if c["ok"] else f"  ({len(c['findings'])} issue(s))"
            if warnings:
                suffix += f"  ({len(warnings)} warning(s))"
            print(f"  {mark} {c['name']}{suffix}")
            if not c["ok"]:
                for f in c["findings"][:10]:
                    print(f"      - {_format_finding(f)}")
            for w in warnings[:10]:
                print(f"      ~ {_format_finding(w)}")
        print(f"\n{'PASS' if result['ok'] else 'FAIL'}: {result['app']} ({result['runtime']})")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
