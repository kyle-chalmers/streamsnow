"""Block imports that resolve under ``streamlit run`` but not in the deployed app.

Only the **app root** is on ``sys.path`` in the Snowflake runtime. ``streamlit run``
*additionally* puts the executing page's own directory there. So a shared helper at
``apps/<slug>/pages/_header.py`` imported by a sibling page as::

    from _header import render_header

resolves locally, boots clean, and survives a full click-through of every page — then
raises ``ModuleNotFoundError: No module named '_header'`` on every page once deployed.
Local verification is structurally incapable of catching this, which is why it is a
static check rather than a testing convention. The correct form is package-qualified::

    from pages._header import render_header

The discriminator is not the directory's *name* but whether the module sits in the
**importing file's own directory** — that is exactly the ``sys.path`` entry the local
runner adds and the deployed runtime does not. A helper in ``pages/admin/`` imported
bare from ``pages/admin/report.py`` is equally invisible locally; the same helper
imported bare from the app root fails in both environments.

Three findings, for a bare (undotted, non-relative) import of ``M``:

- **unresolvable** — ``M`` lives in the importing file's own directory and nowhere the
  app root can reach. The silent one: works locally, ``ModuleNotFoundError`` deployed.
- **ambiguous** — ``M`` lives in the importing file's own directory *and* is reachable
  from the app root (a root module, the standard library, or a declared dependency).
  Resolution differs between environments, so the page runs different code in each.
- **unreachable** — ``M`` lives in some other app subdirectory. Fails in both
  environments, so it is loud rather than silent, and the message says so.

Flag only when the name cannot resolve from the app root by any other route, or when it
resolves to a different file locally than deployed. A module that legitimately sits
beside the entrypoint (``branding``, ``sql_loader``, ``config``) is never flagged — the
app root IS on ``sys.path`` deployed.

``# noqa: page-imports`` on the import statement suppresses the finding.

Exit codes: 0 = clean, 1 = finding, 2 = tool error.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path

import yaml

# App-root resolution is already solved (marker: snowflake.yml); don't grow a second one.
from .check_artifacts import _app_dirs_for

_KIND = "page-imports"

# Leading distribution name of a dependency spec ("plotly>=5.0" -> "plotly").
_DEP_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")


def _is_tooling_dir(parts: tuple[str, ...]) -> bool:
    """True for a directory chain under dotted tooling dirs (``.review/``) or caches."""
    return any(p.startswith(".") for p in parts) or "__pycache__" in parts


def _app_py_files(app_root: Path) -> list[Path]:
    """Every real ``.py`` file in the app, in stable order."""
    return sorted(
        p
        for p in app_root.rglob("*.py")
        if p.is_file() and not _is_tooling_dir(p.relative_to(app_root).parts[:-1])
    )


def _root_modules(app_root: Path) -> set[str]:
    """Names importable from the app root — the only sys.path entry that exists deployed.

    Includes plain modules (``branding.py``), regular packages, and PEP 420 namespace
    packages (any non-dotted directory), since all three resolve from the root.
    """
    names: set[str] = set()
    for p in app_root.iterdir():
        if p.name.startswith(".") or p.name == "__pycache__":
            continue
        if p.is_file() and p.suffix == ".py":
            names.add(p.stem)
        elif p.is_dir():
            names.add(p.name)
    return names


def _module_map(app_root: Path) -> dict[str, set[str]]:
    """Map module name -> set of app-relative directories holding it, excluding the root.

    A root-level entry is not in the map: it is reachable deployed and belongs to
    :func:`_root_modules`. Covers plain modules, regular packages (``pkg/__init__.py``)
    and namespace packages (a directory containing any ``.py``).
    """
    out: dict[str, set[str]] = {}

    def add(name: str, holder: Path) -> None:
        posix = holder.as_posix()
        if posix != ".":  # root-level -> reachable from the app root, not a finding
            out.setdefault(name, set()).add(posix)

    for p in _app_py_files(app_root):
        rel = p.relative_to(app_root)
        if p.name == "__init__.py":
            if len(rel.parts) >= 2:
                add(rel.parts[-2], rel.parent.parent)
        else:
            add(p.stem, rel.parent)

    # Namespace packages: a directory holding any .py is importable by its own name.
    for d in app_root.rglob("*"):
        if not d.is_dir():
            continue
        rel = d.relative_to(app_root)
        if _is_tooling_dir(rel.parts):
            continue
        if any(f.suffix == ".py" for f in d.iterdir() if f.is_file()):
            add(d.name, rel.parent)

    return out


def _declared_modules(app_root: Path) -> set[str]:
    """Dependency distribution names from the app's manifest. Never raises.

    A malformed manifest yields an empty set: ``validate_app`` deliberately routes
    parse errors to the ``manifest`` check, and this one must not turn a syntax error
    into a traceback that takes down the whole aggregate report.
    """
    specs: list[str] = []

    pyproject = app_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(errors="ignore"))
            project = data.get("project")
            deps = project.get("dependencies") if isinstance(project, dict) else None
            specs += [d for d in deps if isinstance(d, str)] if isinstance(deps, list) else []
        except (tomllib.TOMLDecodeError, OSError):
            pass

    env = app_root / "environment.yml"
    if env.is_file():
        try:
            data = yaml.safe_load(env.read_text(errors="ignore"))
            deps = data.get("dependencies") if isinstance(data, dict) else None
            specs += [d for d in deps if isinstance(d, str)] if isinstance(deps, list) else []
        except (yaml.YAMLError, OSError):
            pass

    names: set[str] = set()
    for spec in specs:
        match = _DEP_NAME_RE.match(spec.strip())
        if not match:
            continue
        dist = match.group(1).lower()
        # Import names can't contain '-'; keep both forms so either spelling matches.
        names.update({dist, dist.replace("-", "_")})
    return names


def _bare_imports(tree: ast.AST) -> list[tuple[str, int, int]]:
    """Every absolute single-segment module imported, as (name, lineno, end_lineno).

    ``from pages.x import y`` is already correct and ``from .x import y`` is relative —
    a different mechanism this check does not police.
    """
    found: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        end = getattr(node, "end_lineno", None) or getattr(node, "lineno", 0)
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative import
                continue
            if node.module and "." not in node.module:
                found.append((node.module, node.lineno, end))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "." not in alias.name:
                    found.append((alias.name, node.lineno, end))
    return found


def _waived(lines: list[str], start: int, end: int) -> bool:
    """True if ``# noqa: page-imports`` appears anywhere in the import statement."""
    return any(
        f"noqa: {_KIND}" in lines[i - 1] for i in range(start, end + 1) if 1 <= i <= len(lines)
    )


def _dotted(holder: str, name: str) -> str:
    return f"{holder.replace('/', '.')}.{name}"


def _display(path: Path) -> str:
    """Report paths relative to the cwd when possible.

    ``_app_dirs_for`` resolves file arguments to absolute paths, so the pre-commit
    route (which passes filenames) would otherwise print an absolute path for a
    finding while the ``apps/`` route prints a relative one.
    """
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _check_file(
    path: Path,
    app_root: Path,
    module_map: dict[str, set[str]],
    root_names: set[str],
    declared: set[str],
) -> list[dict]:
    text = path.read_text(errors="ignore")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []  # ruff owns syntax errors; don't double-report
    lines = text.splitlines()
    own_dir = path.relative_to(app_root).parent.as_posix()

    findings: list[dict] = []
    for name, lineno, end in _bare_imports(tree):
        holders = module_map.get(name)
        if not holders or _waived(lines, lineno, end):
            continue

        in_own_dir = own_dir != "." and own_dir in holders
        if name in root_names:
            reachable = f"the app-root module {name}.py"
        elif name in declared:
            reachable = f"the installed package {name!r}"
        elif name in sys.stdlib_module_names:
            reachable = f"the standard-library module {name!r}"
        else:
            reachable = ""

        if in_own_dir and not reachable:
            detail = (
                f"bare import of {name!r}, which lives in {own_dir}/ — only the app root is on "
                f"sys.path when deployed, so this raises ModuleNotFoundError in Snowflake even "
                f"though `streamlit run` resolves it (it also adds the executing page's own "
                f"directory). Passes local boot AND a full UI walkthrough. Use "
                f"`from {_dotted(own_dir, name)} import ...`"
            )
        elif in_own_dir:
            detail = (
                f"ambiguous import of {name!r}: resolves to {own_dir}/{name}.py under "
                f"`streamlit run` (the executing page's directory is on sys.path) but to "
                f"{reachable} when deployed (only the app root is) — the page runs different "
                f"code in each. Use `from {_dotted(own_dir, name)} import ...`, or rename one"
            )
        elif reachable:
            continue  # resolves from the app root, and its directory is never on sys.path
        else:
            holder = sorted(holders)[0]
            detail = (
                f"bare import of {name!r}, which lives in {holder}/ and not at the app root — "
                f"unresolvable from the app root, so this raises ModuleNotFoundError deployed "
                f"(and locally too, since only the entrypoint's directory is added). Use "
                f"`from {_dotted(holder, name)} import ...`"
            )
        findings.append({"file": _display(path), "line": lineno, "detail": detail})
    return findings


def check_app(app_root: Path) -> dict:
    """Scan one app root. The map is built once per app, then reused for every file."""
    module_map = _module_map(app_root)
    root_names = _root_modules(app_root)
    declared = _declared_modules(app_root)
    findings: list[dict] = []
    for p in _app_py_files(app_root):
        findings.extend(_check_file(p, app_root, module_map, root_names, declared))
    return {"ok": not findings, "findings": findings}


def scan_paths(paths: list[Path]) -> dict:
    """Map the given paths to app roots and scan each app WHOLE.

    Scanning only the passed files would be unsound: the generated pre-commit hook uses
    ``pass_filenames: true``, so adding ``pages/_helper.py`` alone makes an untouched
    ``pages/foo.py`` newly-violating. The offending import lives in a file the hook was
    never handed. ``check_artifacts.scan_paths`` widens the same way.
    """
    findings: list[dict] = []
    for app_root in _app_dirs_for(paths):
        findings.extend(check_app(app_root)["findings"])
    return {"ok": not findings, "findings": findings}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Block imports that resolve under `streamlit run` but not deployed."
    )
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    args = ap.parse_args(argv)

    result = scan_paths([Path(p) for p in (args.paths or ["apps"])])
    if args.format == "json":
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print(f"{_KIND}: clean")
    else:
        for f in result["findings"]:
            print(f"BLOCK {f['file']}:{f['line']} {f['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
