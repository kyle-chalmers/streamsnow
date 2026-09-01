"""Block changes that abandon a deployed STREAMLIT object without tombstoning it.

Why this exists
---------------
The generated deploy pipeline only ever runs ``CREATE OR REPLACE STREAMLIT``
(:mod:`streamsnow.deploy`). It has **no delete path**. So when an app directory
is renamed or removed, the previously deployed object keeps existing in
Snowflake, frozen at the source of the last merge that deployed it — and
``streamsnow verify-deploy`` reports it unhealthy on every later merge. Nothing
ever cleans it up, because nothing is left in the repo that knows it exists.

The organizing principle: **detection is automated and total; destruction
requires explicit committed consent.** This tool is the consent gate,
``deploy/tombstones.yml`` is the consent record, and the deploy workflow's
reconcile step (fed by ``--drop-sql``) is the executor. A PR that stops
declaring an identifier must, in the same PR, either tombstone it or restore
it — the author of that PR is the one person who still has the context to say
which.

How an identifier is derived (one source of truth)
--------------------------------------------------
A directory ``apps/<slug>/`` containing ``snowflake.yml`` is an app. Its
deployed object is :func:`streamsnow.deploy.streamlit_fqn` of the configured
``app_database`` / ``app_schema`` and the slug — the exact same derivation the
deploy workflow uses when it emits ``CREATE OR REPLACE``. The manifest's own
``identifier:`` block is scaffolded to match but is *not* what the pipeline
deploys from, so this check never parses it: parsing it independently would
create a second derivation that could disagree with the one that matters.
Consequently a ``git mv apps/a apps/b`` **is** a rename of the deployed object
(the slug is the identity), while edits inside an app never trip this check.

Because only the manifest's *presence* matters, base-ref state is read with
``git ls-tree`` (which paths existed at the base commit) rather than by
checking anything out — the tool works in a repo where app directories come
and go across branches.

Registry schema (``deploy/tombstones.yml``)
-------------------------------------------
A mapping with a single ``tombstones`` key holding a list of entries::

    tombstones:
      - identifier: DATA_APPS.BI_APPS.ACME_SALES_DASHBOARD
        reason: renamed to ACME_REVENUE_DASHBOARD
        date: 2026-08-31

- ``identifier`` — fully-qualified ``DB.SCHEMA.NAME`` (validated with
  :func:`streamsnow.config.validate_fqn`, exactly three parts). Unique within
  the file (Snowflake identifiers are case-insensitive, so uniqueness is too).
- ``reason`` — non-empty free text: what removed the object and, for a rename,
  what replaced it.
- ``date`` — ISO ``YYYY-MM-DD`` (a quoted string or a bare YAML date).

Unknown keys are rejected so a typo (``data:`` for ``date:``) fails loudly
instead of silently passing. A missing registry file is not an error — the
registry is optional until the first rename needs it. Validated identifiers
are restricted to the safe identifier charset, which is what makes
``--drop-sql`` safe to render without quoting.

Modes
-----
``check_tombstones.py`` (default)
    Validate the registry, then apply the diff rule: every identifier declared
    at the merge-base of ``--base-ref`` (default ``origin/main``) and ``HEAD``
    but not declared by the working tree must appear in the registry. Also
    flags the contradiction — a tombstone whose identifier is still declared —
    because CI would otherwise create the object in the deploy step and drop
    it in the reconcile step of the same run, flapping forever.

``check_tombstones.py --drop-sql``
    Validate the registry and print one ``DROP STREAMLIT IF EXISTS`` statement
    per tombstone — the shape the deploy workflow's reconcile step consumes.
    Prints nothing on a registry error, so a malformed file can never be
    turned into DROP statements.

Exit codes: 0 = clean, 1 = finding (missing tombstone / live-app tombstone),
2 = cannot verify (unreadable or invalid registry, missing config, git or
base-ref failure). Failing *toward* 2 on a missing base ref is deliberate:
"could not compare" must never look like "nothing was removed".
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ConfigError, load_config, validate_fqn
from ..deploy import streamlit_fqn

_KIND = "tombstones"

DEFAULT_REGISTRY = Path("deploy/tombstones.yml")
DEFAULT_APPS_DIR = Path("apps")

_ENTRY_KEYS = {"identifier", "reason", "date"}


def _manifest_re(apps_dir: Path) -> re.Pattern[str]:
    """Manifest matcher for a given apps directory (relative repo path).

    Parameterized so ``--apps-dir dashboards`` applies to BOTH sides of the
    diff — hard-coding ``apps/`` here would silently miss removals under a
    custom directory while the worktree side honored it.
    """
    rel = str(apps_dir).strip("/")
    return re.compile(rf"^{re.escape(rel)}/([^/]+)/snowflake\.yml$")


class ToolError(RuntimeError):
    """Cannot verify — reported on stderr with exit 2, never as a finding."""


@dataclass
class Tombstone:
    """One validated row of the registry."""

    identifier: str
    reason: str
    date: str


@dataclass
class Result:
    """Accumulated outcome so every problem is reported in one pass.

    A blocking check that reveals problems one re-run at a time loses the
    author's context between runs; collect everything, then report once.
    """

    tombstones: list[Tombstone] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


# --------------------------------------------------------------------------- #
# Git plumbing
# --------------------------------------------------------------------------- #
def _git(args: list[str]) -> str:
    """Run git in the *current working directory*, not the package location.

    CI and pre-commit both invoke this tool from the repo root, and honoring
    cwd is what lets the tests drive the diff rule against throwaway repos.
    """
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ToolError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def resolve_base(base_ref: str) -> str:
    """Resolve ``base_ref`` to the commit to diff against (its merge-base with
    HEAD, so a stale local ``origin/main`` compares at the fork point, not at
    unrelated newer commits)."""
    try:
        return _git(["merge-base", base_ref, "HEAD"]).strip()
    except ToolError as exc:
        raise ToolError(
            f"cannot resolve base ref {base_ref!r} ({exc}). The diff rule needs a "
            f"reachable base to compare against — pass --base-ref <ref> explicitly, "
            f"or fetch the default remote branch first."
        ) from exc


# --------------------------------------------------------------------------- #
# Identifier inventories
# --------------------------------------------------------------------------- #
def worktree_identifiers(cfg, apps_dir: Path) -> dict[str, str]:
    """Map UPPERCASED deployed identifier -> slug for every working-tree app.

    An invalid slug raises: it could never deploy, but silently dropping it
    from the "declared" set would let a tombstone for a live app pass the
    contradiction check, which is the exact bug class this tool closes.
    """
    out: dict[str, str] = {}
    for yml in sorted(apps_dir.glob("*/snowflake.yml")):
        slug = yml.parent.name
        try:
            fqn = streamlit_fqn(cfg, slug)
        except ValueError as exc:
            raise ToolError(f"apps/{slug}: {exc}") from exc
        out[fqn.upper()] = slug
    return out


def base_identifiers(
    cfg, base_commit: str, apps_dir: Path = Path("apps")
) -> tuple[dict[str, str], list[str]]:
    """Map UPPERCASED deployed identifier -> slug for every app at ``base_commit``.

    Enumerated with ``git ls-tree`` because the identity of an app is its slug
    plus config — manifest *presence* is the marker, manifest content never
    changes the derivation (see module docstring). A slug that is invalid at
    base is skipped with a note rather than failing the run: it could never
    have deployed, so it cannot have left an orphan, and blocking today's
    change on it would be wrong.
    """
    rel = str(apps_dir).strip("/")
    listing = _git(["ls-tree", "-r", "--name-only", base_commit, "--", f"{rel}/"])
    manifest_re = _manifest_re(apps_dir)
    out: dict[str, str] = {}
    notes: list[str] = []
    for line in listing.splitlines():
        match = manifest_re.match(line)
        if not match:
            continue
        slug = match.group(1)
        try:
            fqn = streamlit_fqn(cfg, slug)
        except ValueError:
            notes.append(f"skipped {line} at base: slug {slug!r} could never have deployed")
            continue
        out[fqn.upper()] = slug
    return out, notes


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def _valid_fqn(value: str) -> bool:
    try:
        validate_fqn(value, "tombstones[].identifier")
    except ConfigError:
        return False
    return value.count(".") == 2  # DROP needs the full DB.SCHEMA.NAME


def load_registry(path: Path) -> tuple[list[Tombstone], list[str]]:
    """Parse and schema-validate the registry. Returns ``(tombstones, errors)``.

    A missing file yields ``([], [])``. Any error means the registry cannot be
    trusted (the caller exits 2): a half-valid consent record must not gate a
    merge, and must never be rendered into DROP statements.
    """
    import yaml

    if not path.is_file():
        return [], []
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        return [], [f"{path}: invalid YAML: {exc}"]

    if data is None:
        return [], []
    if not isinstance(data, dict):
        return [], [f"{path}: expected a mapping with a 'tombstones' key at the top level"]
    unknown_top = set(data) - {"tombstones"}
    if unknown_top:
        return [], [f"{path}: unknown top-level key(s): {', '.join(sorted(unknown_top))}"]

    raw = data.get("tombstones")
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [f"{path}: 'tombstones' must be a list"]

    errors: list[str] = []
    seen: set[str] = set()
    tombstones: list[Tombstone] = []
    for index, entry in enumerate(raw):
        where = f"{path}: tombstones[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping")
            continue

        unknown = set(entry) - _ENTRY_KEYS
        if unknown:
            errors.append(
                f"{where}: unknown key(s): {', '.join(sorted(unknown))} "
                f"(allowed: identifier, reason, date)"
            )

        identifier = str(entry.get("identifier") or "").strip()
        if not identifier:
            errors.append(f"{where}: 'identifier' is required")
        elif not _valid_fqn(identifier):
            errors.append(
                f"{where}: identifier {identifier!r} is not a fully-qualified "
                f"DB.SCHEMA.NAME (three dot-separated Snowflake identifiers)"
            )
        elif identifier.upper() in seen:
            errors.append(f"{where}: duplicate identifier {identifier}")
        else:
            seen.add(identifier.upper())

        reason = str(entry.get("reason") or "").strip()
        if not reason:
            errors.append(f"{where}: 'reason' is required — say what removed this object")

        # yaml.safe_load parses a bare 2026-08-31 as datetime.date; accept both.
        raw_date = entry.get("date")
        if isinstance(raw_date, _dt.date):
            date_str = raw_date.isoformat()
        else:
            date_str = str(raw_date or "").strip()
            try:
                _dt.date.fromisoformat(date_str)
            except ValueError:
                errors.append(f"{where}: date {date_str!r} is not ISO YYYY-MM-DD")

        tombstones.append(Tombstone(identifier=identifier, reason=reason, date=date_str))
    return tombstones, errors


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def run_check(cfg, registry_path: Path, apps_dir: Path, base_ref: str) -> Result:
    """Registry contradictions + the removed-identifier diff rule."""
    result = Result()
    tombstones, errors = load_registry(registry_path)
    if errors:
        raise ToolError("\n".join(errors))
    result.tombstones = tombstones

    live = worktree_identifiers(cfg, apps_dir)

    for stone in tombstones:
        slug = live.get(stone.identifier.upper())
        if slug is not None:
            result.findings.append(
                {
                    "file": str(registry_path),
                    "line": 1,
                    "detail": (
                        f"tombstone {stone.identifier} is still declared by "
                        f"apps/{slug}/. Tombstoning a live app would make CI create "
                        f"it in the deploy step and drop it in the reconcile step of "
                        f"the same run. Remove this entry, or remove/rename the app."
                    ),
                }
            )

    base_commit = resolve_base(base_ref)
    base, notes = base_identifiers(cfg, base_commit, apps_dir)
    result.notes.extend(notes)

    tombstoned = {t.identifier.upper() for t in tombstones}
    today = _dt.date.today().isoformat()
    for identifier in sorted(set(base) - set(live) - tombstoned):
        slug = base[identifier]
        result.findings.append(
            {
                "file": f"apps/{slug}/snowflake.yml",
                "line": 1,
                "detail": (
                    f"removed app abandons STREAMLIT object {identifier} (declared by "
                    f"apps/{slug}/ at base {base_commit[:12]}, no longer declared, not in "
                    f"{registry_path}). The deploy pipeline only runs CREATE OR REPLACE — "
                    f"no delete path — so the object stays in Snowflake frozen at its "
                    f"last deploy and verify-deploy flags it on every later merge. "
                    f"Fix in THIS change: add to {registry_path}:  "
                    f"- identifier: {identifier}  "
                    f"reason: <renamed to NEW_NAME / retired>  date: {today}  "
                    f"(on merge, the reconcile step runs DROP STREAMLIT IF EXISTS "
                    f"{identifier}). If the object should keep existing, restore "
                    f"apps/{slug}/ instead — a rename mints a new object with a new URL."
                ),
            }
        )
    return result


def drop_sql(tombstones: list[Tombstone]) -> str:
    """One DROP per tombstone. Identifiers were validated to the safe FQN
    charset at load time, so rendering them directly cannot inject."""
    return "\n".join(f"DROP STREAMLIT IF EXISTS {t.identifier};" for t in tombstones)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Block changes that stop declaring a STREAMLIT identifier without "
            "tombstoning it in deploy/tombstones.yml; --drop-sql emits the "
            "reconcile statements."
        )
    )
    ap.add_argument("--registry", default=DEFAULT_REGISTRY, type=Path)
    ap.add_argument("--apps-dir", default=DEFAULT_APPS_DIR, type=Path)
    ap.add_argument(
        "--base-ref",
        default="origin/main",
        help=(
            "Ref to diff against; the comparison point is `git merge-base <ref> HEAD`. "
            "An unresolvable ref exits 2 — 'could not compare' must never pass as clean."
        ),
    )
    ap.add_argument(
        "--drop-sql",
        action="store_true",
        help="Print DROP STREAMLIT IF EXISTS for every tombstone and skip the diff rule.",
    )
    ap.add_argument("--config", type=Path, default=None, help="Path to streamsnow.config.yaml.")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    # pre-commit passes changed filenames positionally. The check is whole-repo
    # by nature (a removed identifier is not visible in any surviving file), so
    # filenames are accepted and ignored.
    ap.add_argument("paths", nargs="*", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.drop_sql:
        tombstones, errors = load_registry(args.registry)
        if errors:
            for err in errors:
                print(f"{_KIND}: {err}", file=sys.stderr)
            return 2
        sql = drop_sql(tombstones)
        if sql:
            print(sql)
        return 0

    try:
        cfg = load_config(args.config)
        result = run_check(cfg, args.registry, args.apps_dir, args.base_ref)
    except (ConfigError, ToolError) as exc:
        print(f"{_KIND}: cannot verify — {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "findings": result.findings,
                    "tombstones": [t.__dict__ for t in result.tombstones],
                    "notes": result.notes,
                },
                indent=2,
            )
        )
        return 0 if result.ok else 1

    for note in result.notes:
        print(f"{_KIND}: (note) {note}")
    if result.ok:
        print(
            f"{_KIND}: clean — {len(result.tombstones)} tombstone(s), "
            f"no identifier removed without one."
        )
    else:
        for f in result.findings:
            print(f"BLOCK {f['file']}:{f['line']} {f['detail']}")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
