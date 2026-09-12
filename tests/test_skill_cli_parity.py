"""Every `streamsnow <verb>` a skill or doc cites must exist in the CLI.

A SKILL.md that names a renamed verb silently tells Claude to run a command
that does not exist, and the user meets a confusing failure mid-workflow. The
production fleet this plugin came from kept a `check_skill_tool_parity.py`
pre-commit hook for exactly that; this is the plugin's version, against the
Typer app (and the argparse subparsers behind the passthrough groups).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import typer

from streamsnow import cli
from streamsnow.tools import migrate_app, preview_app, review_gate, review_loop, sql_review

REPO_ROOT = Path(__file__).resolve().parent.parent

# Passthrough groups: the Typer command forwards argv to an argparse tool whose
# subparsers define the real verbs.
_PASSTHROUGH = {
    "preview": preview_app,
    "sql-review": sql_review,
    "review-gate": review_gate,
    "review-loop": review_loop,
    "migrate": migrate_app,
}
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_CALL_RE = re.compile(r"(?<![\w/.-])streamsnow[ \t]+([a-z][a-z-]*)(?:[ \t]+([a-z][a-z-]*))?")
_SCAN_DIRS = ("skills", "docs", "commands", "hooks")
_SCAN_FILES = ("README.md", "CONTRIBUTING.md", "RELEASING.md")


def _argparse_verbs(module) -> set[str]:
    """Names passed to `sub.add_parser("<name>", ...)` anywhere in the module."""
    tree = ast.parse(Path(module.__file__).read_text())
    verbs: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            verbs.add(node.args[0].value)
    return verbs


def _cli_surface() -> tuple[set[str], dict[str, set[str]]]:
    root = typer.main.get_command(cli.app)
    verbs = set(root.commands)
    subverbs: dict[str, set[str]] = {}
    for name, cmd in root.commands.items():
        if hasattr(cmd, "commands"):  # a real Typer/click group (e.g. `check`)
            subverbs[name] = set(cmd.commands)
    for name, module in _PASSTHROUGH.items():
        assert name in verbs, f"passthrough group {name!r} missing from the CLI"
        found = _argparse_verbs(module)
        assert found, f"could not enumerate argparse verbs for {name!r} — refactor broke the test"
        subverbs[name] = found
    return verbs, subverbs


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for d in _SCAN_DIRS:
        files += sorted((REPO_ROOT / d).rglob("*.md"))
        files += sorted((REPO_ROOT / d).rglob("*.sh"))
    files += [REPO_ROOT / f for f in _SCAN_FILES]
    return [f for f in files if f.is_file()]


@pytest.mark.parametrize("path", _scan_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_cited_streamsnow_verb_exists(path: Path):
    verbs, subverbs = _cli_surface()
    text = path.read_text()
    bad: list[str] = []
    for m in _CALL_RE.finditer(text):
        verb, second = m.group(1), m.group(2)
        if verb not in verbs:
            bad.append(f"streamsnow {verb}")
            continue
        if second and verb in subverbs:
            ok = second in subverbs[verb]
            if verb == "preview" and not ok:
                ok = bool(_SLUG_RE.match(second))  # `streamsnow preview <slug>` shorthand
            if not ok:
                bad.append(f"streamsnow {verb} {second}")
    assert not bad, f"{path.relative_to(REPO_ROOT)} cites verbs the CLI lacks: {sorted(set(bad))}"


def test_scan_actually_sees_verbs():
    """Guard the regex: the front-door skill must cite at least the core verbs."""
    text = (REPO_ROOT / "skills" / "start-app" / "SKILL.md").read_text()
    seen = {m.group(1) for m in _CALL_RE.finditer(text)}
    assert {"doctor", "validate-app"} <= seen
