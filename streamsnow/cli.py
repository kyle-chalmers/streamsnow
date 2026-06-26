"""StreamSnow command-line interface.

streamsnow configure      Set up / update streamsnow.config.yaml for your Snowflake env
streamsnow init           Configure + scaffold a governed repo + starter app
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
from typing import Any

import typer
import yaml
from rich.console import Console

from . import __version__
from .config import (
    CONFIG_FILENAME,
    DEPLOY_SOURCES,
    GITHUB_AUTH_MODES,
    RUNTIMES,
    Config,
    ConfigError,
    find_config,
    load_config,
)
from .deploy import generate_create_sql, generate_refresh_sql, generate_setup_sql, stage_path
from .scaffolder import APP_ITEMS, REPO_ITEMS, scaffold
from .tools.check_app_security import main as _security_main
from .tools.check_bind_predicates import main as _bind_main
from .tools.check_caching import main as _caching_main
from .tools.check_schema_refs import main as _schema_refs_main
from .tools.validate_app import main as _validate_app_main

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


def _pf(prefill: dict | None, dotted: str, fallback) -> Any:
    """Pull a default from an existing config dict (for idempotent re-config)."""
    cur: object = prefill or {}
    for key in dotted.split("."):
        if not isinstance(cur, dict):
            return fallback
        cur = cur.get(key)
    return cur if cur not in (None, "") else fallback


def _prompt_choice(label: str, choices: tuple[str, ...], default: str) -> str:
    """Prompt until the answer is one of ``choices`` (no end-of-wizard dead-end)."""
    while True:
        val = typer.prompt(f"{label} ({'/'.join(choices)})", default=default)
        if val in choices:
            return val
        console.print(f"[yellow]'{val}' must be one of {', '.join(choices)} — try again.[/]")


def _prompt_deploy(prefill: dict | None) -> dict:
    """Prompt for deploy config, including the dependent git-repository fields."""
    source = _prompt_choice(
        "Deploy source", DEPLOY_SOURCES, _pf(prefill, "deploy.source", "stage-copy")
    )
    deploy: dict = {"source": source}
    if source == "git-repository":
        p = typer.prompt
        deploy["git_repository_fqn"] = p(
            "Git repository FQN (DB.SCHEMA.NAME)",
            default=_pf(prefill, "deploy.git_repository_fqn", "DATA_APPS.BI_APPS.STREAMLIT_REPO"),
        )
        deploy["git_branch"] = p("Git branch", default=_pf(prefill, "deploy.git_branch", "main"))
        deploy["api_integration_name"] = p(
            "API integration name",
            default=_pf(prefill, "deploy.api_integration_name", "GITHUB_API_INTEGRATION"),
        )
        deploy["secret_name"] = p(
            "Secret FQN (DB.SCHEMA.NAME)",
            default=_pf(prefill, "deploy.secret_name", "DATA_APPS.BI_APPS.GITHUB_PAT_SECRET"),
        )
        deploy["github_auth_mode"] = _prompt_choice(
            "GitHub auth mode", GITHUB_AUTH_MODES, _pf(prefill, "deploy.github_auth_mode", "pat")
        )
    return deploy


def _prompt_config(prefill: dict | None = None) -> dict:
    """Interactive setup wizard. Returns a config dict (validated by caller).

    When ``prefill`` is supplied (an existing config being updated), its values
    become the prompt defaults — so re-running ``configure`` is an edit, not a
    restart.
    """
    console.print(
        "[bold]StreamSnow setup[/] — answer a few questions (Enter accepts the default).\n"
    )
    p = typer.prompt
    allow_default = ",".join(_pf(prefill, "governance.schema_allow", ["ANALYTICS", "REPORTING"]))
    deny_default = ",".join(_pf(prefill, "governance.schema_deny", ["RAW", "STAGING"]))
    runtime = _prompt_choice("Runtime", RUNTIMES, _pf(prefill, "runtime", "container"))
    name = p("Project name", default=_pf(prefill, "project.name", "My Dashboards"))
    slug = p("Project slug (kebab-case)", default=_pf(prefill, "project.slug", "my-dashboards"))
    account = p(
        "Snowflake account locator (no .snowflakecomputing.com)",
        default=_pf(prefill, "snowflake.account", None),
    )
    connection = p(
        "snow CLI connection name", default=_pf(prefill, "snowflake.connection_name", slug)
    )
    app_db = p(
        "Database for deployed STREAMLIT objects",
        default=_pf(prefill, "snowflake.objects.app_database", "DATA_APPS"),
    )
    app_schema = p(
        "Schema for deployed STREAMLIT objects",
        default=_pf(prefill, "snowflake.objects.app_schema", "BI_APPS"),
    )
    warehouse = p(
        "Query warehouse",
        default=_pf(prefill, "snowflake.objects.default_warehouse", "STREAMLIT_WH"),
    )
    ci_role = p(
        "CI deploy role", default=_pf(prefill, "snowflake.roles.ci_role", "STREAMLIT_CI_ROLE")
    )
    viewer_role = p(
        "App viewer role", default=_pf(prefill, "snowflake.roles.viewer_role", "STREAMLIT_APP_ROLE")
    )
    gov_db = p(
        "BI database your apps query", default=_pf(prefill, "governance.database", "ANALYTICS_DB")
    )
    allow = p("Allowed schemas (comma-separated)", default=allow_default)
    deny = p("Denied schemas (comma-separated)", default=deny_default)
    objects: dict = {
        "app_database": app_db,
        "app_schema": app_schema,
        "stage_database": _pf(prefill, "snowflake.objects.stage_database", app_db),
        "stage_schema": _pf(prefill, "snowflake.objects.stage_schema", app_schema),
        "default_warehouse": warehouse,
        "allowed_warehouses": [warehouse],
    }
    if runtime == "container":
        objects["compute_pool"] = p(
            "Compute pool (container)",
            default=_pf(prefill, "snowflake.objects.compute_pool", "STREAMLIT_POOL"),
        )
        objects["external_access_integration"] = p(
            "External access integration (container)",
            default=_pf(
                prefill, "snowflake.objects.external_access_integration", "PYPI_ACCESS_INTEGRATION"
            ),
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
        "deploy": _prompt_deploy(prefill),
    }


def _resolve_config(config: Path | None, prefill: dict | None) -> tuple[Config, str]:
    """Return (validated Config, YAML text to persist). Raises ConfigError."""
    if config is not None:
        return load_config(config), Path(config).read_text()
    cfg_dict = _prompt_config(prefill)
    return Config.from_dict(cfg_dict), yaml.safe_dump(cfg_dict, sort_keys=False)


def _read_prefill(cfg_out: Path) -> dict | None:
    if not cfg_out.exists():
        return None
    try:
        return yaml.safe_load(cfg_out.read_text())
    except yaml.YAMLError:
        return None


def _connection_hint(cfg: Config) -> str:
    return (
        f"snow connection add --connection-name {cfg.snowflake.connection_name} "
        f"--account {cfg.snowflake.account} --user <your_user> "
        f"--authenticator externalbrowser "
        f"--warehouse {cfg.snowflake.objects.default_warehouse} "
        f"--role {cfg.snowflake.roles.viewer_role}"
    )


@app.command()
def configure(
    directory: Path = typer.Option(Path("."), "--dir", help="Repo directory."),
    config: Path = typer.Option(None, "--config", help="Import an existing config file."),
) -> None:
    """Set up (or update) streamsnow.config.yaml for your Snowflake environment.

    Run after `streamsnow doctor`/onboard (machine setup) and before/around
    building apps. Idempotent: re-running prefills from the current config, so
    it's an edit rather than a restart. Writes no secrets.
    """
    target = directory.resolve()
    target.mkdir(parents=True, exist_ok=True)
    cfg_out = target / CONFIG_FILENAME
    prefill = _read_prefill(cfg_out) if config is None else None
    if prefill is not None:
        console.print(
            f"[dim]updating existing {CONFIG_FILENAME} (Enter keeps the current value)[/]"
        )
    try:
        cfg, text = _resolve_config(config, prefill)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    cfg_out.write_text(text)
    console.print(f"[green]✓[/] wrote {cfg_out}")
    console.print(
        "\nConnect your machine to Snowflake (one-time):\n"
        f"  {_connection_hint(cfg)}\n"
        "\nThen, per app, create local preview secrets (gitignored):\n"
        "  cp apps/<slug>/.streamlit/secrets.toml.example apps/<slug>/.streamlit/secrets.toml"
    )


@app.command()
def init(
    config: Path = typer.Option(None, "--config", help="Import an existing config file."),
    directory: Path = typer.Option(Path("."), "--dir", help="Target directory to scaffold into."),
    app_slug: str = typer.Option(
        "example-dashboard", "--app", help="Starter app slug (kebab-case)."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing scaffold files."),
    reconfigure: bool = typer.Option(
        False, "--reconfigure", help="Re-run the config wizard even if a config already exists."
    ),
) -> None:
    """Set up a governed repo with a starter app: configure + scaffold.

    Reuses an existing streamsnow.config.yaml unless --reconfigure/--config is
    given, so re-running init to add the scaffold is safe.
    """
    _validate_slug(app_slug)
    target = directory.resolve()
    target.mkdir(parents=True, exist_ok=True)
    cfg_out = target / CONFIG_FILENAME

    try:
        if cfg_out.exists() and config is None and not reconfigure:
            cfg = load_config(cfg_out)
            console.print(f"[dim]using existing {CONFIG_FILENAME}[/]")
        else:
            if cfg_out.exists() and not force and not reconfigure:
                _err(f"{cfg_out} already exists (use --reconfigure to edit, or --force).")
                raise typer.Exit(2)
            cfg, text = _resolve_config(config, _read_prefill(cfg_out) if reconfigure else None)
            cfg_out.write_text(text)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc

    try:
        # Repo-level files are idempotent (skipped if already present); per-app
        # files are guarded so re-scaffolding the same app needs --force.
        repo_written = scaffold(
            cfg, target, app_slug, items=REPO_ITEMS, force=force, skip_existing=True
        )
        app_written = scaffold(cfg, target, app_slug, items=APP_ITEMS, force=force)
    except FileExistsError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc

    written = repo_written + app_written
    console.print(f"[green]✓[/] scaffolded {len(written)} files into {target}")
    console.print(
        f"\nNext:\n"
        f"  1. streamsnow configure   (if you haven't set your Snowflake env yet)\n"
        f"  2. cp apps/{app_slug}/.streamlit/secrets.toml.example apps/{app_slug}/.streamlit/secrets.toml\n"
        f"  3. uv pip install streamsnow && pre-commit install\n"
        f"  4. streamlit run apps/{app_slug}/streamlit_app.py\n"
        f"  5. /plugin marketplace add kyle-chalmers/streamsnow  (in Claude Code)"
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
    # Config check, when run inside a StreamSnow repo. Distinguish a missing
    # config (fine — just not configured here) from a malformed one (a real
    # error that must not be masked).
    cfg_path = find_config()
    if cfg_path is None:
        console.print("[yellow]∘[/] no streamsnow.config.yaml here (run 'streamsnow configure')")
    else:
        try:
            cfg = load_config(cfg_path)
            console.print(f"[green]✓[/] {cfg_path.name} valid (schema v{cfg.schema_version})")
        except ConfigError as exc:
            console.print(f"[red]✗[/] {cfg_path} is invalid: {exc}")
            ok = False
    raise typer.Exit(code=0 if ok else 1)


@app.command(name="deploy-setup")
def deploy_setup(
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
) -> None:
    """Emit the one-time Snowflake DDL for your configured deploy source.

    Pipe to `snow sql --stdin` (with an admin/CI role) to create the stage (or
    the API integration + secret + git repository). Review before running.
    """
    try:
        cfg = load_config(Path(config) if config else None)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    print(generate_setup_sql(cfg))


@app.command(name="stage-path")
def stage_path_cmd(
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
) -> None:
    """Print the stage-copy base path (@DB.SCHEMA.STAGE) — used by the deploy workflow."""
    try:
        cfg = load_config(Path(config) if config else None)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    print(stage_path(cfg))


@app.command(name="deploy-sql")
def deploy_sql(
    slug: str = typer.Argument(..., help="App slug to deploy."),
    sha: str = typer.Option("<sha>", "--sha", help="Commit SHA (stage-copy path embeds it)."),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="git-repository: emit the ABORT/PULL/COMMIT refresh for an existing app.",
    ),
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
) -> None:
    """Emit the CREATE OR REPLACE STREAMLIT SQL for one app (used by the deploy workflow)."""
    try:
        cfg = load_config(Path(config) if config else None)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    print(generate_refresh_sql(cfg, slug) if refresh else generate_create_sql(cfg, slug, sha))


@app.command()
def update() -> None:
    """Re-vendor templates/tools and bump the Claude Code plugin (Phase 4)."""
    console.print("streamsnow update: " + _NOT_YET.format(phase="Phase 4"))


def _run_check(main_fn, paths: list[str] | None, output_format: str) -> None:
    raise typer.Exit(code=main_fn(list(paths or ["apps"]) + ["--format", output_format]))


@check_app.command("security")
def check_security_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs (default: apps/)."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """Block egress / code-exec / write-SQL / dynamic-SQL in app code."""
    _run_check(_security_main, paths, output_format)


@check_app.command("caching")
def check_caching_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs (default: apps/)."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """Require @st.cache_data(ttl=...) on data-fetching functions."""
    _run_check(_caching_main, paths, output_format)


@check_app.command("bind-predicates")
def check_bind_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs (default: apps/)."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """Block the `:N IS NULL OR` Go-driver bind-predicate trap."""
    _run_check(_bind_main, paths, output_format)


@app.command("validate-app")
def validate_app_cmd(
    slug: str = typer.Argument(..., help="App slug (directory under apps/)."),
    directory: Path = typer.Option(Path("."), "--dir", help="Repo root."),
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """PASS/FAIL preflight for one app — the deterministic ship gate."""
    argv = [slug, "--dir", str(directory), "--format", output_format]
    if config is not None:
        argv += ["--config", str(config)]
    raise typer.Exit(code=_validate_app_main(argv))


@app.command()
def preview(
    slug: str = typer.Argument(..., help="App slug to run locally."),
    directory: Path = typer.Option(Path("."), "--dir", help="Repo root."),
    port: int = typer.Option(8501, "--port"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the command without launching."),
) -> None:
    """Run an app locally against live Snowflake (reads .streamlit/secrets.toml)."""
    app_py = directory / "apps" / slug / "streamlit_app.py"
    if not app_py.is_file():
        _err(f"no entrypoint at {app_py}")
        raise typer.Exit(2)
    cmd = ["streamlit", "run", str(app_py), "--server.port", str(port)]
    if dry_run:
        console.print(" ".join(cmd))
        return
    if not shutil.which("streamlit"):
        _err(
            "streamlit not found — install it in this app's environment (uv pip install streamlit)."
        )
        raise typer.Exit(2)
    import subprocess

    raise typer.Exit(code=subprocess.call(cmd))


if __name__ == "__main__":  # pragma: no cover
    app()
