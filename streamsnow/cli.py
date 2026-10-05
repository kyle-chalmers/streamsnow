"""StreamSnow command-line interface.

streamsnow configure      Set up / update streamsnow.config.yaml for your Snowflake env
streamsnow init           Configure + scaffold a governed repo (+ starter app unless
                          --no-starter-app)
streamsnow new            Scaffold another app in an existing StreamSnow repo
streamsnow doctor         Check the local environment for prerequisites
streamsnow check ...      Run a governance check (e.g. schema-refs)
streamsnow deploy-setup   Emit the one-time Snowflake DDL for your deploy source
                          (--admin: full bootstrap; --teardown: start-fresh reverse)
streamsnow ci-key create  Make the CI user's key pair + the deploy secret files
streamsnow ci-key push    Set the five deploy secrets on GitHub from those files
streamsnow update         Re-vendor templates/tools and bump the plugin
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console

from . import __version__
from . import ci_key as _ci_key
from .agent_skills import main as _agent_skills_main
from .config import (
    CONFIG_FILENAME,
    DEPLOY_SOURCES,
    GITHUB_AUTH_MODES,
    RUNTIMES,
    Config,
    ConfigError,
    load_config,
    normalize_account,
    validate_github_origin,
)
from .deploy import (
    ci_user_name,
    generate_admin_sql,
    generate_create_sql,
    generate_setup_sql,
    generate_teardown_sql,
    read_public_key,
    stage_path,
    with_source,
)
from .scaffolder import (
    APP_ITEMS,
    CREATE_IF_MISSING_ITEMS,
    GOVERNANCE_ITEMS,
    REPO_ITEMS,
    missing_repo_files,
    render_item,
    scaffold,
)
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
from .tools.preview_app import local_install_command
from .tools.preview_app import main as _preview_main
from .tools.review_gate import main as _review_gate_main
from .tools.review_loop import main as _review_loop_main
from .tools.sql_review import main as _sql_review_main
from .tools.validate_app import main as _validate_app_main


def _utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 when the platform default is not.

    Windows pipes default to the ANSI code page (cp1252), which cannot encode the
    ✓/✗ marks, arrows and dashes the CLI prints, so ``streamsnow validate-app`` or
    ``doctor`` crashed with UnicodeEncodeError the moment its output went to a pipe,
    which is how agents and pre-commit always run it. This runs at import, before
    Typer renders even ``--help``. A no-op wherever the stream is already UTF-8
    (macOS, Linux, the Windows console).
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")


_utf8_stdio()

app = typer.Typer(
    name="streamsnow",
    help="Build, govern, and ship Streamlit-in-Snowflake apps with Claude Code.",
    no_args_is_help=True,
    add_completion=False,
)
check_app = typer.Typer(help="Run a governance check (config-driven).", no_args_is_help=True)
app.add_typer(check_app, name="check")
ci_key_app = typer.Typer(
    help="Create the CI service user's key pair and the deploy secret files.",
    no_args_is_help=True,
)
app.add_typer(ci_key_app, name="ci-key")
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


def _snow_connections() -> list[dict] | None:
    """``snow connection list`` rows (None when snow is missing or broken).

    One indirection so the test suite can keep the wizard off the developer's
    real ``snow`` (tests/conftest.py stubs it).
    """
    return _doctor.snow_connections()


def _detect_connection_name(account: str, slug: str) -> str:
    """The default ``snow`` connection's name when it opens ``account``, else ``slug``.

    An existing default connection (a prior tutorial, another project) is what
    ``st.connection("snowflake")`` already reads locally, so adopting it spares
    those users a second ``--default`` connection. Adopting it without comparing
    accounts wrote a config whose connection opens some other account, so the
    default is used only when its ``account`` parameter matches the answer
    (case-insensitive). Neither account value is ever printed.
    """
    rows = _snow_connections()
    detected = _doctor.default_connection_name(rows)
    if not detected:
        return slug
    row = _doctor.default_connection(rows) or {}
    params = row.get("parameters") if isinstance(row.get("parameters"), dict) else {}
    theirs = params.get("account")
    if isinstance(theirs, str) and theirs.strip().casefold() == account.strip().casefold():
        console.print(f"[dim]using your default snow connection {detected!r}[/]")
        return detected
    why = "names no account" if not theirs else "is for another account"
    console.print(
        f"[dim]default snow connection {detected!r} {why}, so connection_name is {slug!r}[/]"
    )
    return slug


def _split_schemas(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


# The five wizard questions, as the keys ``_prompt_config``'s ``given`` accepts
# (``deny_schemas`` and ``connection`` are extra answers the wizard never asks).
_QUESTIONS = ("runtime", "account", "database", "schemas", "deploy_source")


def _prompt_config(
    prefill: dict | None = None, directory: Path | None = None, given: dict | None = None
) -> dict:
    """Setup wizard: detect first, ask at most 5 questions.

    Only the values nothing can detect or default are asked — runtime, account,
    the governed database, the allowed schemas, and the deploy source.
    Everything else is written as a commented default in the config file (the
    file is the editing surface). When ``prefill`` is supplied (an existing
    config being updated), its values become the defaults everywhere — and the
    result is deep-merged over the prefill so hand-edited keys the wizard
    doesn't ask about survive the rewrite.

    ``given`` holds answers passed as flags (``_QUESTIONS`` plus
    ``deny_schemas`` and ``connection``); each one replaces its prompt, so the
    same answers build the same dict whether typed or passed. All five given
    means no prompt fires (the non-interactive path ``/onboard`` uses).
    """
    given = {k: v for k, v in (given or {}).items() if v is not None}
    left = sum(1 for q in _QUESTIONS if q not in given)
    if left == len(_QUESTIONS):
        console.print(
            "[bold]StreamSnow setup[/] — 5 questions (Enter accepts the default);\n"
            "everything else is written as an editable, commented default.\n"
        )
    elif left:
        console.print(
            f"[bold]StreamSnow setup:[/] {5 - left} answers from flags, {left} "
            "question(s) left (Enter accepts the default).\n"
        )
    else:
        console.print(
            "[bold]StreamSnow setup:[/] all 5 answers from flags; everything else is\n"
            "written as an editable, commented default.\n"
        )
    p = typer.prompt
    # Detected / defaulted (never asked; prefill wins so hand-edits survive).
    dir_slug = _slugify(directory.name) if directory is not None else "my-dashboards"
    slug = _pf(prefill, "project.slug", dir_slug)
    name = _pf(prefill, "project.name", slug.replace("-", " ").title())
    # The five questions.
    runtime = given.get("runtime") or _prompt_choice(
        "Runtime", RUNTIMES, _pf(prefill, "runtime", "container")
    )
    account = given.get("account") or p(
        "Snowflake account locator (no .snowflakecomputing.com)",
        default=_pf(prefill, "snowflake.account", None),
    )
    # The snow connection: the one named by --connection, else the default one when
    # it opens this account, else the slug (see _detect_connection_name). snow is
    # only asked when the config does not already name a connection; missing or
    # broken snow falls back to the slug.
    connection_name = given.get("connection") or _pf(prefill, "snowflake.connection_name", None)
    if connection_name is None:
        connection_name = _detect_connection_name(account, slug)
    gov_db = given.get("database") or p(
        "Database your apps query", default=_pf(prefill, "governance.database", "ANALYTICS_DB")
    )
    allow = given.get("schemas") or p(
        "Schemas apps may query (comma-separated)",
        default=",".join(_pf(prefill, "governance.schema_allow", ["ANALYTICS", "REPORTING"])),
    )
    source = given.get("deploy_source") or _prompt_choice(
        "Deploy source", DEPLOY_SOURCES, _pf(prefill, "deploy.source", "stage-copy")
    )
    if "deny_schemas" in given:
        deny = _split_schemas(given["deny_schemas"])
    else:
        deny = _pf(prefill, "governance.schema_deny", ["RAW", "STAGING"])
    if given:
        # Flags can meet a default or prefilled list they never named (--schemas RAW
        # against the RAW,STAGING default), so check the lists that will be written.
        both = {s.upper() for s in _split_schemas(allow)} & {s.upper() for s in deny}
        if both:
            raise ConfigError(
                f"schema(s) {', '.join(sorted(both))} are both allowed and denied; "
                "drop them from --schemas or set --deny-schemas."
            )
    # Everything below ships as a commented default in the written file.
    app_db = _pf(prefill, "snowflake.objects.app_database", "STREAMSNOW_APPS")
    app_schema = _pf(prefill, "snowflake.objects.app_schema", "DASHBOARDS")
    warehouse = _pf(prefill, "snowflake.objects.default_warehouse", "STREAMSNOW_WH")
    objects: dict = {
        "app_database": app_db,
        "app_schema": app_schema,
        "stage_database": _pf(prefill, "snowflake.objects.stage_database", app_db),
        "stage_schema": _pf(prefill, "snowflake.objects.stage_schema", app_schema),
        "default_warehouse": warehouse,
        "allowed_warehouses": _pf(prefill, "snowflake.objects.allowed_warehouses", [warehouse]),
    }
    if runtime == "container":
        objects["compute_pool"] = _pf(
            prefill, "snowflake.objects.compute_pool", "SYSTEM_COMPUTE_POOL_CPU"
        )
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
            "connection_name": connection_name,
            "objects": objects,
            "roles": {
                "ci_role": _pf(prefill, "snowflake.roles.ci_role", "STREAMSNOW_DEPLOY_ROLE"),
                "viewer_role": _pf(
                    prefill, "snowflake.roles.viewer_role", "STREAMSNOW_VIEWER_ROLE"
                ),
            },
        },
        "governance": {
            "database": gov_db,
            "schema_allow": _split_schemas(allow),
            "schema_deny": deny,
        },
        "deploy": deploy,
    }
    return _deep_merge(prefill or {}, answers)


# Inline "when to change this" comments for the values the wizard defaults
# rather than asks. Rendered next to the value in the written YAML.
_DEFAULT_COMMENTS: dict[str, str] = {
    "project.name": "display name — edit freely",
    "project.slug": "derived from the directory name",
    "snowflake.connection_name": "snow CLI connection (your default one when it is this account)",
    "snowflake.objects.app_database": "where deployed STREAMLIT objects live",
    "snowflake.objects.app_schema": "schema for deployed STREAMLIT objects",
    "snowflake.objects.stage_database": "stage-copy deploys stage code here",
    "snowflake.objects.stage_schema": "schema for the deploy stage",
    "snowflake.objects.default_warehouse": "warehouse apps query with",
    "snowflake.objects.allowed_warehouses": "warehouses apps may use",
    "snowflake.objects.compute_pool": "container only; SYSTEM_COMPUTE_POOL_CPU is pre-provisioned",
    "snowflake.objects.external_access_integration": "container: PyPI access during image build",
    "snowflake.roles.ci_role": "role the CI deploy runs as",
    "snowflake.roles.viewer_role": "role viewers (and local preview) use",
    "governance.schema_deny": "schemas apps must never query",
    "deploy.git_repository_fqn": "TODO: confirm before first deploy",
    "deploy.git_branch": "branch the deploy tracks",
    "deploy.api_integration_name": "TODO: confirm before first deploy",
    "deploy.secret_name": "TODO: confirm before first deploy",
    "deploy.github_auth_mode": "pat | github-app | public (a public repo needs no token)",
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
    config: Path | None,
    prefill: dict | None,
    directory: Path | None = None,
    given: dict | None = None,
) -> tuple[Config, str]:
    """Return (validated Config, YAML text to persist). Raises ConfigError."""
    if config is not None:
        return load_config(config), Path(config).read_text(encoding="utf-8")
    cfg_dict = _prompt_config(prefill, directory, given)
    return Config.from_dict(cfg_dict), _render_config_yaml(cfg_dict)


# Help text for the answer flags, shared by `init` and `configure`.
_ANSWER_HELP = {
    "runtime": f"Wizard answer: runtime ({' | '.join(RUNTIMES)}).",
    "account": "Wizard answer: Snowflake account locator (no .snowflakecomputing.com).",
    "connection": "Read the account from this snow connection (never printed) and use it "
    "as snowflake.connection_name. Instead of --account.",
    "database": "Wizard answer: the database your apps query (governance.database).",
    "schemas": "Wizard answer: schemas apps may query, comma-separated (governance.schema_allow).",
    "deny_schemas": "Schemas apps must never query, comma-separated (governance.schema_deny; "
    "default RAW,STAGING; '' denies none).",
    "deploy_source": f"Wizard answer: deploy source ({' | '.join(DEPLOY_SOURCES)}).",
}


def _account_from_connection(name: str) -> str:
    """The account locator a ``snow`` connection opens. Raises ConfigError.

    Errors name connections, never an account value: the point of
    ``--connection`` is that the locator never reaches the screen.
    """
    rows = _snow_connections()
    if rows is None:
        raise ConfigError(
            f"--connection {name}: could not read `snow connection list` (snow missing or "
            "broken); pass --account instead or answer the wizard's prompt."
        )
    row = next((r for r in rows if (r.get("connection_name") or r.get("name")) == name), None)
    if row is None:
        known = ", ".join(sorted(str(r.get("connection_name") or r.get("name")) for r in rows))
        raise ConfigError(
            f"--connection {name}: no snow connection by that name (known: {known or 'none'})."
        )
    params = row.get("parameters") if isinstance(row.get("parameters"), dict) else {}
    account = params.get("account")
    if not isinstance(account, str) or not account.strip():
        raise ConfigError(
            f"--connection {name}: that snow connection names no account; pass --account instead."
        )
    try:
        return normalize_account(account)
    except ConfigError:
        raise ConfigError(
            f"--connection {name}: its account is not a valid locator; pass --account instead."
        ) from None


def _flag_answers(
    *,
    runtime: str | None,
    account: str | None,
    connection: str | None,
    database: str | None,
    schemas: str | None,
    deny_schemas: str | None,
    deploy_source: str | None,
    config: Path | None,
) -> dict:
    """Validate the answer flags into ``_prompt_config``'s ``given``. Raises ConfigError.

    Everything is checked before the wizard runs, so a bad flag never leaves a
    half-asked wizard or a written file behind.
    """
    given = {
        "runtime": runtime,
        "account": account,
        "connection": connection,
        "database": database,
        "schemas": schemas,
        "deny_schemas": deny_schemas,
        "deploy_source": deploy_source,
    }
    given = {k: v for k, v in given.items() if v is not None}
    if not given:
        return {}
    if config is not None:
        raise ConfigError("--config imports a whole file; it cannot be combined with answer flags.")
    if runtime is not None and runtime not in RUNTIMES:
        raise ConfigError(f"--runtime must be one of {', '.join(RUNTIMES)} (got {runtime!r}).")
    if deploy_source is not None and deploy_source not in DEPLOY_SOURCES:
        raise ConfigError(
            f"--deploy-source must be one of {', '.join(DEPLOY_SOURCES)} (got {deploy_source!r})."
        )
    if database is not None and not database.strip():
        raise ConfigError("--database is empty.")
    if schemas is not None and not _split_schemas(schemas):
        raise ConfigError("--schemas must name at least one schema.")
    if account is not None and connection is not None:
        raise ConfigError("pass --account or --connection, not both.")
    if account is not None:
        try:
            normalize_account(account)
        except ConfigError:
            raise ConfigError(
                "--account must be an account locator such as ab12345.us-east-1 "
                "(no .snowflakecomputing.com)."
            ) from None
    if schemas is not None and deny_schemas is not None:
        both = {s.upper() for s in _split_schemas(schemas)} & {
            s.upper() for s in _split_schemas(deny_schemas)
        }
        if both:
            raise ConfigError(
                f"schema(s) {', '.join(sorted(both))} are both allowed and denied; "
                "drop them from --schemas or --deny-schemas."
            )
    if connection is not None:
        given["account"] = _account_from_connection(connection)
    return given


def _read_prefill(cfg_out: Path) -> dict | None:
    if not cfg_out.exists():
        return None
    try:
        return yaml.safe_load(cfg_out.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None


PREVIEW_ROLE_NOTE = (
    "Local preview reads data with this role. By default deploy-setup --admin gives the\n"
    "viewer role no data grants (deployed apps read with the CI role's rights), so either\n"
    "uncomment its opt-in viewer data grants or swap --role for a role with the CI role's reads."
)


def _connection_hint(cfg: Config, rows: list[dict] | None = None) -> str:
    """The one-time connection step, given what ``snow connection list`` reported.

    Only a machine with no connection by the configured name gets the
    ``snow connection add ... --default`` line: re-adding an existing default
    would fail or, worse, repoint every tool that reads the default.
    """
    name = cfg.snowflake.connection_name
    if _doctor.default_connection_name(rows) == name:
        return (
            f"snow connection {name!r} is already your default connection: nothing to add "
            "(st.connection('snowflake') reads it locally)"
        )
    if any((r.get("connection_name") or r.get("name")) == name for r in rows or []):
        return (
            f"snow connection set-default {name}   (it exists but is not the default, and "
            "st.connection('snowflake') reads the default locally)"
        )
    return (
        f"snow connection add --connection-name {name} "
        f"--account {cfg.snowflake.account} --user <your_user> "
        f"--authenticator externalbrowser "
        f"--warehouse {cfg.snowflake.objects.default_warehouse} "
        f"--role {cfg.snowflake.roles.viewer_role} --default"
    )


@app.command()
def configure(
    directory: Path = typer.Option(Path("."), "--dir", help="Repo directory."),
    config: Path = typer.Option(None, "--config", help="Import an existing config file."),
    runtime: str = typer.Option(None, "--runtime", help=_ANSWER_HELP["runtime"]),
    account: str = typer.Option(None, "--account", help=_ANSWER_HELP["account"]),
    connection: str = typer.Option(None, "--connection", help=_ANSWER_HELP["connection"]),
    database: str = typer.Option(None, "--database", help=_ANSWER_HELP["database"]),
    schemas: str = typer.Option(None, "--schemas", help=_ANSWER_HELP["schemas"]),
    deny_schemas: str = typer.Option(None, "--deny-schemas", help=_ANSWER_HELP["deny_schemas"]),
    deploy_source: str = typer.Option(None, "--deploy-source", help=_ANSWER_HELP["deploy_source"]),
) -> None:
    """Set up (or update) streamsnow.config.yaml for your Snowflake environment.

    Run after `streamsnow doctor` (machine setup) and before/around
    building apps. Idempotent: re-running prefills from the current config, so
    it's an edit rather than a restart. Writes no secrets. The answer flags
    (--runtime, --account or --connection, --database, --schemas,
    --deploy-source, plus --deny-schemas) skip their questions; all five skip
    the prompts entirely.
    """
    target = directory.resolve()
    target.mkdir(parents=True, exist_ok=True)
    cfg_out = target / CONFIG_FILENAME
    try:
        given = _flag_answers(
            runtime=runtime,
            account=account,
            connection=connection,
            database=database,
            schemas=schemas,
            deny_schemas=deny_schemas,
            deploy_source=deploy_source,
            config=config,
        )
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    prefill = _read_prefill(cfg_out) if config is None else None
    if prefill is not None:
        console.print(
            f"[dim]updating existing {CONFIG_FILENAME} (Enter keeps the current value)[/]"
        )
    try:
        cfg, text = _resolve_config(config, prefill, target, given)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    cfg_out.write_text(text, encoding="utf-8")
    console.print(f"[green]✓[/] wrote {cfg_out}")
    console.print(
        "\nConnect your machine to Snowflake (one-time, one store — the snow CLI's\n"
        "connections.toml is what st.connection('snowflake') reads locally):\n"
        f"  {_connection_hint(cfg, _snow_connections())}\n"
        f"{PREVIEW_ROLE_NOTE}\n"
        "\nPer-app apps/<slug>/.streamlit/secrets.toml (gitignored) is an optional override —\n"
        "copy secrets.toml.example only if an app needs a different role or warehouse."
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
    no_starter_app: bool = typer.Option(
        False,
        "--no-starter-app",
        help="Write the governed repo files (AGENTS.md, hooks, CI, .gitignore, README, "
        "tombstones) without the example app. The setup path for /onboard.",
    ),
    runtime: str = typer.Option(None, "--runtime", help=_ANSWER_HELP["runtime"]),
    account: str = typer.Option(None, "--account", help=_ANSWER_HELP["account"]),
    connection: str = typer.Option(None, "--connection", help=_ANSWER_HELP["connection"]),
    database: str = typer.Option(None, "--database", help=_ANSWER_HELP["database"]),
    schemas: str = typer.Option(None, "--schemas", help=_ANSWER_HELP["schemas"]),
    deny_schemas: str = typer.Option(None, "--deny-schemas", help=_ANSWER_HELP["deny_schemas"]),
    deploy_source: str = typer.Option(None, "--deploy-source", help=_ANSWER_HELP["deploy_source"]),
) -> None:
    """Set up a governed repo: configure + repo files + a starter app.

    Reuses an existing streamsnow.config.yaml unless --reconfigure/--config is
    given, so re-running init to add the scaffold is safe. Repo-level files that
    already exist are left alone. --no-starter-app skips the example app, which
    is what `/onboard` runs before `streamsnow new` builds the real one.
    The answer flags (see `configure`) replace the wizard's questions; on an
    existing config they need --reconfigure, so they are never silently ignored.
    """
    if no_starter_app:
        app_slug = ""
    else:
        _validate_slug(app_slug)
    target = directory.resolve()
    target.mkdir(parents=True, exist_ok=True)
    cfg_out = target / CONFIG_FILENAME

    try:
        given = _flag_answers(
            runtime=runtime,
            account=account,
            connection=connection,
            database=database,
            schemas=schemas,
            deny_schemas=deny_schemas,
            deploy_source=deploy_source,
            config=config,
        )
        if given and cfg_out.exists() and not reconfigure:
            _err(
                f"{cfg_out} already exists, so these answers would be ignored: add "
                "--reconfigure to apply them (or drop the flags to reuse the file as is)."
            )
            raise typer.Exit(2)
        if cfg_out.exists() and config is None and not reconfigure:
            cfg = load_config(cfg_out)
            console.print(f"[dim]using existing {CONFIG_FILENAME}[/]")
        else:
            if cfg_out.exists() and not force and not reconfigure:
                _err(f"{cfg_out} already exists (use --reconfigure to edit, or --force).")
                raise typer.Exit(2)
            cfg, text = _resolve_config(
                config, _read_prefill(cfg_out) if reconfigure else None, target, given
            )
            cfg_out.write_text(text, encoding="utf-8")
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc

    try:
        # Repo-level files are idempotent (skipped if already present); per-app
        # files are guarded so re-scaffolding the same app needs --force.
        repo_written = scaffold(
            cfg, target, app_slug, items=REPO_ITEMS, force=force, skip_existing=True
        )
        app_written = (
            [] if no_starter_app else scaffold(cfg, target, app_slug, items=APP_ITEMS, force=force)
        )
    except FileExistsError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc

    written = repo_written + app_written
    # Render the starter app's audit trail so the sql_review pattern is live
    # from commit 1 (static manifest — deterministic, no app imports).
    if app_written and _sql_review_main(["generate", app_slug, "--dir", str(target)]) != 0:
        console.print("[yellow]∘[/] sql_review companion generation failed — see error above")
    # soft_wrap: commands in these blocks must stay copy-pasteable on one line.
    console.print(f"[green]✓[/] scaffolded {len(written)} files into {target}", soft_wrap=True)
    console.print(_init_next_steps(cfg, target, app_slug or None), soft_wrap=True)


def _init_next_steps(cfg: Config, target: Path, app_slug: str | None) -> str:
    """The closing Next: block. The plugin comes first, matching docs Path B;
    CLI-only users skip that step."""
    lines = [
        "",
        "Next:",
        "  1. Claude Code users: /plugin marketplace add kyle-chalmers/streamsnow",
        "                        /plugin install streamsnow@streamsnow   then /onboard",
        "     (CLI only? skip this step. Codex: streamsnow agent-skills install --agent codex)",
        f"  2. {_connection_hint(cfg, _snow_connections())}",
        "     (one-time; st.connection('snowflake') reads this default connection locally.",
        "      Per-app apps/<slug>/.streamlit/secrets.toml is an optional override.)",
        *[f"     {line}" for line in PREVIEW_ROLE_NOTE.splitlines()],
        "  3. uv tool install pre-commit && pre-commit install   (the governance hooks)",
    ]
    if app_slug is None:
        lines += [
            "  4. streamsnow new <domain> <function>   (or /start-app) to scaffold your first app",
            "  One-time Snowflake objects for the first deploy: streamsnow deploy-setup --admin",
            "  (review it, then hand it to your Snowflake admin). streamsnow ci-key create",
            "  makes the CI key pair; pass its .pub to deploy-setup --admin --public-key-file.",
        ]
        return "\n".join(lines)
    install = local_install_command(target / "apps" / app_slug)
    lines += [
        f"  4. Replace the starter placeholders in apps/{app_slug}: queries/example_metric.sql",
        "     and the window in sql_review/manifests/example_metric.json read YOUR_TABLE, and",
        "     pages/overview.py shows sample numbers. validate-app FAILS until they are gone.",
        f"  5. streamsnow validate-app {app_slug}   (PASS once step 4 is done)",
        f"  6. {install}",
        f"     streamsnow preview {app_slug}",
        "  Add the app to README.md's Apps table.",
    ]
    return "\n".join(lines)


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
    if _sql_review_main(["generate", slug, "--dir", str(Path.cwd())]) != 0:
        console.print("[yellow]∘[/] sql_review companion generation failed — see error above")
    console.print(f"[green]✓[/] created app {slug} ({len(written)} files)")
    # `new` writes app files only. A repo set up with `configure` alone (the
    # pre-0.7.1 plugin setup path) has no .gitignore, hooks or CI: warn loudly,
    # because without .gitignore an app's .streamlit/secrets.toml can be committed.
    missing = missing_repo_files(cfg, Path.cwd())
    if missing:
        console.print(
            "[yellow]warning:[/] this repo is missing StreamSnow's governed repo files: "
            f"{', '.join(missing)}.\n"
            "  Without them there are no pre-commit hooks or CI checks, and nothing "
            "gitignores .streamlit/secrets.toml.\n"
            "  Fix: streamsnow init --no-starter-app   (reuses your config; writes only the "
            "missing files)"
        )
    console.print(
        "The starter files are placeholders: queries/example_metric.sql, its sql_review "
        "manifest and pages/overview.py (sample numbers). Replace them with your real pages "
        "and queries (/start-app does this in its build phase); validate-app FAILS while any "
        "file still reads YOUR_TABLE."
    )
    # The runtime-matched install, so the first local preview works. markup off:
    # pip extras in brackets (pkg[extra]) are not Rich markup.
    console.print(
        "Install the app's packages for local preview: "
        f"{local_install_command(Path.cwd() / 'apps' / slug)}  "
        "(if this repo already has a .venv, run only the part after &&)",
        markup=False,
        highlight=False,
        soft_wrap=True,  # never break the command mid-line; it gets pasted
    )
    console.print(
        f"Next: streamsnow validate-app {slug}, then add {slug} to README.md's Apps table "
        "(the index is hand-maintained and the row is the step teams forget)."
    )


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
    output_format: str = typer.Option(
        "md", "--format", help="md | json (package-wide check contract; --json is an alias)."
    ),
) -> None:
    """Check the local environment for the prerequisites StreamSnow needs.

    Each prerequisite is an independent sub-check with a machine-readable
    result ({name, ok, level, detail, hint}) so skills can shell out per
    check instead of prose-detecting. Exit 0 = all required checks pass,
    1 = a required check failed, 2 = tool error.
    """
    argv = ["--format", output_format]
    if output_json:
        argv.append("--json")
    raise typer.Exit(code=_doctor.main(argv))


@app.command(name="deploy-setup")
def deploy_setup(
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
    admin: bool = typer.Option(
        False,
        "--admin",
        help="Emit the full one-time admin bootstrap: database, schema, warehouse, roles, "
        "CI service user, grants, and container/git account objects.",
    ),
    source: str = typer.Option(
        None,
        "--source",
        help="Print the setup for this deploy source (stage-copy | git-repository) instead "
        "of the one in your config, to review before switching. Changes nothing.",
    ),
    git_origin: str = typer.Option(
        None,
        "--git-origin",
        help="git-repository: the repo's HTTPS URL (default: deploy.git_origin, else this "
        "checkout's GitHub origin remote).",
    ),
    github_auth: str = typer.Option(
        None,
        "--github-auth",
        help="git-repository: pat | github-app | public (a public repo needs no token).",
    ),
    public_key_file: Path = typer.Option(
        None,
        "--public-key-file",
        help="With --admin: the CI user's PEM public key (e.g. from `streamsnow ci-key "
        "create`), filled into RSA_PUBLIC_KEY and re-applied on a re-run.",
    ),
    viewer_users: list[str] = typer.Option(
        None,
        "--viewer-user",
        help="With --admin: also grant the viewer role to this user (repeatable). The user "
        "running the script always gets it.",
    ),
    teardown: bool = typer.Option(
        False,
        "--teardown",
        help="Print (never run) the reverse of --admin: DROP the app database, warehouse, "
        "roles, CI user and integrations, to start fresh. Keeps the governance database.",
    ),
) -> None:
    """Emit the one-time Snowflake DDL for your configured deploy source.

    Pipe to `snow sql --stdin` (with an admin/CI role) to create the stage (or
    the API integration + secret + git repository). Review before running.
    With --admin, emit everything a first deploy needs, in USE ROLE sections
    (SYSADMIN, USERADMIN, SECURITYADMIN, ACCOUNTADMIN, then the CI role): the
    block to hand a Snowflake admin when you cannot create these yourself.
    With --teardown, emit the reviewable reverse, to uninstall or start fresh.
    """
    if teardown and admin:
        _err("--teardown and --admin are separate scripts: pass one of them.")
        raise typer.Exit(2)
    if (public_key_file or viewer_users) and not admin:
        _err("--public-key-file and --viewer-user only apply to --admin.")
        raise typer.Exit(2)
    try:
        cfg = load_config(Path(config) if config else None)
        configured = cfg.deploy.source
        if source or git_origin or github_auth:
            target = source or configured
            origin = git_origin
            if target == "git-repository" and not origin and not cfg.deploy.git_origin:
                origin = _checkout_github_origin()
            cfg = with_source(cfg, target, git_origin=origin, github_auth=github_auth)
        if teardown:
            sql = generate_teardown_sql(cfg)
        elif admin:
            key = read_public_key(public_key_file) if public_key_file else None
            sql = generate_admin_sql(cfg, public_key=key, viewer_users=viewer_users or ())
        else:
            sql = generate_setup_sql(cfg)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    if cfg.deploy.source != configured:
        sql = (
            f"-- PREVIEW of the {cfg.deploy.source} deploy source. Your config uses {configured};\n"
            "-- nothing here takes effect until you switch deploy.source (`streamsnow configure`)\n"
            "-- and re-render the deploy workflow (`streamsnow update --apply`). See\n"
            "-- docs/git-repository.md. Review only: this command never runs SQL.\n" + sql
        )
    print(sql)


@ci_key_app.command(name="create")
def ci_key_create(
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
    directory: Path = typer.Option(
        _ci_key.DEFAULT_DIR,
        "--dir",
        help="Where to write the key pair and secrets/ (keep it OUTSIDE the repo).",
    ),
    account: str = typer.Option(
        None, "--account", help="Account locator for SNOWFLAKE_ACCOUNT (default: the config's)."
    ),
) -> None:
    """Create (or reuse) the CI user's key pair and the five deploy secret files.

    Never overwrites an existing key or secret file, and never prints a secret
    value: only file names, the public-key fingerprint, and the next commands.
    """
    try:
        cfg = load_config(Path(config) if config else None)
        result = _ci_key.create(
            directory,
            account=normalize_account(account) if account else cfg.snowflake.account,
            user=ci_user_name(cfg.snowflake.roles.ci_role),
            warehouse=cfg.snowflake.objects.default_warehouse,
            role=cfg.snowflake.roles.ci_role,
        )
    except (ConfigError, _ci_key.CiKeyError) as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    d = result.directory
    print(f"{'Created' if result.key_created else 'Reused'} key pair in {d}:")
    print(f"  {result.private_key.name}   private key, mode 600 (never commit or share it)")
    print(f"  {result.public_key.name}  public key")
    print(f"  fingerprint {result.fingerprint}  (DESC USER shows it as RSA_PUBLIC_KEY_FP)")
    if result.written:
        print(f"Wrote secrets/: {', '.join(result.written)}")
    if result.kept:
        print(f"Kept existing secrets/: {', '.join(result.kept)}")
    for warning in result.warnings:
        console.print(f"[yellow]warning:[/] {warning}")
    for name in result.mismatched:
        console.print(
            f"[yellow]warning:[/] secrets/{name} differs from this config; left unchanged. "
            "Delete it and re-run to rewrite it."
        )
    print("")
    print("Next:")
    print(
        "  1. streamsnow deploy-setup --admin --public-key-file "
        f"{result.public_key} > admin-setup.sql"
    )
    print("     (review, then run it as ACCOUNTADMIN)")
    print("  2. Once your admin has run it: streamsnow ci-key push")
    print("     (sets the five GitHub secrets from these files, SNOWFLAKE_ACCOUNT last;")
    print("      no value is ever printed)")
    print(f"  3. Save a copy of {result.private_key} somewhere safe,")
    print("     such as a password manager. If it is lost, make a new pair and re-run step 1.")


@ci_key_app.command(name="push")
def ci_key_push(
    directory: Path = typer.Option(
        _ci_key.DEFAULT_DIR, "--dir", help="The directory `ci-key create` wrote."
    ),
    repo: str = typer.Option(
        None, "--repo", help="owner/name, when the checkout has several GitHub remotes."
    ),
) -> None:
    """Set the five deploy secrets on GitHub from the files `ci-key create` wrote.

    Each value goes from its file straight to `gh secret set` on stdin, never on
    the command line, and is never printed. SNOWFLAKE_ACCOUNT goes last because
    it switches the deploy job on; a failure stops before it.
    """
    try:
        result = _ci_key.push(directory, repo=repo)
    except _ci_key.CiKeyError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    print(f"Setting the deploy secrets on {result.repo} (SNOWFLAKE_ACCOUNT last):")
    for name in result.done:
        print(f"  {name}: set")
    if result.failed:
        print(f"  {result.failed}: failed: {result.message}")
        for name in result.not_attempted:
            print(f"  {name}: not set (stopped after the failure)")
        raise typer.Exit(1)
    print("Done. The next merge to main deploys.")


_SSH_GITHUB_RE = re.compile(
    r"^(?:ssh://)?git@github\.com[:/](?P<path>[^/\s]+/[^/\s]+?)(?:\.git)?/?$"
)


def _checkout_github_origin() -> str | None:
    """This checkout's ``origin`` remote as a GitHub HTTPS URL, or None.

    SSH remotes (the ``git@`` form) become HTTPS, the form a
    Snowflake GIT REPOSITORY clones from; anything else is left for the user
    to pass with --git-origin rather than guessed at.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    url = proc.stdout.strip().rstrip("/") if proc.returncode == 0 else ""
    m = _SSH_GITHUB_RE.match(url)
    if m:
        url = f"https://github.com/{m.group('path')}.git"
    try:
        return validate_github_origin(url, "origin remote")
    except ConfigError:
        return None


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
        hidden=True,
        help="Deprecated no-op: git-repository deploys now CREATE OR REPLACE.",
    ),
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
) -> None:
    """Emit the CREATE OR REPLACE STREAMLIT SQL for one app (used by the deploy workflow)."""
    try:
        cfg = load_config(Path(config) if config else None)
        # Workflows rendered before 0.7.4 still run `deploy-sql --refresh` (an
        # ABORT/PULL/COMMIT refresh, piped to `snow sql ... || true`) after the
        # create step. The create step now redeploys on its own, so the refresh
        # is a comment that runs nothing.
        sql = (
            "-- refresh is no longer needed: the create step redeploys with CREATE OR REPLACE."
            if refresh
            else generate_create_sql(cfg, slug, sha)
        )
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
    sha: str = typer.Option(
        None, "--sha", help="Expected commit SHA: the version-source check confirms it is live."
    ),
    attempts: int = typer.Option(3, "--attempts", help="Retries for cold-start absorption."),
    delay: float = typer.Option(20.0, "--delay", help="Seconds between retries."),
    temporary_connection: bool = typer.Option(
        False,
        "--temporary-connection",
        help="Pass --temporary-connection to every snow call, so snow connects from "
        "SNOWFLAKE_* environment variables instead of config.toml (the generated deploy "
        "workflow passes it in CI). Omit locally to use your default connection.",
    ),
    config: Path = typer.Option(None, "--config", help="Path to streamsnow.config.yaml."),
    output_format: str = typer.Option("md", "--format"),
) -> None:
    """Verify a deployed app actually serves: object exists, live version set,
    version source matches the merge SHA, container logs show no crash loop.
    A check that cannot run is reported as skipped, never as a pass."""
    from functools import partial

    from .verify import run_query_snow, summary_line, verify_app

    try:
        cfg = load_config(Path(config) if config else None)
    except ConfigError as exc:
        _err(str(exc))
        raise typer.Exit(2) from exc
    run_query = partial(run_query_snow, temporary_connection=temporary_connection)
    try:
        result = verify_app(cfg, slug, sha=sha, run_query=run_query, attempts=attempts, delay=delay)
    except ValueError as exc:  # invalid slug
        _err(str(exc))
        raise typer.Exit(2) from exc
    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        # A check that could not run gets its own mark and word: a check mark
        # beside "skipped" read as a pass in CI logs.
        marks = {"pass": "✓", "fail": "✗", "skipped": "○"}
        for c in result["checks"]:
            label = f"{c['name']} (skipped)" if c["status"] == "skipped" else c["name"]
            print(f"  {marks[c['status']]} {label}")
            for f in c["findings"]:
                print(f"      - {f}")
        print(f"\n{summary_line(result)}")
    raise typer.Exit(code=0 if result["ok"] else 1)


@app.command()
def update(
    directory: Path = typer.Option(Path("."), "--dir", help="Repo root."),
    apply: bool = typer.Option(False, "--apply", help="Write changes (default: dry-run)."),
) -> None:
    """Re-render governance files (AGENTS.md, hooks, CI, deploy) from your current
    config + installed StreamSnow templates. README and .gitignore are left alone;
    a missing warehouse-runtime osv_allowlist.json is created, never overwritten.
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
        old = out.read_text(encoding="utf-8") if out.exists() else None
        if new != old:
            changed.append(item.output)
            if apply:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(new, encoding="utf-8")
    for item in CREATE_IF_MISSING_ITEMS:
        out = target / item.output
        if not item.when(cfg) or out.exists():
            continue
        changed.append(f"{item.output} (new)")
        if apply:
            out.write_text(render_item(cfg, item, cfg.project.slug), encoding="utf-8")

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
    # The shorthand must also route flag-first invocations (`preview --port
    # 8501 my-app`): when NO verb appears anywhere, this is the shorthand —
    # unless the user is asking for help.
    wants_help = any(a in ("-h", "--help") for a in argv)
    has_verb = any(a in _PREVIEW_VERBS for a in argv)
    if argv and not has_verb and not wants_help:
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
    name="sql-review",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def sql_review_cmd(ctx: typer.Context) -> None:
    """Human-runnable audit trail per app (discover | generate | check | index).

    Every UI-feeding query gets a fully-rendered, paste-and-runnable review
    file under apps/<slug>/sql_review/ so a person can trace each visual back
    to the data. `check` is the import-free freshness + coverage gate."""
    raise typer.Exit(code=_sql_review_main(list(ctx.args)))


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


@app.command(
    name="agent-skills",
    add_help_option=False,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def agent_skills_cmd(ctx: typer.Context) -> None:
    """Install the skills for other AI coding agents, e.g. Codex (install | list)."""
    raise typer.Exit(code=_agent_skills_main(list(ctx.args)))


if __name__ == "__main__":  # pragma: no cover
    app()
