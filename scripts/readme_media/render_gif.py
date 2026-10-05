"""Build docs/images/demo.gif: title card, the real CLI flow, the sample dashboard, end card.

A README GIF that is drawn by hand drifts from the product the first time the CLI
output changes. This one is regenerated from the real thing: the terminal part
replays the transcript render_terminal.py captures from the offline CLI, and the
dashboard part screenshots examples/sample-dashboard running under Streamlit (no
Snowflake needed). Frames are HTML scenes screenshotted by headless Chromium, then
stitched by ffmpeg with a generated palette.

    uv run --with playwright --with streamlit --with "pandas>=2,<3" \\
        --with "plotly>=5,<6" python scripts/readme_media/render_gif.py

Needs ffmpeg on PATH and a Chromium Playwright can drive (see _common.find_chromium;
on a laptop run `uv run --with playwright playwright install chromium` once).
"""

from __future__ import annotations

import argparse
import html
import io
import math
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from rich.console import Console
from rich.terminal_theme import TerminalTheme
from rich.text import Text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import IMAGES, REPO, launch, route_web_fonts  # noqa: E402
from render_terminal import WIDTH, run_flow, style_output, wrap_command  # noqa: E402

OUT = IMAGES / "demo.gif"
LOGO = IMAGES / "logo-dark.svg"
DASHBOARD = REPO / "examples" / "sample-dashboard" / "streamlit_app.py"
STAGE_W, STAGE_H = 960, 600
FPS = 10
TICK = 1 / FPS
DASH_VIEWPORT = {"width": 1200, "height": 960}
DASH_SCALE = 912 / DASH_VIEWPORT["width"]  # the window's inner width in the stage

THEME = TerminalTheme(
    (13, 17, 23),
    (230, 237, 243),
    [
        (72, 79, 88),
        (255, 123, 114),
        (63, 185, 80),
        (210, 153, 34),
        (88, 166, 255),
        (188, 140, 255),
        (57, 197, 207),
        (177, 186, 196),
    ],
    [
        (110, 118, 129),
        (255, 161, 152),
        (86, 211, 100),
        (227, 179, 65),
        (121, 192, 255),
        (210, 168, 255),
        (86, 212, 221),
        (240, 246, 252),
    ],
)

STAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root { --blue:#29B5E8; --navy:#11567F; --ice:#E8F4FA; --orange:#D97757; --bg:#07182A; }
  html, body { margin:0; padding:0; }
  body { width:%(w)dpx; height:%(h)dpx; overflow:hidden; background:var(--bg); color:var(--ice);
    font-family:"Inter",-apple-system,"Segoe UI","Helvetica Neue",Arial,sans-serif; position:relative; }
  .glow { position:absolute; inset:0; background:
    radial-gradient(700px 360px at 50%% 30%%, rgba(41,181,232,.16), transparent 70%%),
    radial-gradient(500px 260px at 95%% 110%%, rgba(17,86,127,.5), transparent 70%%); }
  #stage { position:absolute; inset:0; }
  .caption { position:absolute; top:0; left:24px; right:24px; height:56px; display:flex;
    align-items:center; gap:12px; font-size:17px; font-weight:600; white-space:nowrap; }
  .caption .n { width:26px; height:26px; border-radius:50%%; background:var(--blue); color:#05223A;
    display:flex; align-items:center; justify-content:center; font-size:14px; font-weight:700; }
  .caption > * { white-space:nowrap; flex-shrink:0; }
  .caption .sub { color:#8FBFD6; font-weight:400; font-size:15px; }
  .caption code { font-family:"JetBrains Mono","DejaVu Sans Mono",Menlo,monospace; font-size:13.5px;
    color:#BFE6F6; background:rgba(41,181,232,.12); padding:2px 6px; border-radius:5px; }
  .window { position:absolute; top:56px; left:24px; right:24px; bottom:20px; border-radius:10px;
    overflow:hidden; box-shadow:0 18px 50px rgba(0,0,0,.45), 0 0 0 1px rgba(232,244,250,.08); }
  .bar { height:30px; display:flex; align-items:center; padding:0 12px; gap:7px; position:relative; }
  .bar i { width:11px; height:11px; border-radius:50%%; display:block; }
  .bar i:nth-child(1){background:#ff5f57} .bar i:nth-child(2){background:#febc2e}
  .bar i:nth-child(3){background:#28c840}
  .bar .t { position:absolute; left:0; right:0; text-align:center; font-size:12.5px; pointer-events:none; }
  .term { background:#0D1117; }
  .term .bar { background:#161B22; color:#8B949E; }
  .screen { position:absolute; top:30px; left:0; right:0; bottom:0; padding:8px 16px 10px;
    display:flex; flex-direction:column; justify-content:flex-end; overflow:hidden; }
  .line { font-family:"JetBrains Mono","DejaVu Sans Mono",Menlo,Consolas,monospace; font-size:14px;
    line-height:19px; white-space:pre; color:#E6EDF3; min-height:19px; }
  .cursor { display:inline-block; width:8px; height:16px; background:#E6EDF3; vertical-align:-3px; }
  .browser { background:#ffffff; }
  .browser .bar { background:#E9EEF3; color:#4B5563; }
  .browser .url { position:absolute; left:50%%; transform:translateX(-50%%); top:5px; height:20px;
    width:420px; border-radius:6px; background:#fff; color:#4B5563; font-size:12px;
    display:flex; align-items:center; justify-content:center; }
  .viewport { position:absolute; top:30px; left:0; right:0; bottom:0; overflow:hidden; }
  .viewport img { width:100%%; display:block; }
  .card { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center;
    justify-content:center; gap:26px; text-align:center; }
  .card img { height:112px; }
  .card h1 { margin:0; font-size:34px; font-weight:600; letter-spacing:-.02em; }
  .card h1 em { font-style:normal; color:var(--blue); }
  .card p { margin:0; font-size:18px; color:#9CCFE6; }
  .card code { font-family:"JetBrains Mono","DejaVu Sans Mono",Menlo,monospace; color:var(--ice);
    background:rgba(41,181,232,.14); padding:3px 9px; border-radius:6px; font-size:17px; }
  .arrow { color:var(--orange); padding:0 6px; }
</style></head>
<body><div class="glow"></div><div id="stage"></div></body></html>
"""


class Recorder:
    """Screenshots stage scenes into numbered PNGs, each held for a duration."""

    def __init__(self, page, frames_dir: Path) -> None:
        self.page = page
        self.dir = frames_dir
        self.frames: list[tuple[Path, float]] = []

    def scene(self, inner_html: str, seconds: float) -> None:
        self.page.evaluate(
            """async (h) => {
                document.getElementById('stage').innerHTML = h;
                await Promise.all([...document.images].map(i => i.decode().catch(() => null)));
            }""",
            inner_html,
        )
        path = self.dir / f"f{len(self.frames):04d}.png"
        self.page.screenshot(path=str(path))
        ticks = max(1, round(seconds / TICK))
        self.frames.append((path, ticks * TICK))

    @property
    def seconds(self) -> float:
        return sum(d for _, d in self.frames)


# ---------- terminal ------------------------------------------------------------------


_console = Console(
    record=True, width=WIDTH, file=io.StringIO(), force_terminal=True, color_system="truecolor"
)


def _line_html(text: Text) -> str:
    _console.print(text, end="")
    out = _console.export_html(inline_styles=True, code_format="{code}", theme=THEME)
    return f'<div class="line">{out.rstrip(chr(10)) or " "}</div>'


def _prompt_lines(typed: str, cursor: bool) -> list[str]:
    lines = typed.split("\n") if typed else [""]
    out = []
    for i, part in enumerate(lines):
        t = Text()
        t.append("$ " if i == 0 else "  ", style="bold #29B5E8")
        t.append(part, style="bold #F2F2F2")
        h = _line_html(t)
        if cursor and i == len(lines) - 1:
            h = h.replace("</div>", '<span class="cursor"></span></div>')
        out.append(h)
    return out


def _term_scene(lines: list[str]) -> str:
    return (
        '<div class="caption"><span class="n">1</span>The real CLI, offline'
        '<span class="sub">init &rarr; new &rarr; validate-app &middot; no Snowflake needed</span>'
        "</div>"
        '<div class="window term"><div class="bar"><i></i><i></i><i></i>'
        '<span class="t">~/acme-analytics </span></div>'
        f'<div class="screen">{"".join(lines[-25:])}</div></div>'
    )


def record_terminal(rec: Recorder) -> None:
    steps = run_flow()
    shown: list[str] = []
    rec.scene(_term_scene(_prompt_lines("", cursor=True)), 0.8)
    for step in steps:
        if step.comment:
            shown.append(_line_html(Text(step.comment, style="dim")))
        typed_full = "\n".join(wrap_command(step.command))
        n = min(10, max(3, math.ceil(len(typed_full) / 14)))
        chunk = math.ceil(len(typed_full) / n)
        for k in range(chunk, len(typed_full) + chunk, chunk):
            rec.scene(_term_scene(shown + _prompt_lines(typed_full[:k], cursor=True)), TICK)
        rec.scene(_term_scene(shown + _prompt_lines(typed_full, cursor=True)), 0.4)
        shown += _prompt_lines(typed_full, cursor=False)
        out = [_line_html(style_output(line)) for line in step.output]
        group = max(1, math.ceil(len(out) / 6))
        for i in range(0, len(out), group):
            shown += out[i : i + group]
            rec.scene(_term_scene(shown), TICK)
        hold = 2.6 if "validate-app" in step.command else 1.1
        if step.command.startswith(("ls", "echo")):
            hold = 0.8
        rec.scene(_term_scene(shown), hold)
        shown.append(_line_html(Text("")))
    rec.scene(_term_scene(shown + _prompt_lines("", cursor=True)), 1.6)


# ---------- dashboard -----------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, proc: subprocess.Popen, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit("streamlit exited before it was ready")
        try:
            with urllib.request.urlopen(url + "/_stcore/health", timeout=2):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    raise SystemExit("streamlit did not become ready in time")


def capture_dashboard(browser, work: Path) -> dict[str, Path]:
    """Run the sample dashboard and screenshot its pages (with one chart hover each)."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(DASHBOARD),
        "--server.headless=true",
        f"--server.port={port}",
        "--server.address=127.0.0.1",
        "--browser.gatherUsageStats=false",
        "--client.toolbarMode=minimal",
        "--theme.base=light",
    ]
    shots: dict[str, Path] = {}
    proc = subprocess.Popen(
        cmd, cwd=DASHBOARD.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        _wait_ready(url, proc)
        page = browser.new_page(viewport=DASH_VIEWPORT, device_scale_factor=1)
        page.goto(url)
        page.wait_for_selector(".js-plotly-plot .scatterlayer path.point", timeout=60_000)
        page.wait_for_timeout(1500)
        shots["overview"] = work / "dash-overview.png"
        page.screenshot(path=str(shots["overview"]))

        last_point = page.locator(".js-plotly-plot").first.locator(".scatterlayer path.point")
        last_point.nth(last_point.count() - 1).hover(force=True)
        page.wait_for_timeout(600)
        shots["overview_hover"] = work / "dash-overview-hover.png"
        page.screenshot(path=str(shots["overview_hover"]))

        page.get_by_role("link", name="Trends").click()
        page.wait_for_selector("text=Day-N retention", timeout=30_000)
        page.wait_for_selector(".js-plotly-plot .scatterlayer path.point", timeout=30_000)
        page.wait_for_timeout(1500)
        page.mouse.move(5, 5)
        shots["trends"] = work / "dash-trends.png"
        page.screenshot(path=str(shots["trends"]))
        points = page.locator(".js-plotly-plot .scatterlayer path.point")
        points.nth(min(3, points.count() - 1)).hover(force=True)
        page.wait_for_timeout(600)
        shots["trends_hover"] = work / "dash-trends-hover.png"
        page.screenshot(path=str(shots["trends_hover"]))
        page.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return shots


def _dash_scene(img: Path, scroll: float, page_name: str) -> str:
    return (
        '<div class="caption"><span class="n">2</span>The sample dashboard'
        '<span class="sub">sample data, no Snowflake</span>'
        "<code>streamlit run examples/sample-dashboard/streamlit_app.py</code></div>"
        '<div class="window browser"><div class="bar"><i></i><i></i><i></i>'
        f'<span class="url">localhost:8501/{page_name}</span></div>'
        f'<div class="viewport"><img src="{img.as_uri()}" '
        f'style="transform:translateY(-{scroll:.0f}px)"></div></div>'
    )


def record_dashboard(rec: Recorder, shots: dict[str, Path]) -> None:
    window_h = STAGE_H - 56 - 20 - 30
    max_scroll = max(0.0, DASH_VIEWPORT["height"] * DASH_SCALE - window_h)
    rec.scene(_dash_scene(shots["overview"], 0, ""), 1.6)
    steps = 8
    for i in range(1, steps + 1):
        eased = (1 - math.cos(math.pi * i / steps)) / 2
        rec.scene(_dash_scene(shots["overview"], max_scroll * eased, ""), TICK)
    rec.scene(_dash_scene(shots["overview_hover"], max_scroll, ""), 2.0)
    rec.scene(_dash_scene(shots["trends"], 0, "trends"), 1.0)
    rec.scene(_dash_scene(shots["trends_hover"], 0, "trends"), 2.0)


# ---------- cards ---------------------------------------------------------------------


def _title_scene() -> str:
    return (
        f'<div class="card"><img src="{LOGO.as_uri()}" alt="">'
        '<h1>idea <span class="arrow">&rarr;</span> governed app '
        '<span class="arrow">&rarr;</span> <em>Snowflake</em></h1>'
        "<p>A Claude Code plugin + Python CLI for Streamlit-in-Snowflake apps</p></div>"
    )


def _end_scene() -> str:
    return (
        f'<div class="card"><img src="{LOGO.as_uri()}" alt="" style="height:84px">'
        "<h1>Then <em>/ship-app</em> opens the PR<br>and CI deploys to Snowflake.</h1>"
        f"<p>Get started: <code>{html.escape('uvx streamsnow init')}</code></p></div>"
    )


# ---------- stitch --------------------------------------------------------------------


def stitch(frames: list[tuple[Path, float]], work: Path, out: Path, width: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found on PATH")
    listing = work / "frames.txt"
    lines = []
    for path, seconds in frames:
        lines += [f"file '{path.as_posix()}'", f"duration {seconds:.2f}"]
    lines.append(f"file '{frames[-1][0].as_posix()}'")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    graph = (
        f"fps={FPS},scale={width}:-1:flags=lanczos,split[a][b];"
        "[a]palettegen=max_colors=192:stats_mode=full[p];"
        "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
    )
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing)]
        + ["-filter_complex", graph, "-loop", "0", str(out)],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", type=Path, help="Scratch dir for frames (default: a temp dir).")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--width", type=int, default=STAGE_W)
    parser.add_argument("--skip-dashboard", action="store_true", help="Terminal part only.")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="streamsnow-gif-") as tmp:
        work = (args.work or Path(tmp)).resolve()
        frames_dir = work / "frames"
        if frames_dir.exists():
            shutil.rmtree(frames_dir)
        frames_dir.mkdir(parents=True)
        stage = work / "stage.html"
        stage.write_text(STAGE_HTML % {"w": STAGE_W, "h": STAGE_H}, encoding="utf-8")

        with sync_playwright() as pw:
            browser = launch(pw)
            shots = {} if args.skip_dashboard else capture_dashboard(browser, work)
            page = browser.new_page(
                viewport={"width": STAGE_W, "height": STAGE_H}, device_scale_factor=1
            )
            route_web_fonts(page)
            page.goto(stage.as_uri(), wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            rec = Recorder(page, frames_dir)
            rec.scene(_title_scene(), 2.6)
            record_terminal(rec)
            if shots:
                record_dashboard(rec, shots)
            rec.scene(_end_scene(), 3.2)
            browser.close()

        stitch(rec.frames, work, args.out, args.width)
    size = args.out.stat().st_size
    print(
        f"wrote {args.out.name}: {len(rec.frames)} scenes, {rec.seconds:.1f}s, {size / 1e6:.2f} MB"
    )


if __name__ == "__main__":
    main()
