"""StreamSnow command-line interface.

The ``streamsnow`` CLI scaffolds a governed Streamlit-in-Snowflake monorepo and
wires up the Claude Code plugin. Commands:

    streamsnow init           Interactive setup wizard + scaffold a new repo
    streamsnow new            Scaffold a new app in an existing StreamSnow repo
    streamsnow doctor         Check the local environment for prerequisites
    streamsnow deploy-setup   Emit the one-time Snowflake DDL for your deploy source
    streamsnow update         Re-vendor templates/tools and bump the plugin

Phase 0 ships the command surface and environment checks; the scaffolding,
config, and deploy engines are filled in across Phases 1-3 (see the project plan).
"""

from __future__ import annotations

import shutil
import sys

import typer
from rich.console import Console

from . import __version__

app = typer.Typer(
    name="streamsnow",
    help="Build, govern, and ship Streamlit-in-Snowflake apps with Claude Code.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

_NOT_YET = "[yellow]not yet implemented[/] — lands in {phase}. Tracked in the project plan."


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


@app.command()
def init() -> None:
    """Interactive setup wizard + scaffold a governed monorepo (Phase 1)."""
    console.print("streamsnow init: " + _NOT_YET.format(phase="Phase 1"))


@app.command()
def new(
    domain: str = typer.Argument(..., help="Business domain, e.g. 'marketing'."),
    function: str = typer.Argument(..., help="App function, e.g. 'campaign-dashboard'."),
) -> None:
    """Scaffold a new app ({domain}-{function}) from the Copier template (Phase 1)."""
    console.print(f"streamsnow new {domain} {function}: " + _NOT_YET.format(phase="Phase 1"))


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

    console.print(
        "\n[dim]Full prerequisite checks (venv, pre-commit, Snowflake connection, "
        "Claude Code MCP servers) arrive with the ported onboard tool in Phase 1.[/]"
    )
    raise typer.Exit(code=0 if ok else 1)


@app.command(name="deploy-setup")
def deploy_setup() -> None:
    """Emit the one-time Snowflake DDL for your configured deploy source (Phase 2/3)."""
    console.print(
        "streamsnow deploy-setup: "
        + _NOT_YET.format(phase="Phase 2 (stage) / Phase 3 (git-repository)")
    )


@app.command()
def update() -> None:
    """Re-vendor templates/tools and bump the Claude Code plugin (Phase 4)."""
    console.print("streamsnow update: " + _NOT_YET.format(phase="Phase 4"))


if __name__ == "__main__":  # pragma: no cover
    app()
