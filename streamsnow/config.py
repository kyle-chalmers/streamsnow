"""Load and validate ``streamsnow.config.yaml`` — the single source of truth.

Every other consumer (the validation tools, CI/workflow rendering, the scaffold
templates, ``AGENTS.md``, ``.mcp.json``, the branding generator) reads
org-specific values from here. **Secrets never live in this file.**

Two requirements are load-bearing (flagged by the cross-agent review):

1. **Typed validation.** Snowflake identifiers, roles, warehouses, branch names
   and choices are validated against strict patterns up front, so a malformed
   value fails fast with a clear message instead of corrupting a generated
   artifact downstream.

2. **Safe rendering.** Config values flow into generated SQL, YAML, TOML, and
   shell. Every value is validated to a safe charset up front — identifiers via
   ``validate_identifier`` / ``validate_fqn``, names/accounts via
   ``validate_name`` / ``normalize_account``, versions via ``validate_pyver`` —
   so a hostile value (quotes, semicolons, shell metacharacters, newlines) is
   rejected before it can reach a template. ``quote_ident`` / ``quote_sql_literal``
   are available for defensive quoting where a value must be embedded dynamically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILENAME = "streamsnow.config.yaml"

# Bumped when the config schema changes shape. ``streamsnow doctor`` compares
# this against a generated repo's config to catch CLI/repo drift.
CONFIG_SCHEMA_VERSION = 1

RUNTIMES = ("container", "warehouse")
DEPLOY_SOURCES = ("stage-copy", "git-repository")
GITHUB_AUTH_MODES = ("pat", "github-app")

# Snowflake unquoted identifier: starts with letter/underscore, then
# letters/digits/underscore/dollar. Case-insensitive in Snowflake.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
# A dotted FQN like DB.SCHEMA.NAME (each part a valid identifier).
_FQN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$")
# Git branch: conservative safe subset (no spaces, shell metachars, or '..').
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class ConfigError(ValueError):
    """Raised when ``streamsnow.config.yaml`` is missing required values or
    contains an invalid identifier/choice. Message is user-facing."""


# --------------------------------------------------------------------------- #
# Validation + safe-rendering helpers (importable by tools and the scaffolder)
# --------------------------------------------------------------------------- #
def validate_identifier(value: str, field_name: str) -> str:
    """Return ``value`` if it is a safe Snowflake identifier, else raise."""
    if not isinstance(value, str) or not _IDENT_RE.match(value):
        raise ConfigError(
            f"{field_name!r} = {value!r} is not a valid Snowflake identifier "
            r"(must match [A-Za-z_][A-Za-z0-9_$]*). This value is rendered into "
            "generated SQL/YAML, so it is validated strictly."
        )
    return value


def validate_fqn(value: str, field_name: str) -> str:
    """Return ``value`` if it is a safe dotted identifier (DB.SCHEMA.NAME)."""
    if not isinstance(value, str) or not _FQN_RE.match(value):
        raise ConfigError(
            f"{field_name!r} = {value!r} is not a valid Snowflake object name "
            "(expected DB.SCHEMA.OBJECT, each part a valid identifier)."
        )
    return value


def validate_branch(value: str, field_name: str) -> str:
    if not isinstance(value, str) or ".." in value or not _BRANCH_RE.match(value):
        raise ConfigError(f"{field_name!r} = {value!r} is not a valid git branch name.")
    return value


def validate_choice(value: str, choices: tuple[str, ...], field_name: str) -> str:
    if value not in choices:
        raise ConfigError(f"{field_name!r} = {value!r} must be one of {choices}.")
    return value


def validate_name(value: str, field_name: str) -> str:
    """Validate a CLI/connection-style name (letters, digits, dot, dash, underscore).

    Used for values that flow into shell (the `snow connection add` hint) and
    config files but aren't Snowflake identifiers.
    """
    if not isinstance(value, str) or not re.match(r"^[A-Za-z0-9._-]+$", value):
        raise ConfigError(f"{field_name!r} = {value!r} must match [A-Za-z0-9._-]+.")
    return value


def validate_pyver(value: str, field_name: str) -> str:
    """Validate a Python version like '3.11'."""
    if not isinstance(value, str) or not re.match(r"^3\.\d{1,2}$", value):
        raise ConfigError(f"{field_name!r} = {value!r} must look like '3.11'.")
    return value


def quote_ident(name: str) -> str:
    """Render a Snowflake identifier safely. Inputs are already validated to the
    safe charset, so this is normally a no-op; quotes defensively otherwise."""
    if _IDENT_RE.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def quote_sql_literal(value: str) -> str:
    """Render a SQL string literal (single-quoted, doubled internal quotes)."""
    return "'" + str(value).replace("'", "''") + "'"


def normalize_account(account: str) -> str:
    """Return the Snowflake account *locator*, never the hostname.

    The connector appends ``.snowflakecomputing.com`` itself; passing the full
    hostname double-suffixes and 404s on auth. Strip scheme + that suffix.
    """
    a = account.strip().rstrip("/")
    a = re.sub(r"^https?://", "", a)
    a = re.sub(r"\.snowflakecomputing\.com.*$", "", a, flags=re.IGNORECASE)
    if not a:
        raise ConfigError("snowflake.account is empty after normalization.")
    # Account locators are letters/digits/dot/dash/underscore (org-account or
    # legacy region forms). Reject anything else — this value flows into the
    # `snow connection add --account` shell hint and secrets.toml.
    if not re.match(r"^[A-Za-z0-9._-]+$", a):
        raise ConfigError(
            f"snowflake.account {account!r} normalizes to {a!r}, which is not a "
            "valid account locator (expected [A-Za-z0-9._-]+, e.g. ab12345.us-east-1)."
        )
    return a


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"missing required config value: {ctx}.{key}")
    return d[key]


# --------------------------------------------------------------------------- #
# Typed model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProjectCfg:
    name: str
    slug: str
    agents_md_char_limit: int = 40000

    @classmethod
    def from_dict(cls, d: dict) -> ProjectCfg:
        name = str(_require(d, "name", "project"))
        slug = validate_branch(str(_require(d, "slug", "project")), "project.slug")
        return cls(
            name=name, slug=slug, agents_md_char_limit=int(d.get("agents_md_char_limit", 40000))
        )


@dataclass(frozen=True)
class SnowflakeObjects:
    stage_database: str
    stage_schema: str
    app_database: str
    app_schema: str
    default_warehouse: str
    allowed_warehouses: tuple[str, ...]
    stage_name: str = "STREAMLIT_CODE_STAGE"
    compute_pool: str = ""
    external_access_integration: str = ""
    runtime_name: str = "SYSTEM$ST_CONTAINER_RUNTIME_PY3_11"
    container_python: str = "3.11"

    @classmethod
    def from_dict(cls, d: dict) -> SnowflakeObjects:
        vi = validate_identifier
        return cls(
            stage_database=vi(
                str(_require(d, "stage_database", "snowflake.objects")),
                "snowflake.objects.stage_database",
            ),
            stage_schema=vi(
                str(_require(d, "stage_schema", "snowflake.objects")),
                "snowflake.objects.stage_schema",
            ),
            stage_name=vi(
                str(d.get("stage_name", "STREAMLIT_CODE_STAGE")), "snowflake.objects.stage_name"
            ),
            app_database=vi(
                str(_require(d, "app_database", "snowflake.objects")),
                "snowflake.objects.app_database",
            ),
            app_schema=vi(
                str(_require(d, "app_schema", "snowflake.objects")), "snowflake.objects.app_schema"
            ),
            default_warehouse=vi(
                str(_require(d, "default_warehouse", "snowflake.objects")),
                "snowflake.objects.default_warehouse",
            ),
            allowed_warehouses=tuple(
                vi(str(w), "snowflake.objects.allowed_warehouses[]")
                for w in (d.get("allowed_warehouses") or [d.get("default_warehouse")])
            ),
            compute_pool=vi(str(d["compute_pool"]), "snowflake.objects.compute_pool")
            if d.get("compute_pool")
            else "",
            external_access_integration=(
                vi(
                    str(d["external_access_integration"]),
                    "snowflake.objects.external_access_integration",
                )
                if d.get("external_access_integration")
                else ""
            ),
            runtime_name=validate_identifier(
                str(d.get("runtime_name", "SYSTEM$ST_CONTAINER_RUNTIME_PY3_11")),
                "snowflake.objects.runtime_name",
            ),
            container_python=validate_pyver(
                str(d.get("container_python", "3.11")), "snowflake.objects.container_python"
            ),
        )


@dataclass(frozen=True)
class SnowflakeRoles:
    ci_role: str
    viewer_role: str

    @classmethod
    def from_dict(cls, d: dict) -> SnowflakeRoles:
        return cls(
            ci_role=validate_identifier(
                str(_require(d, "ci_role", "snowflake.roles")), "snowflake.roles.ci_role"
            ),
            viewer_role=validate_identifier(
                str(_require(d, "viewer_role", "snowflake.roles")), "snowflake.roles.viewer_role"
            ),
        )


@dataclass(frozen=True)
class SnowflakeCfg:
    account: str
    connection_name: str
    objects: SnowflakeObjects
    roles: SnowflakeRoles

    @classmethod
    def from_dict(cls, d: dict) -> SnowflakeCfg:
        return cls(
            account=normalize_account(str(_require(d, "account", "snowflake"))),
            connection_name=validate_name(
                str(_require(d, "connection_name", "snowflake")), "snowflake.connection_name"
            ),
            objects=SnowflakeObjects.from_dict(dict(_require(d, "objects", "snowflake"))),
            roles=SnowflakeRoles.from_dict(dict(_require(d, "roles", "snowflake"))),
        )


@dataclass(frozen=True)
class GovernanceCfg:
    database: str
    schema_allow: tuple[str, ...]
    schema_deny: tuple[str, ...]
    read_exceptions: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> GovernanceCfg:
        vi = validate_identifier
        schema_allow = tuple(
            vi(str(s), "governance.schema_allow[]") for s in (d.get("schema_allow") or [])
        )
        if not schema_allow:
            raise ConfigError(
                "governance.schema_allow must list at least one allowed schema "
                "(it is what your apps query and what the scaffold templates target)."
            )
        return cls(
            database=vi(str(_require(d, "database", "governance")), "governance.database"),
            schema_allow=schema_allow,
            schema_deny=tuple(
                vi(str(s), "governance.schema_deny[]") for s in (d.get("schema_deny") or [])
            ),
            # read_exceptions are FQNs (DB.SCHEMA.OBJECT) for sanctioned direct reads.
            read_exceptions=tuple(
                validate_fqn(str(s), "governance.read_exceptions[]")
                for s in (d.get("read_exceptions") or [])
            ),
        )


@dataclass(frozen=True)
class DeployCfg:
    source: str = "stage-copy"
    git_repository_fqn: str = ""
    git_branch: str = "main"
    api_integration_name: str = ""
    secret_name: str = ""
    github_auth_mode: str = "pat"

    @classmethod
    def from_dict(cls, d: dict) -> DeployCfg:
        source = validate_choice(
            str(d.get("source", "stage-copy")), DEPLOY_SOURCES, "deploy.source"
        )
        if source == "git-repository":
            return cls(
                source=source,
                git_repository_fqn=validate_fqn(
                    str(_require(d, "git_repository_fqn", "deploy")), "deploy.git_repository_fqn"
                ),
                git_branch=validate_branch(str(d.get("git_branch", "main")), "deploy.git_branch"),
                api_integration_name=validate_identifier(
                    str(_require(d, "api_integration_name", "deploy")),
                    "deploy.api_integration_name",
                ),
                secret_name=validate_fqn(
                    str(_require(d, "secret_name", "deploy")), "deploy.secret_name"
                ),
                github_auth_mode=validate_choice(
                    str(d.get("github_auth_mode", "pat")),
                    GITHUB_AUTH_MODES,
                    "deploy.github_auth_mode",
                ),
            )
        return cls(source=source)


@dataclass(frozen=True)
class Config:
    schema_version: int
    project: ProjectCfg
    snowflake: SnowflakeCfg
    governance: GovernanceCfg
    deploy: DeployCfg
    runtime: str = "container"
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: dict) -> Config:
        if not isinstance(d, dict):
            raise ConfigError("config root must be a mapping")
        schema_version = int(d.get("schema_version", CONFIG_SCHEMA_VERSION))
        if schema_version > CONFIG_SCHEMA_VERSION:
            raise ConfigError(
                f"config schema_version {schema_version} is newer than this "
                f"streamsnow ({CONFIG_SCHEMA_VERSION}); upgrade streamsnow."
            )
        runtime = validate_choice(str(d.get("runtime", "container")), RUNTIMES, "runtime")
        snowflake = SnowflakeCfg.from_dict(dict(_require(d, "snowflake", "<root>")))
        if runtime == "container" and not (
            snowflake.objects.compute_pool and snowflake.objects.external_access_integration
        ):
            raise ConfigError(
                "runtime 'container' requires snowflake.objects.compute_pool and "
                "snowflake.objects.external_access_integration to be set."
            )
        return cls(
            schema_version=schema_version,
            project=ProjectCfg.from_dict(dict(_require(d, "project", "<root>"))),
            snowflake=snowflake,
            governance=GovernanceCfg.from_dict(dict(_require(d, "governance", "<root>"))),
            deploy=DeployCfg.from_dict(dict(d.get("deploy", {}))),
            runtime=runtime,
            raw=d,
        )


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default: cwd) looking for streamsnow.config.yaml."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None = None) -> Config:
    """Load + validate the config. Raises ConfigError on any problem."""
    cfg_path = path or find_config()
    if cfg_path is None:
        raise ConfigError(
            f"no {CONFIG_FILENAME} found (searched cwd and parents). Run "
            "'streamsnow init' to create one."
        )
    try:
        data = yaml.safe_load(Path(cfg_path).read_text()) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - passthrough
        raise ConfigError(f"{cfg_path}: invalid YAML: {exc}") from exc
    return Config.from_dict(data)
