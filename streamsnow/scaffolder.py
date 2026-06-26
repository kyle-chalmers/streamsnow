"""Render a governed Streamlit-in-Snowflake repo from a validated Config.

Templates live in ``streamsnow/_templates/`` (packaged) and are rendered with
Jinja2 using a context built from ``streamsnow.config.yaml``. Runtime mode
(container vs warehouse) and deploy source are honored by gating which files are
written and by ``{% if %}`` branches inside the templates.

This is the engine behind ``streamsnow init`` (scaffold a repo + starter app)
and ``streamsnow new`` (add another app).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .config import Config

TEMPLATES_DIR = Path(__file__).parent / "_templates"

_DEFAULT_CHART_SEQUENCE = ["#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6", "#06B6D4"]


@dataclass(frozen=True)
class RenderItem:
    template: str
    output: str  # may contain {slug}
    when: Callable[[Config], bool] = lambda _cfg: True


# Repo-level files + a starter app. Output paths use {slug} for the app dir.
RENDER_MAP: tuple[RenderItem, ...] = (
    RenderItem("repo/AGENTS.md.j2", "AGENTS.md"),
    RenderItem("repo/CLAUDE.md.j2", "CLAUDE.md"),
    RenderItem("repo/gitignore.j2", ".gitignore"),
    RenderItem("repo/pre-commit-config.yaml.j2", ".pre-commit-config.yaml"),
    RenderItem("repo/ci.yml.j2", ".github/workflows/checks.yml"),
    RenderItem("repo/README.md.j2", "README.md"),
    RenderItem("app/streamlit_app.py.j2", "apps/{slug}/streamlit_app.py"),
    RenderItem("app/AGENTS.md.j2", "apps/{slug}/AGENTS.md"),
    RenderItem("app/snowflake.yml.j2", "apps/{slug}/snowflake.yml"),
    RenderItem(
        "app/pyproject.toml.j2", "apps/{slug}/pyproject.toml", lambda c: c.runtime == "container"
    ),
    RenderItem(
        "app/environment.yml.j2", "apps/{slug}/environment.yml", lambda c: c.runtime == "warehouse"
    ),
    RenderItem("app/branding.py.j2", "apps/{slug}/branding.py"),
    RenderItem("app/sql_loader.py.j2", "apps/{slug}/sql_loader.py"),
    RenderItem("app/config.toml.j2", "apps/{slug}/.streamlit/config.toml"),
    RenderItem("app/secrets.toml.example.j2", "apps/{slug}/.streamlit/secrets.toml.example"),
    RenderItem("app/example_metric.sql.j2", "apps/{slug}/queries/example_metric.sql"),
    RenderItem("app/overview.py.j2", "apps/{slug}/pages/overview.py"),
)

# Just the app subset (for `streamsnow new` and additional apps).
APP_ITEMS = tuple(i for i in RENDER_MAP if i.output.startswith("apps/{slug}/"))
# Repo-level files (rendered once per repo; idempotent on re-init).
REPO_ITEMS = tuple(i for i in RENDER_MAP if not i.output.startswith("apps/{slug}/"))


def _title_from_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("_", "-").split("-"))


def build_context(cfg: Config, app_slug: str) -> dict:
    o = cfg.snowflake.objects
    brand = (cfg.raw or {}).get("brand") or {}
    theme = brand.get("theme") or {}
    return {
        "project_name": cfg.project.name,
        "project_slug": cfg.project.slug,
        "app_slug": app_slug,
        "app_title": _title_from_slug(app_slug),
        "runtime": cfg.runtime,
        "deploy_source": cfg.deploy.source,
        "account": cfg.snowflake.account,
        "connection_name": cfg.snowflake.connection_name,
        "app_database": o.app_database,
        "app_schema": o.app_schema,
        "stage_database": o.stage_database,
        "stage_schema": o.stage_schema,
        "stage_name": o.stage_name,
        "default_warehouse": o.default_warehouse,
        "compute_pool": o.compute_pool,
        "external_access_integration": o.external_access_integration,
        "runtime_name": o.runtime_name,
        "container_python": o.container_python,
        "ci_role": cfg.snowflake.roles.ci_role,
        "viewer_role": cfg.snowflake.roles.viewer_role,
        "gov_database": cfg.governance.database,
        "schema_allow": list(cfg.governance.schema_allow),
        "schema_deny": list(cfg.governance.schema_deny),
        "read_exceptions": list(cfg.governance.read_exceptions),
        "cache_ttl": int((cfg.raw or {}).get("cache_ttl", 1800)),
        "brand": {
            "primary": theme.get("primary", "#3B82F6"),
            "background": theme.get("background", "#FFFFFF"),
            "text_color": theme.get("text_color", "#1F2937"),
            "secondary_background": theme.get("secondary_background", "#F3F4F6"),
            "font": brand.get("font", "Inter, sans-serif"),
            "chart_sequence": brand.get("chart_sequence") or _DEFAULT_CHART_SEQUENCE,
        },
    }


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )


def scaffold(
    cfg: Config,
    target: Path,
    app_slug: str,
    *,
    items: tuple[RenderItem, ...] = RENDER_MAP,
    force: bool = False,
    skip_existing: bool = False,
) -> list[Path]:
    """Render ``items`` into ``target``. Returns the list of written paths.

    For an existing output: overwrite if ``force``; skip (idempotent) if
    ``skip_existing``; otherwise raise FileExistsError. ``skip_existing`` is how
    re-running ``init`` leaves already-present repo-level files untouched while
    still guarding per-app files.
    """
    env = _env()
    ctx = build_context(cfg, app_slug)
    written: list[Path] = []
    for item in items:
        if not item.when(cfg):
            continue
        out = target / item.output.format(slug=app_slug)
        if out.exists() and not force:
            if skip_existing:
                continue
            raise FileExistsError(f"{out} already exists (use --force to overwrite)")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(env.get_template(item.template).render(**ctx))
        written.append(out)
    return written
