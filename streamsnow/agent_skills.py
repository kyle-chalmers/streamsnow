"""Install StreamSnow's skills for AI coding agents other than Claude Code.

Claude Code gets the skills from the plugin marketplace. Other agents that read
the open Agent Skills format (https://agentskills.io: a folder holding a
``SKILL.md`` with ``name`` and ``description`` frontmatter) only need the files
on disk where they look, so this module copies them there.

Subcommands::

    install --agent codex [--scope repo|user] [--dir .] [--dry-run] [--force]
        Copy every skill plus the shared recipes (``skills/_shared/``, which
        the skills link to as ``../_shared/``) into the agent's skills folder.
        repo scope: <dir>/.agents/skills, committed so every teammate's agent
        reads the same version. user scope: ~/.agents/skills.

    list --agent codex [--scope repo|user] [--dir .]
        The skills this StreamSnow version ships and their state at the target.

Where Codex looks (0.157.1, ``codex-rs/ext/skills/src/host_roots.rs``):
``.agents/skills`` in every directory from the working directory up to the
repository root, then ``~/.agents/skills``. Discovery is recursive and matches
files named ``SKILL.md``, so ``_shared/`` (no ``SKILL.md``) is carried along
without being listed as a skill.

Why copy, not symlink. Codex does follow symlinked skill folders, but a repo
install is committed, and a link into one machine's Python environment is a
dangling path on every other machine and after every ``uv tool upgrade``. A
link into a StreamSnow source checkout also changes the skill names: Codex
resolves the link, finds the checkout's ``.claude-plugin/plugin.json`` above
it, and lists the skills as ``streamsnow:start-app``. Copies behave the same
everywhere; re-run ``install`` after upgrading the CLI.

Ownership. A manifest (``.streamsnow-skills.json``) in the target records the
folders and file hashes this command wrote. A re-run replaces only those, and
refuses, unless ``--force``, to overwrite a file edited since the install, a
file added inside an installed folder, or a same-named folder it did not
write. Org-specific changes belong in ``.streamsnow/overlays/<skill>.md``,
which survives every re-install.

Codex adaptation. A skill whose frontmatter sets ``disable-model-invocation:
true`` (Claude Code: only the user can start it) gets ``agents/openai.yaml``
with ``policy.allow_implicit_invocation: false``, Codex's equivalent: the skill
leaves the model's automatic skill list and ``$<name>`` still runs it.

Links. The copy keeps every relative link inside the skills tree working
(``_shared/`` sits beside the skills, as in the source). A link that leaves the
tree, such as ``../../docs/production-lessons.md``, only resolves in the plugin,
which ships the whole repo, so the copy points it at the file on GitHub.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import __version__

MANIFEST_NAME = ".streamsnow-skills.json"
_SKIP_NAMES = {"__pycache__", ".DS_Store"}
# A link that leaves the skills tree (e.g. ../../docs/...) resolves inside the plugin,
# which ships the whole repo, but dangles in a copy; point it at the published file.
REPO_BLOB_URL = "https://github.com/kyle-chalmers/streamsnow/blob/main"
_MD_LINK_RE = re.compile(r"\]\(([^)\s#]+)(#[^)\s]*)?\)")

_CODEX_EXPLICIT_ONLY = """\
# Written by `streamsnow agent-skills install`. Codex's equivalent of Claude
# Code's `disable-model-invocation: true`: this skill is left out of the
# model's automatic skill list. Type ${name} to run it.
policy:
  allow_implicit_invocation: false
"""


@dataclass(frozen=True)
class AgentTarget:
    """Where one agent reads Agent Skills folders, and what it needs added."""

    name: str
    repo_dir: str  # relative to the repository root
    user_dir: str  # relative to the home directory
    invoke_hint: str  # how a user starts a skill in this agent
    explicit_only_file: str | None = None  # template for human-only skills


AGENTS: dict[str, AgentTarget] = {
    "codex": AgentTarget(
        name="codex",
        repo_dir=".agents/skills",
        user_dir=".agents/skills",
        invoke_hint="type $start-app (or pick a skill from /skills)",
        explicit_only_file=_CODEX_EXPLICIT_ONLY,
    ),
}


class AgentSkillsError(Exception):
    """A usage or environment problem; reported as exit code 2."""


def skills_source() -> Path:
    """The skills this StreamSnow version ships.

    The wheel carries them as ``streamsnow/_skills`` (pyproject force-include);
    a source checkout or editable install reads the repo's ``skills/``.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here / "_skills", here.parent / "skills"):
        if (candidate / "start-app" / "SKILL.md").is_file():
            return candidate
    raise AgentSkillsError(
        "cannot find the bundled skills (expected streamsnow/_skills in the installed "
        "package, or skills/ in a source checkout); reinstall streamsnow"
    )


def read_frontmatter(skill_md: Path) -> dict:
    """Parse the YAML frontmatter of a SKILL.md (empty dict when absent)."""
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for end, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            data = yaml.safe_load("\n".join(lines[1:end]))
            return data if isinstance(data, dict) else {}
    return {}


def shipped_entries(source: Path) -> list[str]:
    """Top-level folders to install: every skill, plus ``_``-prefixed shared ones."""
    return sorted(
        p.name
        for p in source.iterdir()
        if p.is_dir()
        and p.name not in _SKIP_NAMES
        and ((p / "SKILL.md").is_file() or p.name.startswith("_"))
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry_files(root: Path, entry: str) -> dict[str, Path]:
    """``entry/rel/path`` -> file, for every file under ``root/entry``."""
    base = root / entry
    return {
        f"{entry}/{p.relative_to(base).as_posix()}": p
        for p in sorted(base.rglob("*"))
        if p.is_file() and not (set(p.relative_to(base).parts) & _SKIP_NAMES)
    }


def _rewrite_escaping_links(text: str, rel: str) -> str:
    """Point relative links that leave the skills tree at the file on GitHub."""

    def repl(match: re.Match[str]) -> str:
        target, anchor = match.group(1), match.group(2) or ""
        if "://" in target or target.startswith(("/", "mailto:")):
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
        if not resolved.startswith("../") or resolved.startswith("../../"):
            return match.group(0)
        return f"]({REPO_BLOB_URL}/{resolved[3:]}{anchor})"

    return _MD_LINK_RE.sub(repl, text)


def render_entry(source: Path, entry: str, agent: AgentTarget) -> dict[str, bytes]:
    """The files one entry installs as, for this agent: ``relpath -> bytes``."""
    files = {rel: p.read_bytes() for rel, p in _entry_files(source, entry).items()}
    for rel, data in files.items():
        if rel.endswith(".md"):
            text = data.decode("utf-8")
            rewritten = _rewrite_escaping_links(text, rel)
            if rewritten != text:
                files[rel] = rewritten.encode("utf-8")
    skill_md = source / entry / "SKILL.md"
    if agent.explicit_only_file and skill_md.is_file():
        meta = read_frontmatter(skill_md)
        if meta.get("disable-model-invocation") is True:
            name = str(meta.get("name") or entry)
            text = agent.explicit_only_file.replace("{name}", name)
            files[f"{entry}/agents/openai.yaml"] = text.encode("utf-8")
    return files


def target_dir(agent: AgentTarget, scope: str, repo: Path, home: Path | None = None) -> Path:
    if scope == "repo":
        return repo / agent.repo_dir
    return (home or Path.home()) / agent.user_dir


def read_manifest(dest: Path) -> dict:
    path = dest / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentSkillsError(f"{path} is not valid JSON ({exc}); fix or delete it") from exc
    return data if isinstance(data, dict) else {}


@dataclass
class InstallPlan:
    dest: Path
    agent: AgentTarget
    new: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    explicit_only: list[str] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    previous_version: str | None = None


def _drift(dest: Path, entry: str, recorded: dict[str, str]) -> list[str]:
    """Files under an installed entry that differ from what the install wrote."""
    problems = []
    on_disk = _entry_files(dest, entry)
    for rel, path in on_disk.items():
        if rel not in recorded:
            problems.append(f"{rel} was added after the install")
        elif _sha256(path.read_bytes()) != recorded[rel]:
            problems.append(f"{rel} was edited after the install")
    return problems


def plan_install(
    source: Path, dest: Path, agent: AgentTarget, *, force: bool = False
) -> InstallPlan:
    plan = InstallPlan(dest=dest, agent=agent)
    manifest = read_manifest(dest)
    owned = set(manifest.get("entries") or [])
    recorded: dict[str, str] = manifest.get("files") or {}
    plan.previous_version = manifest.get("streamsnow_version")

    entries = shipped_entries(source)
    for entry in entries:
        rendered = render_entry(source, entry, agent)
        plan.files.update(rendered)
        if any(rel.endswith("/agents/openai.yaml") for rel in rendered):
            plan.explicit_only.append(entry)
        existing = dest / entry
        if not existing.exists():
            plan.new.append(entry)
            continue
        if entry not in owned:
            if not force:
                plan.conflicts.append(
                    f"{entry}/ already exists and was not installed by streamsnow"
                )
            plan.updated.append(entry)
            continue
        if not force:
            plan.conflicts.extend(_drift(dest, entry, recorded))
        current = {rel: _sha256(p.read_bytes()) for rel, p in _entry_files(dest, entry).items()}
        wanted = {rel: _sha256(data) for rel, data in rendered.items()}
        (plan.unchanged if current == wanted else plan.updated).append(entry)

    for entry in sorted(owned - set(entries)):
        if not (dest / entry).exists():
            continue
        if not force:
            plan.conflicts.extend(_drift(dest, entry, recorded))
        plan.removed.append(entry)
    return plan


def apply_install(plan: InstallPlan) -> None:
    dest = plan.dest
    dest.mkdir(parents=True, exist_ok=True)
    for entry in plan.updated + plan.removed:
        target = dest / entry
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
    for rel, data in plan.files.items():
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    entries = sorted(set(plan.new + plan.updated + plan.unchanged))
    manifest = {
        "installed_by": "streamsnow agent-skills install",
        "streamsnow_version": __version__,
        "agent": plan.agent.name,
        "entries": entries,
        "files": {rel: _sha256(data) for rel, data in sorted(plan.files.items())},
    }
    (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve())) or "."
    except ValueError:
        return str(path)


def _resolve_target(args: argparse.Namespace) -> tuple[AgentTarget, Path]:
    agent = AGENTS[args.agent]
    repo = Path(args.dir)
    if args.scope == "repo" and not repo.is_dir():
        raise AgentSkillsError(f"--dir {repo} is not a directory")
    return agent, target_dir(agent, args.scope, repo)


def cmd_install(args: argparse.Namespace) -> int:
    agent, dest = _resolve_target(args)
    plan = plan_install(skills_source(), dest, agent, force=args.force)
    skills = [e for e in plan.new + plan.updated + plan.unchanged if not e.startswith("_")]
    print(
        f"StreamSnow {__version__}: {len(skills)} skills plus shared recipes for "
        f"{agent.name} ({args.scope} scope) -> {_display(dest)}"
    )
    for label, names in (
        ("new", plan.new),
        ("update", plan.updated),
        ("unchanged", plan.unchanged),
        ("remove (no longer shipped)", plan.removed),
    ):
        for name in names:
            print(f"  {label:<10} {name}")
    if plan.conflicts:
        print("\nNothing written. These would be overwritten or removed:")
        for problem in plan.conflicts:
            print(f"  - {problem}")
        print(
            "Move your changes into .streamsnow/overlays/<skill>.md (they survive every "
            "re-install), then re-run; or pass --force to overwrite."
        )
        return 1
    if plan.explicit_only:
        print(
            f"  explicit-only in {agent.name}: {', '.join(plan.explicit_only)} "
            "(Claude Code's disable-model-invocation)"
        )
    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0
    apply_install(plan)
    print(f"\nInstalled. In {agent.name}, {agent.invoke_hint}.")
    if args.scope == "repo":
        print(
            f"Commit {_display(dest)} so every teammate's agent reads this version; "
            "re-run this command after upgrading streamsnow."
        )
    else:
        print("Re-run this command after upgrading streamsnow.")
    return 0


def _first_sentence(text: str, limit: int = 72) -> str:
    text = " ".join(str(text).split())
    cuts = [i for i in (text.find(stop) for stop in (". ", " \u2014 ", " - ")) if i > 0]
    if cuts:
        text = text[: min(cuts)]
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def cmd_list(args: argparse.Namespace) -> int:
    agent, dest = _resolve_target(args)
    source = skills_source()
    manifest = read_manifest(dest)
    owned = set(manifest.get("entries") or [])
    recorded: dict[str, str] = manifest.get("files") or {}
    print(f"StreamSnow {__version__} ships these skills:")
    rows = []
    for entry in shipped_entries(source):
        skill_md = source / entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        if not (dest / entry).exists():
            state = "not installed"
        elif entry not in owned:
            state = "present, not installed by streamsnow"
        elif _drift(dest, entry, recorded):
            state = "installed, edited since"
        else:
            state = "installed"
        rows.append((entry, state, read_frontmatter(skill_md).get("description", "")))
    width = max(len(state) for _, state, _ in rows)
    for entry, state, desc in rows:
        print(f"  {entry:<14} {state:<{width}}  {_first_sentence(desc)}")
    where = f"{agent.name}, {args.scope} scope: {_display(dest)}"
    if manifest:
        installed = manifest.get("streamsnow_version", "unknown")
        note = "" if installed == __version__ else " (re-run install to update)"
        print(f"\nTarget ({where}) holds the skills from streamsnow {installed}{note}.")
    else:
        print(
            f"\nTarget ({where}) has no StreamSnow install. "
            f"Run: streamsnow agent-skills install --agent {agent.name} --scope {args.scope}"
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="streamsnow agent-skills",
        description="Install StreamSnow's skills for AI coding agents other than Claude Code "
        "(Claude Code users: install the plugin instead).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--agent", required=True, choices=sorted(AGENTS), help="Target agent.")
        p.add_argument(
            "--scope",
            choices=("repo", "user"),
            default="repo",
            help="repo: <dir>/.agents/skills, committed with the repo (default). "
            "user: ~/.agents/skills, for every repo on this machine.",
        )
        p.add_argument("--dir", default=".", help="Repository root for --scope repo.")

    p = sub.add_parser("install", help="Copy the skills where the agent reads them.")
    common(p)
    p.add_argument("--dry-run", action="store_true", help="Print the plan; write nothing.")
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite edited or foreign folders of the same name.",
    )

    p = sub.add_parser("list", help="The shipped skills and their state at the target.")
    common(p)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {"install": cmd_install, "list": cmd_list}
    try:
        return handlers[args.cmd](args)
    except AgentSkillsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
