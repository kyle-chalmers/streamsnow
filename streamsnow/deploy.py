"""Generate Snowflake deploy SQL from config — the deploy-source strategy seam.

Both deploy sources run the same idempotent ``CREATE OR REPLACE STREAMLIT``
and share its *tail* (the container ALTER, the ``ADD LIVE VERSION FROM LAST``
defense, the ``GRANT USAGE`` to the viewer role); only the ``FROM`` clause and
the CI pre-step differ:

- **stage-copy** (default): CI uploads app source to a SHA-versioned internal
  stage and deploys ``FROM '@stage/commits/<sha>/apps/<slug>/'``.
- **git-repository**: Snowflake's GIT REPOSITORY object mirrors the GitHub
  repo; CI runs ``snow git fetch`` and deploys
  ``FROM '@<repo>/branches/<branch>/apps/<slug>/'``. CREATE copies the files
  once, so a later push changes nothing until the next deploy. Snowflake
  rejects a ``/commits/<sha>/`` path here ("Invalid git branch path", observed
  2026-10-03), so freshness is proven afterwards: ``DESCRIBE STREAMLIT``
  reports ``last_version_git_commit_hash``, which verify-deploy compares with
  the CI commit.

All identifiers come from a validated :class:`~streamsnow.config.Config`, so
they are rendered into SQL directly (the config layer is the injection gate).
This module is pure (no DB calls); the CLI / generated deploy workflow runs it.
"""

import base64
import binascii
import dataclasses
import re
from collections.abc import Sequence
from pathlib import Path

from .app_data import SQL_KIND, is_plain_fqn, one_line
from .config import (
    DEPLOY_SOURCES,
    GITHUB_AUTH_MODES,
    SHARED_DATABASES,
    Config,
    ConfigError,
    github_owner,
    validate_choice,
    validate_github_origin,
    validate_identifier,
)

# slug + sha reach SQL/object names — validate at the boundary (defense in depth
# alongside the config-layer gate), so a hostile value can't inject.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{7,64}|<sha>)$")


def _safe_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid app slug {slug!r} (expected kebab-case [a-z][a-z0-9-]*)")
    return slug


def _safe_sha(sha: str) -> str:
    if not _SHA_RE.match(sha):
        raise ValueError(f"invalid commit sha {sha!r} (expected 7-64 hex chars)")
    return sha


def _title(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-"))


def streamlit_fqn(cfg: Config, slug: str) -> str:
    o = cfg.snowflake.objects
    return f"{o.app_database}.{o.app_schema}.{_safe_slug(slug).replace('-', '_').upper()}"


def stage_path(cfg: Config) -> str:
    """The internal stage base path (``@DB.SCHEMA.STAGE``) for stage-copy deploys."""
    o = cfg.snowflake.objects
    return f"@{o.stage_database}.{o.stage_schema}.{o.stage_name}"


def _from_clause(cfg: Config, slug: str, sha: str) -> str:
    o = cfg.snowflake.objects
    slug = _safe_slug(slug)
    if cfg.deploy.source == "stage-copy":
        _safe_sha(sha)
        stage = f"{o.stage_database}.{o.stage_schema}.{o.stage_name}"
        return f"FROM '@{stage}/commits/{sha}/apps/{slug}/'"
    repo = cfg.deploy.git_repository_fqn
    branch = cfg.deploy.git_branch
    return f"FROM '@{repo}/branches/{branch}/apps/{slug}/'"


def generate_create_sql(cfg: Config, slug: str, sha: str = "<sha>") -> str:
    """The create/replace statement + container ALTER + live-version + grant."""
    o = cfg.snowflake.objects
    fqn = streamlit_fqn(cfg, slug)
    lines = [
        f"CREATE OR REPLACE STREAMLIT {fqn}",
        f"  {_from_clause(cfg, slug, sha)}",
        "  MAIN_FILE = 'streamlit_app.py'",
        f"  QUERY_WAREHOUSE = {o.default_warehouse}",
        f"  TITLE = '{_title(slug)}';",
    ]
    if cfg.runtime == "container":
        lines.append(
            f"ALTER STREAMLIT {fqn} SET\n"
            f"  RUNTIME_NAME = '{o.runtime_name}'\n"
            f"  COMPUTE_POOL = {o.compute_pool}\n"
            f"  EXTERNAL_ACCESS_INTEGRATIONS = ({o.external_access_integration});"
        )
    # ADD LIVE VERSION FROM LAST is required: CREATE/COMMIT alone leaves
    # live_version_location_uri NULL and the app fails to render.
    lines.append(f"ALTER STREAMLIT {fqn} ADD LIVE VERSION FROM LAST;")
    lines.append(f"GRANT USAGE ON STREAMLIT {fqn} TO ROLE {cfg.snowflake.roles.viewer_role};")
    return "\n".join(lines)


# Snowflake's pre-provisioned CPU pool for Streamlit container apps. It exists
# in every account (owned by ACCOUNTADMIN, USAGE granted to PUBLIC by default),
# so setup SQL must never try to create it.
SYSTEM_POOL = "SYSTEM_COMPUTE_POOL_CPU"
_SHARED_DATABASES = SHARED_DATABASES  # moved to config: the loader refuses app data there

# Object types an app can read, as GRANT's plural keywords. Snowflake keeps a separate
# grant per type: "Grants on TABLE don't apply to dynamic tables", so a dynamic table in
# an allowed schema was unreadable by the CI role that owns the deployed app, and the
# dashboard came up empty while local preview (a broader personal role) worked (#80).
# Hybrid tables are covered by TABLES. Left out on purpose: event tables (telemetry,
# not report data), streams (change-data plumbing), functions and procedures (execution
# rights, and a procedure can write). Adding a type here is a deliberate change; a test
# pins this tuple. Docs: https://docs.snowflake.com/en/sql-reference/sql/grant-privilege
# and https://docs.snowflake.com/en/user-guide/dynamic-tables/privileges
READ_OBJECT_TYPES = (
    "TABLES",
    "VIEWS",
    "DYNAMIC TABLES",
    "MATERIALIZED VIEWS",
    "SEMANTIC VIEWS",
    "ICEBERG TABLES",
    "EXTERNAL TABLES",
)


def imported_databases(cfg: Config) -> frozenset[str]:
    """Databases that take IMPORTED PRIVILEGES instead of per-schema grants: the
    configured ``governance.imported_databases`` plus Snowflake's own shares. A
    share rejects USAGE + SELECT grants, so the admin script would fail there."""
    return frozenset({*_SHARED_DATABASES, *(d.upper() for d in cfg.governance.imported_databases)})


def source_databases(cfg: Config) -> dict[str, list[str]]:
    """``governance.sources`` grouped by database, in config order.

    One ``GRANT USAGE ON DATABASE`` per database however many of its schemas
    are sources: Snowflake accepts the repeat, but the admin reading the file
    should see each grant once.
    """
    grouped: dict[str, list[str]] = {}
    for fq in cfg.governance.sources:
        grouped.setdefault(fq.split(".", 1)[0].upper(), []).append(fq.upper())
    return grouped


def _stage_objects(cfg: Config) -> list[str]:
    o = cfg.snowflake.objects
    return [
        f"CREATE STAGE IF NOT EXISTS {o.stage_database}.{o.stage_schema}.{o.stage_name}",
        "  COMMENT = 'StreamSnow app source, SHA-versioned per deploy';",
    ]


def with_source(
    cfg: Config,
    source: str,
    *,
    git_origin: str | None = None,
    github_auth: str | None = None,
) -> Config:
    """``cfg`` with its deploy source swapped, for previewing the other path's
    setup SQL without editing streamsnow.config.yaml. Git fields the config
    does not set get the wizard's defaults (named after the app database and
    schema), so the output matches what a git-repository config would emit."""
    validate_choice(source, DEPLOY_SOURCES, "--source")
    d = cfg.deploy
    if source == "stage-copy":
        return dataclasses.replace(cfg, deploy=dataclasses.replace(d, source=source))
    o = cfg.snowflake.objects
    auth = validate_choice(
        github_auth or (d.github_auth_mode if d.source == source else "pat"),
        GITHUB_AUTH_MODES,
        "--github-auth",
    )
    origin = git_origin or d.git_origin
    secret = d.secret_name or f"{o.app_database}.{o.app_schema}.GITHUB_PAT_SECRET"
    return dataclasses.replace(
        cfg,
        deploy=dataclasses.replace(
            d,
            source=source,
            git_repository_fqn=d.git_repository_fqn
            or f"{o.app_database}.{o.app_schema}.STREAMLIT_REPO",
            git_origin=validate_github_origin(origin, "--git-origin") if origin else "",
            api_integration_name=d.api_integration_name or "GITHUB_API_INTEGRATION",
            secret_name="" if auth == "public" else secret,
            github_auth_mode=auth,
        ),
    )


def _git_origin(cfg: Config) -> str:
    """The configured repo URL; setup SQL cannot be generated without it."""
    if not cfg.deploy.git_origin:
        raise ConfigError(
            "deploy.git_origin is not set: add your repo's HTTPS URL "
            "(https://github.com/<owner>/<repo>.git) under deploy: in streamsnow.config.yaml, "
            "or pass --git-origin to `streamsnow deploy-setup`."
        )
    return cfg.deploy.git_origin


def _uses_secret(cfg: Config) -> bool:
    return cfg.deploy.github_auth_mode != "public"


def _git_api_integration(cfg: Config) -> list[str]:
    # Allow only this repo's owner, not all of github.com (Snowflake's own
    # guidance: restrict allowed locations as narrowly as practical).
    owner = github_owner(_git_origin(cfg))
    lines = [
        "-- Needs ACCOUNTADMIN (or the CREATE INTEGRATION privilege).",
        f"CREATE API INTEGRATION IF NOT EXISTS {cfg.deploy.api_integration_name}",
        "  API_PROVIDER = git_https_api",
        f"  API_ALLOWED_PREFIXES = ('https://github.com/{owner}')",
    ]
    # ALLOWED_AUTHENTICATION_SECRETS is left at Snowflake's default (any
    # secret): naming the token secret here would reference it before the CI
    # role creates it later in the same script.
    lines.append("  ENABLED = TRUE;")
    return lines


def _git_secret_and_repo(cfg: Config) -> list[str]:
    origin = _git_origin(cfg)
    out: list[str] = []
    repo = [
        f"CREATE GIT REPOSITORY IF NOT EXISTS {cfg.deploy.git_repository_fqn}",
        f"  API_INTEGRATION = {cfg.deploy.api_integration_name}",
    ]
    if _uses_secret(cfg):
        out += [
            "-- Paste a GitHub token with read access to the repo (a fine-grained PAT",
            "-- with Contents: read) in place of <github-token>. Never commit it.",
            f"CREATE SECRET IF NOT EXISTS {cfg.deploy.secret_name}",
            "  TYPE = password USERNAME = 'x-access-token' PASSWORD = '<github-token>';",
            "",
        ]
        repo.append(f"  GIT_CREDENTIALS = {cfg.deploy.secret_name}")
    else:
        out.append("-- Public repo: no token or secret needed.")
    repo.append(f"  ORIGIN = '{origin}';")
    return out + repo


def generate_setup_sql(cfg: Config) -> str:
    """One-time Snowflake objects the deploy source needs. Run once by an admin
    (or the CI role with the right grants). Container account-level prerequisites
    (compute pool, external access integration) are emitted as commented guidance;
    ``generate_admin_sql`` is the full bootstrap an admin runs before this.
    """
    o = cfg.snowflake.objects
    ci = cfg.snowflake.roles.ci_role
    out: list[str] = [
        f"-- StreamSnow one-time setup ({one_line(cfg.deploy.source)} deploy source)",
        "-- Assumes the database, schema, warehouse, roles and CI user already exist;",
        "-- `streamsnow deploy-setup --admin` emits that full admin bootstrap.",
    ]
    if cfg.deploy.source == "stage-copy":
        out += _stage_objects(cfg)
    else:
        out += _git_api_integration(cfg)
        out += ["", *_git_secret_and_repo(cfg), ""]
        out += [
            # FETCH needs WRITE (or OWNERSHIP) when an admin, not the CI role,
            # ran this and so owns the repository.
            f"GRANT READ, WRITE ON GIT REPOSITORY {cfg.deploy.git_repository_fqn} TO ROLE {ci};",
            f"GRANT USAGE ON INTEGRATION {cfg.deploy.api_integration_name} TO ROLE {ci};",
        ]
    if cfg.runtime == "container":
        out += ["", "-- Container runtime account-level prerequisites (admin, one-time):"]
        if o.compute_pool.upper() == SYSTEM_POOL:
            out.append(
                f"--   {SYSTEM_POOL} is pre-provisioned by Snowflake in every account: nothing "
                "to create."
            )
        else:
            out.append(
                f"--   CREATE COMPUTE POOL {one_line(o.compute_pool)} (see deploy-setup --admin);"
            )
        out.append(
            f"--   CREATE EXTERNAL ACCESS INTEGRATION {one_line(o.external_access_integration)} "
            "(PyPI; see deploy-setup --admin);"
        )
    return "\n".join(out)


def _schema_of(fqn: str) -> str:
    """``DB.SCHEMA.NAME`` -> ``DB.SCHEMA``."""
    return fqn.rsplit(".", 1)[0]


def ci_user_name(ci_role: str) -> str:
    base = ci_role[: -len("_ROLE")] if ci_role.upper().endswith("_ROLE") else ci_role
    return f"{base}_USER"


_PEM_PUBLIC_HEADER = "-----BEGIN PUBLIC KEY-----"
_PEM_PUBLIC_FOOTER = "-----END PUBLIC KEY-----"
# Roles Snowflake provides; teardown must never name one in a DROP.
_SYSTEM_ROLES = ("ACCOUNTADMIN", "ORGADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN", "PUBLIC")


def read_public_key(path: Path) -> str:
    """The base64 body of a PEM ``PUBLIC KEY`` file, as ``RSA_PUBLIC_KEY`` takes it.

    Refuses anything that is not a single PEM public key, above all a private
    key, and never echoes the file's contents in the error.
    """
    path = Path(path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"--public-key-file: cannot read {path} ({type(exc).__name__}).") from exc
    if "PRIVATE KEY" in text:
        raise ConfigError(
            f"--public-key-file: {path} is a PRIVATE key. Pass the public key (.pub) instead; "
            "the private key stays in the CI secret SNOWFLAKE_PRIVATE_KEY_RAW."
        )
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 3 or lines[0] != _PEM_PUBLIC_HEADER or lines[-1] != _PEM_PUBLIC_FOOTER:
        raise ConfigError(
            f"--public-key-file: {path} is not a PEM public key (expected "
            f"{_PEM_PUBLIC_HEADER} ... {_PEM_PUBLIC_FOOTER})."
        )
    body = "".join(lines[1:-1])
    try:
        base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigError(f"--public-key-file: {path} has an invalid base64 body.") from exc
    return body


def generate_admin_sql(
    cfg: Config,
    *,
    public_key: str | None = None,
    viewer_users: Sequence[str] = (),
    viewer_roles: Sequence[str] = (),
) -> str:
    """The full, reviewable one-time admin bootstrap for a first deploy.

    ``deploy-setup`` alone assumed the database, schema, warehouse, CI and
    viewer roles, CI service user, ``CREATE STREAMLIT`` grant and data-read
    grants all existed, and nothing told a user without admin rights what to
    ask for. This emits all of it from ``streamsnow.config.yaml``, split into
    ``USE ROLE`` sections so each statement runs under the narrowest system
    role that can: SYSADMIN creates objects, USERADMIN creates roles and the
    user, SECURITYADMIN grants, ACCOUNTADMIN handles account-level objects,
    and the CI role creates what it will own. Safe to re-run: objects use
    ``IF NOT EXISTS``, grants are idempotent, the CI user's key is re-applied
    with ``ALTER USER``, and the external access integration (which has no
    ``IF NOT EXISTS``) is replaced with ``OR REPLACE`` and re-granted.

    ``public_key`` (the base64 body from :func:`read_public_key`) fills the CI
    user's ``RSA_PUBLIC_KEY``; without it the placeholder stays for a human to
    paste. The viewer role is granted to whoever runs the script, plus each
    name in ``viewer_users`` and each existing role in ``viewer_roles``.

    ``viewer_roles`` exists because the admin who runs this is usually not the
    person (or agent) building the apps: their everyday role (an analyst or
    agent role) otherwise cannot see the app database, warehouse or apps, so
    nothing confirms the setup worked. PUBLIC, the system roles and
    StreamSnow's own two roles are refused: PUBLIC would open every app to
    every user, and the others would widen an admin role or make a cycle.
    """
    o = cfg.snowflake.objects
    ci = cfg.snowflake.roles.ci_role
    viewer = cfg.snowflake.roles.viewer_role
    gov = cfg.governance
    both = (ci, viewer)
    app_schema = f"{o.app_database}.{o.app_schema}"
    stage_schema = f"{o.stage_database}.{o.stage_schema}"
    databases = list(dict.fromkeys([o.app_database, o.stage_database]))
    schemas = list(dict.fromkeys([app_schema, stage_schema]))
    ci_user = ci_user_name(ci)
    viewer_users = [validate_identifier(u, "--viewer-user") for u in viewer_users]
    viewer_roles = [validate_identifier(r, "--viewer-role") for r in viewer_roles]
    for r in viewer_roles:
        if r.upper() in _SYSTEM_ROLES or r.upper() in (ci.upper(), viewer.upper()):
            raise ConfigError(
                f"--viewer-role {r!r} is a Snowflake system role or one of StreamSnow's own "
                "roles: grant the viewer role to a role people or agents use day to day."
            )
    by_db = source_databases(cfg)
    imported = imported_databases(cfg)
    granted = {db: fqs for db, fqs in by_db.items() if db not in imported}
    shared = [db for db in by_db if db in imported]
    app_data = gov.app_data
    app_data_db = app_data.split(".", 1)[0]
    created_dbs = {d.upper() for d in databases}

    out: list[str] = [
        "-- StreamSnow admin bootstrap: one-time Snowflake objects for a first deploy.",
        f"-- Generated from streamsnow.config.yaml ({one_line(cfg.runtime)} runtime, "
        f"{one_line(cfg.deploy.source)} deploy source).",
        "-- Review every statement, then run it once as an account admin (Snowsight",
        "-- worksheet, or `snow sql --stdin` on an admin connection). Safe to re-run:",
        "-- every statement skips or re-applies what already exists.",
        "-- Deployed apps run with owner's rights: queries execute as the CI role that",
        "-- deploys them, and viewers need only USAGE on each app.",
        "",
        "-- 1. Objects ------------------------------------------------------------",
        "USE ROLE SYSADMIN;",
    ]
    out += [f"CREATE DATABASE IF NOT EXISTS {db};" for db in databases]
    if app_data_db.upper() not in created_dbs | set(by_db):
        out.append(f"CREATE DATABASE IF NOT EXISTS {app_data_db};")
    out += [f"CREATE SCHEMA IF NOT EXISTS {s};" for s in schemas]
    if app_data.upper() not in {s.upper() for s in schemas}:
        if app_data_db.upper() in by_db:
            out += [
                f"-- App data lives in source database {one_line(app_data_db)}: SYSADMIN needs "
                "CREATE SCHEMA",
                "-- on it, or run the next line as that database's owner.",
            ]
        out += [
            "-- App data: the schema for views and dynamic tables built for the apps.",
            f"CREATE SCHEMA IF NOT EXISTS {app_data};",
        ]
    out += [
        f"CREATE WAREHOUSE IF NOT EXISTS {o.default_warehouse}",
        "  WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE",
        "  INITIALLY_SUSPENDED = TRUE;",
        "",
        "-- 2. Roles and the CI service user ----------------------------------------",
        "USE ROLE USERADMIN;",
        f"CREATE ROLE IF NOT EXISTS {ci};  -- deploys and owns the apps (CI)",
        f"CREATE ROLE IF NOT EXISTS {viewer};  -- opens the apps (no data grants by default)",
    ]
    if public_key:
        out += [
            "-- Key-pair auth (no password). The private key is the SNOWFLAKE_PRIVATE_KEY_RAW",
            "-- repo secret; SNOWFLAKE_USER must match this user. The ALTER re-applies the",
            "-- key on a re-run (CREATE ... IF NOT EXISTS leaves an existing user untouched).",
        ]
    else:
        out += [
            "-- Key-pair auth (no password): generate a key pair, paste the PUBLIC key",
            "-- below, and store the private key as the SNOWFLAKE_PRIVATE_KEY_RAW repo",
            "-- secret. Rename the user freely; SNOWFLAKE_USER must match.",
            "-- (`streamsnow ci-key create` makes the key pair; `--public-key-file` fills this in.)",
        ]
    out += [
        f"CREATE USER IF NOT EXISTS {ci_user}",
        "  TYPE = SERVICE",
        f"  DEFAULT_ROLE = {ci}",
        f"  DEFAULT_WAREHOUSE = {o.default_warehouse}",
        f"  RSA_PUBLIC_KEY = '{public_key or '<paste public key>'}';",
    ]
    if public_key:
        out += [
            "-- (USERADMIN owns a user this script created; a user someone else created needs",
            "-- its owner, or ACCOUNTADMIN, to run the ALTER.)",
            f"ALTER USER IF EXISTS {ci_user} SET RSA_PUBLIC_KEY = '{public_key}';",
        ]
    out += [
        "",
        "-- 3. Grants -------------------------------------------------------------",
        "USE ROLE SECURITYADMIN;",
        f"GRANT ROLE {ci} TO USER {ci_user};",
        f"GRANT ROLE {ci} TO ROLE SYSADMIN;",
        f"GRANT ROLE {viewer} TO ROLE SYSADMIN;",
        "-- So you can open the apps yourself: the viewer role goes to whoever runs this",
        "-- (quoted, so any user name resolves exactly).",
        "SET streamsnow_me = '\"' || CURRENT_USER() || '\"';",
        f"GRANT ROLE {viewer} TO USER IDENTIFIER($streamsnow_me);",
    ]
    out += [f"GRANT ROLE {viewer} TO USER {u};" for u in viewer_users]
    for role in both:
        out += [f"GRANT USAGE ON DATABASE {db} TO ROLE {role};" for db in databases]
        out += [f"GRANT USAGE ON SCHEMA {s} TO ROLE {role};" for s in schemas]
        out.append(f"GRANT USAGE ON WAREHOUSE {o.default_warehouse} TO ROLE {role};")
    out.append(f"GRANT CREATE STREAMLIT ON SCHEMA {app_schema} TO ROLE {ci};")
    if cfg.deploy.source == "stage-copy":
        out.append(f"GRANT CREATE STAGE ON SCHEMA {stage_schema} TO ROLE {ci};")
    else:
        if _uses_secret(cfg):
            out.append(
                f"GRANT CREATE SECRET ON SCHEMA {_schema_of(cfg.deploy.secret_name)} TO ROLE {ci};"
            )
        out.append(
            "GRANT CREATE GIT REPOSITORY ON SCHEMA "
            f"{_schema_of(cfg.deploy.git_repository_fqn)} TO ROLE {ci};"
        )

    out += [
        "",
        f"-- Data the apps read: governance.sources ({', '.join(map(one_line, gov.sources))}).",
        "-- Only the CI role gets it: deployed apps run with",
        "-- their owner's rights, so viewers need USAGE on the app, not SELECT on the data.",
        "-- Schema-level future grants replace database-level ones of the same object type, for",
        "-- every role: check SHOW FUTURE GRANTS IN DATABASE <db> first, and repeat at schema",
        "-- level any database-level future grant another role relies on.",
        "-- If Snowflake rejects a GRANT for an object type this account lacks, delete that line",
        "-- and run the script again: every statement is safe to re-run.",
    ]

    def _data_grants(role: str) -> list[str]:
        grants: list[str] = []
        for db, fqs in granted.items():
            grants.append(f"GRANT USAGE ON DATABASE {db} TO ROLE {role};")
            for fq in fqs:
                grants.append(f"GRANT USAGE ON SCHEMA {fq} TO ROLE {role};")
                for scope in ("ALL", "FUTURE"):
                    grants += [
                        f"GRANT SELECT ON {scope} {kind} IN SCHEMA {fq} TO ROLE {role};"
                        for kind in READ_OBJECT_TYPES
                    ]
        return grants

    def _imported(db: str, role: str) -> str:
        return f"GRANT IMPORTED PRIVILEGES ON DATABASE {db} TO ROLE {role};"

    viewer_opt_in = [
        "-- Opt-in only: let the viewer role query this data directly (for example so",
        "-- local preview can connect as the viewer role). Leave commented for least privilege:",
    ]
    for db in shared:
        out += [
            f"-- {one_line(db)} is a shared database: USAGE + SELECT grants do not apply to it;",
            "-- IMPORTED PRIVILEGES (below, as ACCOUNTADMIN) grants read on the whole share.",
        ]
    if granted:
        out += _data_grants(ci)
        out += viewer_opt_in + [f"--   {one_line(g)}" for g in _data_grants(viewer)]
        out += [
            "-- If one of these is a SHARED database (a Marketplace or data-share import),",
            "-- its grants above fail: list it under governance.imported_databases and re-run",
            "-- this command. It then gets, as ACCOUNTADMIN (covering the whole share):",
            *[f"--   {one_line(_imported(db, ci))}" for db in granted],
        ]

    out += [
        "",
        f"-- App data ({one_line(app_data)}): views and dynamic tables built for the apps. "
        "The deploy",
        "-- job creates them from DDL in the repo, owned by the CI role. No CREATE TABLE:",
        "-- deployed DDL never holds a plain table, whose CREATE OR ALTER can drop column data.",
        f"-- Dynamic tables there refresh with WAREHOUSE = {one_line(o.default_warehouse)}, "
        "which the CI",
        "-- role already uses. Before the first one: enable change tracking on each source",
        "-- table an incremental refresh reads (its owner runs ALTER TABLE ... SET",
        "-- CHANGE_TRACKING = TRUE), or use REFRESH_MODE = FULL; and grant the CI role",
        "-- OPERATE on any dynamic table in a source that one of them reads.",
    ]
    if app_data_db.upper() not in created_dbs | set(granted):
        out.append(f"GRANT USAGE ON DATABASE {app_data_db} TO ROLE {ci};")
    out += [
        f"GRANT USAGE ON SCHEMA {app_data} TO ROLE {ci};",
        f"GRANT CREATE VIEW ON SCHEMA {app_data} TO ROLE {ci};",
        f"GRANT CREATE DYNAMIC TABLE ON SCHEMA {app_data} TO ROLE {ci};",
    ]
    viewer_app_data: list[str] = []
    if app_data_db.upper() not in created_dbs:
        viewer_app_data.append(f"GRANT USAGE ON DATABASE {app_data_db} TO ROLE {viewer};")
    viewer_app_data.append(f"GRANT USAGE ON SCHEMA {app_data} TO ROLE {viewer};")
    for scope in ("ALL", "FUTURE"):
        viewer_app_data += [
            f"GRANT SELECT ON {scope} {kind} IN SCHEMA {app_data} TO ROLE {viewer};"
            for kind in READ_OBJECT_TYPES
        ]
    out += [
        "-- Opt-in only: let the viewer role query app data directly (local preview).",
        "-- Leave commented for least privilege:",
        *[f"--   {one_line(g)}" for g in viewer_app_data],
    ]

    out += ["", "-- 4. Account-level objects -----------------------------------------------"]
    out.append("USE ROLE ACCOUNTADMIN;")
    account: list[str] = []
    if shared:
        account += [_imported(db, ci) for db in shared]
        account += viewer_opt_in + [f"--   {one_line(_imported(db, viewer))}" for db in shared]
    if cfg.runtime == "container":
        eai = o.external_access_integration
        account += [
            "-- PyPI access for the container image build. Uses Snowflake's managed",
            "-- network rule for PyPI. If your account prefers an artifact repository,",
            "-- skip this: configuring both disables the EAI.",
            "-- Snowflake has no IF NOT EXISTS for this statement, so a re-run replaces it;",
            "-- deployed apps keep working (verified live), and the GRANT below restores",
            "-- the CI role's usage. Grants or settings added to it by hand are reset.",
            f"CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION {eai}",
            "  ALLOWED_NETWORK_RULES = (snowflake.external_access.pypi_rule)",
            "  ENABLED = TRUE;",
            "-- Without the managed rule, create your own and list it above instead:",
            f"--   CREATE NETWORK RULE {one_line(app_schema)}.PYPI_NETWORK_RULE MODE = EGRESS "
            "TYPE = HOST_PORT",
            "--     VALUE_LIST = ('pypi.org', 'files.pythonhosted.org');",
            f"GRANT USAGE ON INTEGRATION {eai} TO ROLE {ci};",
        ]
        if o.compute_pool.upper() == SYSTEM_POOL:
            account += [
                f"-- {SYSTEM_POOL} is pre-provisioned by Snowflake in every account (USAGE",
                "-- goes to PUBLIC by default), so it is not created here. The explicit",
                "-- grant keeps deploys working if PUBLIC's USAGE is ever revoked.",
            ]
        else:
            account += [
                "-- A pool you create runs one app per node (the pre-provisioned",
                f"-- {SYSTEM_POOL} packs three); size MAX_NODES to apps running at once.",
                f"CREATE COMPUTE POOL IF NOT EXISTS {o.compute_pool}",
                "  MIN_NODES = 1 MAX_NODES = 1 INSTANCE_FAMILY = CPU_X64_XS",
                "  AUTO_SUSPEND_SECS = 300;",
            ]
        account.append(f"GRANT USAGE ON COMPUTE POOL {o.compute_pool} TO ROLE {ci};")
    if cfg.deploy.source == "git-repository":
        account += _git_api_integration(cfg)
        account.append(
            f"GRANT USAGE ON INTEGRATION {cfg.deploy.api_integration_name} TO ROLE {ci};"
        )
    out += account or ["-- Nothing account-level for this configuration."]

    out += [
        "",
        "-- 5. Deploy-source objects, created by the CI role that will own them ---------",
        f"USE ROLE {ci};",
    ]
    if cfg.deploy.source == "stage-copy":
        out += _stage_objects(cfg)
    else:
        out += _git_secret_and_repo(cfg)
    if viewer_roles:
        # Last, so a role name that does not resolve stops nothing above it.
        out += [
            "",
            "-- 6. Roles you already use, so they can open the apps and see these objects ----",
            "USE ROLE SECURITYADMIN;",
        ]
        out += [f"GRANT ROLE {viewer} TO ROLE {r};" for r in viewer_roles]
    return "\n".join(out)


def generate_teardown_sql(
    cfg: Config,
    app_data_objects: Sequence[tuple[str, str]] = (),
    *,
    inventory_incomplete: Sequence[str] = (),
    skipped: Sequence[str] = (),
) -> str:
    """Reviewable reverse of :func:`generate_admin_sql`: the start-fresh path.

    Printed, never run. Everything the bootstrap created goes, in an order
    where each DROP succeeds: the app database first (it holds the apps,
    stage, secret and git repository the CI role owns), then the warehouse,
    the CI user, the roles, and the account-level integrations. Every
    database that holds a source, Snowflake's shares and the pre-provisioned
    compute pool are never named, system roles are refused, and every DROP of
    something that may predate StreamSnow says so. Every statement uses
    ``IF EXISTS``, so a partial teardown can simply be re-run. App data in the
    app database goes with it; elsewhere its DROP SCHEMA is printed commented.
    Elsewhere, the views and dynamic tables the deploy job built there are
    dropped first (``app_data_objects``, dependents first), before the CI role
    that owns them, so nothing is left owned by a dropped role; when an
    object's kind is unknown, or the inventory is incomplete
    (``inventory_incomplete``), the CI role's DROP is printed commented with the
    reason, for the reviewer to finish in that order. ``skipped`` names declared
    objects the loader rejected for their form: they are listed, escaped, in one
    comment line and never dropped. They also hold the CI role's DROP back: a name
    malformed now (a stray newline or quotes added after a deploy) cannot prove the
    object it named was never built and owned by that role.
    """
    o = cfg.snowflake.objects
    ci = cfg.snowflake.roles.ci_role
    viewer = cfg.snowflake.roles.viewer_role
    by_db = source_databases(cfg)
    protected = set(by_db) | imported_databases(cfg)
    for field, db in (("app_database", o.app_database), ("stage_database", o.stage_database)):
        if db.upper() in protected:
            raise ConfigError(
                f"snowflake.objects.{field} = {db!r} holds a governance source or is a "
                "Snowflake-shared database; teardown would drop data the apps read. Refusing: "
                f"point {field} at a StreamSnow-only database first, or drop objects by hand."
            )
    for field, role in (("ci_role", ci), ("viewer_role", viewer)):
        if role.upper() in _SYSTEM_ROLES:
            raise ConfigError(
                f"snowflake.roles.{field} = {role!r} is a Snowflake system role; teardown "
                "would drop it. Refusing: use a StreamSnow-only role."
            )
    existed = "-- Existed before StreamSnow and used by other work? Delete the next line."

    out: list[str] = [
        "-- StreamSnow teardown: removes what `streamsnow deploy-setup --admin` created,",
        "-- so you can start fresh. REVIEW EVERY LINE before running; this cannot be undone.",
        f"-- Generated from streamsnow.config.yaml ({one_line(cfg.runtime)} runtime, "
        f"{one_line(cfg.deploy.source)} deploy source).",
        f"-- Kept: the source databases ({', '.join(map(one_line, by_db))}) and their data, "
        f"and {SYSTEM_POOL}.",
        "-- Every DROP uses IF EXISTS, so re-running a partial teardown is safe.",
        "-- Run as ACCOUNTADMIN, which owns or inherits everything the bootstrap made.",
        "USE ROLE ACCOUNTADMIN;",
        "",
        f"-- 1. App database: every deployed app, plus the {one_line(cfg.deploy.source)} "
        "objects in it.",
        existed,
        f"DROP DATABASE IF EXISTS {o.app_database};",
    ]
    if o.stage_database.upper() != o.app_database.upper() and cfg.deploy.source == "stage-copy":
        out += [
            f"-- The stage lives in {one_line(o.stage_database)}, which may hold other things: "
            "drop the",
            "-- stage only, not that database.",
            f"DROP STAGE IF EXISTS {o.stage_database}.{o.stage_schema}.{o.stage_name};",
        ]
    if cfg.deploy.source == "git-repository":
        app_db = o.app_database.upper()
        outside = [
            f"DROP {kind} IF EXISTS {fqn};"
            for kind, fqn in (
                ("GIT REPOSITORY", cfg.deploy.git_repository_fqn),
                ("SECRET", cfg.deploy.secret_name if _uses_secret(cfg) else ""),
            )
            if fqn and fqn.split(".", 1)[0].upper() != app_db
        ]
        if outside:
            out += ["-- Deploy-source objects that live outside the app database:", *outside]
    app_data = cfg.governance.app_data
    unsure: list[str] = []  # objects whose DROP the reviewer must pick: their owner stays too
    holds: list[str] = []  # why the CI role's DROP is held back
    out += ["", f"-- 2. App data ({one_line(app_data)})."]
    if app_data.split(".", 1)[0].upper() == o.app_database.upper():
        out.append(
            f"-- It lives in {one_line(o.app_database)}, so step 1 dropped it with everything in it."
        )
        declared = [f for f, _ in app_data_objects if is_plain_fqn(f)]
        if declared:
            out.append(
                f"-- That includes the {len(declared)} view(s) and dynamic table(s) "
                "the deploy job built there (as declared)."
            )
    else:
        if inventory_incomplete:
            listed = ", ".join(one_line(i) for i in inventory_incomplete)
            holds.append(
                f"objects: in {listed} did not load in full, so this script may not list "
                "every object the deploy job built"
            )
            out += [
                f"-- Incomplete inventory: objects: in {listed} did not load in full. Objects",
                "-- the deploy job built there may exist that this script cannot list.",
            ]
        odd = [*skipped, *(f for f, _ in app_data_objects if not is_plain_fqn(f))]
        if odd:
            # Never rendered into a DROP; but an earlier deploy may have built what the name
            # meant before it went bad, so the inventory is uncertain and the role stays.
            out.append(
                "-- Skipped (the declared name is not a plain DATABASE.SCHEMA.NAME, so this "
                "script cannot drop it): " + ", ".join(dict.fromkeys(one_line(f) for f in odd))
            )
            holds.append(
                "the skipped name(s) above are not a plain DATABASE.SCHEMA.NAME, so this "
                "script cannot tell whether the deploy job built them"
            )
        app_data_objects = [(f, k) for f, k in app_data_objects if is_plain_fqn(f)]
        if app_data_objects:
            out.append(
                "-- The views and dynamic tables the deploy job built there go first, before "
                "the CI role that owns them:"
            )
            for fqn, kind in app_data_objects:
                if kind in SQL_KIND:
                    out.append(f"DROP {SQL_KIND[kind]} IF EXISTS {fqn};")
                else:
                    unsure.append(fqn)
                    out += [
                        f"-- {one_line(fqn)}: its DDL file does not show whether it is a view or a "
                        "dynamic table. Uncomment the line that matches:",
                        f"--   DROP VIEW IF EXISTS {one_line(fqn)};",
                        f"--   DROP DYNAMIC TABLE IF EXISTS {one_line(fqn)};",
                    ]
        out += [
            "-- It lives outside the app database. Drop it only if nothing else lives there:",
            "-- uncomment the next line.",
            f"--   DROP SCHEMA IF EXISTS {one_line(app_data)};",
        ]
    if unsure:
        holds.append(f"{', '.join(map(one_line, unsure))} may still exist")
    ci_drop = (
        [
            f"-- Held back: {'; '.join(holds)}, owned by {one_line(ci)}. Dropping the role first "
            "would",
            "-- leave those objects owned by whoever runs this script. Drop them, then uncomment:",
            f"--   DROP ROLE IF EXISTS {one_line(ci)};",
        ]
        if holds
        else [f"DROP ROLE IF EXISTS {ci};"]
    )
    out += [
        "",
        "-- 3. Warehouse, CI service user, roles (after the objects they own are gone).",
        existed,
        f"DROP WAREHOUSE IF EXISTS {o.default_warehouse};",
        f"DROP USER IF EXISTS {ci_user_name(ci)};",
        f"DROP ROLE IF EXISTS {viewer};",
        *ci_drop,
        "",
        "-- 4. Account-level objects.",
    ]
    account: list[str] = []
    if cfg.runtime == "container":
        account += [
            existed,
            f"DROP EXTERNAL ACCESS INTEGRATION IF EXISTS {o.external_access_integration};",
        ]
        if o.compute_pool.upper() != SYSTEM_POOL:
            account += [
                "-- STOP ALL stops EVERY service on this pool, not only StreamSnow's apps.",
                "-- If the DROP then fails because services are still stopping, re-run it.",
                existed,
                f"ALTER COMPUTE POOL IF EXISTS {o.compute_pool} STOP ALL;",
                f"DROP COMPUTE POOL IF EXISTS {o.compute_pool};",
            ]
    if cfg.deploy.source == "git-repository":
        account += [
            "-- Other Git repositories in the account may use this integration:",
            existed,
            f"DROP API INTEGRATION IF EXISTS {cfg.deploy.api_integration_name};",
        ]
    out += account or ["-- Nothing account-level for this configuration."]
    return "\n".join(out)


@dataclasses.dataclass(frozen=True)
class CiGrant:
    """One grant the admin script gives the CI role, as ``SHOW GRANTS TO ROLE`` lists it."""

    privilege: str
    granted_on: str
    name: str


_GRANT_TO_ROLE_RE = re.compile(r"^GRANT (?P<priv>.+?) ON (?P<target>.+) TO ROLE (?P<role>\S+);$")


def expected_ci_grants(cfg: Config) -> list[CiGrant]:
    """The grants ``generate_admin_sql`` gives the CI role that one ``SHOW GRANTS`` row proves.

    ``ci-key verify`` checks these against what the CI role really holds. They are
    read back out of the admin script itself, never kept as a second list: a
    grant added to (or dropped from) the script changes what verify expects in
    the same commit, so the two cannot drift apart and report a working setup as
    broken, or a broken one as working. Commented lines (the viewer opt-in, the
    shared-database alternative) are skipped. ``ALL``/``FUTURE`` grants are
    skipped too: they show up as one row per object, or not at all, so verify's
    ``LIMIT 0`` read is what proves the data grants.
    """
    ci = cfg.snowflake.roles.ci_role
    out: dict[CiGrant, None] = {}
    for line in generate_admin_sql(cfg).splitlines():
        m = _GRANT_TO_ROLE_RE.match(line.strip())
        if not m or m["role"].upper() != ci.upper():
            continue
        target = m["target"]
        if target.startswith(("ALL ", "FUTURE ")):
            continue
        kind, _, name = target.rpartition(" ")
        out[CiGrant(privilege=m["priv"], granted_on=kind, name=name)] = None
    return list(out)
