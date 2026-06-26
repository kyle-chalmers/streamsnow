"""Load and validate ``streamsnow.config.yaml`` — the single source of truth.

Every other consumer (the validation tools, CI workflow rendering, the Copier
template defaults, ``AGENTS.md.jinja``, ``.mcp.json``, the branding generator)
reads org-specific values from here. Secrets NEVER live in this file.

Phase 1 fleshes this out into a typed, validated model. Two requirements are
load-bearing and called out now so they aren't forgotten:

1. **Typed validation.** Snowflake identifiers, roles, warehouse names, branch
   names, URLs, and secret-names must be validated against strict patterns
   before they are rendered anywhere. Reject early with a clear error.

2. **Safe rendering.** Config values flow into generated SQL, YAML, shell, and
   GitHub Actions. Each sink needs the right quoting/escaping (Snowflake
   identifier quoting, YAML string quoting, shell quoting) so a malformed or
   hostile value cannot break a deploy or inject into generated artifacts.
   Identifiers are validated to ``[A-Za-z_][A-Za-z0-9_$]*`` (or double-quoted)
   rather than interpolated raw.

See the project plan §"The config model" and the codex review (config-injection
BLOCK) for the rationale.
"""

from __future__ import annotations

from pathlib import Path

CONFIG_FILENAME = "streamsnow.config.yaml"

# Bumped when the config schema changes shape. ``streamsnow doctor`` compares
# this against the value in a generated repo's config to catch CLI/repo drift.
CONFIG_SCHEMA_VERSION = 1


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default: cwd) looking for streamsnow.config.yaml."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None = None) -> dict:
    """Load the config dict. Phase 1 will return a validated, typed model."""
    raise NotImplementedError("config loading + validation lands in Phase 1")
