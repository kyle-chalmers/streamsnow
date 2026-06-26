"""StreamSnow command-line interface.

streamsnow init           Setup wizard + scaffold a governed repo + starter app
streamsnow new            Scaffold another app in an existing StreamSnow repo
streamsnow doctor         Check the local environment for prerequisites
streamsnow check ...      Run a governance check (e.g. schema-refs)
streamsnow deploy-setup   Emit the one-time Snowflake DDL for your deploy source
streamsnow update         Re-vendor templates/tools and bump the plugin
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import typer
import yaml
from rich.console import Console

from . import __version__
from .config import CONFIG_FILENAME, Config, ConfigError, load_config
from .scaffolder import APP_ITEMS, scaffold
from .tools.check_schema_refs import main as _schema_refs_main

app = typer.Typer(
    name="streamsnow",
    help="Build, govern, and ship Streamlit-in-Snowflake apps with Claude Code.",
    no_args_is_help=True,
    add_completion=False,
)
check_app = typer.Typer(help="Run a governance check (config-driven).", no_args_is_help=True)
app.add_typer(check_app, name="check")
console = Console()

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_NOT_YET = "[yellow]not yet implemented[/] — lands in {phase}."


def _err(msg: str) -> None:
    console.print(f"[red]error:[/] {msg}")


def _validate_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug):
        _err(f"app slug {slug!r} must be kebab-case (^[a-z][a-z0-9-]*$).")
        raise typer.Exit(2)
    return slug


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"streamsnow {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the StreamSnow version and exit.",
    ),
) -> None:
    """StreamSnow — Streamlit-in-Snowflake apps, governed, with Claude Code."""


def _prompt_config() -> dict:
    """Interactive setup wizard. Returns a config dict (validated by caller)."""
    console.print(
        "[bold]StreamSnow setup[/] — answer a few questions (Enter accepts the default).\n"
    )
    p = typer.prompt
    runtime = p("Runtime (container/warehouse)", default="container")
    name = p("Project name", default="My Dashboards")
    slug = p("Project slug (kebab-case)", default="my-dashboards")
    account = p("Snowflake account locator (no .snowflakecomputing.com)")
    connection = p("snow CLI connection name", default=slug)
    app_db = p("Database for deployed STREAMLIT objects", default="DATA_APPS")
    app_schema = p("Schema for deployed STREAMLIT objects", default="BI_APPS")
    warehouse = p("Query warehouse", default="STREAMLIT_WH")
    ci_role = p("CI deploy role", default="STREAMLIT_CI_ROLE")
    viewer_role = p("App viewer role", default="STREAMLIT_APP_ROLE")
    gov_db = p("BI database your apps query", default="ANALYTICS_DB")
    allow = p("Allowed schemas (comma-separated)", default="ANALYTICS,REPORTING")
    deny = p("Denied schemas (comma-separated)", default="RAW,STAGING")
    objects: dict = {
        "app_database": app_db,
        "app_schema": app_schema,
        "stage_database": app_db,
        "stage_schema": app_schema,
        "default_warehouse": warehouse,
        "allowed_warehouses": [warehouse],
    }
    if runtime == "container":
        objects["compute_pool"] = p("Compute pool (container)", default="STREAMLIT_POOL")
        objects["external_access_integration"] = p(
            "External access integration (container)", default="PYPI_ACCESS_INTEGRATION"
        )
    return {
        "schema_version": 1,
        "runtime": runtime,
        "project": {"name": name, "slug": slug},
        "snowflake": {
            "account": account,
            "connection_name": connection,
            "objects": objects,
            "roles": {"ci_role": ci_role, "viewer_role": viewer_role},
        },
        "governance": {
            "database": gov_db,
            "schema_allow": [s.strip() for s in allow.split(",") if s.strip()],
            "schema_deny": [s.strip() for s in deny.split(",") if s.strip()],
        },
        "deploy": {"source": p("Deploy source (stage-copy/git-repository)", default="stage-copy")},
    }


@app.command()
def init(
    config: Path = typer.Option(
        None, "--config", help="Use an existing config file instead of prompting."
    ),
    directory: Path = typer.Option(Path("."), "--dir", help="Target directory to scaffold into."),
    app_slug: str = typer.Option(
        "example-dashboard", "--app", help="Starter app slug (kebab-case)."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Set up a governed Streamlit-in-Snowflake repo with a starter app."""
    _validate_slug(app_slug)
    target = directory.resolve()
    target.mkdir(parents=True, exist_ok=True)

    try:
        if config is not None:
            cfg = load_config(config)  # validate
            config_text = Path(config).read_text()
        else:
            cfg_dict = _prompt_config()
            cfg = Config.from_dict(cfg_dict)  # validate
            config_text = yaml.safe_dump(cfg_dict, sort_keys=False)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc

    cfg_out = target / CONFIG_FILENAME
    if cfg_out.exists() and not force:
        _err(f"{cfg_out} already exists (use --force).")
        raise typer.Exit(2)
    cfg_out.write_text(config_text)

    try:
        written = scaffold(cfg, target, app_slug, force=force)
    except FileExistsError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc

    console.print(f"[green]✓[/] scaffolded {len(written) + 1} files into {target}")
    console.print(
        f"\nNext:\n"
        f"  1. cp apps/{app_slug}/.streamlit/secrets.toml.example apps/{app_slug}/.streamlit/secrets.toml  (then edit)\n"
        f"  2. uv pip install streamsnow && pre-commit install\n"
        f"  3. streamlit run apps/{app_slug}/streamlit_app.py\n"
        f"  4. /plugin marketplace add kyle-chalmers/streamsnow  (in Claude Code)"
    )


@app.command()
def new(
    domain: str = typer.Argument(..., help="Business domain, e.g. 'marketing'."),
    function: str = typer.Argument(..., help="App function, e.g. 'campaign-dashboard'."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Scaffold a new app ({domain}-{function}) into an existing StreamSnow repo."""
    slug = _validate_slug(f"{domain}-{function}")
    try:
        cfg = load_config()
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    try:
        written = scaffold(cfg, Path.cwd(), slug, items=APP_ITEMS, force=force)
    except FileExistsError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    console.print(f"[green]✓[/] created app {slug} ({len(written)} files)")


@check_app.command("schema-refs")
def check_schema_refs_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs to scan (default: apps/)."),
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
    output_format: str = typer.Option("md", "--format", help="md | json"),
) -> None:
    """Block references to denied Snowflake schemas in app code."""
    argv: list[str] = list(paths or ["apps"])
    argv += ["--format", output_format]
    if config is not None:
        argv += ["--config", str(config)]
    raise typer.Exit(code=_schema_refs_main(argv))


@app.command()
def doctor() -> None:
    """Check the local environment for the prerequisites StreamSnow needs."""
    ok = True
    py = sys.version_info
    if (py.major, py.minor) >= (3, 11):
        console.print(f"[green]✓[/] Python {py.major}.{py.minor} (>=3.11)")
    else:
        console.print(f"[red]✗[/] Python {py.major}.{py.minor} — need >=3.11")
        ok = False
    for tool, hint in (
        ("git", "install git"),
        ("uv", "https://docs.astral.sh/uv/"),
        ("snow", "pip install snowflake-cli"),
    ):
        if shutil.which(tool):
            console.print(f"[green]✓[/] {tool} found")
        else:
            console.print(f"[yellow]∘[/] {tool} not found — {hint}")
            if tool in {"git", "uv"}:
                ok = False
    # Config + schema-version drift check, when run inside a StreamSnow repo.
    try:
        cfg = load_config()
        console.print(f"[green]✓[/] streamsnow.config.yaml valid (schema v{cfg.schema_version})")
    except ConfigError:
        console.print("[yellow]∘[/] no streamsnow.config.yaml here (run 'streamsnow init')")
    raise typer.Exit(code=0 if ok else 1)


@app.command(name="deploy-setup")
def deploy_setup() -> None:
    """Emit the one-time Snowflake DDL for your configured deploy source (Phase 2/3)."""
    console.print("streamsnow deploy-setup: " + _NOT_YET.format(phase="Phase 2/3"))


@app.command()
def update() -> None:
    """Re-vendor templates/tools and bump the Claude Code plugin (Phase 4)."""
    console.print("streamsnow update: " + _NOT_YET.format(phase="Phase 4"))


if __name__ == "__main__":  # pragma: no cover
    app()
