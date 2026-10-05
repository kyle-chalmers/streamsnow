"""The CLI's public surface is pinned in a snapshot: nothing is renamed or removed by accident.

docs/versioning.md promises that command names, flag names and positional arguments
only break in a major release (after a deprecation window). That promise is only as
good as the thing that enforces it: a refactor, or an agent working an issue, can
rename `--base-ref` to `--base` in one keystroke and every consumer repo's generated
CI breaks on the next `uv tool upgrade`. This test turns that into a red CI run.

The snapshot (tests/fixtures/cli_surface.json) covers every Typer command and
sub-command with its options and arguments, and for each passthrough verb
(`preview`, `sql-review`, ...) the argparse sub-verbs and option strings of the
tool it forwards to. Hidden commands are included on purpose: the generated
workflows call some of them (`config-get`, `stage-path`).

When it fails:
- REMOVED entries are a breaking change. Restore the name (keep it working as a
  deprecated alias with a warning) unless this is a major release; see
  docs/versioning.md.
- ADDED entries only need the snapshot regenerated, deliberately:
      uv run python tests/test_cli_surface.py --update
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import typer

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = REPO_ROOT / "tests" / "fixtures" / "cli_surface.json"

# Passthrough commands forward raw argv to an argparse tool. Their real surface lives
# in that module, so it is read from the module's add_parser / add_argument calls.
_PASSTHROUGH_MODULES = {
    "agent-skills": "streamsnow/agent_skills.py",
    "migrate": "streamsnow/tools/migrate_app.py",
    "preview": "streamsnow/tools/preview_app.py",
    "review-gate": "streamsnow/tools/review_gate.py",
    "review-loop": "streamsnow/tools/review_loop.py",
    "sql-review": "streamsnow/tools/sql_review.py",
}


def _argparse_surface(path: Path) -> dict[str, list[str]]:
    """Sub-verbs (add_parser) and option/positional names (add_argument) in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    verbs: set[str] = set()
    args: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        consts = [a.value for a in node.args if isinstance(a, ast.Constant)]
        names = [c for c in consts if isinstance(c, str)]
        if node.func.attr == "add_parser" and names:
            verbs.add(names[0])
        elif node.func.attr == "add_argument":
            args.update(names)
    return {"verbs": sorted(verbs), "arguments": sorted(args)}


def _params(cmd) -> dict[str, list[str]]:
    options: set[str] = set()
    arguments: set[str] = set()
    for p in cmd.params:
        if p.param_type_name == "argument":
            arguments.add(p.name)
        else:
            options.update(p.opts)
            options.update(p.secondary_opts)
    return {"options": sorted(options), "arguments": sorted(arguments)}


def current_surface() -> dict:
    from streamsnow import cli

    root = typer.main.get_command(cli.app)
    commands: dict[str, dict] = {"": _params(root)}
    passthrough: dict[str, dict] = {}

    def walk(group, prefix: list[str]) -> None:
        for name, cmd in sorted(group.commands.items()):
            path = " ".join([*prefix, name])
            if (cmd.context_settings or {}).get("allow_extra_args"):
                module = _PASSTHROUGH_MODULES.get(path)
                assert module, (
                    f"`streamsnow {path}` forwards raw argv but has no entry in "
                    "_PASSTHROUGH_MODULES, so its flags would go unpinned. Add it."
                )
                passthrough[path] = _argparse_surface(REPO_ROOT / module)
            else:
                commands[path] = _params(cmd)
            if hasattr(cmd, "commands"):
                walk(cmd, [*prefix, name])

    walk(root, [])
    return {"commands": commands, "passthrough": passthrough}


def _flatten(surface: dict) -> set[str]:
    out: set[str] = set()
    for kind, entries in surface.items():
        for cmd, fields in entries.items():
            out.add(f"{kind}: streamsnow {cmd}".rstrip())
            for field, values in fields.items():
                out.update(
                    f"{kind}: streamsnow {cmd} {field} {v}".replace("  ", " ") for v in values
                )
    return out


def test_cli_surface_matches_snapshot():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    actual = current_surface()
    removed = sorted(_flatten(expected) - _flatten(actual))
    added = sorted(_flatten(actual) - _flatten(expected))
    msg = []
    if removed:
        msg.append(
            "BREAKING: these were removed or renamed. Restore them (deprecate first; see "
            "docs/versioning.md) unless this is a major release:\n  " + "\n  ".join(removed)
        )
    if added:
        msg.append(
            "New surface. Regenerate the snapshot deliberately with "
            "`uv run python tests/test_cli_surface.py --update`:\n  " + "\n  ".join(added)
        )
    assert not msg, "\n\n".join(msg)


def test_snapshot_is_non_trivial():
    """Guard the guard: an empty or truncated snapshot would make the test above vacuous."""
    surface = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert {"init", "doctor", "check schema-refs", "ci-key create"} <= set(surface["commands"])
    assert "--version" in surface["commands"][""]["options"]
    assert set(_PASSTHROUGH_MODULES) == set(surface["passthrough"])
    assert all(v["arguments"] for v in surface["passthrough"].values())


if __name__ == "__main__":
    if sys.argv[1:] != ["--update"]:
        sys.exit("usage: uv run python tests/test_cli_surface.py --update")
    SNAPSHOT.write_text(json.dumps(current_surface(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT.relative_to(REPO_ROOT)}")
