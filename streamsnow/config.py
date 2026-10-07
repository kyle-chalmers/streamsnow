"""Load and validate ``streamsnow.config.yaml`` — the single source of truth.

Every other consumer (the validation tools, CI/workflow rendering, the scaffold
templates, ``AGENTS.md``, the branding generator) reads
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
CONFIG_SCHEMA_VERSION = 2

RUNTIMES = ("container", "warehouse")
BOUNDARY_MODES = ("warn", "enforce")
#: The app-data schema's name when ``governance.app_data`` is unset. It lives in
#: ``snowflake.objects.app_database``, so leaving StreamSnow drops it with the apps.
DEFAULT_APP_DATA_SCHEMA = "STREAMSNOW_REPORTING"
#: schema_version 1 governance keys: their presence is the v1 error, never a fallback.
RETIRED_GOVERNANCE_KEYS = ("database", "schema_allow")
#: Databases Snowflake shares into every account: reads need IMPORTED PRIVILEGES.
SHARED_DATABASES = ("SNOWFLAKE_SAMPLE_DATA", "SNOWFLAKE")
V1_CONFIG_ERROR = (
    "this streamsnow.config.yaml uses schema_version 1 (governance.database + "
    "governance.schema_allow), which this StreamSnow no longer reads: governance.sources "
    "(a list of DATABASE.SCHEMA entries, in any databases) and governance.app_data replace "
    "them. Run `streamsnow configure` to rewrite the file as schema_version 2; it keeps your "
    "other values and proposes sources from the old ones."
)
DEPLOY_SOURCES = ("stage-copy", "git-repository")
# pat / github-app: a token stored in deploy.secret_name; public: a public
# GitHub repo, no token and no secret.
GITHUB_AUTH_MODES = ("pat", "github-app", "public")
# The HTTPS clone URL of a GitHub repository. It is rendered into the GIT
# REPOSITORY's ORIGIN and the API integration's allowed prefix, so it is
# validated strictly: no credentials, query string or quotes.
_GITHUB_ORIGIN_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/[A-Za-z0-9._-]+?(?:\.git)?$"
)

# Snowflake unquoted identifier: starts with letter/underscore, then
# letters/digits/underscore/dollar. Case-insensitive in Snowflake.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
# A dotted FQN like DB.SCHEMA.NAME (each part a valid identifier).
_FQN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)*$")
# Exactly DATABASE.SCHEMA: two identifiers, one dot.
_SCHEMA_REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$")
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


def validate_schema_ref(value: object, field_name: str) -> str:
    """Return ``value`` upper-cased if it is exactly ``DATABASE.SCHEMA``, else raise.

    Two unquoted identifiers only: Snowflake stores unquoted names upper-case,
    which is what every comparison against a quoted name in SQL assumes.
    """
    text = str(value).strip() if value is not None else ""
    if not _SCHEMA_REF_RE.match(text):
        raise ConfigError(
            f"{field_name!r} = {value!r} must be DATABASE.SCHEMA (two Snowflake identifiers "
            "joined by a dot, e.g. ANALYTICS_DB.REPORTING)."
        )
    return text.upper()


def validate_deny_entry(value: object, field_name: str) -> str:
    """A deny entry: a schema name (that schema in every database) or DATABASE.SCHEMA."""
    text = str(value) if value is not None else ""
    if _IDENT_RE.match(text) or _SCHEMA_REF_RE.match(text):
        return text
    raise ConfigError(
        f"{field_name!r} = {value!r} must be a schema name (RAW: that schema in every "
        "database) or DATABASE.SCHEMA (FINANCE.RAW: one database only)."
    )


def governance_overlaps(sources, deny) -> list[str]:
    """Each ``DATABASE.SCHEMA`` in *sources* that an entry of *deny* also blocks.

    One rule for the loader and the wizard's early flag check: a bare entry
    blocks the schema in every database, a qualified one only its own.
    """
    bare = {str(e).upper() for e in deny if "." not in str(e)}
    qualified = {str(e).upper() for e in deny if "." in str(e)}
    out = []
    for fq in sources:
        up = str(fq).strip().upper()
        if up in qualified or up.split(".", 1)[-1] in bare:
            out.append(up)
    return out


def _str_list(d: dict, key: str) -> list[str]:
    value = d.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"governance.{key} must be a list.")
    return [str(v) for v in value]


def _reject_v1(d: dict) -> None:
    """A schema_version 1 file, or v2 with v1 keys, fails with the D5 message."""
    gov = d.get("governance") if isinstance(d.get("governance"), dict) else {}
    retired = [f"governance.{k}" for k in RETIRED_GOVERNANCE_KEYS if k in gov]
    version = d.get("schema_version")
    old = version is not None and int(version) < CONFIG_SCHEMA_VERSION
    if old or retired:
        found = f" (found {', '.join(retired)})" if retired else ""
        raise ConfigError(V1_CONFIG_ERROR + found)


def validate_branch(value: str, field_name: str) -> str:
    if not isinstance(value, str) or ".." in value or not _BRANCH_RE.match(value):
        raise ConfigError(f"{field_name!r} = {value!r} is not a valid git branch name.")
    return value


def validate_github_origin(value: str, field_name: str) -> str:
    """Return ``value`` if it is a plain ``https://github.com/<owner>/<repo>`` URL."""
    if (
        not isinstance(value, str)
        or not _GITHUB_ORIGIN_RE.fullmatch(value)
        or value.removesuffix(".git").rsplit("/", 1)[-1] in ("", ".", "..")
    ):
        raise ConfigError(
            f"{field_name!r} = {value!r} is not a GitHub HTTPS URL "
            "(expected https://github.com/<owner>/<repo>.git, no credentials)."
        )
    return value


def github_owner(origin: str) -> str:
    """``https://github.com/acme/apps.git`` -> ``acme``."""
    m = _GITHUB_ORIGIN_RE.fullmatch(origin)
    if not m:
        raise ConfigError(f"{origin!r} is not a GitHub HTTPS URL")
    return m.group(1)


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


def _mapping(d: dict, key: str) -> dict:
    """An optional top-level block that must be a mapping when present.

    ``sql_review: warn`` (a scalar where a block was meant) used to reach
    ``dict("warn")`` and surface as a bare ``ValueError`` traceback from
    ``validate-app``, which only catches ``ConfigError``.
    """
    value = d.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            f"{key} must be a mapping (e.g. `{key}:` followed by indented keys), got {value!r}."
        )
    return dict(value)


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
    stage_name: str = "STREAMSNOW_CODE_STAGE"
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
                str(d.get("stage_name", "STREAMSNOW_CODE_STAGE")), "snowflake.objects.stage_name"
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
    """The data boundary: what app code may read, and where app-built objects live.

    ``sources`` are existing report-ready ``DATABASE.SCHEMA`` locations in any
    databases (#78): deploy-setup grants the CI role read there and nowhere
    else, and ``check schema-refs`` treats them as the boundary. ``app_data`` is
    the one schema per repo for views and dynamic tables built for the apps.
    ``schema_deny`` entries are bare (that schema in every database) or
    ``DATABASE.SCHEMA``. ``imported_databases`` are shares that hold sources
    (they take IMPORTED PRIVILEGES); Snowflake's own shares always count.
    ``boundary`` decides whether reads outside sources and app data warn or fail.
    """

    sources: tuple[str, ...]
    app_data: str
    schema_deny: tuple[str, ...] = ()
    read_exceptions: tuple[str, ...] = ()
    imported_databases: tuple[str, ...] = ()
    boundary: str = "warn"

    @classmethod
    def from_dict(cls, d: dict, app_database: str) -> GovernanceCfg:
        raw = d.get("sources")
        if not isinstance(raw, list) or not raw:
            raise ConfigError(
                "governance.sources must list at least one DATABASE.SCHEMA your apps read, e.g. "
                '["ANALYTICS_DB.REPORTING"]: deploy-setup --admin grants read on exactly these, '
                "and schema-refs checks app SQL against them."
            )
        gov = cls(
            sources=tuple(
                dict.fromkeys(validate_schema_ref(s, "governance.sources[]") for s in raw)
            ),
            app_data=validate_schema_ref(
                d.get("app_data") or f"{app_database}.{DEFAULT_APP_DATA_SCHEMA}",
                "governance.app_data",
            ),
            schema_deny=tuple(
                validate_deny_entry(s, "governance.schema_deny[]")
                for s in _str_list(d, "schema_deny")
            ),
            # read_exceptions are FQNs (DB.SCHEMA.OBJECT) for sanctioned direct reads.
            read_exceptions=tuple(
                validate_fqn(s, "governance.read_exceptions[]")
                for s in _str_list(d, "read_exceptions")
            ),
            imported_databases=tuple(
                dict.fromkeys(
                    validate_identifier(s, "governance.imported_databases[]").upper()
                    for s in _str_list(d, "imported_databases")
                )
            ),
            boundary=validate_choice(
                str(d.get("boundary", "warn")), BOUNDARY_MODES, "governance.boundary"
            ),
        )
        gov._check_consistency()
        return gov

    def _check_consistency(self) -> None:
        both = governance_overlaps(self.sources, self.schema_deny)
        if both:
            raise ConfigError(
                f"schema(s) {', '.join(both)} are both allowed and denied: listed in "
                "governance.sources and blocked by governance.schema_deny. Drop them from one "
                "of the two (--sources or --deny-schemas)."
            )
        if self.app_data in self.sources:
            raise ConfigError(
                f"governance.app_data {self.app_data} is also a source. App data is a separate "
                "schema the deploy job writes views and dynamic tables into; choose another "
                f"(default: <app_database>.{DEFAULT_APP_DATA_SCHEMA})."
            )
        if governance_overlaps((self.app_data,), self.schema_deny):
            raise ConfigError(
                f"governance.app_data {self.app_data} is denied by governance.schema_deny; "
                "the apps could never read what the deploy job builds there."
            )
        db = self.app_data.split(".", 1)[0]
        if db in {*SHARED_DATABASES, *self.imported_databases}:
            raise ConfigError(
                f"governance.app_data {self.app_data} is in shared database {db}, which is "
                "read-only; choose a schema in a database your account owns."
            )


# Files an app may deliberately leave out of ``snowflake.yml`` ``artifacts:``
# because the repo's deploy pipeline ships them another way (the generated
# stage-copy workflow uploads ``.streamlit/config.toml`` in its own loop, and a
# fleet with a hand-rolled deploy did the same). The allowlist is deliberately
# narrow: exact relative paths, no globs, never code — an exclusion that could
# name ``streamlit_app.py`` or a query would turn the artifacts gate into an
# opt-out, which is the escape the adversarial review of 0.7 rejected.
_ARTIFACT_EXCLUDE_SUFFIXES = (".toml", ".md", ".txt", ".png", ".svg", ".jpg", ".jpeg", ".ico")
_ARTIFACT_EXCLUDE_FORBIDDEN_DIRS = ("pages", "queries")
SQL_REVIEW_COVERAGE_POLICIES = ("warn", "fail")


def validate_artifact_exclude(value: str, field_name: str) -> str:
    """Return ``value`` if it is an exact, safe, non-code app-relative path."""
    raw = str(value).strip()
    if not raw or raw != value:
        raise ConfigError(f"{field_name!r} = {value!r} must be a bare relative path.")
    if "\\" in raw or raw.startswith("/") or raw.startswith("~"):
        raise ConfigError(f"{field_name!r} = {raw!r} must be a forward-slash relative path.")
    parts = raw.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ConfigError(f"{field_name!r} = {raw!r} may not contain '.', '..' or empty parts.")
    if any(c in raw for c in "*?["):
        raise ConfigError(f"{field_name!r} = {raw!r} may not contain glob characters.")
    if parts[0] in _ARTIFACT_EXCLUDE_FORBIDDEN_DIRS:
        raise ConfigError(
            f"{field_name!r} = {raw!r} is under {parts[0]}/ — page and query files are "
            "always artifacts."
        )
    if raw == "streamlit_app.py" or not raw.lower().endswith(_ARTIFACT_EXCLUDE_SUFFIXES):
        raise ConfigError(
            f"{field_name!r} = {raw!r} is not an excludable file — only non-code files "
            f"({', '.join(_ARTIFACT_EXCLUDE_SUFFIXES)}) shipped by another deploy step may be "
            "left out of artifacts:."
        )
    return raw


@dataclass(frozen=True)
class SqlReviewCfg:
    """The ``sql_review:`` block. ``coverage`` decides whether a page or
    ``queries/*.sql`` file that ``sql_review/index.yaml`` does not account for
    fails the gate (``fail``) or is reported as a warning (``warn``, the default
    so an adopting fleet can backfill). Drift, hand edits, marker mismatches,
    lint and write statements are correctness failures and are never
    downgraded by this policy."""

    coverage: str = "warn"

    @classmethod
    def from_dict(cls, d: dict) -> SqlReviewCfg:
        return cls(
            coverage=validate_choice(
                str(d.get("coverage", "warn")), SQL_REVIEW_COVERAGE_POLICIES, "sql_review.coverage"
            )
        )


@dataclass(frozen=True)
class DeployCfg:
    source: str = "stage-copy"
    git_repository_fqn: str = ""
    git_origin: str = ""
    git_branch: str = "main"
    api_integration_name: str = ""
    secret_name: str = ""
    github_auth_mode: str = "pat"
    artifact_exclude: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> DeployCfg:
        source = validate_choice(
            str(d.get("source", "stage-copy")), DEPLOY_SOURCES, "deploy.source"
        )
        raw_exclude = d.get("artifact_exclude") or []
        if not isinstance(raw_exclude, list):
            raise ConfigError("deploy.artifact_exclude must be a list of app-relative paths.")
        artifact_exclude = tuple(
            validate_artifact_exclude(str(s), "deploy.artifact_exclude[]") for s in raw_exclude
        )
        if source == "git-repository":
            auth = validate_choice(
                str(d.get("github_auth_mode", "pat")),
                GITHUB_AUTH_MODES,
                "deploy.github_auth_mode",
            )
            # The origin is required only where it is rendered (deploy-setup),
            # so a pre-0.8 git config without it still loads.
            origin = d.get("git_origin") or ""
            return cls(
                source=source,
                artifact_exclude=artifact_exclude,
                git_repository_fqn=validate_fqn(
                    str(_require(d, "git_repository_fqn", "deploy")), "deploy.git_repository_fqn"
                ),
                git_origin=validate_github_origin(str(origin), "deploy.git_origin")
                if origin
                else "",
                git_branch=validate_branch(str(d.get("git_branch", "main")), "deploy.git_branch"),
                api_integration_name=validate_identifier(
                    str(_require(d, "api_integration_name", "deploy")),
                    "deploy.api_integration_name",
                ),
                # A public repo needs no token, so no secret.
                secret_name=""
                if auth == "public"
                else validate_fqn(str(_require(d, "secret_name", "deploy")), "deploy.secret_name"),
                github_auth_mode=auth,
            )
        return cls(source=source, artifact_exclude=artifact_exclude)


@dataclass(frozen=True)
class Config:
    schema_version: int
    project: ProjectCfg
    snowflake: SnowflakeCfg
    governance: GovernanceCfg
    deploy: DeployCfg
    runtime: str = "container"
    sql_review: SqlReviewCfg = field(default_factory=SqlReviewCfg)
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, d: dict) -> Config:
        if not isinstance(d, dict):
            raise ConfigError("config root must be a mapping")
        _reject_v1(d)
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
            governance=GovernanceCfg.from_dict(
                dict(_require(d, "governance", "<root>")), snowflake.objects.app_database
            ),
            deploy=DeployCfg.from_dict(_mapping(d, "deploy")),
            runtime=runtime,
            sql_review=SqlReviewCfg.from_dict(_mapping(d, "sql_review")),
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
        data = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        # Problem and position only: PyYAML's full message quotes a snippet of
        # the file, and --config can point at a file that holds a secret.
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or type(exc).__name__
        raise ConfigError(f"{cfg_path}: invalid YAML{where}: {problem}") from exc
    except OSError as exc:
        # An explicit path that doesn't exist must be the same friendly error
        # as no discovered config — not a raw traceback (seen live from
        # `streamsnow update` outside a configured repo).
        raise ConfigError(f"{cfg_path}: cannot read config: {exc}") from exc
    return Config.from_dict(data)
