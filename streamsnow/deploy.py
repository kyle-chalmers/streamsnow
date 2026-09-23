"""Generate Snowflake deploy SQL from config — the deploy-source strategy seam.

Two deploy sources share an identical *tail* (the container ALTER, the
``ADD LIVE VERSION FROM LAST`` defense, the ``GRANT USAGE`` to the viewer role);
only the ``FROM`` clause, the create/refresh verb, and the CI pre-step differ:

- **stage-copy** (default): CI uploads app source to a SHA-versioned internal
  stage; deploy runs idempotent ``CREATE OR REPLACE STREAMLIT ... FROM '@stage/
  commits/<sha>/...'``.
- **git-repository**: Snowflake's GIT REPOSITORY object holds the source;
  new apps ``CREATE STREAMLIT ... FROM '@<repo>/branches/<branch>/...'`` and
  existing apps refresh via the ``ABORT -> PULL -> COMMIT -> ADD LIVE VERSION``
  state machine.

All identifiers come from a validated :class:`~streamsnow.config.Config`, so
they are rendered into SQL directly (the config layer is the injection gate).
This module is pure (no DB calls); the CLI / generated deploy workflow runs it.
"""

import re

from .config import Config

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
    verb = (
        "CREATE OR REPLACE STREAMLIT"
        if cfg.deploy.source == "stage-copy"
        else "CREATE STREAMLIT IF NOT EXISTS"
    )
    lines = [
        f"{verb} {fqn}",
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


def generate_refresh_sql(cfg: Config, slug: str) -> str:
    """Git-repository refresh for an EXISTING app (the ABORT/PULL/COMMIT state
    machine). Not used by stage-copy (CREATE OR REPLACE is idempotent there)."""
    if cfg.deploy.source != "git-repository":
        raise ValueError("refresh SQL only applies to the git-repository deploy source")
    fqn = streamlit_fqn(cfg, slug)
    return (
        f"ALTER STREAMLIT {fqn} ABORT;\n"
        f"ALTER STREAMLIT {fqn} PULL;\n"
        f"ALTER STREAMLIT {fqn} COMMIT;\n"
        f"ALTER STREAMLIT {fqn} ADD LIVE VERSION FROM LAST;\n"
        "-- If PULL reports 'already up to date': skip COMMIT, keep the trailing ADD LIVE VERSION."
    )


# Snowflake's pre-provisioned CPU pool for Streamlit container apps. It exists
# in every account (owned by ACCOUNTADMIN, USAGE granted to PUBLIC by default),
# so setup SQL must never try to create it.
SYSTEM_POOL = "SYSTEM_COMPUTE_POOL_CPU"
# Databases Snowflake shares into every account: reads need IMPORTED PRIVILEGES,
# not USAGE + SELECT.
_SHARED_DATABASES = ("SNOWFLAKE_SAMPLE_DATA", "SNOWFLAKE")


def _stage_objects(cfg: Config) -> list[str]:
    o = cfg.snowflake.objects
    return [
        f"CREATE STAGE IF NOT EXISTS {o.stage_database}.{o.stage_schema}.{o.stage_name}",
        "  COMMENT = 'StreamSnow app source, SHA-versioned per deploy';",
    ]


def _git_api_integration(cfg: Config) -> list[str]:
    return [
        f"CREATE API INTEGRATION IF NOT EXISTS {cfg.deploy.api_integration_name}",
        "  API_PROVIDER = git_https_api",
        "  API_ALLOWED_PREFIXES = ('https://github.com/')",
        "  ENABLED = TRUE;",
    ]


def _git_secret_and_repo(cfg: Config) -> list[str]:
    return [
        f"-- Store a GitHub token (PAT or GitHub-App installation token) in {cfg.deploy.secret_name}",
        f"CREATE SECRET IF NOT EXISTS {cfg.deploy.secret_name}",
        "  TYPE = password USERNAME = 'x-access-token' PASSWORD = '<github-token>';",
        "",
        f"CREATE GIT REPOSITORY IF NOT EXISTS {cfg.deploy.git_repository_fqn}",
        f"  API_INTEGRATION = {cfg.deploy.api_integration_name}",
        f"  GIT_CREDENTIALS = {cfg.deploy.secret_name}",
        "  ORIGIN = '<https://github.com/your-org/your-repo.git>';",
    ]


def generate_setup_sql(cfg: Config) -> str:
    """One-time Snowflake objects the deploy source needs. Run once by an admin
    (or the CI role with the right grants). Container account-level prerequisites
    (compute pool, external access integration) are emitted as commented guidance;
    ``generate_admin_sql`` is the full bootstrap an admin runs before this.
    """
    o = cfg.snowflake.objects
    ci = cfg.snowflake.roles.ci_role
    out: list[str] = [
        f"-- StreamSnow one-time setup ({cfg.deploy.source} deploy source)",
        "-- Assumes the database, schema, warehouse, roles and CI user already exist;",
        "-- `streamsnow deploy-setup --admin` emits that full admin bootstrap.",
    ]
    if cfg.deploy.source == "stage-copy":
        out += _stage_objects(cfg)
    else:
        out += _git_api_integration(cfg)
        out += ["", *_git_secret_and_repo(cfg), ""]
        out += [
            f"GRANT READ ON GIT REPOSITORY {cfg.deploy.git_repository_fqn} TO ROLE {ci};",
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
            out.append(f"--   CREATE COMPUTE POOL {o.compute_pool} (see deploy-setup --admin);")
        out.append(
            f"--   CREATE EXTERNAL ACCESS INTEGRATION {o.external_access_integration} "
            "(PyPI; see deploy-setup --admin);"
        )
    return "\n".join(out)


def _schema_of(fqn: str) -> str:
    """``DB.SCHEMA.NAME`` -> ``DB.SCHEMA``."""
    return fqn.rsplit(".", 1)[0]


def _ci_user_name(ci_role: str) -> str:
    base = ci_role[: -len("_ROLE")] if ci_role.upper().endswith("_ROLE") else ci_role
    return f"{base}_USER"


def generate_admin_sql(cfg: Config) -> str:
    """The full, reviewable one-time admin bootstrap for a first deploy.

    ``deploy-setup`` alone assumed the database, schema, warehouse, CI and
    viewer roles, CI service user, ``CREATE STREAMLIT`` grant and data-read
    grants all existed, and nothing told a user without admin rights what to
    ask for. This emits all of it from ``streamsnow.config.yaml``, split into
    ``USE ROLE`` sections so each statement runs under the narrowest system
    role that can: SYSADMIN creates objects, USERADMIN creates roles and the
    user, SECURITYADMIN grants, ACCOUNTADMIN handles account-level objects,
    and the CI role creates what it will own. Re-runnable (``IF NOT EXISTS``)
    except the external access integration, which Snowflake cannot guard that
    way: skip that statement on a re-run.
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
    ci_user = _ci_user_name(ci)
    shared_gov = gov.database.upper() in _SHARED_DATABASES

    out: list[str] = [
        "-- StreamSnow admin bootstrap: one-time Snowflake objects for a first deploy.",
        f"-- Generated from streamsnow.config.yaml ({cfg.runtime} runtime, "
        f"{cfg.deploy.source} deploy source).",
        "-- Review every statement, then run it once as an account admin (Snowsight",
        "-- worksheet, or `snow sql --stdin` on an admin connection). Idempotent.",
        "-- Deployed apps run with owner's rights: queries execute as the CI role that",
        "-- deploys them, and viewers need only USAGE on each app.",
        "",
        "-- 1. Objects ------------------------------------------------------------",
        "USE ROLE SYSADMIN;",
    ]
    out += [f"CREATE DATABASE IF NOT EXISTS {db};" for db in databases]
    out += [f"CREATE SCHEMA IF NOT EXISTS {s};" for s in schemas]
    out += [
        f"CREATE WAREHOUSE IF NOT EXISTS {o.default_warehouse}",
        "  WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE",
        "  INITIALLY_SUSPENDED = TRUE;",
        "",
        "-- 2. Roles and the CI service user ----------------------------------------",
        "USE ROLE USERADMIN;",
        f"CREATE ROLE IF NOT EXISTS {ci};      -- deploys and owns the apps (CI)",
        f"CREATE ROLE IF NOT EXISTS {viewer};  -- opens the apps (no data grants by default)",
        "-- Key-pair auth (no password): generate a key pair, paste the PUBLIC key",
        "-- below, and store the private key as the SNOWFLAKE_PRIVATE_KEY_RAW repo",
        "-- secret. Rename the user freely; SNOWFLAKE_USER must match.",
        f"CREATE USER IF NOT EXISTS {ci_user}",
        "  TYPE = SERVICE",
        f"  DEFAULT_ROLE = {ci}",
        f"  DEFAULT_WAREHOUSE = {o.default_warehouse}",
        "  RSA_PUBLIC_KEY = '<paste public key>';",
        "",
        "-- 3. Grants -------------------------------------------------------------",
        "USE ROLE SECURITYADMIN;",
        f"GRANT ROLE {ci} TO USER {ci_user};",
        f"GRANT ROLE {ci} TO ROLE SYSADMIN;",
        f"GRANT ROLE {viewer} TO ROLE SYSADMIN;",
        f"-- So you can open the apps yourself: GRANT ROLE {viewer} TO USER <your_user>;",
    ]
    for role in both:
        out += [f"GRANT USAGE ON DATABASE {db} TO ROLE {role};" for db in databases]
        out += [f"GRANT USAGE ON SCHEMA {s} TO ROLE {role};" for s in schemas]
        out.append(f"GRANT USAGE ON WAREHOUSE {o.default_warehouse} TO ROLE {role};")
    out.append(f"GRANT CREATE STREAMLIT ON SCHEMA {app_schema} TO ROLE {ci};")
    if cfg.deploy.source == "stage-copy":
        out.append(f"GRANT CREATE STAGE ON SCHEMA {stage_schema} TO ROLE {ci};")
    else:
        out.append(
            f"GRANT CREATE SECRET ON SCHEMA {_schema_of(cfg.deploy.secret_name)} TO ROLE {ci};"
        )
        out.append(
            "GRANT CREATE GIT REPOSITORY ON SCHEMA "
            f"{_schema_of(cfg.deploy.git_repository_fqn)} TO ROLE {ci};"
        )

    out += [
        "",
        f"-- Data the apps read: governance database {gov.database}, allowed schemas only",
        f"-- ({', '.join(gov.schema_allow)}). Only the CI role gets it: deployed apps run with",
        "-- their owner's rights, so viewers need USAGE on the app, not SELECT on the data.",
    ]

    def _data_grants(role: str) -> list[str]:
        grants = [f"GRANT USAGE ON DATABASE {gov.database} TO ROLE {role};"]
        for schema in gov.schema_allow:
            fq = f"{gov.database}.{schema}"
            grants += [
                f"GRANT USAGE ON SCHEMA {fq} TO ROLE {role};",
                f"GRANT SELECT ON ALL TABLES IN SCHEMA {fq} TO ROLE {role};",
                f"GRANT SELECT ON ALL VIEWS IN SCHEMA {fq} TO ROLE {role};",
                f"GRANT SELECT ON FUTURE TABLES IN SCHEMA {fq} TO ROLE {role};",
                f"GRANT SELECT ON FUTURE VIEWS IN SCHEMA {fq} TO ROLE {role};",
            ]
        return grants

    def _imported(role: str) -> str:
        return f"GRANT IMPORTED PRIVILEGES ON DATABASE {gov.database} TO ROLE {role};"

    viewer_opt_in = [
        "-- Opt-in only: let the viewer role query this data directly (for example so",
        "-- local preview can connect as the viewer role). Leave commented for least privilege:",
    ]
    if shared_gov:
        out += [
            f"-- {gov.database} is a shared database: USAGE + SELECT grants do not apply to it;",
            "-- IMPORTED PRIVILEGES (below, as ACCOUNTADMIN) grants read on the whole share.",
        ]
    else:
        out += _data_grants(ci)
        out += viewer_opt_in + [f"--   {g}" for g in _data_grants(viewer)]
        out += [
            f"-- If {gov.database} is a SHARED database (a Marketplace or data-share import,",
            "-- e.g. SNOWFLAKE_SAMPLE_DATA), the grants above fail; use this instead,",
            "-- as ACCOUNTADMIN (it covers the whole share, not just the allowed schemas):",
            f"--   {_imported(ci)}",
        ]

    out += ["", "-- 4. Account-level objects -----------------------------------------------"]
    out.append("USE ROLE ACCOUNTADMIN;")
    account: list[str] = []
    if shared_gov:
        account.append(_imported(ci))
        account += viewer_opt_in + [f"--   {_imported(viewer)}"]
    if cfg.runtime == "container":
        eai = o.external_access_integration
        account += [
            "-- PyPI access for the container image build. Uses Snowflake's managed",
            "-- network rule for PyPI. If your account prefers an artifact repository,",
            "-- skip this: configuring both disables the EAI.",
            "-- (Snowflake has no IF NOT EXISTS for this statement: skip it on a re-run.)",
            f"CREATE EXTERNAL ACCESS INTEGRATION {eai}",
            "  ALLOWED_NETWORK_RULES = (snowflake.external_access.pypi_rule)",
            "  ENABLED = TRUE;",
            "-- Without the managed rule, create your own and list it above instead:",
            f"--   CREATE NETWORK RULE {app_schema}.PYPI_NETWORK_RULE MODE = EGRESS TYPE = HOST_PORT",
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
    return "\n".join(out)
