"""Block changes that abandon a deployed STREAMLIT object without tombstoning it.

Why this exists
---------------
The generated deploy pipeline only ever runs ``CREATE OR REPLACE STREAMLIT``
(:mod:`streamsnow.deploy`). It has **no delete path**. So when an app directory
is renamed or removed, the previously deployed object keeps existing in
Snowflake, frozen at the source of the last merge that deployed it — and
nothing after the merge notices: ``streamsnow verify-deploy`` only checks the
app directories that still exist. Nothing ever cleans it up, because nothing is
left in the repo that knows it exists, so this PR-time check is the only
detection.

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
      - identifier: STREAMSNOW_APPS.DASHBOARDS.ACME_SALES_DASHBOARD
        reason: renamed to ACME_REVENUE_DASHBOARD
        date: 2026-08-31

- ``identifier`` — fully-qualified ``DB.SCHEMA.NAME`` (validated with
  :func:`streamsnow.config.validate_fqn`, exactly three parts). Unique within
  the file (Snowflake identifiers are case-insensitive, so uniqueness is too).
- ``reason`` — non-empty free text: what removed the object and, for a rename,
  what replaced it.
- ``date`` — ISO ``YYYY-MM-DD`` (a quoted string or a bare YAML date).
- ``kind``: optional, ``view`` or ``dynamic_table`` for an app-data object
  (#79), absent for a Streamlit app. It picks the DROP, because the DDL file
  that defined a retired object is gone by the time a later deploy runs.

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
    it in the reconcile step of the same run, flapping forever. The same rule
    covers app-data views and dynamic tables declared at the merge base: each
    removed one needs a tombstone whose ``kind`` is ``view`` or ``dynamic_table``
    (and matches the base DDL when that can be read), and an inventory the tool
    cannot read completely is a finding, never an empty set.

``check_tombstones.py --drop-sql``
    Validate the registry and print one ``DROP STREAMLIT|VIEW|DYNAMIC TABLE IF
    EXISTS`` statement per tombstone, by its recorded kind: the shape the deploy
    workflow's reconcile step consumes. Prints nothing on a registry error, so a
    malformed file can never be turned into DROP statements, and refuses (exit 2)
    while the live app-data inventory is incomplete (an empty registry exits 0
    without checking: it prints no DROP, so there is nothing to get wrong). A
    tombstone with a kind must name an object in ``governance.app_data``.

Exit codes: 0 = clean, 1 = finding (missing tombstone / live-app tombstone),
2 = cannot verify (unreadable or invalid registry, missing config, git or
base-ref failure). Failing *toward* 2 on a missing base ref is deliberate:
"could not compare" must never look like "nothing was removed".
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..app_data import OBJECT_KINDS, SQL_KIND, ddl_kind, is_plain_fqn, load_app_data
from ..config import (
    CONFIG_SCHEMA_VERSION,
    DEFAULT_APP_DATA_SCHEMA,
    Config,
    ConfigError,
    DeployCfg,
    SnowflakeObjects,
    load_config,
    validate_fqn,
)
from ..deploy import streamlit_fqn
from ..policy import display_name, split_name
from .sql_review import OBJECTS_DIR

_KIND = "tombstones"

DEFAULT_REGISTRY = Path("deploy/tombstones.yml")
DEFAULT_APPS_DIR = Path("apps")

_ENTRY_KEYS = {"identifier", "reason", "date", "kind"}
#: The DROP keyword per tombstone kind; no kind is a Streamlit app (the original shape).
_DROP_KEYWORD = {"": "STREAMLIT", **SQL_KIND}


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
    kind: str = ""  # "" = a Streamlit app; view | dynamic_table = an app-data object


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
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
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
    if not apps_dir.is_dir():
        # glob on a missing directory silently yields [] — and an empty live
        # map would let the --drop-sql live guard pass a DROP for a declared
        # app when the tool is run from the wrong cwd. "Cannot see the apps"
        # must never read as "no apps exist".
        raise ToolError(
            f"apps directory {apps_dir} not found from cwd {Path.cwd()} — run from "
            "the repo root (or pass --apps-dir)"
        )
    out: dict[str, str] = {}
    for yml in sorted(apps_dir.glob("*/snowflake.yml")):
        slug = yml.parent.name
        try:
            fqn = streamlit_fqn(cfg, slug)
        except ValueError as exc:
            raise ToolError(f"apps/{slug}: {exc}") from exc
        out[fqn.upper()] = slug
    return out


def _base_config(cfg, base_commit: str) -> tuple[Config, list[str]]:
    """The deploy identity AS OF the base commit, for deriving what was deployed THEN.

    A PR that moves ``app_database``/``app_schema`` re-derives every base FQN
    into the NEW namespace if the current config is used on both sides: the
    old objects silently orphan with no tombstone required. Deriving the base
    inventory from the base commit's own config closes that.

    Only the deploy identity is read from the base (``snowflake.objects`` and
    ``deploy``), because a Streamlit FQN depends on nothing else. Parsing the
    whole base config meant any unrelated block this StreamSnow can no longer
    read (a schema_version 1 governance block, say) fell back to the current
    config: the exact silent orphan this function exists to prevent. When the
    identity itself is missing or unparseable, fall back with a note: a
    wrong-namespace nag beats a silent orphan, and the note says why.
    """
    try:
        raw = _git(["show", f"{base_commit}:streamsnow.config.yaml"])
    except ToolError:
        return cfg, ["base commit has no streamsnow.config.yaml: using current config"]
    try:
        import yaml as _yaml

        data = _yaml.safe_load(raw) or {}
        objects = SnowflakeObjects.from_dict(dict(data["snowflake"]["objects"]))
        deploy = DeployCfg.from_dict(dict(data.get("deploy") or {}))
    except Exception as exc:  # noqa: BLE001 (fall back rather than block)
        return cfg, [f"base config unparseable ({exc}): using current config"]
    snowflake = dataclasses.replace(cfg.snowflake, objects=objects)
    return dataclasses.replace(cfg, snowflake=snowflake, deploy=deploy), []


def base_identifiers(
    cfg, base_commit: str, apps_dir: Path = Path("apps")
) -> tuple[dict[str, str], list[str]]:
    """Map UPPERCASED deployed identifier -> slug for every app at ``base_commit``.

    Enumerated with ``git ls-tree`` because the identity of an app is its slug
    plus config — manifest *presence* is the marker, manifest content never
    changes the derivation (see module docstring). The derivation uses the
    BASE commit's config (see :func:`_base_config`): what was deployed then is
    a function of the config then. A slug that is invalid at base is skipped
    with a note rather than failing the run: it could never have deployed, so
    it cannot have left an orphan, and blocking today's change on it would be
    wrong.
    """
    base_cfg, notes = _base_config(cfg, base_commit)
    rel = str(apps_dir).strip("/")
    listing = _git(["ls-tree", "-r", "--name-only", base_commit, "--", f"{rel}/"])
    manifest_re = _manifest_re(apps_dir)
    out: dict[str, str] = {}
    for line in listing.splitlines():
        match = manifest_re.match(line)
        if not match:
            continue
        slug = match.group(1)
        try:
            fqn = streamlit_fqn(base_cfg, slug)
        except ValueError:
            notes.append(f"skipped {line} at base: slug {slug!r} could never have deployed")
            continue
        out[fqn.upper()] = slug
    return out, notes


def live_app_data(cfg, apps_dir: Path) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """UPPER FQN -> (slug, kind) for every app-data object the working tree declares,
    plus the index.yaml files whose objects: did not load in full. An incomplete
    inventory must never read as "nothing declared": a tombstone for a live object
    would then pass the guard and drop what the deploy just built (#79)."""
    plan = load_app_data(Path.cwd(), cfg, apps_dir=apps_dir)
    return {fqn.upper(): owner for fqn, owner in plan.declared.items()}, list(plan.incomplete)


def base_app_data(base_commit: str, apps_dir: Path) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """UPPER FQN -> (slug, kind) for every app-data object declared at the base commit,
    plus every problem that makes that inventory unverifiable.

    Read from the base commit's own config and index files, like the Streamlit
    inventory: what the deploy job built then is a function of the repo then. A
    schema_version 1 base had no app data, so nothing there was ever deployed, and a
    base without a config compares nothing (as for Streamlit apps). Anything else the
    tool cannot read is a problem the caller reports as a finding: "could not read
    the base" must never look like "nothing was removed". The kind comes from the
    base DDL file; "" when it cannot be told (the tombstone still needs one).
    """
    try:
        raw_text = _git(["show", f"{base_commit}:streamsnow.config.yaml"])
    except ToolError:
        return {}, []

    def unverifiable(why: str) -> list[str]:
        return [
            f"base streamsnow.config.yaml {why}, so the tool cannot tell which app-data "
            "objects were deployed"
        ]

    try:
        raw = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError:
        return {}, unverifiable("is not valid YAML")
    if not isinstance(raw, dict):
        return {}, unverifiable("is not a mapping")
    gov = raw.get("governance") or {}
    if not isinstance(gov, dict):
        return {}, unverifiable("has a governance: that is not a mapping")
    declared_version = raw.get("schema_version", CONFIG_SCHEMA_VERSION)
    try:  # mirror Config.from_dict: a missing key is the current version, "2" is 2
        if isinstance(declared_version, bool):
            raise TypeError("a boolean is not a version: int(True) would read as version 1")
        version = int(declared_version)
    except (TypeError, ValueError):
        return {}, unverifiable(f"has schema_version {raw.get('schema_version')!r}, not a number")
    if version < CONFIG_SCHEMA_VERSION or "database" in gov or "schema_allow" in gov:
        return {}, []  # schema_version 1: no app data existed, nothing was deployed there
    if version > CONFIG_SCHEMA_VERSION:
        return {}, unverifiable(f"has schema_version {version}, newer than this streamsnow")
    snowflake = raw.get("snowflake")
    objects = snowflake.get("objects") if isinstance(snowflake, dict) else None
    app_db = objects.get("app_database") if isinstance(objects, dict) else None
    app_data = gov.get("app_data") or (
        f"{app_db}.{DEFAULT_APP_DATA_SCHEMA}" if isinstance(app_db, str) and app_db else None
    )
    target = split_name(app_data) if isinstance(app_data, str) else ()
    if len(target) != 2:
        return {}, unverifiable(
            "names no readable app data (governance.app_data, or snowflake.objects.app_database "
            "for the default)"
        )
    rel = str(apps_dir).strip("/")
    listing = _git(["ls-tree", "-r", "--name-only", base_commit, "--", f"{rel}/"]).splitlines()
    present = set(listing)
    out: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    for line in listing:
        m = re.match(rf"^{re.escape(rel)}/([^/]+)/sql_review/index\.yaml$", line)
        if not m or f"{rel}/{m.group(1)}/snowflake.yml" not in present:
            continue
        slug = m.group(1)
        unreadable = (
            f"{line} at base {base_commit[:12]}: its objects: cannot be read in full, so the "
            "tool cannot tell which app-data objects this app declared"
        )
        try:
            index = yaml.safe_load(_git(["show", f"{base_commit}:{line}"]))
        except (ToolError, yaml.YAMLError):
            problems.append(unreadable)
            continue
        index = {} if index is None else index
        entries = index.get("objects") if isinstance(index, dict) else None
        entries = [] if isinstance(index, dict) and entries is None else entries
        if not isinstance(entries, list):
            problems.append(unreadable)
            continue
        prefix = f"{rel}/{slug}/sql_review/{OBJECTS_DIR}/"
        files = {
            split_name(p[len(prefix) : -len(".sql")]): p
            for p in listing
            if p.startswith(prefix) and p.endswith(".sql") and "/" not in p[len(prefix) :]
        }
        for entry in entries:
            name = entry.get("name") if isinstance(entry, dict) else None
            parts = split_name(name) if isinstance(name, str) else ()
            if len(parts) != 3:
                # A name that cannot be compared might have been an app-data object.
                problems.append(unreadable)
                break
            if parts[:2] != target:
                continue
            path = files.get(parts)
            kind = ddl_kind(_git(["show", f"{base_commit}:{path}"])) if path else ""
            out[display_name(parts).upper()] = (slug, kind)
    return out, problems


def _in_app_data(cfg, identifier: str) -> bool:
    parts = split_name(identifier)
    return len(parts) == 3 and parts[:2] == split_name(cfg.governance.app_data)


_OUTSIDE_APP_DATA = (
    "is outside governance.app_data: a tombstone with a kind retires objects in "
    "governance.app_data only; drop objects in other schemas by hand"
)

_NEEDS_KIND = (
    "needs kind: view or kind: dynamic_table: a tombstone without one is a Streamlit "
    "app, so --drop-sql would render DROP STREAMLIT for an app-data object"
)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def _valid_fqn(value: str) -> bool:
    try:
        validate_fqn(value, "tombstones[].identifier")
    except ConfigError:
        return False
    # DROP needs the full DB.SCHEMA.NAME; fullmatch, since `$` lets a trailing newline through.
    return is_plain_fqn(value)


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
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
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
                f"(allowed: identifier, reason, date, kind)"
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

        kind = str(entry.get("kind") or "").strip()
        if kind and kind not in OBJECT_KINDS:
            errors.append(
                f"{where}: kind must be view or dynamic_table for an app-data object "
                f"(got {kind!r}); leave it out for a Streamlit app"
            )
        tombstones.append(Tombstone(identifier=identifier, reason=reason, date=date_str, kind=kind))
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

    rel_apps = str(apps_dir).strip("/")
    live_objects, incomplete = live_app_data(cfg, apps_dir)
    for path in incomplete:
        result.findings.append(
            {
                "file": path,
                "line": 1,
                "detail": "objects: did not load in full, so the tool cannot tell which "
                "app-data objects this app declares; fix the index first",
            }
        )
    for stone in tombstones:
        owner = live_objects.get(stone.identifier.upper())
        if owner is not None:
            result.findings.append(
                {
                    "file": str(registry_path),
                    "line": 1,
                    "detail": (
                        f"tombstone {stone.identifier} is still declared as an app-data object "
                        f"by {rel_apps}/{owner[0]}/: the deploy job would build it and the reconcile "
                        "step drop it in the same run. Remove this entry, or remove the object."
                    ),
                }
            )
        if stone.kind and not _in_app_data(cfg, stone.identifier):
            result.findings.append(
                {
                    "file": str(registry_path),
                    "line": 1,
                    "detail": f"tombstone {stone.identifier} {_OUTSIDE_APP_DATA}",
                }
            )
        if not stone.kind and _in_app_data(cfg, stone.identifier):
            result.findings.append(
                {
                    "file": str(registry_path),
                    "line": 1,
                    "detail": f"tombstone {stone.identifier} {_NEEDS_KIND}",
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

    by_id = {t.identifier.upper(): t for t in tombstones}
    for identifier in sorted(set(base) - set(live)):
        stone = by_id.get(identifier)
        if stone is not None and stone.kind:
            result.findings.append(
                {
                    "file": str(registry_path),
                    "line": 1,
                    "detail": f"tombstone {identifier} names a Streamlit app but has kind: "
                    f"{stone.kind}, so --drop-sql would run the wrong DROP. Remove kind:.",
                }
            )
    base_objects, problems = base_app_data(base_commit, apps_dir)
    for problem in problems:  # fail closed: an unreadable base is a finding, not a note
        result.findings.append({"file": "streamsnow.config.yaml", "line": 1, "detail": problem})
    for fqn in sorted(set(base_objects) & set(live_objects)):
        (_slug, was), (_owner, now) = base_objects[fqn], live_objects[fqn]
        if was and now and was != now:
            result.findings.append(
                {
                    "file": f"{rel_apps}/{_owner}/sql_review/{OBJECTS_DIR}/{fqn}.sql",
                    "line": 1,
                    "detail": (
                        f"{fqn} was a {was.replace('_', ' ')} at base and is a "
                        f"{now.replace('_', ' ')} now: CREATE OR ALTER cannot change an "
                        "object's kind, so the deploy would fail. Give the new object a new "
                        f"name and tombstone {fqn} with kind: {was}."
                    ),
                }
            )
    for fqn in sorted(set(base_objects) - set(live_objects)):
        slug, kind = base_objects[fqn]
        stone = by_id.get(fqn)
        if stone is None:
            result.findings.append(
                {
                    "file": f"{rel_apps}/{slug}/sql_review/index.yaml",
                    "line": 1,
                    "detail": (
                        f"removed app-data object {fqn} (declared by {rel_apps}/{slug}/ at base "
                        f"{base_commit[:12]}, not in {registry_path}) stays in Snowflake, still "
                        "refreshing if it is a dynamic table. Fix in THIS change: add to "
                        f"{registry_path}:  - identifier: {fqn}  kind: "
                        f"{kind or 'view | dynamic_table'}  reason: <why it went>  date: {today}"
                    ),
                }
            )
        elif stone.kind not in OBJECT_KINDS:
            # Whatever the base shows: without a kind --drop-sql renders DROP STREAMLIT.
            if _in_app_data(cfg, fqn):
                continue  # already reported once by the loop above
            result.findings.append(
                {"file": str(registry_path), "line": 1, "detail": f"tombstone {fqn} {_NEEDS_KIND}"}
            )
        elif kind and stone.kind != kind:
            result.findings.append(
                {
                    "file": str(registry_path),
                    "line": 1,
                    "detail": (
                        f"tombstone {fqn} has kind {stone.kind}, but at base it was a "
                        f"{kind.replace('_', ' ')}, so --drop-sql would run the wrong DROP. "
                        f"Set kind: {kind}."
                    ),
                }
            )
    return result


def drop_sql(tombstones: list[Tombstone]) -> str:
    """One DROP per tombstone, by its recorded kind, so a later deploy drops a retired
    app-data object without the DDL file that defined it. Identifiers were validated
    to the safe FQN charset and kinds to a fixed set at load time, so nothing here
    can inject."""
    return "\n".join(f"DROP {_DROP_KEYWORD[t.kind]} IF EXISTS {t.identifier};" for t in tombstones)


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
        help="Print DROP STREAMLIT|VIEW|DYNAMIC TABLE IF EXISTS for every tombstone, by its kind, and skip the diff rule.",
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
        if not tombstones:
            return 0  # nothing to drop — no config or live-app check needed
        # Live-app guard: a tombstone matching a CURRENTLY DECLARED app means
        # the deploy that just created/replaced it would drop it moments
        # later. The PR check catches this too, but the reconcile step is the
        # last hand on the DROP — it must refuse on its own evidence (a
        # direct push to main never went through the PR check).
        try:
            cfg = load_config(args.config)
            live = worktree_identifiers(cfg, args.apps_dir)
            live_objects, incomplete = live_app_data(cfg, args.apps_dir)
        except (ConfigError, ToolError) as exc:
            print(f"{_KIND}: cannot verify live apps before emitting DROPs: {exc}", file=sys.stderr)
            return 2
        if incomplete:
            print(
                f"{_KIND}: refusing --drop-sql: the app-data inventory is incomplete "
                f"({', '.join(incomplete)}: objects: did not load in full), so a tombstone "
                "for a live object could not be told apart",
                file=sys.stderr,
            )
            return 2
        kindless = [
            t.identifier for t in tombstones if not t.kind and _in_app_data(cfg, t.identifier)
        ]
        outside = [
            t.identifier for t in tombstones if t.kind and not _in_app_data(cfg, t.identifier)
        ]
        for ident in outside:
            print(
                f"{_KIND}: refusing --drop-sql: tombstone {ident} {_OUTSIDE_APP_DATA}",
                file=sys.stderr,
            )
        for ident in kindless:
            print(f"{_KIND}: refusing --drop-sql: tombstone {ident} {_NEEDS_KIND}", file=sys.stderr)
        conflicts = [t.identifier for t in tombstones if t.identifier.upper() in live]
        for ident in conflicts:
            print(
                f"{_KIND}: refusing --drop-sql: tombstone {ident} is still a "
                f"declared app ({live[ident.upper()]}) - dropping it would kill "
                "the app this very deploy just created",
                file=sys.stderr,
            )
        for t in tombstones:
            owner = live_objects.get(t.identifier.upper())
            if owner is not None:
                conflicts.append(t.identifier)
                print(
                    f"{_KIND}: refusing --drop-sql: tombstone {t.identifier} is still declared "
                    f"as an app-data object by {args.apps_dir}/{owner[0]}/; dropping it would remove what "
                    "this very deploy just built",
                    file=sys.stderr,
                )
        if kindless or conflicts or outside:
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
