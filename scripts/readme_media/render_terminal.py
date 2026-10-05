"""Record the real offline StreamSnow CLI flow into docs/images/demo-terminal.svg.

The README demo must show what the CLI actually prints, not a mock-up that drifts
from it. This script runs ``streamsnow init`` -> ``streamsnow new`` ->
``streamsnow validate-app`` in a throwaway directory (no Snowflake, no network),
captures their real output, and renders it with rich's SVG exporter. The last step
FAILS on purpose: a fresh scaffold still carries starter placeholders, and the ship
gate refusing them is the point of the demo.

Absolute temp paths are rewritten to ``~/acme-analytics`` so no machine detail
leaks into the image. Very long lines are cut with an ellipsis; nothing is
reworded.

    uv run python scripts/readme_media/render_terminal.py

render_gif.py imports ``run_flow`` from here so the GIF plays the same transcript.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import IMAGES  # noqa: E402

OUT = IMAGES / "demo-terminal.svg"
DISPLAY_ROOT = "~/acme-analytics"
WIDTH = 100
MAX_LINE = WIDTH

INIT_ARGS = [
    "init",
    "--no-starter-app",
    "--runtime",
    "container",
    "--account",
    "ab12345.us-east-1",
    "--database",
    "ACME_ANALYTICS",
    "--schemas",
    "ANALYTICS,REPORTING",
    "--deploy-source",
    "stage-copy",
]
NEW_ARGS = ["new", "sales", "pipeline-dashboard"]
SLUG = "sales-pipeline-dashboard"
VALIDATE_ARGS = ["validate-app", SLUG]


@dataclass
class Step:
    """One prompt line plus the (sanitised, trimmed) output it produced."""

    comment: str
    command: str
    output: list[str] = field(default_factory=list)
    exit_code: int = 0


def _cli() -> list[str]:
    exe = shutil.which("streamsnow")
    if exe:
        return [exe]
    return [sys.executable, "-c", "from streamsnow.cli import app; app()"]


def _sanitise(text: str, roots: list[str]) -> str:
    for root in sorted(roots, key=len, reverse=True):
        text = text.replace(root, DISPLAY_ROOT)
    return text


def _clip(line: str) -> str:
    line = line.rstrip()
    return line if len(line) <= MAX_LINE else line[: MAX_LINE - 1].rstrip() + "…"


def _run(args: list[str], cwd: Path, roots: list[str]) -> tuple[list[str], int]:
    env = dict(os.environ, COLUMNS=str(WIDTH), NO_COLOR="1", TERM="dumb")
    env.pop("FORCE_COLOR", None)
    proc = subprocess.run(
        [*_cli(), *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    text = _sanitise(proc.stdout + proc.stderr, roots)
    return text.rstrip().splitlines(), proc.returncode


def _trim_init(lines: list[str]) -> list[str]:
    """Keep the summary and the first next step; the remaining setup tips are long."""
    out: list[str] = []
    for line in lines:
        if line.lstrip().startswith("2."):
            out.append("  … (snow connection, pre-commit and deploy-setup tips trimmed)")
            break
        out.append(line)
    return out


def _trim_new(lines: list[str]) -> list[str]:
    """Keep the result line and the placeholder warning; drop per-file and install chatter."""
    out = [ln for ln in lines if ln.lstrip().startswith("✓")]
    in_warning = False
    for line in lines:
        if line.startswith("The starter files"):
            in_warning = True
        elif in_warning and line.startswith(("Install", "Next:")):
            break
        if in_warning:
            out.append(line)
    return out


def _columns(names: list[str]) -> list[str]:
    """Lay names out like `ls`: space-separated, wrapped to the terminal width."""
    lines = [""]
    for name in names:
        if lines[-1] and len(lines[-1]) + 2 + len(name) > MAX_LINE:
            lines.append("")
        lines[-1] = f"{lines[-1]}  {name}" if lines[-1] else name
    return lines


def run_flow() -> list[Step]:
    """Run the offline flow in a fresh temp directory and return the transcript."""
    with tempfile.TemporaryDirectory(prefix="streamsnow-demo-") as tmp:
        repo = Path(tmp) / "acme-analytics"
        repo.mkdir()
        roots = [str(repo), str(repo.resolve()), tmp, str(Path(tmp).resolve())]

        steps: list[Step] = []

        init_out, code = _run(INIT_ARGS, repo, roots)
        steps.append(
            Step(
                "# 1. Set up a governed repo: config, AGENTS.md, hooks, CI (offline)",
                "streamsnow " + " ".join(INIT_ARGS),
                _trim_init(init_out),
                code,
            )
        )

        new_out, code = _run(NEW_ARGS, repo, roots)
        steps.append(
            Step(
                "# 2. Scaffold an app: {domain}-{function}",
                "streamsnow " + " ".join(NEW_ARGS),
                _trim_new(new_out),
                code,
            )
        )

        app_dir = repo / "apps" / SLUG
        listing = sorted(p.name + ("/" if p.is_dir() else "") for p in app_dir.iterdir())
        steps.append(Step("", f"ls apps/{SLUG}", _columns(listing), 0))

        val_out, code = _run(VALIDATE_ARGS, repo, roots)
        steps.append(
            Step(
                "# 3. The ship gate: deterministic PASS/FAIL (starter placeholders must go)",
                "streamsnow " + " ".join(VALIDATE_ARGS),
                val_out,
                code,
            )
        )
        steps.append(Step("", "echo $?", [str(code)], 0))
    for step in steps:
        step.output = [_clip(ln) for ln in step.output]
    return steps


def wrap_command(command: str) -> list[str]:
    """Split a long command into shell continuation lines that fit the terminal."""
    words = command.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) + 4 > MAX_LINE - 2:
            lines.append(current + " \\")
            current = "    " + word
        else:
            current = candidate if not current.startswith("    ") else f"{current} {word}"
    lines.append(current)
    return lines


def style_output(line: str) -> Text:
    text = Text(line)
    text.highlight_regex(r"✓", "bold green")
    text.highlight_regex(r"✗.*", "bold red")
    text.highlight_regex(r"^FAIL:.*", "bold red")
    text.highlight_regex(r"^PASS:.*", "bold green")
    text.highlight_regex(r"YOUR_TABLE", "yellow")
    text.highlight_regex(r"….*trimmed\)", "dim italic")
    return text


def render_svg(steps: list[Step], path: Path) -> None:
    console = Console(
        record=True,
        width=WIDTH,
        force_terminal=True,
        color_system="truecolor",
        file=io.StringIO(),
    )
    for i, step in enumerate(steps):
        if i:
            console.print()
        if step.comment:
            console.print(Text(step.comment, style="dim"))
        for j, cmd_line in enumerate(wrap_command(step.command)):
            prompt = Text()
            prompt.append("$ " if j == 0 else "  ", style="bold #29B5E8")
            prompt.append(cmd_line, style="bold #F2F2F2")
            console.print(prompt)
        for line in step.output:
            console.print(style_output(line))
    console.save_svg(str(path), title="streamsnow")


def main() -> None:
    steps = run_flow()
    render_svg(steps, OUT)
    text = OUT.read_text(encoding="utf-8")
    for needle in ("/home/", "/root/", "/tmp/", "/Users/", "/private/", "\\Users\\"):
        if needle in text:
            raise SystemExit(f"refusing to keep {OUT.name}: it contains {needle!r}")
    print(f"wrote {OUT.relative_to(IMAGES.parent.parent)} ({len(text) // 1024} KB)")


if __name__ == "__main__":
    main()
