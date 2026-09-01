"""Enumerate the pages of a Streamlit app's ``streamlit_app.py``.

Walkthrough tooling (Playwright smoke tests, review skills) needs the ordered
list of pages to visit. Parsing the entrypoint via AST is more robust than
regex against ``st.Page(...)`` / ``st.navigation(...)`` calls — it handles
multi-line arguments, nested groups, and the ``default=True`` flag without
false matches inside strings or comments.

Four entrypoint shapes are recognized, in precedence order:

1. ``st.navigation({...})`` dict form — grouped sidebar sections. Entries carry
   their group label.
2. ``st.navigation([...])`` list form — a flat page list; ``group`` is null.
3. ``st.Page(...)`` assignments with no ``st.navigation`` call — enumerated in
   source order (``group`` null), so a half-migrated app still reports.
4. No ``st.navigation`` at all:
   - a ``pages/`` directory beside the entrypoint means the legacy auto-pages
     convention — the entrypoint plus each ``pages/*.py`` (sorted by filename,
     Streamlit's legacy ordering; underscore-prefixed helper modules skipped),
     reported with ``source: "pages-dir"``;
   - otherwise the app is single-page and the entrypoint itself is the one
     entry (``source: "entrypoint"``).

Output: one JSON object per line (convenient for ``while read``-style shell
loops in skill recipes), or a single JSON array with ``--json-array`` for
callers that want a structured payload::

    {"title": "Revenue Overview", "path": "pages/revenue_overview.py",
     "group": "Sales", "default": true, "var": "revenue_overview",
     "source": "navigation"}

Limitations
-----------

- Page paths and titles must be string literals — dynamically built navigation
  (loops, comprehensions, computed titles) can't be enumerated statically and
  triggers a partial-enumeration warning on stderr rather than silently
  looking complete.
- Streamlit must be imported as ``st`` (the universal convention).

Exit codes: 0 = enumerated (possibly with a partial-enumeration warning),
2 = tool error (missing entrypoint, unparseable Python).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

_LEGACY_PREFIX_RE = re.compile(r"^\d+[_ -]*")


def _string_arg(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _bool_kwarg(call: ast.Call, name: str) -> bool:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            return bool(kw.value.value)
    return False


def _string_kwarg(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name:
            return _string_arg(kw.value)
    return None


def _is_st_call(call: ast.Call, attr: str) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "st"
        and call.func.attr == attr
    )


def _page_from_call(call: ast.Call) -> dict | None:
    """Build a page entry (title, path, default) from an ``st.Page(...)`` call.

    Returns None when the path isn't a string literal — computed paths can't
    be enumerated statically.
    """
    path = _string_arg(call.args[0]) if call.args else None
    if path is None:
        path = _string_kwarg(call, "page")
    if path is None:
        return None
    title = _string_kwarg(call, "title")
    default = _bool_kwarg(call, "default")
    return {"title": title or path, "path": path, "default": default}


def _collect(tree: ast.Module) -> tuple[dict[str, dict], ast.expr | None]:
    """Walk the AST once. Return (var name → page entry, st.navigation arg or None).

    The first ``st.navigation`` call wins — a second one is a Streamlit runtime
    error anyway, so there is nothing sensible to merge.
    """
    pages: dict[str, dict] = {}
    nav_arg: ast.expr | None = None

    for node in ast.walk(tree):
        # Assignments like ``revenue_overview = st.Page(...)``
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and _is_st_call(node.value, "Page")
        ):
            page = _page_from_call(node.value)
            if page:
                pages[node.targets[0].id] = page
        if nav_arg is None and isinstance(node, ast.Call) and _is_st_call(node, "navigation"):
            if node.args:
                nav_arg = node.args[0]
            else:
                for kw in node.keywords:
                    if kw.arg == "pages":
                        nav_arg = kw.value
                        break

    return pages, nav_arg


def _warn_partial() -> None:
    print(
        "warning: navigation has dynamic keys/values; partial enumeration",
        file=sys.stderr,
    )


def _resolve_elements(
    elts: list[ast.expr], group: str | None, pages: dict[str, dict], warned: list[bool]
) -> list[dict]:
    """Resolve one navigation list's elements — variable references or inline
    ``st.Page(...)`` calls — into page entries tagged with ``group``."""
    out: list[dict] = []
    for elt in elts:
        if isinstance(elt, ast.Name) and elt.id in pages:
            out.append({**pages[elt.id], "group": group, "var": elt.id, "source": "navigation"})
        elif isinstance(elt, ast.Call) and _is_st_call(elt, "Page"):
            # Inline ``st.Page(...)`` written directly in the nav literal — a
            # valid, common Streamlit style. Resolve it in place.
            page = _page_from_call(elt)
            if page:
                out.append({**page, "group": group, "var": None, "source": "navigation"})
            elif not warned[0]:
                _warn_partial()
                warned[0] = True
        elif not warned[0]:
            # Unresolvable entry (unknown name, comprehension, call we don't
            # recognize) — warn so the output never silently looks complete.
            _warn_partial()
            warned[0] = True
    return out


def _resolve_navigation(pages: dict[str, dict], nav_arg: ast.expr | None) -> list[dict]:
    """Turn the ``st.navigation`` argument into an ordered page list."""
    warned = [False]
    if isinstance(nav_arg, ast.Dict):
        out: list[dict] = []
        for key, value in zip(nav_arg.keys, nav_arg.values, strict=True):
            group = _string_arg(key)
            if group is None or not isinstance(value, ast.List):
                if not warned[0]:
                    _warn_partial()
                    warned[0] = True
                continue
            out.extend(_resolve_elements(value.elts, group, pages, warned))
        return out
    if isinstance(nav_arg, ast.List):
        return _resolve_elements(nav_arg.elts, None, pages, warned)
    if nav_arg is not None and not warned[0]:
        _warn_partial()
    # No navigation call: fall back to st.Page assignments in source order.
    return [{**p, "group": None, "var": var, "source": "navigation"} for var, p in pages.items()]


def _legacy_title(stem: str) -> str:
    """Streamlit's legacy label rule: strip the ordering prefix (``01_``),
    underscores become spaces (``02_sales_by_region`` → ``sales by region``)."""
    label = _LEGACY_PREFIX_RE.sub("", stem).replace("_", " ").strip()
    return label or stem


def _legacy_pages(app_dir: Path) -> list[dict]:
    """Enumerate a legacy auto-pages app: entrypoint first, then ``pages/*.py``
    sorted by filename. Underscore-prefixed files are helper modules by
    convention, not pages."""
    entries = [
        {
            "title": app_dir.name,
            "path": "streamlit_app.py",
            "group": None,
            "default": True,
            "var": None,
            "source": "pages-dir",
        }
    ]
    for p in sorted((app_dir / "pages").glob("*.py")):
        if p.name.startswith("_"):
            continue
        entries.append(
            {
                "title": _legacy_title(p.stem),
                "path": f"pages/{p.name}",
                "group": None,
                "default": False,
                "var": None,
                "source": "pages-dir",
            }
        )
    return entries


def extract_nav(app_dir: Path) -> list[dict]:
    """Enumerate the pages of the app at ``app_dir``.

    Raises FileNotFoundError when the entrypoint is missing and SyntaxError
    when it isn't valid Python — the CLI wrapper maps both to exit 2.
    """
    entrypoint = app_dir / "streamlit_app.py"
    if not entrypoint.is_file():
        raise FileNotFoundError(str(entrypoint))
    tree = ast.parse(entrypoint.read_text(encoding="utf-8"), filename=str(entrypoint))

    pages, nav_arg = _collect(tree)
    if nav_arg is not None or pages:
        return _resolve_navigation(pages, nav_arg)
    if (app_dir / "pages").is_dir():
        return _legacy_pages(app_dir)
    # Single-page app: the entrypoint is the only page.
    return [
        {
            "title": app_dir.name,
            "path": "streamlit_app.py",
            "group": None,
            "default": True,
            "var": None,
            "source": "entrypoint",
        }
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Enumerate a Streamlit app's navigation pages.")
    ap.add_argument("app_dir", help="Path to the apps/<slug> directory.")
    ap.add_argument(
        "--json-array",
        action="store_true",
        help="Emit a single JSON array instead of one JSON object per line.",
    )
    args = ap.parse_args(argv)

    app_dir = Path(args.app_dir)
    try:
        entries = extract_nav(app_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc} not found", file=sys.stderr)
        return 2
    except SyntaxError as exc:
        print(
            f"error: {app_dir / 'streamlit_app.py'} is not valid Python "
            f"(line {exc.lineno}: {exc.msg}) — fix the entrypoint before extracting nav",
            file=sys.stderr,
        )
        return 2

    if args.json_array:
        json.dump(entries, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for entry in entries:
            sys.stdout.write(json.dumps(entry) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
