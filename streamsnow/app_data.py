"""App data: the views and dynamic tables the deploy job builds for the apps (#79).

Why this exists
---------------
Apps read report-ready sources directly. When a query is slow, or its logic
repeats across pages or apps, the repo may declare an object in
``governance.app_data``: its DDL sits next to the app in
``apps/<slug>/sql_review/app_specific_reporting_objects/<DB>.<SCHEMA>.<OBJECT>.sql``
and the deploy job applies it, owned by the CI role, before any app is
replaced. Before this a person applied that DDL by hand, and an app deployed
against an object nobody had created (or had created from an older file) came
up empty or wrong while local preview worked.

Deployed DDL runs unattended as the CI role, so each file is checked before
anything is printed. Exactly one CREATE, first, whose target is the object the
file name and its index.yaml entry name (a copied file that still creates the
original object would silently redefine it), in a form that survives the next
deploy:

- ``CREATE OR ALTER DYNAMIC TABLE``. ``CREATE OR REPLACE`` recreates the table
  on every deploy: a full refresh each time, and its grants are gone.
- ``CREATE OR REPLACE VIEW ... COPY GRANTS`` (the form to write: a view holds
  no data, so replacing it is cheap and always takes the new query; without
  ``COPY GRANTS`` every deploy drops the view's grants), or ``CREATE OR ALTER
  VIEW`` (accepted; if Snowflake cannot alter the query that way, the deploy
  fails on that statement rather than keep the old query).
- Never a table. ``CREATE OR ALTER TABLE`` drops the data in a renamed or
  removed column and takes no ``AS SELECT``, so a summary table becomes a
  dynamic table instead.

A dynamic table names ``WAREHOUSE = <snowflake.objects.default_warehouse>``,
the one warehouse the CI role that owns it may use, and ``INITIALIZE =
ON_CREATE``, so its first refresh runs inside the deploy and a query that
cannot refresh fails the deploy before any app changes. The only other
statements allowed are ``GRANT SELECT`` on the same object to the roles its
index entry lists. Files whose ``DB.SCHEMA`` is not app data stay review-only:
nothing executes them. Docs: https://docs.snowflake.com/en/sql-reference/sql/create-dynamic-table,
https://docs.snowflake.com/en/sql-reference/sql/create-view and
https://docs.snowflake.com/en/sql-reference/sql/create-or-alter
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .policy import (
    DENIED,
    NAME_PATTERN,
    OUTSIDE_BOUNDARY,
    TWO_PART,
    SchemaPolicy,
    display_name,
    split_name,
)
from .tools import sql_review_index as sri
from .tools.check_schema_refs import _normalize_breaks, relation_names
from .tools.sql_review import OBJECTS_DIR, _mask_with_status

KIND_VIEW = "view"
KIND_DYNAMIC_TABLE = "dynamic_table"
OBJECT_KINDS = (KIND_VIEW, KIND_DYNAMIC_TABLE)
#: The SQL keyword for each kind, in CREATE, GRANT and DROP.
SQL_KIND = {KIND_VIEW: "VIEW", KIND_DYNAMIC_TABLE: "DYNAMIC TABLE"}
#: Why an app-data object may exist (D13): it pre-computes a slow or costly query, or
#: one definition replaces logic that two or more queries, pages or apps would repeat.
REASONS = ("performance", "shared_logic")
KIND_FINDING = "objects"
KIND_ADVISORY = "advisory"

_IDENT = r'"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*'
_NAME_RE = re.compile(NAME_PATTERN)
_CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+(?P<mode>ALTER|REPLACE)\s+)?"
    r"(?P<mods>(?:(?:SECURE|RECURSIVE|TRANSIENT|TEMPORARY|TEMP|VOLATILE|LOCAL|GLOBAL"
    r"|HYBRID|ICEBERG|EXTERNAL|EVENT|MATERIALIZED)\s+)*)"
    r"(?P<kind>DYNAMIC\s+TABLE|[A-Z_]+)\s+(?P<ine>IF\s+NOT\s+EXISTS\s+)?",
    re.I,
)
_GRANT_RE = re.compile(
    r"GRANT\s+(?P<privs>[A-Z_][A-Z_ ,]*?)\s+ON\s+(?P<kind>DYNAMIC\s+TABLE|VIEW)\s+", re.I
)
_TO_ROLE_RE = re.compile(rf"\s+TO\s+ROLE\s+(?P<role>{_IDENT})\s*\Z", re.I)
_WAREHOUSE_RE = re.compile(rf"\bWAREHOUSE\s*=\s*(?P<wh>{_IDENT})", re.I)
_INITIALIZE_RE = re.compile(r"\bINITIALIZE\s*=\s*(?P<v>[A-Z_]+)", re.I)
_COPY_GRANTS_RE = re.compile(r"\bCOPY\s+GRANTS\b", re.I)
_AS_SCAN_RE = re.compile(r"[()]|\bAS\b", re.I)


@dataclass(frozen=True)
class ParsedDDL:
    """One app-data DDL file, parsed. Empty ``problems`` means it may deploy."""

    kind: str = ""  # view | dynamic_table | "" when no deployable CREATE was found
    statements: tuple[str, ...] = ()  # original text, CREATE first, no trailing ';'
    reads: tuple[tuple[int, tuple[str, ...]], ...] = ()  # (file line, parts); one-part resolved
    body: str = ""  # the query after the top-level AS, original text
    line: int = 1  # the CREATE's line
    problems: tuple[tuple[int, str], ...] = ()


def _statements(text: str) -> tuple[list[tuple[int, str, str]], str | None]:
    """``(line, original, masked)`` per statement, split on ``;`` outside literals and
    comments; leading comments dropped, and the original cut where the masked text
    ends, so a trailing comment never swallows the ``;`` printed after it."""
    text = _normalize_breaks(text)  # a lone CR is a line break to the scanners and to us
    masked, unterminated = _mask_with_status(text)
    out: list[tuple[int, str, str]] = []
    start = 0
    for i, ch in enumerate(masked + ";"):
        if ch != ";":
            continue
        piece = masked[start:i]
        if piece.strip():
            lead = len(piece) - len(piece.lstrip())
            body = piece[lead:].rstrip()
            begin = start + lead
            out.append((text.count("\n", 0, begin) + 1, text[begin : begin + len(body)], body))
        start = i + 1
    return out, unterminated


def _kind_of(m: re.Match) -> str:
    word = " ".join(m.group("kind").upper().split())
    mods = set(m.group("mods").upper().split())
    if word == "DYNAMIC TABLE" and mods <= {"TRANSIENT"}:
        return KIND_DYNAMIC_TABLE
    if word == "VIEW" and mods <= {"SECURE", "RECURSIVE"}:
        return KIND_VIEW
    return ""


def _top_level_as(masked: str, start: int) -> re.Match | None:
    depth = 0
    for m in _AS_SCAN_RE.finditer(masked, start):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0:
            return m
    return None


def _grant_role(orig: str, masked: str, kind: str, expect: tuple[str, ...]) -> str | None:
    """The role of ``GRANT SELECT ON <kind> <expect> TO ROLE <role>``, else None."""
    g = _GRANT_RE.match(masked)
    if not g or " ".join(g.group("privs").upper().split()) != "SELECT":
        return None
    if " ".join(g.group("kind").upper().split()) != SQL_KIND[kind]:
        return None
    name = _NAME_RE.match(orig, g.end())
    if not name or split_name(name.group(0)) != expect:
        return None
    r = _TO_ROLE_RE.match(masked, name.end())
    if not r:
        return None
    parts = split_name(orig[r.start("role") : r.end("role")])
    return parts[0] if len(parts) == 1 else None


def ddl_kind(text: str) -> str:
    """The kind the first CREATE in ``text`` builds, or "" (lenient: no other checks)."""
    stmts, _ = _statements(text)
    for _line, _orig, masked in stmts:
        m = _CREATE_RE.match(masked)
        if m:
            return _kind_of(m)
    return ""


def parse_ddl(
    text: str, expect: tuple[str, ...], *, warehouse: str, grants: tuple[str, ...] = ()
) -> ParsedDDL:
    """Check one app-data file against the rules in the module docstring."""
    expect = tuple(expect)
    stmts, unterminated = _statements(text)
    if unterminated:
        return ParsedDDL(problems=((1, f"cannot parse: {unterminated}"),))
    creates = [s for s in stmts if re.match(r"CREATE\b", s[2], re.I)]
    if len(creates) != 1:
        line = creates[1][0] if len(creates) > 1 else 1
        return ParsedDDL(
            problems=(
                (
                    line,
                    f"needs exactly one CREATE statement, found {len(creates)}: the deploy "
                    "job builds one object per file",
                ),
            )
        )
    create = creates[0]
    line, orig, masked = create
    problems: list[tuple[int, str]] = []
    if stmts[0] is not create:
        problems.append(
            (stmts[0][0], "the CREATE comes first: the deploy job runs this file as the CI role")
        )
    m = _CREATE_RE.match(masked)
    kind = _kind_of(m) if m else ""
    if not kind:
        word = " ".join(m.group("kind").upper().split()) if m else ""
        detail = (
            "a plain table cannot deploy to app data: CREATE OR ALTER TABLE drops the data in "
            "a renamed or removed column and takes no AS SELECT. Pre-compute it as a dynamic "
            "table (CREATE OR ALTER DYNAMIC TABLE ... AS SELECT)"
            if word == "TABLE"
            else "only views and dynamic tables deploy to app data (CREATE OR REPLACE VIEW ... "
            "COPY GRANTS, CREATE OR ALTER VIEW, CREATE OR ALTER DYNAMIC TABLE)"
        )
        return ParsedDDL(line=line, problems=(*problems, (line, detail)))
    mode = (m.group("mode") or "").upper()
    if m.group("ine"):
        problems.append(
            (line, "IF NOT EXISTS never applies a changed definition: use CREATE OR ALTER")
        )
    target_m = _NAME_RE.match(orig, m.end())
    target = split_name(target_m.group(0)) if target_m else ()
    if target != expect:
        problems.append(
            (
                line,
                f"the CREATE names {display_name(target) or 'no object'}, but the file and its "
                f"index.yaml entry name {display_name(expect)}",
            )
        )
    after = target_m.end() if target_m else m.end()
    as_m = _top_level_as(masked, after)
    clauses = masked[after : as_m.start()] if as_m else masked[after:]
    body = orig[as_m.end() :] if as_m else ""
    if as_m is None:
        problems.append((line, "no AS <query> follows the object name"))
    if kind == KIND_DYNAMIC_TABLE:
        if mode != "ALTER":
            why = (
                "CREATE OR REPLACE recreates it on every deploy, a full refresh that also "
                "drops its grants"
                if mode == "REPLACE"
                else "a plain CREATE fails on the second deploy"
            )
            problems.append(
                (line, f"a dynamic table deploys with CREATE OR ALTER DYNAMIC TABLE: {why}")
            )
        whs = [
            split_name(orig[after + m_.start("wh") : after + m_.end("wh")])
            for m_ in _WAREHOUSE_RE.finditer(clauses)
        ]
        if whs != [(warehouse.upper(),)]:
            if len(whs) > 1:
                sets = "; this file sets it more than once"
            else:
                sets = f"; this file sets {display_name(whs[0])}" if whs else ""
            problems.append(
                (
                    line,
                    f"set WAREHOUSE = {warehouse} (snowflake.objects.default_warehouse) exactly "
                    "once: the CI role that owns the dynamic table may refresh it on no other "
                    f"warehouse{sets}",
                )
            )
        inits = [m_.group("v").upper() for m_ in _INITIALIZE_RE.finditer(clauses)]
        if inits != ["ON_CREATE"]:
            problems.append(
                (
                    line,
                    "set INITIALIZE = ON_CREATE exactly once: the first refresh then runs inside "
                    "the deploy, so a query that cannot refresh fails the deploy before any app "
                    "changes",
                )
            )
    elif mode == "REPLACE" and not _COPY_GRANTS_RE.search(clauses):
        problems.append(
            (
                line,
                "CREATE OR REPLACE VIEW needs COPY GRANTS before AS, or every deploy drops the "
                "view's grants; or use CREATE OR ALTER VIEW",
            )
        )
    elif not mode:
        problems.append(
            (
                line,
                "a view deploys with CREATE OR REPLACE VIEW ... COPY GRANTS (or CREATE OR "
                "ALTER VIEW): a plain CREATE fails on the second deploy",
            )
        )
    allowed = f"GRANT SELECT ON {SQL_KIND[kind]} {display_name(expect)} TO ROLE <role>"
    roles: list[str] = []
    for stmt in stmts:
        if stmt is create:
            continue
        role = _grant_role(stmt[1], stmt[2], kind, expect)
        if role is None:
            problems.append(
                (
                    stmt[0],
                    f"only `{allowed}` may follow the CREATE: the deploy job runs every "
                    "statement in this file as the CI role",
                )
            )
        else:
            roles.append(role)
    want = sorted({g.upper() for g in grants})
    got = sorted(set(roles))
    if got != want:
        problems.append(
            (
                line,
                f"the file grants SELECT to {', '.join(got) or 'no role'}, but its index.yaml "
                f"grants list {', '.join(want) or 'none'}: keep the two the same",
            )
        )
    reads: list[tuple[int, tuple[str, ...]]] = []
    if as_m is not None:
        # relation_names gives lines within the body; the body starts on this file line.
        base = line + orig.count("\n", 0, as_m.end())
        for rel in relation_names(body):
            if rel.kind != "object" or not rel.parts or rel.cte:
                continue  # a CTE in scope is not a table
            parts = rel.parts
            if len(parts) == 1:
                # Snowflake resolves an unqualified relation in a view's definition in the
                # view's own schema, so it is an app-data dependency (ordered, cycle-checked).
                parts = (*expect[:2], parts[0])
            reads.append((base + rel.line - 1, parts))
    return ParsedDDL(kind, tuple(s[1] for s in stmts), tuple(reads), body, line, tuple(problems))


_PLAIN_FQN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*){2}$")
_READ_PROBLEMS = {
    DENIED: "{name} is in a denied schema (governance.schema_deny): deployed DDL never reads it",
    OUTSIDE_BOUNDARY: "{name} is outside governance.sources: the CI role that builds this "
    "object has no read grant there, so the deploy would fail. Read a source, or add its "
    "schema to governance.sources",
    TWO_PART: "{name} does not name its database: the deploy job's session database is not "
    "yours. Write DATABASE.SCHEMA.OBJECT",
}


@dataclass(frozen=True)
class AppDataObject:
    """One app-data object the deploy job builds."""

    fqn: str
    kind: str
    app: str  # the owning app's slug
    file: str  # repo-relative path of its DDL file
    reason: str
    statements: tuple[str, ...]
    depends_on: tuple[str, ...]  # app-data objects its query reads


@dataclass
class AppDataPlan:
    """Every app-data object in the repo, checked, in the order the deploy applies them."""

    app_data: str
    objects: list[AppDataObject] = field(default_factory=list)  # valid only, apply order
    declared: dict[str, tuple[str, str]] = field(default_factory=dict)  # fqn -> (app, kind)
    findings: list[dict] = field(default_factory=list)
    advisories: list[dict] = field(default_factory=list)
    # index.yaml files whose objects: did not load in full: `declared` may miss entries
    incomplete: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def for_app(self, slug: str) -> list[dict]:
        return [f for f in (*self.findings, *self.advisories) if f["app"] == slug]

    def sql(self) -> str:
        """The DDL the deploy job runs, or "" when there is nothing to build or any finding."""
        if not self.ok or not self.objects:
            return ""
        out = [
            f"-- App data ({self.app_data}): {len(self.objects)} object(s) in dependency "
            "order, from `streamsnow objects-sql`.",
        ]
        for obj in self.objects:
            out += ["", f"-- {obj.fqn} ({obj.kind.replace('_', ' ')}) from {obj.file}"]
            out += [f"{stmt};" for stmt in obj.statements]
        return "\n".join(out) + "\n"

    def drop_order(self) -> list[tuple[str, str]]:
        """``(fqn, kind)`` for every declared object, dependents first (teardown)."""
        ordered = [o.fqn for o in reversed(self.objects)]
        rest = sorted(set(self.declared) - set(ordered))
        return [(f, self.declared[f][1]) for f in (*ordered, *rest)]

    def dynamic_tables(self, slug: str) -> list[str]:
        return sorted(
            f
            for f, (app, kind) in self.declared.items()
            if app == slug and kind == KIND_DYNAMIC_TABLE
        )


def _rel(repo: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _order(objects: dict[str, AppDataObject], add) -> list[AppDataObject]:
    """Dependencies first, ties by name so the output is the same on every run. Whatever
    cannot be ordered is in, or reads from, a cycle, and each such object is a finding."""
    pending = {f: {d for d in o.depends_on if d in objects} for f, o in objects.items()}
    ordered: list[AppDataObject] = []
    while True:
        ready = sorted(f for f, deps in pending.items() if not deps)
        if not ready:
            break
        for f in ready:
            ordered.append(objects[f])
            del pending[f]
        for deps in pending.values():
            deps.difference_update(ready)
    stuck = ", ".join(sorted(pending))
    for f in sorted(pending):
        add(
            objects[f].app,
            objects[f].file,
            1,
            f"{f} cannot be ordered: it is in, or reads from, a dependency cycle among app-data "
            f"objects ({stuck}). Break the cycle",
        )
    return ordered


def load_app_data(repo: Path, cfg: Config, apps_dir: Path | None = None) -> AppDataPlan:
    """Load and check every app's app-data objects (see the module docstring).

    Why repo-wide: one app's view may read another app's dynamic table, so the
    deploy order, the one-owner rule and an unknown app-data name are only
    decidable across all apps, and the deploy job applies all of them in one
    pass before any app is replaced.
    """
    target = tuple(cfg.governance.app_data.split("."))
    policy = SchemaPolicy.from_governance(cfg.governance)
    plan = AppDataPlan(app_data=cfg.governance.app_data)
    root = Path(apps_dir) if apps_dir is not None else Path("apps")
    root = root if root.is_absolute() else repo / root
    apps = (
        sorted(p for p in root.iterdir() if (p / "snowflake.yml").is_file())
        if root.is_dir()
        else []
    )

    def add(slug: str, file: str, line: int, detail: str, kind: str = KIND_FINDING) -> None:
        bucket = plan.findings if kind == KIND_FINDING else plan.advisories
        bucket.append({"kind": kind, "app": slug, "file": file, "line": line, "detail": detail})

    built: dict[str, AppDataObject] = {}
    valid: set[str] = set()
    owners: dict[str, list[tuple[str, str]]] = {}
    for app in apps:
        slug = app.name
        idx = sri.load_index(app)
        rel_index = _rel(repo, idx.path)
        if idx.exists and not idx.objects_complete:
            plan.incomplete.append(rel_index)
            add(
                slug,
                rel_index,
                1,
                "objects: in index.yaml did not load in full (see its index findings), so the "
                "deploy job cannot tell which app-data objects this app declares",
            )
        odir = app / "sql_review" / OBJECTS_DIR
        by_name: dict[tuple[str, ...], list[Path]] = {}
        for path in sorted(odir.glob("*.sql")) if odir.is_dir() else []:
            parts = split_name(path.name[: -len(".sql")])
            if len(parts) == 3 and parts[:2] == target:
                by_name.setdefault(parts, []).append(path)
        on_disk: dict[tuple[str, ...], Path] = {}
        collided: set[tuple[str, ...]] = set()
        for parts, paths in by_name.items():
            if len(paths) == 1:
                on_disk[parts] = paths[0]
                continue
            collided.add(parts)  # never pick one silently
            names = ", ".join(p.name for p in paths)
            for path in paths:
                add(
                    slug,
                    _rel(repo, path),
                    1,
                    f"{names} resolve to the same object, {display_name(parts)} (unquoted names "
                    "fold to upper case): keep one file",
                )
        declared_here: set[tuple[str, ...]] = set()
        for obj in idx.objects:
            parts = split_name(obj.name)
            if len(parts) != 3 or parts[:2] != target:
                continue  # review-only: nothing deploys it
            declared_here.add(parts)
            fqn = display_name(parts)
            owners.setdefault(fqn, []).append((slug, rel_index))
            if not _PLAIN_FQN_RE.match(obj.name):
                plan.declared.setdefault(fqn, (slug, ""))
                add(
                    slug,
                    rel_index,
                    1,
                    f"{obj.name}: an app-data object is named with three unquoted identifiers "
                    "(DATABASE.SCHEMA.NAME), the form the deploy, verify and tombstone steps "
                    "render into SQL",
                )
                continue
            if parts in collided:
                plan.declared.setdefault(fqn, (slug, ""))
                continue  # reported above, once per file
            path = on_disk.get(parts)
            if path is None:
                plan.declared.setdefault(fqn, (slug, ""))
                add(
                    slug,
                    rel_index,
                    1,
                    f"{fqn} is declared under objects: but has no DDL file at "
                    f"sql_review/{OBJECTS_DIR}/{fqn}.sql, so the deploy job cannot build it",
                )
                continue
            rel = _rel(repo, path)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                plan.declared.setdefault(fqn, (slug, ""))
                add(slug, rel, 1, f"unreadable DDL file ({exc})")
                continue
            parsed = parse_ddl(
                text,
                parts,
                warehouse=cfg.snowflake.objects.default_warehouse,
                grants=tuple(obj.grants),
            )
            plan.declared.setdefault(fqn, (slug, parsed.kind))
            problems = list(parsed.problems)
            deps: list[str] = []
            for line, rparts in parsed.reads:
                name = display_name(rparts)
                if len(rparts) == 3 and rparts[:2] == target:
                    if name not in deps:
                        deps.append(name)
                    continue
                if len(rparts) == 2:
                    # Always a finding here, whatever the boundary says: SCHEMA.OBJECT resolves
                    # against the deploy session's database, so neither the order nor a cycle
                    # through it could be seen. (parse_ddl already resolved one-part names.)
                    problems.append((line, _READ_PROBLEMS[TWO_PART].format(name=name)))
                    continue
                verdict = policy.classify_parts(rparts)
                if verdict in _READ_PROBLEMS:
                    problems.append((line, _READ_PROBLEMS[verdict].format(name=name)))
            for line, detail in problems:
                add(slug, rel, line, detail)
            # Keep the first declaration (as `declared` does) so a duplicate never hides the
            # earlier one's findings; the duplicate is reported below and drops it from valid.
            built.setdefault(
                fqn,
                AppDataObject(
                    fqn, parsed.kind, slug, rel, obj.reason, parsed.statements, tuple(deps)
                ),
            )
            if not problems:
                valid.add(fqn)
        for parts, path in on_disk.items():
            if parts not in declared_here and idx.objects_complete:
                add(
                    slug,
                    _rel(repo, path),
                    1,
                    f"{display_name(parts)} sits in app data ({plan.app_data}) but is not under "
                    "objects: in index.yaml, and the deploy job builds only declared objects. "
                    "Declare it (with a reason), or move the file out of app data",
                )
    for fqn, owned in owners.items():
        slugs = list(dict.fromkeys(s for s, _ in owned))
        if len(slugs) > 1:
            valid.discard(fqn)
            names = ", ".join(f"apps/{s}" for s in slugs)
            for s, rel_index in owned:
                add(
                    s,
                    rel_index,
                    1,
                    f"{fqn} is declared by {names}: one app owns each app-data object, so the "
                    "deploy job builds it from one file. Keep it in one app; the others read it",
                )
    for fqn, obj in built.items():
        for dep in obj.depends_on:
            if dep not in plan.declared:
                valid.discard(fqn)
                add(
                    obj.app,
                    obj.file,
                    1,
                    f"{fqn} reads {dep}, which sits in app data but no app declares: the deploy "
                    f"job would fail creating {fqn}. Declare {dep} in the app that owns it, or "
                    "read a source",
                )
    # An object whose dependency is not valid cannot deploy either: the plan holds valid
    # objects only, so drop dependents until nothing more changes.
    while True:
        dropped = {f for f in valid if any(d not in valid for d in built[f].depends_on)}
        if not dropped:
            break
        valid -= dropped
    plan.objects = [o for o in _order(built, add) if o.fqn in valid]
    return plan
