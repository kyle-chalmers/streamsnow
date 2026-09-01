"""Deterministic detection engine behind the ``/migrate-app`` skill.

Migrating an external Streamlit app is a two-step flow: lift-and-shift the
source into ``apps/<slug>/``, then conform it to StreamSnow conventions. Both
steps need *detection* that must not depend on an LLM's mood: whether the
source is migratable at all, what would block a commit, how its dependencies
translate, and which conventions the conform pass still owes. This CLI is that
detection layer — the skill invokes it via subprocess and reasons over the
JSON.

Every subcommand emits JSON on stdout, human-readable errors on stderr, and a
deterministic exit code. AST-based detection only — no code execution
(``ast.parse``; never ``exec``/``eval``/``import``), so identical inputs always
produce identical outputs, and a hostile source tree can't run code on the
migrating machine.

Subcommands
-----------
preflight <source> --target-slug <slug> [--dir <repo-root>]
    Is the source safe to migrate at all? Detects target-dir collisions,
    non-Streamlit sources, multiple entrypoints, and catastrophic dependencies
    (web frameworks / ML stacks that no Snowflake Streamlit runtime hosts).
    Exit 0 on pass, 1 on abort.

scan-hardfails <source> [--config ...]
    What must be scrubbed before the lift commit? Denied-schema references in
    SQL-looking literals (denylist from ``governance.schema_deny`` via
    :class:`streamsnow.policy.SchemaPolicy` — one policy, shared with
    ``check_schema_refs``), hardcoded credentials at module scope, and the
    *presence* of ``.streamlit/secrets.toml`` / ``.env`` (presence only — this
    tool never reads a secrets file's contents, so a secret can't leak into
    JSON output or a transcript). Exit 0 on clean, 1 on blocks.

translate-deps <source> --out <env.yml> [--offline]
    Translate the source's dependency manifest (requirements.txt /
    pyproject.toml / Pipfile / environment.yml) into a warehouse-mode
    ``environment.yml``: PEP 440 pins become conda pins, packages are checked
    against Snowflake's public Anaconda channel repodata (24h-cached; offline
    fallback to a conservative allowlist), and un-hostable specs are reported
    with reasons instead of silently dropped. No manifest → imports are
    AST-inferred as *suggestions only*, never auto-added. Exit 0; 2 if --out
    is unwritable.

graft-plan <source>
    Where does the source's UI land in the scaffold? ``pages/*`` for a legacy
    pages/-directory app, ``pages/overview.py`` for a single file organized
    around module-scope ``st.tabs`` (grafting it into the entrypoint would
    collide with the scaffold's ``st.navigation`` shell), ``streamlit_app.py``
    otherwise. Informational — exit 0 always.

scan-imports <source>
    Relative imports and nested ``__init__.py`` files, so the skill knows which
    subpackages must be grafted whole. Exit 0 always.

scan-conformance <app-dir> [--config ...]
    The conform pass's worklist: uncached data-fetches, ``SELECT *`` literals,
    altair imports, a legacy pages/ layout (no ``st.navigation``), and the
    (database, schema) grants the app needs — split into schemas the CI role
    already covers (``governance.database`` × ``schema_allow``) vs ones that
    need a DBA. Exit 0 always.

scan-inline-sql <app-dir>
    Inline SQL literals that the SQL-file-organization convention says belong
    in ``queries/*.sql`` behind ``load_sql``/``render_sql``. Already-
    externalized queries self-filter (their call arg is a bare query *name*,
    not a SQL body). ``# noqa: inline-sql`` exempts plumbing queries. Exit 0
    always.

Exit codes: 0 = clean/pass, 1 = abort/blocks, 2 = tool or config error.
"""

from __future__ import annotations

import argparse
import ast
import getpass
import json
import re
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from packaging.requirements import InvalidRequirement, Requirement

from ..config import Config, ConfigError, load_config
from ..policy import SchemaPolicy

# The denied-schema detection itself lives in check_schema_refs — one
# implementation consumed by pre-commit, validate-app, AND this scanner.
from .check_schema_refs import find_denied_refs

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

# Packages that no Snowflake Streamlit runtime hosts sensibly: web frameworks
# want to own the server process (Streamlit already does), and the heavy ML
# stacks blow past what a shared app runtime should carry. Their presence means
# the source is not a plain dashboard — abort and make the human look.
CATASTROPHIC_DEPS = {
    "fastapi",
    "django",
    "flask",
    "tornado",
    "aiohttp",
    "celery",
    "torch",
    "tensorflow",
}

# Conservative fallback when the Anaconda-channel repodata is unreachable:
# packages we are confident the snowflake channel carries. Unknown packages
# get an "unverifiable offline" reason instead of a hard "unavailable".
OFFLINE_ANACONDA_ALLOWLIST = {
    "streamlit",
    "snowflake-snowpark-python",
    "pandas",
    "plotly",
    "numpy",
    "python-dateutil",
    "pyarrow",
    "altair",
    "pydeck",
    "requests",
    "scipy",
    "scikit-learn",
}

REPODATA_URLS = (
    "https://repo.anaconda.com/pkgs/snowflake/noarch/repodata.json",
    # The warehouse runtime targets linux-64 — binary builds of pandas/numpy/
    # pyarrow/etc. live under that subdir, NOT under noarch. Checking noarch
    # alone falsely reports the most common data deps as unavailable.
    "https://repo.anaconda.com/pkgs/snowflake/linux-64/repodata.json",
)
# Per-user cache path: a shared /tmp file on a multi-user machine is both a
# collision and a (mild) poisoning surface — getpass.getuser() scopes it.
REPODATA_CACHE = (
    Path(tempfile.gettempdir()) / f"streamsnow-{getpass.getuser()}-anaconda-repodata.json"
)
REPODATA_TTL = 86400  # 24 hours

# Default conda pin injected when the source doesn't declare streamlit. Keep in
# sync with _templates/app/environment.yml.j2 (a version the Anaconda channel
# actually ships — the channel lags PyPI, so never blindly track the container
# pin).
_DEFAULT_STREAMLIT_PIN = "streamlit=1.50.0"

SELECT_STAR_RE = re.compile(r"(?is)\bSELECT\s+\*\s+FROM\b")

AWS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")
SECRET_VAR_NAMES = {
    "password",
    "snowflake_account",
    "snowflake_password",
    "aws_access_key_id",
    "aws_secret_access_key",
    "api_key",
    "secret_key",
}
# A "secret" whose value is an obvious placeholder is documentation, not a leak.
PLACEHOLDER_RE = re.compile(r"^\s*(<|TODO|FIXME|\$\{|\{\{)")

# Attribute names whose calls fetch data (Snowpark session.sql / st.connection
# query / DataFrame materialization). Used to find uncached fetches.
SQL_CALL_ATTRS = {"query", "sql", "to_pandas", "collect"}

SKIP_DIRS = {
    "__pycache__",
    ".venv",
    "venv",
    ".git",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
}

# Tight allowlist of packages translate-deps may *suggest* when the source has
# no dependency manifest. Anything outside this set is ignored rather than
# dragging random third-party imports into environment.yml. Spelled out inline
# so reviewers can eyeball the list without chasing constants.
INFERENCE_ALLOWLIST = frozenset(
    {
        "altair",
        "matplotlib",
        "seaborn",
        "bokeh",
        "plotly",
        "folium",
        "pydeck",
        "numpy",
        "scipy",
        "sklearn",
        "duckdb",
        "pyarrow",
    }
)

# Stdlib names we never emit as suggestions — fallback for exotic builds that
# lack sys.stdlib_module_names.
_STDLIB_FALLBACK = frozenset(
    {
        "os",
        "sys",
        "re",
        "json",
        "ast",
        "typing",
        "pathlib",
        "itertools",
        "functools",
        "subprocess",
        "urllib",
        "socket",
        "time",
        "tomllib",
        "argparse",
        "dataclasses",
        "datetime",
        "math",
        "io",
        "copy",
        "enum",
    }
)

# Regex patterns for fully-qualified SQL / Snowpark object refs, used by the
# required-grants scan. Both require UPPERCASE identifiers on purpose:
# Snowflake object names are canonically uppercase in SQL strings and Snowpark
# calls, while Python imports (``from snowflake.snowpark.context import ...``)
# use lowercase module paths — requiring uppercase keeps the scanner from
# flagging an import statement as a missing grant.
SQL_FQN_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Z_][A-Z0-9_]*)\.([A-Z_][A-Z0-9_]*)\.([A-Z_][A-Z0-9_]*)\b"
)
TABLE_CALL_RE = re.compile(
    r"""\.table\(\s*['"]([A-Z_][A-Z0-9_]*)\."""
    r"""([A-Z_][A-Z0-9_]*)\."""
    r"""([A-Z_][A-Z0-9_]*)['"]"""
)


# --------------------------------------------------------------------------- #
# Filesystem helpers                                                          #
# --------------------------------------------------------------------------- #


def _safe_under(root_resolved: Path, p: Path) -> bool:
    """True if *p* resolves to a path under *root_resolved*.

    Guards against symlink escape: ``Path.rglob`` follows symlinks, so a
    malicious/accidental symlink inside an *external* source tree could point
    at ``/etc/`` or ``~/.ssh/`` and make the scanner read arbitrary files.
    Resolving the candidate and requiring it to stay under the resolved root
    neutralizes that (and swallows the ``ValueError`` from ``relative_to``).
    """
    try:
        p.resolve(strict=False).relative_to(root_resolved)
        return True
    except (OSError, ValueError):
        return False


def _py_files(root: Path) -> list[Path]:
    """Sorted .py files under *root*, skipping venvs, caches, symlink escapes."""
    root_resolved = root.resolve()
    out: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if not _safe_under(root_resolved, path):
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        out.append(path)
    return out


def _rel_posix(path: Path, root: Path) -> str:
    """POSIX-style path of *path* relative to *root* (stable across platforms)."""
    return path.relative_to(root).as_posix()


def _parse_py(path: Path) -> ast.AST | None:
    """Parse a .py file; None on syntax errors or unreadable files.

    An unparseable file can't run as an app anyway — ruff/CI own the syntax
    error, and a text fallback here would reintroduce the docstring/prose
    false positives the AST scan exists to avoid.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        return None


# --------------------------------------------------------------------------- #
# AST helpers                                                                 #
# --------------------------------------------------------------------------- #


def _collect_docstring_ids(tree: ast.AST) -> set[int]:
    """``id()`` of every Constant that is a module/class/function docstring.

    Docstrings are excluded from SQL-shaped scans: prose *about* a query (or a
    ban) is not an instruction to the database, and flagging it blocks
    migrations over documentation.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _attr_chain(node: ast.AST) -> str:
    """Render an Attribute/Name chain as dotted form (e.g. ``st.connection``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_attr_chain(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _attr_chain(node.func)
    return ""


def _has_cache_data_decorator(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if *func* carries ``@st.cache_data`` (or bare ``@cache_data``)."""
    for dec in func.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "cache_data":
            return True
        if isinstance(target, ast.Name) and target.id == "cache_data":
            return True
    return False


# --------------------------------------------------------------------------- #
# Dep-manifest discovery                                                      #
# --------------------------------------------------------------------------- #

DEPS_MANIFEST_ORDER = ("requirements.txt", "pyproject.toml", "Pipfile", "environment.yml")


def _find_deps_manifest(source: Path) -> str | None:
    for name in DEPS_MANIFEST_ORDER:
        if (source / name).is_file():
            return name
    return None


def _extract_dep_specs(source: Path, manifest: str) -> list[str]:
    """Raw dependency specifier strings from *manifest*.

    Version pins are preserved in the strings so translate-deps can re-parse;
    preflight only needs the name portion for catastrophic detection.
    """
    path = source / manifest
    try:
        if manifest == "pyproject.toml":
            with open(path, "rb") as f:
                data = tomllib.load(f)
            deps = data.get("project", {}).get("dependencies") or []
            return [str(d) for d in deps if isinstance(d, str)]
        if manifest == "requirements.txt":
            out: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(("-r", "-e", "--")):
                    continue
                out.append(line)
            return out
        if manifest == "Pipfile":
            with open(path, "rb") as f:
                data = tomllib.load(f)
            out = []
            for name, spec in (data.get("packages") or {}).items():
                if isinstance(spec, str) and spec not in ("*", ""):
                    out.append(f"{name}{spec}" if spec[0] in "=<>~!" else f"{name}=={spec}")
                else:
                    out.append(name)
            return out
        if manifest == "environment.yml":
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                return []
            deps = data.get("dependencies") or []
            flat: list[str] = []
            for item in deps:
                if isinstance(item, str):
                    flat.append(item)
                elif isinstance(item, dict):  # e.g. {"pip": ["foo"]}
                    for inner in item.values():
                        if isinstance(inner, list):
                            flat.extend(str(x) for x in inner if isinstance(x, str))
            return flat
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return []


_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")


def _dep_name(spec: str) -> str | None:
    """Bare package name from a dependency specifier string, lowercased."""
    match = _NAME_RE.match(spec.strip())
    return match.group(1).lower() if match else None


# --------------------------------------------------------------------------- #
# preflight                                                                   #
# --------------------------------------------------------------------------- #


def _scan_entrypoints(source: Path) -> tuple[list[str], bool]:
    """Return (entrypoints, is_streamlit_app) for a source tree.

    An entrypoint is a .py file whose module-level body calls
    ``st.set_page_config`` (or ``streamlit.set_page_config``). The tree is a
    Streamlit app if it has ≥1 entrypoint OR any file imports streamlit —
    pages/ modules often never call set_page_config themselves.
    """
    entrypoints: list[str] = []
    imports_streamlit = False

    for path in _py_files(source):
        tree = _parse_py(path)
        if tree is None:
            continue

        has_config = False
        for node in getattr(tree, "body", []):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                callee = _attr_chain(node.value.func)
                if callee.endswith("set_page_config"):
                    has_config = True
                    break

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Import)
                and any(alias.name.split(".")[0] == "streamlit" for alias in node.names)
            ) or (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] == "streamlit"
            ):
                imports_streamlit = True

        if has_config:
            entrypoints.append(_rel_posix(path, source))

    return entrypoints, (bool(entrypoints) or imports_streamlit)


def preflight(source: Path, target_slug: str, repo_root: Path) -> tuple[int, dict[str, Any]]:
    """Run preflight checks; return (exit_code, result)."""
    entrypoints, is_streamlit_app = _scan_entrypoints(source)
    deps_manifest = _find_deps_manifest(source)

    catastrophic: list[str] = []
    if deps_manifest:
        seen: set[str] = set()
        for spec in _extract_dep_specs(source, deps_manifest):
            name = _dep_name(spec)
            if name and name in CATASTROPHIC_DEPS and name not in seen:
                catastrophic.append(name)
                seen.add(name)

    target_path = repo_root / "apps" / target_slug
    target_exists = target_path.exists()

    abort = False
    abort_reason: str | None = None

    if target_exists:
        abort = True
        abort_reason = (
            f"Target directory apps/{target_slug}/ already exists. "
            "Pick a different slug or remove it first."
        )
    elif not is_streamlit_app:
        abort = True
        abort_reason = (
            "Source doesn't appear to be a Streamlit app (no st.set_page_config "
            "call and no `import streamlit`)."
        )
    elif len(entrypoints) > 1:
        abort = True
        abort_reason = (
            f"Source has multiple entrypoints: {entrypoints}. Migrate one entrypoint at a time."
        )
    elif catastrophic:
        abort = True
        abort_reason = (
            f"Source depends on {catastrophic}, which are unavailable on "
            "Snowflake's Anaconda channel and unsuitable for a hosted Streamlit "
            "app (web frameworks / heavy ML stacks). If the dependency is real, "
            "the app needs the container runtime and a PyPI-installable dep set "
            "— confirm with the user before re-running."
        )

    result = {
        "is_streamlit_app": is_streamlit_app,
        "entrypoints": entrypoints,
        "deps_manifest": deps_manifest,
        "target_exists": target_exists,
        "catastrophic_deps": catastrophic,
        "abort": abort,
        "abort_reason": abort_reason,
    }
    return (1 if abort else 0), result


# --------------------------------------------------------------------------- #
# scan-hardfails                                                              #
# --------------------------------------------------------------------------- #


def _scan_schema_refs(source: Path, policy: SchemaPolicy) -> list[dict[str, Any]]:
    """Denied-schema references in .py and .sql files under *source*.

    Delegates to :func:`check_schema_refs.find_denied_refs` — the same
    implementation pre-commit and validate-app run — so the migration scanner
    can never drift from the enforcement the lifted app will face at commit
    time. Python files get the AST scan (docstrings and prose excluded); .sql
    files get the comment-stripping text scan.
    """
    refs: list[dict[str, Any]] = []
    root_resolved = source.resolve()
    for path in sorted(source.rglob("*")):
        if path.suffix not in (".py", ".sql") or not path.is_file():
            continue
        if not _safe_under(root_resolved, path):
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(source).parts[:-1]):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        file_rel = _rel_posix(path, source)
        for line_no, schema in find_denied_refs(text, policy, is_python=path.suffix == ".py"):
            refs.append({"file": file_rel, "line": line_no, "schema": schema})
    return refs


def _scan_secrets_in_py(source: Path) -> list[dict[str, Any]]:
    """Module-scope secret-like assignments and AWS-access-key string shapes."""
    hits: list[dict[str, Any]] = []
    for path in _py_files(source):
        tree = _parse_py(path)
        if tree is None:
            continue
        file_rel = _rel_posix(path, source)

        # 1. Module-scope Assign targets whose name looks like a credential.
        for node in getattr(tree, "body", []):
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                continue
            value = node.value.value
            if len(value) <= 4 or PLACEHOLDER_RE.match(value):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.lower() in SECRET_VAR_NAMES:
                    hits.append({"file": file_rel, "line": node.lineno, "kind": target.id.lower()})

        # 2. Any string constant matching the AWS access-key shape.
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and AWS_KEY_RE.search(node.value)
            ):
                hits.append({"file": file_rel, "line": node.lineno, "kind": "aws_access_key_id"})

    # De-duplicate (the AWS pass can re-hit an Assign already caught above).
    seen: set[tuple[str, int, str]] = set()
    unique: list[dict[str, Any]] = []
    for hit in hits:
        key = (hit["file"], hit["line"], hit["kind"])
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    return unique


def scan_hardfails(source: Path, policy: SchemaPolicy) -> tuple[int, dict[str, Any]]:
    schema_refs = _scan_schema_refs(source, policy)
    secrets_in_py = _scan_secrets_in_py(source)
    # IMPORTANT: presence-check only — never read a secrets file's contents.
    has_secrets_toml = (source / ".streamlit" / "secrets.toml").exists()
    has_env_file = (source / ".env").exists()
    blocks = bool(schema_refs) or bool(secrets_in_py)
    result = {
        "schema_refs": schema_refs,
        "secrets_in_py": secrets_in_py,
        "has_secrets_toml": has_secrets_toml,
        "has_env_file": has_env_file,
        "blocks": blocks,
    }
    return (1 if blocks else 0), result


# --------------------------------------------------------------------------- #
# graft-plan                                                                  #
# --------------------------------------------------------------------------- #


def _detects_module_scope_tabs(tree: ast.AST) -> bool:
    """True if any module-scope statement is (or assigns from) ``st.tabs(...)``.

    Walk ``tree.body`` directly (NOT ``ast.walk``) — only top-level tabs
    matter. Tabs nested inside a function are an internal detail that won't
    collide with the scaffold's ``st.navigation`` shell. Two shapes count:
    bare ``st.tabs(...)`` expressions and ``tab1, tab2 = st.tabs(...)``.
    """
    if not isinstance(tree, ast.Module):
        return False
    for stmt in tree.body:
        call: ast.Call | None = None
        if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)) or (
            isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)
        ):
            call = stmt.value
        if call is None:
            continue
        func = call.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "tabs"
            and isinstance(func.value, ast.Name)
            and func.value.id == "st"
        ):
            return True
    return False


def graft_plan(source: Path) -> tuple[int, dict[str, Any]]:
    """Decide the graft target for the lift step. Informational — never aborts.

    Decision table:
    1. Source has a populated ``pages/`` directory → graft pages 1:1.
    2. Single-file source organized around module-scope ``st.tabs`` → graft to
       ``pages/overview.py``: merging a tabs layout into the entrypoint would
       fight the scaffold's ``st.navigation`` shell for the page structure.
    3. Otherwise → merge into the scaffold ``streamlit_app.py``.
    """
    pages_dir = source / "pages"
    source_has_pages = pages_dir.is_dir() and any(pages_dir.rglob("*.py"))

    entrypoints: list[Path] = []
    uses_tabs = False
    for path in _py_files(source):
        tree = _parse_py(path)
        if tree is None or not isinstance(tree, ast.Module):
            continue
        has_config = False
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                func = stmt.value.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "set_page_config"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "st"
                ):
                    has_config = True
                    break
        if has_config:
            entrypoints.append(path)
            # Only the entrypoint file's tabs matter — helper-module tabs
            # would not collide with st.navigation.
            if _detects_module_scope_tabs(tree):
                uses_tabs = True

    entrypoint_count = len(entrypoints)

    if source_has_pages:
        graft_target = "pages/*"
        reason = "source has pages/ directory — graft each page 1:1 into target pages/"
    elif entrypoint_count == 1 and uses_tabs:
        graft_target = "pages/overview.py"
        reason = (
            "single-file source with st.tabs at module scope — grafting to "
            "pages/overview.py to avoid collision with the scaffold's "
            "st.navigation shell"
        )
    else:
        graft_target = "streamlit_app.py"
        reason = "single-file source (no tabs) — merging into the scaffold streamlit_app.py"

    return 0, {
        "graft_target": graft_target,
        "reason": reason,
        "source_has_pages": source_has_pages,
        "source_uses_st_tabs": uses_tabs,
        "source_entrypoint_count": entrypoint_count,
    }


# --------------------------------------------------------------------------- #
# scan-imports                                                                #
# --------------------------------------------------------------------------- #


def scan_imports(source: Path) -> tuple[int, dict[str, Any]]:
    relative: list[dict[str, Any]] = []
    init_files: list[str] = []

    for path in _py_files(source):
        tree = _parse_py(path)
        if tree is None:
            continue
        file_rel = _rel_posix(path, source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.level or 0) > 0:
                module = ("." * node.level) + (node.module or "")
                relative.append({"file": file_rel, "lineno": node.lineno, "module": module})

    source_resolved = source.resolve()
    for path in sorted(source.rglob("__init__.py")):
        if not _safe_under(source_resolved, path):
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(source).parts):
            continue
        rel = _rel_posix(path, source)
        # Only nested __init__.py files (non-root) matter for grafting.
        if "/" in rel:
            init_files.append(rel)

    return 0, {"relative_imports": relative, "subpackage_init_files": init_files}


# --------------------------------------------------------------------------- #
# translate-deps                                                              #
# --------------------------------------------------------------------------- #


def _stdlib_names() -> frozenset[str]:
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return frozenset(names)
    return _STDLIB_FALLBACK


def _infer_suggestions(source: Path) -> list[dict[str, Any]]:
    """AST-scan .py files for top-level imports matching INFERENCE_ALLOWLIST.

    Called only when translate-deps finds no source manifest. Suggestions
    only — environment.yml is never auto-populated from this scan, because a
    guessed dependency that happens to exist on the channel would deploy and
    then fail at import-mismatch time, which is far harder to debug than an
    absent line.

    - ``import X`` / ``import X.y`` / ``from X.y import z`` → root package X.
    - Relative imports are local modules by definition — skipped.
    - Stdlib names and first-party modules (a root-level ``foo.py`` or
      ``foo/``) are skipped even if allowlisted.
    - Confidence: ``high`` when imported from ≥2 distinct files, else
      ``medium``.
    """
    stdlib = _stdlib_names()

    local_modules: set[str] = set()
    if source.is_dir():
        for child in source.iterdir():
            if child.is_file() and child.suffix == ".py":
                local_modules.add(child.stem)
            elif child.is_dir():
                local_modules.add(child.name)

    hits: dict[str, set[str]] = {}
    for path in _py_files(source):
        tree = _parse_py(path)
        if tree is None:
            continue
        rel = _rel_posix(path, source)
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name:
                        imported_roots.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue
                if node.module:
                    imported_roots.add(node.module.split(".")[0])
        for name in imported_roots:
            if name in stdlib or name in local_modules or name not in INFERENCE_ALLOWLIST:
                continue
            hits.setdefault(name, set()).add(rel)

    suggestions: list[dict[str, Any]] = []
    for name in sorted(hits):
        files = sorted(hits[name])
        confidence = "high" if len(files) >= 2 else "medium"
        suggestions.append({"package": name, "source_files": files, "confidence": confidence})
    return suggestions


def _get_snowflake_packages(offline: bool = False) -> tuple[set[str], bool]:
    """Return (package-name set, is_online) for the Snowflake Anaconda channel.

    Aggregates names across ``noarch`` (pure-Python) and ``linux-64`` (binary
    builds — the warehouse runtime is Linux). The merged name list is cached
    in the system temp dir for 24h so repeated migrate runs don't refetch two
    multi-MB repodata files. On ``offline=True`` or any network failure, fall
    back to :data:`OFFLINE_ANACONDA_ALLOWLIST` with ``is_online=False`` so
    callers can soften "unavailable" to "unverifiable".
    """
    if offline:
        return {p.lower() for p in OFFLINE_ANACONDA_ALLOWLIST}, False
    try:
        mtime = REPODATA_CACHE.stat().st_mtime if REPODATA_CACHE.exists() else 0
        if (time.time() - mtime) > REPODATA_TTL:
            merged_names: set[str] = set()
            for url in REPODATA_URLS:
                with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
                    data = json.loads(resp.read().decode("utf-8"))
                for bucket in ("packages", "packages.conda"):
                    for pkg in (data.get(bucket) or {}).values():
                        if isinstance(pkg, dict) and pkg.get("name"):
                            merged_names.add(str(pkg["name"]).lower())
            if not merged_names:
                raise ValueError("empty repodata")
            # Persist as a simple name list so re-reads are cheap. Write
            # atomically (temp + rename) so a concurrent invocation can't
            # observe a partial JSON and be forced into a spurious refetch.
            tmp_path = REPODATA_CACHE.with_suffix(".tmp")
            tmp_path.write_text(json.dumps({"names": sorted(merged_names)}), encoding="utf-8")
            tmp_path.replace(REPODATA_CACHE)
            return merged_names, True
        cached = json.loads(REPODATA_CACHE.read_text(encoding="utf-8"))
        names = {str(n).lower() for n in cached.get("names", [])}
        if not names:
            raise ValueError("empty cache")
        return names, True
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return {p.lower() for p in OFFLINE_ANACONDA_ALLOWLIST}, False


_SPECIFIER_SPLIT_RE = re.compile(
    r"""^\s*
        [A-Za-z0-9_.\-]+            # package name
        (?:\[[^\]]+\])?             # optional extras
        \s*
        (?P<specifier>[^;]*?)       # specifier (stop at marker separator)
        \s*
        (?:;.*)?                    # optional environment marker
        \s*$""",
    flags=re.VERBOSE,
)


def _extract_source_specifier(spec: str) -> str:
    """Raw specifier portion of *spec*, preserving source order.

    ``pandas>=2,<3`` → ``>=2,<3``. ``packaging.SpecifierSet`` would reorder
    to ``<3,>=2`` — valid PEP 440 but surprising to a reviewer diffing the
    generated environment.yml against the source manifest.
    """
    match = _SPECIFIER_SPLIT_RE.match(spec)
    if not match:
        return ""
    return (match.group("specifier") or "").strip()


def _convert_specifier(specifier: str) -> str:
    """Convert a PEP 440 specifier string to conda-pin syntax.

    - ``==X.Y.Z`` → ``=X.Y.Z`` (single ``=``)
    - ``~=X.Y``   → ``>=X.Y,<X+1`` ; ``~=X.Y.Z`` → ``>=X.Y.Z,<X.Y+1``
    - compound / inequality specifiers pass through (conda accepts them)
    - empty → empty (caller uses the bare name)
    """
    spec = (specifier or "").strip()
    if not spec:
        return ""

    # Split on commas preserving source order (see _extract_source_specifier).
    specs = [s.strip() for s in spec.split(",") if s.strip()]

    out: list[str] = []
    for s in specs:
        if s.startswith("=="):
            out.append("=" + s[2:])
        elif s.startswith("~="):
            base = s[2:]
            parts = base.split(".")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                lo = f">={base}"
                if len(parts) == 2:
                    hi = f"<{int(parts[0]) + 1}"
                else:
                    hi = f"<{parts[0]}.{int(parts[1]) + 1}"
                out.append(f"{lo},{hi}")
            else:
                out.append(s)  # unexpected shape — pass through unchanged
        else:
            out.append(s)
    return ",".join(out)


def _parse_requirement(spec: str) -> tuple[str | None, str, dict[str, Any]]:
    """Parse *spec* into (name_lower, raw_specifier, meta).

    meta may hold ``extras`` (list), ``marker`` (str), or ``dropped_reason``.
    ``packaging.Requirement`` does the heavy lifting; a regex fallback keeps
    conda-style pins (``pandas=2.1``) and other non-PEP-440 shapes parseable.
    """
    meta: dict[str, Any] = {}
    name: str | None = None
    specifier = ""

    try:
        req = Requirement(spec)
        name = req.name.lower()
        specifier = _extract_source_specifier(spec)
        if req.extras:
            meta["extras"] = sorted(req.extras)
        if req.marker:
            meta["marker"] = str(req.marker)
    except InvalidRequirement:
        pass

    if name is None:
        m = re.match(
            r"""^\s*
                (?P<name>[A-Za-z0-9_.\-]+)
                (?:\[(?P<extras>[^\]]+)\])?
                \s*
                (?P<specifier>[^;]*?)
                \s*
                (?:;\s*(?P<marker>.+))?
                \s*$""",
            spec,
            flags=re.VERBOSE,
        )
        if not m:
            return None, "", {"dropped_reason": f"could not parse requirement: {spec!r}"}
        name = m.group("name").lower()
        specifier = (m.group("specifier") or "").strip()
        if m.group("extras"):
            meta["extras"] = sorted(e.strip() for e in m.group("extras").split(","))
        if m.group("marker"):
            meta["marker"] = m.group("marker").strip()

    return name, specifier, meta


def translate_deps(
    source: Path, out_path: Path, offline: bool = False
) -> tuple[int, dict[str, Any]]:
    manifest = _find_deps_manifest(source)
    if not manifest:
        # No manifest → AST-infer suggestions and write a minimal default
        # env.yml carrying only the required deps. Suggestions are surfaced in
        # JSON for the human to confirm — never auto-added.
        inferred = _infer_suggestions(source)
        default_lines = [
            "# Generated by streamsnow migrate-app (no source manifest found). "
            "Review inferred_suggestions in the scan output and add needed deps manually.",
            "name: sf_env",
            "channels:",
            "  - snowflake",
            "dependencies:",
            f"  - {_DEFAULT_STREAMLIT_PIN}",
            "  - snowflake-snowpark-python",
            "",
        ]
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\n".join(default_lines), encoding="utf-8")
        except OSError as exc:
            return 2, {
                "translated": [],
                "dropped": [],
                "unmapped": [],
                "inferred_suggestions": inferred,
                "error": f"could not write {out_path}: {exc}",
            }
        return 0, {
            "translated": [],
            "dropped": [],
            "unmapped": [],
            "inferred_suggestions": inferred,
            "error": (
                "no dependency manifest found "
                "(requirements.txt, pyproject.toml, Pipfile, environment.yml)"
            ),
        }

    specs = _extract_dep_specs(source, manifest)
    channel_packages, is_online = _get_snowflake_packages(offline=offline)

    translated: list[dict[str, str]] = []
    dropped: list[dict[str, str]] = []
    unmapped: list[dict[str, str]] = []
    ordered_names: list[str] = []
    name_to_output: dict[str, str] = {}

    for raw in specs:
        name, specifier, meta = _parse_requirement(raw)
        if name is None:
            dropped.append({"source": raw, "reason": meta.get("dropped_reason", "unparseable")})
            continue

        # Rule 1: `python` never belongs in a warehouse env.yml — the channel
        # has no exact python build; the runtime supplies Python itself, and a
        # pin breaks CREATE STREAMLIT (same landmine validate-app checks for).
        if name == "python":
            dropped.append(
                {
                    "source": raw,
                    "reason": (
                        "python pin — the warehouse runtime supplies Python via "
                        "default_packages; pinning it breaks CREATE STREAMLIT."
                    ),
                }
            )
            continue

        # Rule 2: extras aren't expressible in conda pins.
        if "extras" in meta:
            dropped.append(
                {
                    "source": raw,
                    "reason": (
                        f"extras {meta['extras']} are not supported in conda pins — "
                        f"flag for manual review (install '{name}' + any extras "
                        "separately if needed)."
                    ),
                }
            )
            continue

        # Rule 3: environment markers aren't supported.
        if "marker" in meta:
            dropped.append(
                {
                    "source": raw,
                    "reason": (
                        f"environment marker {meta['marker']!r} is not supported "
                        "in conda pins — flag for manual review."
                    ),
                }
            )
            continue

        conda_spec = _convert_specifier(specifier)
        output = f"{name}{conda_spec}" if conda_spec else name

        if name not in channel_packages:
            if is_online:
                reason = (
                    f"'{name}' not found on Snowflake's Anaconda channel — "
                    "package unavailable in the warehouse runtime."
                )
            else:
                reason = (
                    f"'{name}' not in the offline allowlist and the Anaconda "
                    "channel is unreachable; verify availability manually."
                )
            unmapped.append({"package": name, "reason": reason, "source": raw})
            continue

        if name not in name_to_output:
            ordered_names.append(name)
            name_to_output[name] = output

    # Ensure required deps are present even when the source omits them.
    if "streamlit" not in name_to_output:
        ordered_names.insert(0, "streamlit")
        name_to_output["streamlit"] = _DEFAULT_STREAMLIT_PIN
    if "snowflake-snowpark-python" not in name_to_output:
        ordered_names.append("snowflake-snowpark-python")
        name_to_output["snowflake-snowpark-python"] = "snowflake-snowpark-python"

    for n in ordered_names:
        translated.append({"source": n, "output": name_to_output[n]})

    # Stable ordering for diff-ability.
    dropped.sort(key=lambda d: d.get("source", ""))
    unmapped.sort(key=lambda d: d.get("package", ""))

    lines: list[str] = [
        f"# Generated by streamsnow migrate-app from {manifest}. Review before commit.",
        "name: sf_env",
        "channels:",
        "  - snowflake",
        "dependencies:",
    ]
    for n in ordered_names:
        lines.append(f"  - {name_to_output[n]}")
    lines.append("")  # trailing newline

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        return 2, {
            "translated": translated,
            "dropped": dropped,
            "unmapped": unmapped,
            "inferred_suggestions": [],
            "error": f"could not write {out_path}: {exc}",
        }

    # Inference runs only when no manifest exists; the field is still emitted
    # (empty) for a stable JSON shape.
    return 0, {
        "translated": translated,
        "dropped": dropped,
        "unmapped": unmapped,
        "inferred_suggestions": [],
    }


# --------------------------------------------------------------------------- #
# scan-conformance                                                            #
# --------------------------------------------------------------------------- #


def _find_sql_calls(node: ast.AST) -> list[tuple[ast.Call, str]]:
    """(Call, callee-attr) pairs under *node* where the attr fetches data.

    Stops descending at nested FunctionDef/AsyncFunctionDef/Lambda scopes
    (other than *node* itself): the caller walks every function separately,
    so descending into a nested def here would double-count its calls — once
    under the outer function and again under the inner one.
    """
    hits: list[tuple[ast.Call, str]] = []
    stack: list[ast.AST] = [node]
    while stack:
        cur = stack.pop()
        for child in ast.iter_child_nodes(cur):
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                and child is not node
            ):
                continue
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and func.attr in SQL_CALL_ATTRS:
                    hits.append((child, func.attr))
            stack.append(child)
    return hits


_CACHE_REQUIRED_NOQA_RE = re.compile(r"#\s*noqa:\s*cache-required\b", re.IGNORECASE)


def _has_noqa(pattern: re.Pattern[str], source_lines: list[str], lineno: int) -> bool:
    """True if the statement at *lineno* carries the given ``# noqa`` pragma.

    Matches on the statement's own line (inline suppression) or on the line
    immediately above when that line is a comment (block suppression for
    calls wrapped across multiple lines). Line numbers are 1-based.
    """
    if lineno <= 0 or lineno > len(source_lines):
        return False
    if pattern.search(source_lines[lineno - 1]):
        return True
    if lineno >= 2:
        prev_line = source_lines[lineno - 2]
        if pattern.search(prev_line) and prev_line.lstrip().startswith("#"):
            return True
    return False


def _detect_required_grants(app_dir: Path, cfg: Config) -> list[dict[str, Any]]:
    """Every (database, schema) pair the app references, with grant status.

    Matches SQL-style ``FROM/JOIN db.schema.table`` substrings AND Snowpark
    ``session.table("db.schema.table")`` calls. A pair is default-granted when
    it is ``governance.database`` × one of ``governance.schema_allow`` — the
    single scoped grant the CI role holds for every repo app. Anything else
    needs a DBA-run GRANT sequence before the deployed app can read it (an
    owner's-rights app runs as the CI role, so a schema the developer can read
    locally is NOT evidence the deployed app can).

    The full detected list is always emitted — even when every schema is
    default-granted — so the skill can show an actionable confirmation line
    rather than a silent empty list.
    """
    gov = cfg.governance
    default_granted = {(gov.database.upper(), s.upper()) for s in gov.schema_allow}
    ci_role = cfg.snowflake.roles.ci_role

    found: set[tuple[str, str]] = set()
    for path in _py_files(app_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in SQL_FQN_RE.finditer(text):
            found.add((match.group(1).upper(), match.group(2).upper()))
        for match in TABLE_CALL_RE.finditer(text):
            found.add((match.group(1).upper(), match.group(2).upper()))

    out: list[dict[str, Any]] = []
    for db, schema in found:
        default = (db, schema) in default_granted
        reason = (
            f"covered by {ci_role} default grants "
            f"({gov.database}.{{{', '.join(gov.schema_allow)}}})"
            if default
            else (
                f"not covered by {ci_role} default grants — a DBA must GRANT "
                "USAGE on the database + schema and GRANT SELECT on ALL TABLES "
                "+ FUTURE TABLES in the schema"
            )
        )
        out.append(
            {"database": db, "schema": schema, "granted_by_default": default, "reason": reason}
        )
    # Defaults first, then deterministic (db, schema) ordering.
    out.sort(key=lambda d: (not d["granted_by_default"], d["database"], d["schema"]))
    return out


def scan_conformance(app_dir: Path, cfg: Config) -> tuple[int, dict[str, Any]]:
    uncached: list[dict[str, Any]] = []
    select_stars: list[dict[str, Any]] = []
    altair_imports: list[dict[str, Any]] = []

    for path in _py_files(app_dir):
        tree = _parse_py(path)
        if tree is None:
            continue
        file_rel = _rel_posix(path, app_dir)
        try:
            source_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            source_lines = []

        # 1. Data-fetching functions without @st.cache_data.
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _has_cache_data_decorator(fn):
                continue
            for call, attr in _find_sql_calls(fn):
                if _has_noqa(_CACHE_REQUIRED_NOQA_RE, source_lines, call.lineno):
                    continue
                uncached.append(
                    {"file": file_rel, "func": fn.name, "lineno": call.lineno, "callee": attr}
                )

        # 2. SELECT * literals anywhere in the tree. Docstring constants are
        # skipped — SELECT * inside prose is documentation, not executed SQL,
        # and would produce bogus conformance items.
        docstring_ids = _collect_docstring_ids(tree)
        for sub in ast.walk(tree):
            if (
                isinstance(sub, ast.Constant)
                and isinstance(sub.value, str)
                and id(sub) not in docstring_ids
                and SELECT_STAR_RE.search(sub.value)
            ):
                snippet = sub.value.strip().replace("\n", " ")[:80]
                select_stars.append({"file": file_rel, "lineno": sub.lineno, "snippet": snippet})

        # 3. altair imports (its default 5,000-row cap breaks real dashboards;
        # the conform pass swaps chart layers to the repo standard).
        for sub in ast.walk(tree):
            if isinstance(sub, ast.Import):
                for alias in sub.names:
                    if alias.name.split(".")[0] == "altair":
                        altair_imports.append({"file": file_rel, "lineno": sub.lineno})
            elif (
                isinstance(sub, ast.ImportFrom)
                and sub.module
                and sub.module.split(".")[0] == "altair"
            ):
                altair_imports.append({"file": file_rel, "lineno": sub.lineno})

    # 4. Legacy pages/ layout: a pages/ dir whose entrypoint never calls
    # st.navigation is the auto-pages v1 convention — it cannot be mixed with
    # the st.navigation shell the scaffold generates.
    legacy_pages_only = False
    pages_dir = app_dir / "pages"
    entrypoint = app_dir / "streamlit_app.py"
    if pages_dir.is_dir() and entrypoint.is_file():
        try:
            content = entrypoint.read_text(encoding="utf-8")
        except OSError:
            content = ""
        legacy_pages_only = "st.navigation(" not in content

    required_grants = _detect_required_grants(app_dir, cfg)

    return 0, {
        "uncached_queries": uncached,
        "select_stars": select_stars,
        "altair_imports": altair_imports,
        "legacy_pages_only": legacy_pages_only,
        "required_grants": required_grants,
    }


# --------------------------------------------------------------------------- #
# scan-inline-sql                                                             #
# --------------------------------------------------------------------------- #

# String literals with real SQL *shape* — multiple clause keywords together,
# not a single keyword — so documentation prose mentioning "SELECT" in passing
# never matches.
_INLINE_SQL_SHAPE_PATTERNS = (
    re.compile(r"(?is)\bSELECT\b.*\bFROM\b"),
    re.compile(r"(?is)\bWITH\b.*\bAS\s*\("),
    re.compile(r"(?is)\bINSERT\s+INTO\b"),
    re.compile(r"(?is)\bUPDATE\b.*\bSET\b"),
    re.compile(r"(?is)\bDELETE\s+FROM\b"),
)

_INLINE_SQL_NOQA_RE = re.compile(r"#\s*noqa:\s*inline-sql\b", re.IGNORECASE)


def _looks_like_sql(text: str) -> bool:
    """True if *text* has the shape of a SQL statement.

    Stronger than a single-keyword heuristic: the hardfail scanner pairs a
    keyword with a denied schema name to confirm SQL intent, but this scan has
    no such pairing, so it leans on clause-combination patterns instead.
    """
    return any(p.search(text) for p in _INLINE_SQL_SHAPE_PATTERNS)


def _enclosing_function_name(tree: ast.AST, target: ast.AST) -> str | None:
    """Name of the innermost function def enclosing *target*, or None.

    Matches by object identity, not source location, so identical literals in
    different functions attribute correctly.
    """
    result: list[str] = []
    stack: list[str] = []

    def visit(node: ast.AST) -> bool:
        if node is target:
            if stack:
                result.append(stack[-1])
            return True
        entered = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.append(node.name)
            entered = True
        for child in ast.iter_child_nodes(node):
            if visit(child):
                return True
        if entered:
            stack.pop()
        return False

    visit(tree)
    return result[0] if result else None


def scan_inline_sql(app_dir: Path) -> tuple[int, dict[str, Any]]:
    """Flag inline SQL literals for externalization into ``queries/*.sql``.

    Scans ALL string constants, not just Call args: the common
    assign-then-pass pattern (``sql = \"\"\"SELECT ...\"\"\"; _run(sql)``)
    flows the constant through a Name first, so a Call-arg-only scan misses
    it. Queries already externalized self-filter — their call arg is a bare
    query *name* (``load_sql("revenue_daily")``), which has no SQL shape.

    Docstrings are excluded; ``# noqa: inline-sql`` (on the literal's line or
    the comment line above) exempts plumbing queries the convention allows
    inline (``SELECT CURRENT_TIMESTAMP()`` heartbeats, INFORMATION_SCHEMA
    discovery).

    Exit 0 always — the skill reasons over the candidate *list*, not the exit
    code.
    """
    candidates: list[dict[str, Any]] = []
    for path in _py_files(app_dir):
        tree = _parse_py(path)
        if tree is None:
            continue
        source_lines = path.read_text(encoding="utf-8").splitlines()
        docstring_ids = _collect_docstring_ids(tree)
        file_rel = _rel_posix(path, app_dir)

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docstring_ids:
                continue
            text = node.value
            if not _looks_like_sql(text):
                continue
            lineno = getattr(node, "lineno", 0)
            if _has_noqa(_INLINE_SQL_NOQA_RE, source_lines, lineno):
                continue
            candidates.append(
                {
                    "file": file_rel,
                    "line": lineno,
                    "function": _enclosing_function_name(tree, node),
                    "sample": text.strip().replace("\n", " ")[:120],
                }
            )

    candidates.sort(key=lambda c: (c["file"], c["line"]))
    return 0, {"candidates": candidates}


# --------------------------------------------------------------------------- #
# CLI entry point                                                             #
# --------------------------------------------------------------------------- #


def _emit(result: dict[str, Any]) -> None:
    json.dump(result, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def _load_cfg(config_arg: str | None) -> Config:
    return load_config(Path(config_arg) if config_arg else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_app",
        description=(
            "Deterministic detection engine for the /migrate-app skill. "
            "Every subcommand emits JSON on stdout with a stable exit code "
            "(0 = clean/pass, 1 = abort/blocks, 2 = tool or config error)."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_pre = subparsers.add_parser("preflight", help="Check whether a source is safe to migrate.")
    p_pre.add_argument("source", type=Path)
    p_pre.add_argument("--target-slug", required=True)
    p_pre.add_argument("--dir", default=".", help="Repo root holding apps/ (default: cwd).")

    p_hf = subparsers.add_parser(
        "scan-hardfails", help="Scan source files for denied-schema refs and hardcoded secrets."
    )
    p_hf.add_argument("source", type=Path)
    p_hf.add_argument("--config", help="Path to streamsnow.config.yaml (default: discover).")

    p_td = subparsers.add_parser(
        "translate-deps", help="Translate source deps to a warehouse-mode environment.yml."
    )
    p_td.add_argument("source", type=Path)
    p_td.add_argument("--out", required=True, type=Path)
    p_td.add_argument(
        "--offline",
        action="store_true",
        help="Skip the Anaconda-channel repodata fetch (use the offline allowlist).",
    )

    p_sc = subparsers.add_parser(
        "scan-conformance",
        help="Find uncached queries, SELECT *, altair imports, legacy pages/ layouts, grants.",
    )
    p_sc.add_argument("app_path", type=Path)
    p_sc.add_argument("--config", help="Path to streamsnow.config.yaml (default: discover).")

    p_si = subparsers.add_parser(
        "scan-imports", help="List relative imports and nested __init__.py files."
    )
    p_si.add_argument("source", type=Path)

    p_gp = subparsers.add_parser(
        "graft-plan",
        help=(
            "Decide where to graft a source app's entrypoint "
            "(pages/*, pages/overview.py, or streamlit_app.py)."
        ),
    )
    p_gp.add_argument("source", type=Path)

    p_sis = subparsers.add_parser(
        "scan-inline-sql",
        help=(
            "Flag inline SQL literals (candidates for queries/*.sql "
            "externalization per the SQL file-organization convention)."
        ),
    )
    p_sis.add_argument("app_path", type=Path)

    args = parser.parse_args(argv)

    src = getattr(args, "source", None) or getattr(args, "app_path", None)
    if src is not None and not Path(src).is_dir():
        print(f"not a directory: {src}", file=sys.stderr)
        return 2

    try:
        if args.command == "preflight":
            code, result = preflight(args.source, args.target_slug, Path(args.dir))
        elif args.command == "scan-hardfails":
            policy = SchemaPolicy.from_governance(_load_cfg(args.config).governance)
            code, result = scan_hardfails(args.source, policy)
        elif args.command == "translate-deps":
            code, result = translate_deps(args.source, args.out, offline=args.offline)
        elif args.command == "scan-conformance":
            code, result = scan_conformance(args.app_path, _load_cfg(args.config))
        elif args.command == "scan-imports":
            code, result = scan_imports(args.source)
        elif args.command == "graft-plan":
            code, result = graft_plan(args.source)
        elif args.command == "scan-inline-sql":
            code, result = scan_inline_sql(args.app_path)
        else:  # pragma: no cover — argparse enforces the choices
            parser.error(f"unknown command: {args.command}")
            return 2
    except (ConfigError, OSError) as exc:
        # OSError covers an explicit --config path that doesn't exist/read.
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    _emit(result)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
