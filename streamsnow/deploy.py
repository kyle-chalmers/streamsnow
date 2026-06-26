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


def generate_setup_sql(cfg: Config) -> str:
    """One-time Snowflake objects the deploy source needs. Run once by an admin
    (or the CI role with the right grants). Container account-level prerequisites
    (compute pool, external access integration) are emitted as commented guidance.
    """
    o = cfg.snowflake.objects
    out: list[str] = [f"-- StreamSnow one-time setup ({cfg.deploy.source} deploy source)"]
    if cfg.deploy.source == "stage-copy":
        out += [
            f"CREATE STAGE IF NOT EXISTS {o.stage_database}.{o.stage_schema}.{o.stage_name}",
            "  COMMENT = 'StreamSnow app source, SHA-versioned per deploy';",
        ]
    else:
        out += [
            f"CREATE API INTEGRATION IF NOT EXISTS {cfg.deploy.api_integration_name}",
            "  API_PROVIDER = git_https_api",
            "  API_ALLOWED_PREFIXES = ('https://github.com/')",
            "  ENABLED = TRUE;",
            "",
            f"-- Store a GitHub token (PAT or GitHub-App installation token) in {cfg.deploy.secret_name}",
            f"CREATE SECRET IF NOT EXISTS {cfg.deploy.secret_name}",
            "  TYPE = password USERNAME = 'x-access-token' PASSWORD = '<github-token>';",
            "",
            f"CREATE GIT REPOSITORY IF NOT EXISTS {cfg.deploy.git_repository_fqn}",
            f"  API_INTEGRATION = {cfg.deploy.api_integration_name}",
            f"  GIT_CREDENTIALS = {cfg.deploy.secret_name}",
            "  ORIGIN = '<https://github.com/your-org/your-repo.git>';",
            "",
            f"GRANT READ ON GIT REPOSITORY {cfg.deploy.git_repository_fqn} TO ROLE {cfg.snowflake.roles.ci_role};",
            f"GRANT USAGE ON INTEGRATION {cfg.deploy.api_integration_name} TO ROLE {cfg.snowflake.roles.ci_role};",
        ]
    if cfg.runtime == "container":
        out += [
            "",
            "-- Container runtime account-level prerequisites (admin, one-time):",
            f"--   CREATE COMPUTE POOL {o.compute_pool} ... ;",
            f"--   CREATE EXTERNAL ACCESS INTEGRATION {o.external_access_integration} ... ;  (PyPI)",
        ]
    return "\n".join(out)
