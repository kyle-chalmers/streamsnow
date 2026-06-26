"""Schema access policy — the single source of truth for allow/deny rules.

In the source monorepo the schema denylist was duplicated across two tools
(``check_schema_refs.py`` and ``migrate_app.py``) with the allowlist only in
prose. StreamSnow consolidates it here: both the schema-ref check and the
migration scanner import ``SchemaPolicy``, built from the ``governance`` section
of ``streamsnow.config.yaml``. One implementation, many consumers — no drift.

The flat allow/deny/read-exception model is a starting point; the cross-agent
review flagged that real customers need scoped rules (db scopes, object-level
exceptions, PII-passthrough). The structure is intentionally easy to extend.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import GovernanceCfg


@dataclass(frozen=True)
class SchemaPolicy:
    """Resolved governance policy. Built from config; consumed by the checks."""

    database: str
    schema_allow: tuple[str, ...] = ()
    schema_deny: tuple[str, ...] = ()
    read_exceptions: tuple[str, ...] = ()

    @classmethod
    def from_governance(cls, gov: GovernanceCfg) -> SchemaPolicy:
        return cls(
            database=gov.database,
            schema_allow=gov.schema_allow,
            schema_deny=gov.schema_deny,
            read_exceptions=gov.read_exceptions,
        )

    def is_denied(self, schema: str) -> bool:
        """True if a reference to ``schema`` should be blocked in app code.

        Snowflake identifiers are case-insensitive, so comparison is upper-cased.
        """
        return schema.strip().upper() in {d.upper() for d in self.schema_deny}

    def is_allowed(self, schema: str) -> bool:
        """True if ``schema`` is on the explicit allowlist (case-insensitive)."""
        return schema.strip().upper() in {a.upper() for a in self.schema_allow}
