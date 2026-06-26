"""Schema access policy — the single source of truth for allow/deny rules.

In the source monorepo the schema denylist was duplicated across two tools
(``check_schema_refs.py`` and ``migrate_app.py``) with the allowlist living only
in prose. StreamSnow consolidates that here: both the schema-ref check and the
migration scanner import ``SchemaPolicy``, which is constructed from the
``governance`` section of ``streamsnow.config.yaml``. One implementation, many
consumers — no drift.

Phase 1 fleshes this out. The codex review flagged that a flat
``schema_allow / schema_deny / read_exceptions`` model is too coarse for real
customers (database-level scopes, object-level exceptions, PII-passthrough
views, discovery-only allowances), so the structure below is intentionally a
starting point to be expanded into scoped rules, not the final shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SchemaPolicy:
    """Resolved governance policy. Built from config; consumed by the checks."""

    database: str
    schema_allow: tuple[str, ...] = ()
    schema_deny: tuple[str, ...] = ()
    read_exceptions: tuple[str, ...] = field(default=())

    def is_denied(self, schema: str) -> bool:
        """True if a reference to ``schema`` should be blocked in app code.

        Snowflake identifiers are case-insensitive, so comparison is upper-cased.
        Phase 1 will extend this to scoped (database.schema.object) rules and the
        sanctioned ``read_exceptions`` carve-outs.
        """
        s = schema.strip().upper()
        return s in {d.upper() for d in self.schema_deny}
