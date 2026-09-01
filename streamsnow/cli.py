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

import json
import re
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
    load_config,
)
from .deploy import generate_create_sql, generate_refresh_sql, generate_setup_sql, stage_path
from .scaffolder import APP_ITEMS, GOVERNANCE_ITEMS, REPO_ITEMS, render_item, scaffold
from .tools import doctor as _doctor
from .tools.app_nav import main as _app_nav_main
from .tools.check_app_security import main as _security_main
from .tools.check_artifacts import main as _artifacts_main
from .tools.check_bind_predicates import main as _bind_main
from .tools.check_branding_parity import main as _branding_parity_main
from .tools.check_caching import main as _caching_main
from .tools.check_dependency_vulns import main as _dependency_vulns_main
from .tools.check_page_imports import main as _page_imports_main
from .tools.check_path_leaks import main as _path_leaks_main
from .tools.check_requirements import main as _requirements_main
from .tools.check_schema_refs import main as _schema_refs_main
from .tools.check_session_fallback import main as _session_fallback_main
from .tools.check_sql_tokens import main as _sql_tokens_main
from .tools.check_tombstones import main as _tombstones_main
from .tools.migrate_app import main as _migrate_main
from .tools.preview_app import main as _preview_main
from .tools.review_gate import main as _review_gate_main
from .tools.review_loop import main as _review_loop_main
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


def _slugify(name: str) -> str:
    """Kebab-case a directory name into a usable project slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = re.sub(r"^[^a-z]+", "", slug)  # slugs must start with a letter ("2024-reports")
    return slug if _SLUG_RE.match(slug) else "my-dashboards"


def _deep_merge(base: dict, override: dict) -> dict:
    """New dict: ``override`` wins, nested dicts merge, ``base``-only keys survive.

    This is what makes re-running ``configure`` an edit rather than a restart:
    hand-edited keys the wizard doesn't ask about (``read_exceptions``,
    ``stage_name``, ``container_python``, …) carry into the rewritten file.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _prompt_config(prefill: dict | None = None, directory: Path | None = None) -> dict:
    """Interactive setup wizard: detect first, ask at most 5 questions.

    Only the values nothing can detect or default are asked — runtime, account,
    the governed database, the allowed schemas, and the deploy source.
    Everything else is written as a commented default in the config file (the
    file is the editing surface). When ``prefill`` is supplied (an existing
    config being updated), its values become the defaults everywhere — and the
    result is deep-merged over the prefill so hand-edited keys the wizard
    doesn't ask about survive the rewrite.
    """
    console.print(
        "[bold]StreamSnow setup[/] — 5 questions (Enter accepts the default);\n"
        "everything else is written as an editable, commented default.\n"
    )
    p = typer.prompt
    # Detected / defaulted (never asked; prefill wins so hand-edits survive).
    dir_slug = _slugify(directory.name) if directory is not None else "my-dashboards"
    slug = _pf(prefill, "project.slug", dir_slug)
    name = _pf(prefill, "project.name", slug.replace("-", " ").title())
    # The five questions.
    runtime = _prompt_choice("Runtime", RUNTIMES, _pf(prefill, "runtime", "container"))
    account = p(
        "Snowflake account locator (no .snowflakecomputing.com)",
        default=_pf(prefill, "snowflake.account", None),
    )
    gov_db = p(
        "Database your apps query", default=_pf(prefill, "governance.database", "ANALYTICS_DB")
    )
    allow = p(
        "Schemas apps may query (comma-separated)",
        default=",".join(_pf(prefill, "governance.schema_allow", ["ANALYTICS", "REPORTING"])),
    )
    source = _prompt_choice(
        "Deploy source", DEPLOY_SOURCES, _pf(prefill, "deploy.source", "stage-copy")
    )
    # Everything below ships as a commented default in the written file.
    app_db = _pf(prefill, "snowflake.objects.app_database", "DATA_APPS")
    app_schema = _pf(prefill, "snowflake.objects.app_schema", "BI_APPS")
    warehouse = _pf(prefill, "snowflake.objects.default_warehouse", "STREAMLIT_WH")
    objects: dict = {
        "app_database": app_db,
        "app_schema": app_schema,
        "stage_database": _pf(prefill, "snowflake.objects.stage_database", app_db),
        "stage_schema": _pf(prefill, "snowflake.objects.stage_schema", app_schema),
        "default_warehouse": warehouse,
        "allowed_warehouses": _pf(prefill, "snowflake.objects.allowed_warehouses", [warehouse]),
    }
    if runtime == "container":
        objects["compute_pool"] = _pf(prefill, "snowflake.objects.compute_pool", "STREAMLIT_POOL")
        objects["external_access_integration"] = _pf(
            prefill, "snowflake.objects.external_access_integration", "PYPI_ACCESS_INTEGRATION"
        )
    deploy: dict = {"source": source}
    if source == "git-repository":
        deploy["git_repository_fqn"] = _pf(
            prefill, "deploy.git_repository_fqn", f"{app_db}.{app_schema}.STREAMLIT_REPO"
        )
        deploy["git_branch"] = _pf(prefill, "deploy.git_branch", "main")
        deploy["api_integration_name"] = _pf(
            prefill, "deploy.api_integration_name", "GITHUB_API_INTEGRATION"
        )
        deploy["secret_name"] = _pf(
            prefill, "deploy.secret_name", f"{app_db}.{app_schema}.GITHUB_PAT_SECRET"
        )
        deploy["github_auth_mode"] = _pf(prefill, "deploy.github_auth_mode", GITHUB_AUTH_MODES[0])
    answers = {
        "schema_version": 1,
        "runtime": runtime,
        "project": {"name": name, "slug": slug},
        "snowflake": {
            "account": account,
            "connection_name": _pf(prefill, "snowflake.connection_name", slug),
            "objects": objects,
            "roles": {
                "ci_role": _pf(prefill, "snowflake.roles.ci_role", "STREAMLIT_CI_ROLE"),
                "viewer_role": _pf(prefill, "snowflake.roles.viewer_role", "STREAMLIT_APP_ROLE"),
            },
        },
        "governance": {
            "database": gov_db,
            "schema_allow": [s.strip() for s in allow.split(",") if s.strip()],
            "schema_deny": _pf(prefill, "governance.schema_deny", ["RAW", "STAGING"]),
        },
        "deploy": deploy,
    }
    return _deep_merge(prefill or {}, answers)


# Inline "when to change this" comments for the values the wizard defaults
# rather than asks. Rendered next to the value in the written YAML.
_DEFAULT_COMMENTS: dict[str, str] = {
    "project.name": "display name — edit freely",
    "project.slug": "derived from the directory name",
    "snowflake.connection_name": "snow CLI connection to create/use",
    "snowflake.objects.app_database": "where deployed STREAMLIT objects live",
    "snowflake.objects.app_schema": "schema for deployed STREAMLIT objects",
    "snowflake.objects.stage_database": "stage-copy deploys stage code here",
    "snowflake.objects.stage_schema": "schema for the deploy stage",
    "snowflake.objects.default_warehouse": "warehouse apps query with",
    "snowflake.objects.allowed_warehouses": "warehouses apps may use",
    "snowflake.objects.compute_pool": "container runtime only",
    "snowflake.objects.external_access_integration": "container: PyPI access during image build",
    "snowflake.roles.ci_role": "role the CI deploy runs as",
    "snowflake.roles.viewer_role": "role viewers (and local preview) use",
    "governance.schema_deny": "schemas apps must never query",
    "deploy.git_repository_fqn": "TODO: confirm before first deploy",
    "deploy.git_branch": "branch the deploy tracks",
    "deploy.api_integration_name": "TODO: confirm before first deploy",
    "deploy.secret_name": "TODO: confirm before first deploy",
    "deploy.github_auth_mode": "pat or github-app",
}


def _yaml_scalar(value: Any) -> str:
    """One YAML-safe scalar/flow value on a single line.

    ``width=inf`` stops PyYAML line-wrapping long flow lists (wrapping would
    truncate at the ``partition``); ``safe_dump`` of a bare scalar appends a
    ``...`` document-end marker on a second line — keep only the value line.
    """
    return yaml.safe_dump(
        value, default_flow_style=True, sort_keys=False, allow_unicode=True, width=float("inf")
    ).partition("\n")[0]


def _render_config_yaml(cfg_dict: dict) -> str:
    """Render config YAML with inline comments on the defaulted values.

    Comments make the file self-documenting: `configure` asks 5 questions and
    the rest is edited here. Falls back to plain YAML if the commented render
    ever fails to round-trip (defensive — comments must never corrupt config).
    """

    def walk(node: dict, path: str, indent: int) -> list[str]:
        lines: list[str] = []
        pad = "  " * indent
        for key, value in node.items():
            dotted = f"{path}.{key}" if path else key
            if isinstance(value, dict):
                lines.append(f"{pad}{key}:")
                lines.extend(walk(value, dotted, indent + 1))
            else:
                comment = _DEFAULT_COMMENTS.get(dotted)
                suffix = f"  # {comment}" if comment else ""
                lines.append(f"{pad}{key}: {_yaml_scalar(value)}{suffix}")
        return lines

    text = "\n".join(walk(cfg_dict, "", 0)) + "\n"
    try:
        round_trip = yaml.safe_load(text)
    except yaml.YAMLError:  # pragma: no cover - defensive
        round_trip = None
    if round_trip != cfg_dict:  # pragma: no cover - defensive
        return yaml.safe_dump(cfg_dict, sort_keys=False)
    return text


def _resolve_config(
    config: Path | None, prefill: dict | None, directory: Path | None = None
) -> tuple[Config, str]:
    """Return (validated Config, YAML text to persist). Raises ConfigError."""
    if config is not None:
        return load_config(config), Path(config).read_text()
    cfg_dict = _prompt_config(prefill, directory)
    return Config.from_dict(cfg_dict), _render_config_yaml(cfg_dict)


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

    Run after `streamsnow doctor` (machine setup) and before/around
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
        cfg, text = _resolve_config(config, prefill, target)
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
            cfg, text = _resolve_config(
                config, _read_prefill(cfg_out) if reconfigure else None, target
            )
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
def doctor(
    output_json: bool = typer.Option(False, "--json", help="Emit per-check JSON results."),
) -> None:
    """Check the local environment for the prerequisites StreamSnow needs.

    Each prerequisite is an independent sub-check with a machine-readable
    result ({name, ok, level, detail, hint}) so skills can shell out per
    check instead of prose-detecting. Exit 0 = all required checks pass,
    1 = a required check failed, 2 = tool error.
    """
    raise typer.Exit(code=_doctor.main(["--json"] if output_json else []))


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


@app.command(name="config-get")
def config_get(
    key: str = typer.Argument(..., help="Dotted config path, e.g. deploy.git_repository_fqn."),
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
) -> None:
    """Print a single config value by dotted path (used by the deploy workflow)."""
    try:
        cfg = load_config(Path(config) if config else None)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    cur: object = cfg.raw
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            _err(f"no config key {key!r}")
            raise typer.Exit(2)
        cur = cur[part]
    print(cur)


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
        sql = generate_refresh_sql(cfg, slug) if refresh else generate_create_sql(cfg, slug, sha)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    except ValueError as exc:  # invalid slug / sha
        _err(str(exc))
        raise typer.Exit(2) from exc
    print(sql)


@app.command(name="verify-deploy")
def verify_deploy_cmd(
    slug: str = typer.Argument(..., help="App slug to verify."),
    sha: str = typer.Option(None, "--sha", help="Expected commit SHA (stage-copy source check)."),
    attempts: int = typer.Option(3, "--attempts", help="Retries for cold-start absorption."),
    delay: float = typer.Option(20.0, "--delay", help="Seconds between retries."),
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """Verify a deployed app actually serves: object exists, live version set,
    version source matches the merge SHA, container logs show no crash loop."""
    from .verify import verify_app

    try:
        cfg = load_config(Path(config) if config else None)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    try:
        result = verify_app(cfg, slug, sha=sha, attempts=attempts, delay=delay)
    except ValueError as exc:  # invalid slug
        _err(str(exc))
        raise typer.Exit(2) from exc
    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        for c in result["checks"]:
            mark = "✓" if c["ok"] else "✗"
            print(f"  {mark} {c['name']}")
            for f in c["findings"]:
                print(f"      - {f}")
        print(f"\n{'PASS' if result['ok'] else 'FAIL'}: {result['app']}")
    raise typer.Exit(code=0 if result["ok"] else 1)


@app.command()
def update(
    directory: Path = typer.Option(Path("."), "--dir", help="Repo root."),
    apply: bool = typer.Option(False, "--apply", help="Write changes (default: dry-run)."),
) -> None:
    """Re-render governance files (AGENTS.md, hooks, CI, deploy) from your current
    config + installed StreamSnow templates. README and .gitignore are left alone.
    Dry-run by default; pass --apply to write."""
    target = directory.resolve()
    try:
        cfg = load_config(target / CONFIG_FILENAME)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc

    changed: list[str] = []
    for item in GOVERNANCE_ITEMS:
        if not item.when(cfg):
            continue
        out = target / item.output
        new = render_item(cfg, item, cfg.project.slug)
        old = out.read_text() if out.exists() else None
        if new != old:
            changed.append(item.output)
            if apply:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(new)

    if not changed:
        console.print("[green]✓[/] governance files already up to date")
    elif apply:
        console.print(f"[green]✓[/] updated {len(changed)} file(s): {', '.join(changed)}")
    else:
        console.print(
            "Would update:\n  " + "\n  ".join(changed) + "\n\nRe-run with --apply to write."
        )


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


@check_app.command("sql-tokens")
def check_sql_tokens_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs (default: apps/)."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """Flag {TOKEN} placeholders inside SQL comments (render_sql substitutes them)."""
    _run_check(_sql_tokens_main, paths, output_format)


@check_app.command("session-fallback")
def check_session_fallback_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs (default: apps/)."),
    output_format: str = typer.Option("md", "--format"),
    base_ref: str = typer.Option(
        None, "--base-ref", help="Flag only calls introduced vs this ref (default origin/main)."
    ),
    scan_all: bool = typer.Option(
        False, "--all", help="Tree-wide scan (legacy debt included) instead of new-only."
    ),
) -> None:
    """Require broad try/except around get_active_session() calls (new-only by default)."""
    argv = list(paths or ["apps"]) + ["--format", output_format]
    if base_ref:
        argv += ["--base-ref", base_ref]
    if scan_all:
        argv.append("--all")
    raise typer.Exit(code=_session_fallback_main(argv))


@check_app.command("page-imports")
def check_page_imports_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs (default: apps/)."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """Block imports that resolve under `streamlit run` but not in the deployed app."""
    _run_check(_page_imports_main, paths, output_format)


@check_app.command("artifacts")
def check_artifacts_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs (default: apps/)."),
    output_format: str = typer.Option("md", "--format"),
    fix: bool = typer.Option(
        False, "--fix", help="Repair each app's artifacts block from files on disk."
    ),
) -> None:
    """Cross-check snowflake.yml artifacts against files on disk."""
    argv = list(paths or ["apps"]) + ["--format", output_format]
    if fix:
        argv.append("--fix")
    raise typer.Exit(code=_artifacts_main(argv))


@check_app.command("path-leaks")
def check_path_leaks_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs (default: apps/)."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """Block personal absolute paths (home directories) in committed code and docs."""
    _run_check(_path_leaks_main, paths, output_format)


@check_app.command("requirements")
def check_requirements_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs (default: apps/)."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """Validate the REQUIREMENTS.md §11 build-state contract /start-app resumes from."""
    _run_check(_requirements_main, paths, output_format)


@check_app.command("branding-parity")
def check_branding_parity_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs (default: apps/)."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """Flag _BRANDING_VERSION skew across apps' branding.py copies."""
    _run_check(_branding_parity_main, paths, output_format)


@check_app.command("dependency-vulns")
def check_dependency_vulns_cmd(
    paths: list[str] = typer.Argument(None, help="Files/dirs (default: apps/)."),
    output_format: str = typer.Option("md", "--format"),
    allowlist: Path = typer.Option(
        None, "--allowlist", help="Allowlist JSON (default: osv_allowlist.json beside the config)."
    ),
    best_effort: bool = typer.Option(
        False, "--best-effort", help="Warn instead of failing when OSV.dev is unreachable."
    ),
) -> None:
    """Scan exact dependency pins against OSV.dev (range pins reported unscanned)."""
    argv = list(paths or ["apps"]) + ["--format", output_format]
    if allowlist is not None:
        argv += ["--allowlist", str(allowlist)]
    if best_effort:
        argv.append("--best-effort")
    raise typer.Exit(code=_dependency_vulns_main(argv))


@check_app.command("tombstones")
def check_tombstones_cmd(
    base_ref: str = typer.Option("origin/main", "--base-ref"),
    registry: Path = typer.Option(None, "--registry", help="Path to deploy/tombstones.yml."),
    drop_sql: bool = typer.Option(
        False, "--drop-sql", help="Emit DROP STREAMLIT IF EXISTS for tombstoned identifiers."
    ),
    apps_dir: Path = typer.Option(None, "--apps-dir", help="Apps directory (default: apps)."),
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """Block renames/removals that abandon a deployed STREAMLIT without a tombstone."""
    argv = ["--base-ref", base_ref, "--format", output_format]
    if registry is not None:
        argv += ["--registry", str(registry)]
    if apps_dir is not None:
        argv += ["--apps-dir", str(apps_dir)]
    if drop_sql:
        argv.append("--drop-sql")
    if config is not None:
        argv += ["--config", str(config)]
    raise typer.Exit(code=_tombstones_main(argv))


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


_PREVIEW_VERBS = {"start", "status", "stop", "logs"}


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def preview(ctx: typer.Context) -> None:
    """Run an app locally against live Snowflake (reads .streamlit/secrets.toml).

    Subcommands: start <slug> (background launch + health poll), status <slug>,
    stop <slug>, logs <slug>. A bare `streamsnow preview <slug>` is shorthand
    for `preview start <slug>` (compatibility with pre-0.6 usage).
    """
    argv = list(ctx.args)
    if argv and argv[0] not in _PREVIEW_VERBS and not argv[0].startswith("-"):
        argv = ["start", *argv]
    raise typer.Exit(code=_preview_main(argv))


@app.command(
    name="review-gate",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def review_gate_cmd(ctx: typer.Context) -> None:
    """Decide whether an app change needs review (classify | baseline | stamp | stop-hook)."""
    raise typer.Exit(code=_review_gate_main(list(ctx.args)))


@app.command(
    name="review-loop",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def review_loop_cmd(ctx: typer.Context) -> None:
    """Deterministic /review-app --auto primitives (parse-findings | dedup-findings |
    write-resolutions | exit-condition | merge-findings)."""
    raise typer.Exit(code=_review_loop_main(list(ctx.args)))


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def migrate(ctx: typer.Context) -> None:
    """Migration engine for /migrate-app (preflight | scan-hardfails | translate-deps |
    graft-plan | scan-imports | scan-conformance | scan-inline-sql)."""
    raise typer.Exit(code=_migrate_main(list(ctx.args)))


@app.command()
def nav(
    slug: str = typer.Argument(..., help="App slug (directory under apps/)."),
    directory: Path = typer.Option(Path("."), "--dir", help="Repo root."),
    json_array: bool = typer.Option(
        False, "--json-array", help="Emit one JSON array instead of JSONL."
    ),
) -> None:
    """Enumerate an app's pages (st.navigation / single-page / legacy pages-dir)."""
    argv = [str(directory / "apps" / slug)]
    if json_array:
        argv.append("--json-array")
    raise typer.Exit(code=_app_nav_main(argv))


if __name__ == "__main__":  # pragma: no cover
    app()
