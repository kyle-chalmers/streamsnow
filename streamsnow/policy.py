"""Schema access policy: the single source of truth for the governance boundary.

Every check that asks "may app code read this?" (``check schema-refs``, the
``sf_exec`` guard in front of the live SQL review, ``validate-app``,
``migrate``, ``ci-key verify --object``) builds one ``SchemaPolicy`` from the
``governance`` section of ``streamsnow.config.yaml`` and asks it, so the
pre-commit hook, CI and the live review can never disagree.

It is database-aware because a team's report-ready data lives in several
databases (#78). One database plus bare schema names could not say "FINANCE_DB
MARTS yes, SALES_DB MARTS no", and a bare deny entry could not be narrowed to
one database. So the policy holds full ``DATABASE.SCHEMA`` sources plus the
repo's app-data schema, and deny entries are bare (``RAW``: that schema in
every database) or qualified (``FINANCE.RAW``: one database).

Names are compared the way Snowflake resolves them (:func:`split_name`). An
unquoted identifier folds to upper case; a quoted one keeps its exact text and
may contain dots. Config entries are unquoted, so ``"analytics_db"."reporting"``
(quoted, lower case) is a different schema from the source
``ANALYTICS_DB.REPORTING``, and ``SALES_DB."PUBLIC.EXTRA".T`` has three parts.
The deny list alone compares case-insensitively, erring toward blocking.

``classify`` gives one verdict per relation name:

- ``denied``: the deny list matches (an exact ``read_exceptions`` entry aside).
  Always a failure: a query against a raw layer compiled under a broad
  personal role and failed deployed under the app owner's grants.
- ``allowed``: a three-part name in a source or the app data, or a read exception.
- ``outside_boundary``: any other three-part name. The CI role that owns the
  deployed app has no grant there, so the dashboard comes up empty in
  production while local preview works.
- ``two_part``: ``SCHEMA.OBJECT``. It resolves against the session's database,
  which differs between preview and the deployed app. A two-part name meets
  bare deny entries only: its database is unknown, so a qualified entry cannot
  be judged, and the two-part finding already asks for the full name.
- ``ignored``: one-part names (CTEs, table functions), ``INFORMATION_SCHEMA``
  and ``SNOWFLAKE.*`` system objects, or anything when no boundary is set.

``outside_boundary`` and ``two_part`` follow ``boundary``: warnings under
``warn`` (the default for one release, so a fleet can fix its SQL first),
failures under ``enforce``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import GovernanceCfg

ALLOWED = "allowed"
DENIED = "denied"
OUTSIDE_BOUNDARY = "outside_boundary"
TWO_PART = "two_part"
IGNORED = "ignored"
#: Verdicts that follow ``boundary`` (warn or enforce). DENIED always fails.
BOUNDARY_VERDICTS = (OUTSIDE_BOUNDARY, TWO_PART)
PUBLIC_SCHEMA = "PUBLIC"
_SYSTEM_SCHEMA = "INFORMATION_SCHEMA"
_SYSTEM_DATABASES = frozenset({"SNOWFLAKE"})

_PART = r'"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*'
#: One dotted SQL name: unquoted or "quoted" parts, whitespace (newlines too) around dots.
#: ``DB..OBJ`` is one name too: Snowflake resolves the empty middle part as ``PUBLIC``
#: (https://docs.snowflake.com/en/sql-reference/name-resolution).
NAME_PATTERN = rf"(?:{_PART})(?:\s*\.\s*\.\s*(?:{_PART}))?(?:\s*\.\s*(?:{_PART}))*"
_PART_RE = re.compile(_PART)
_NAME_RE = re.compile(NAME_PATTERN)
_PLAIN_RE = re.compile(r"^[A-Z_][A-Z0-9_$]*$")


def split_name(text: str) -> tuple[str, ...]:
    """A dotted SQL name as Snowflake resolves it, one entry per identifier.

    Unquoted parts fold to upper case; a quoted part keeps its exact text, may
    hold dots, and has ``""`` unescaped. ``DB..OBJ`` is ``DB.PUBLIC.OBJ``: Snowflake
    resolves an empty schema part as ``PUBLIC``, so a double dot must not hide a
    schema from the boundary or the deny list. ``()`` when *text* is not one name.
    """
    text = text.strip()
    if not _NAME_RE.fullmatch(text):
        return ()
    parts: list[str] = []
    for m in _PART_RE.finditer(text):
        part = m.group(0)
        parts.append(part[1:-1].replace('""', '"') if part.startswith('"') else part.upper())
        if len(parts) == 1 and re.match(r"\s*\.\s*\.", text[m.end() :]):
            parts.append(PUBLIC_SCHEMA)
    return tuple(parts)


def display_name(parts: tuple[str, ...]) -> str:
    """Parts back to SQL: plain upper-case identifiers bare, every other part quoted."""
    return ".".join(p if _PLAIN_RE.match(p) else '"' + p.replace('"', '""') + '"' for p in parts)


def _cfg_parts(entry: str) -> tuple[str, ...]:
    """A config entry's parts. Config values are validated unquoted identifiers."""
    return tuple(p.strip().upper() for p in entry.split("."))


@dataclass(frozen=True)
class SchemaPolicy:
    """Resolved governance policy. Built from config; consumed by the checks."""

    sources: tuple[str, ...] = ()
    app_data: str = ""
    schema_deny: tuple[str, ...] = ()
    read_exceptions: tuple[str, ...] = ()
    boundary: str = "warn"

    @classmethod
    def from_governance(cls, gov: GovernanceCfg) -> SchemaPolicy:
        return cls(
            sources=tuple(gov.sources),
            app_data=gov.app_data,
            schema_deny=tuple(gov.schema_deny),
            read_exceptions=tuple(gov.read_exceptions),
            boundary=gov.boundary,
        )

    @property
    def boundary_schemas(self) -> tuple[str, ...]:
        """Every ``DATABASE.SCHEMA`` app code may read: the sources, then app data."""
        names = [*self.sources, *([self.app_data] if self.app_data else [])]
        return tuple(dict.fromkeys(".".join(_cfg_parts(n)) for n in names))

    @property
    def enforcing(self) -> bool:
        return self.boundary == "enforce"

    def in_boundary(self, database: str, schema: str) -> bool:
        """True if normalized ``(database, schema)`` is a source or the app data."""
        return (database, schema) in {_cfg_parts(b) for b in self.boundary_schemas}

    def is_denied(self, schema: str, database: str | None = None) -> bool:
        """True if app code must never reference ``schema`` (in ``database``).

        Bare entries match the schema in every database; qualified entries only
        in their database, so with ``database`` unknown only bare entries apply.
        Case-insensitive on purpose: the deny list errs toward blocking.
        """
        s = schema.strip().replace('"', "").upper()
        db = database.strip().replace('"', "").upper() if database else None
        for entry in self.schema_deny:
            parts = _cfg_parts(entry)
            if len(parts) == 1 and parts[0] == s:
                return True
            if len(parts) == 2 and db is not None and parts == (db, s):
                return True
        return False

    def classify(self, ref: str) -> str:
        """The verdict for one relation name written as SQL (quotes honored)."""
        return self.classify_parts(split_name(ref))

    def classify_parts(self, parts: tuple[str, ...]) -> str:
        """The verdict for one relation name already split by :func:`split_name`."""
        bounded = bool(self.boundary_schemas)
        if len(parts) == 3:
            db, schema, _obj = parts
            if parts in {_cfg_parts(e) for e in self.read_exceptions}:
                return ALLOWED
            if self.is_denied(schema, db):
                return DENIED
            if self.in_boundary(db, schema):
                return ALLOWED
            if schema == _SYSTEM_SCHEMA or db in _SYSTEM_DATABASES or not bounded:
                return IGNORED
            return OUTSIDE_BOUNDARY
        if len(parts) == 2:
            schema = parts[0]
            if self.is_denied(schema):
                return DENIED
            if schema == _SYSTEM_SCHEMA or not bounded:
                return IGNORED
            return TWO_PART
        return IGNORED

    def classify_schema(self, database: str | None, schema: str) -> str:
        """The verdict for a schema reference (``USE SCHEMA DB.S`` or ``USE SCHEMA S``)."""
        if self.is_denied(schema, database):
            return DENIED
        if schema == _SYSTEM_SCHEMA or not self.boundary_schemas:
            return IGNORED
        if database is None:
            return TWO_PART
        if database in _SYSTEM_DATABASES:
            return IGNORED
        return ALLOWED if self.in_boundary(database, schema) else OUTSIDE_BOUNDARY

    def classify_database(self, database: str) -> str:
        """The verdict for ``USE DATABASE X``: allowed when a source or app data lives there."""
        if not self.boundary_schemas or database in _SYSTEM_DATABASES:
            return IGNORED
        held = {_cfg_parts(b)[0] for b in self.boundary_schemas}
        return ALLOWED if database in held else OUTSIDE_BOUNDARY
