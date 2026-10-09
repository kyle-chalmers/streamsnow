"""Build docs/images/demo.gif: the StreamSnow journey in Claude, through CI, into Snowflake.

A README GIF should show what a user actually does with StreamSnow: talk to Claude. This
one plays /build-app, /review-app, /preview-app and /ship-app in a Claude desktop chat,
then the pull request's checks, the merge and the deploy run, and ends on the app live in
Snowsight. The words come from one real end-to-end run and the app pictures are that run's
own preview screenshots (see claude_flow.py), so the GIF stays true to the product without a
Snowflake account. A GIF drawn by hand drifts the first time a skill's output changes; rerun
this when it does. Frames are HTML scenes screenshotted by headless Chromium, then stitched
by ffmpeg with a generated palette.

    uv run --with playwright python scripts/readme_media/render_gif.py

Needs ffmpeg on PATH and a Chromium Playwright can drive (see _common.find_chromium;
on a laptop run `uv run --with playwright playwright install chromium` once).
"""

from __future__ import annotations

import argparse
import html
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import IMAGES, launch, route_web_fonts  # noqa: E402
from claude_flow import (  # noqa: E402
    CSS,
    DEPLOY_STEPS,
    SHOT_W,
    SHOTS,
    STEPS,
    Step,
    browser_frame,
    caption,
    chat_frame,
    check_clean,
    eased,
    pr_frame,
    snowflake_frame,
    user,
)

OUT = IMAGES / "demo.gif"
LOGO = IMAGES / "logo-dark.svg"
STAGE_W, STAGE_H = 960, 600
FPS = 10
TICK = 1 / FPS
WINDOW_W = STAGE_W - 48  # .window / .app inner width
WINDOW_H = STAGE_H - 56 - 20 - 30  # below the caption, above the margin, under the title bar
SF_FRAME_W = WINDOW_W - 52  # Snowsight frame minus its nav rail
SF_FRAME_H = WINDOW_H - 46  # minus its header row

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
%(extra)s</style></head>
<body><div class="glow"></div><div id="stage"></div></body></html>
"""


class Recorder:
    """Screenshots stage scenes into numbered PNGs, each held for a duration."""

    def __init__(self, page, frames_dir: Path) -> None:
        self.page = page
        self.dir = frames_dir
        self.frames: list[tuple[Path, float]] = []

    def scene(self, inner_html: str, seconds: float) -> None:
        check_clean(inner_html)
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


# ---------- Claude chat ---------------------------------------------------------------


def play(rec: Recorder, step: Step, feed: list[str]) -> list[str]:
    """Type the step's prompt, send it, then reveal its beats. Returns the feed."""
    prompt = step.prompt
    rec.scene(chat_frame(step.cap, feed), 0.4)
    n = min(10, max(4, math.ceil(len(prompt) / 7)))
    for k in range(1, n + 1):
        rec.scene(chat_frame(step.cap, feed, prompt[: math.ceil(len(prompt) * k / n)]), TICK)
    rec.scene(chat_frame(step.cap, feed, prompt), 0.4)
    feed = feed + [user(prompt)]
    rec.scene(chat_frame(step.cap, feed), 0.5)
    for op, item, hold in step.beats:
        feed = feed[:-1] + [item] if op == "set" else feed + [item]
        rec.scene(chat_frame(step.cap, feed), hold)
    return feed


# ---------- preview, PR, Snowflake ------------------------------------------------------


def _scroll_to(rec: Recorder, frame, start: float, stop: float, steps: int = 8) -> None:
    for i in range(1, steps + 1):
        rec.scene(frame(start + (stop - start) * eased(i, steps)), TICK)


def record_preview(rec: Recorder, cap: str) -> None:
    scale = WINDOW_W / SHOT_W
    rec.scene(browser_frame(cap, SHOTS["overview"], 0, ""), 1.8)
    rec.scene(browser_frame(cap, SHOTS["regions"], 0, "regions"), 0.9)
    regions_max = 1420 * scale - WINDOW_H
    _scroll_to(rec, lambda y: browser_frame(cap, SHOTS["regions"], y, "regions"), 0, regions_max)
    rec.scene(browser_frame(cap, SHOTS["regions"], regions_max, "regions"), 1.2)
    rec.scene(browser_frame(cap, SHOTS["products"], 0, "products"), 1.6)


def record_pr(rec: Recorder, cap: str) -> None:
    rec.scene(pr_frame(cap, merged=False, checks="run", deploy=[]), 1.2)
    rec.scene(pr_frame(cap, merged=False, checks="ok", deploy=[]), 1.4)
    rows = [("run", DEPLOY_STEPS[0])]
    rec.scene(pr_frame(cap, merged=True, checks="ok", deploy=rows), 0.8)
    for i in range(1, len(DEPLOY_STEPS) + 1):
        rows = [("ok", s) for s in DEPLOY_STEPS[:i]]
        if i < len(DEPLOY_STEPS):
            rows.append(("run", DEPLOY_STEPS[i]))
        rec.scene(pr_frame(cap, merged=True, checks="ok", deploy=rows), 0.4)
    rec.scene(pr_frame(cap, merged=True, checks="ok", deploy=rows), 1.6)


def record_snowflake(rec: Recorder) -> None:
    cap = caption(5, "Live in Snowflake", "the same app, deployed by CI, opened in Snowsight")
    scale = SF_FRAME_W / SHOT_W
    over_max = 1150 * scale - SF_FRAME_H - 52 * scale

    def frame(y: float) -> str:
        return snowflake_frame(cap, SHOTS["overview"], y, SF_FRAME_W)

    rec.scene(frame(0), 2.0)
    _scroll_to(rec, frame, 0, over_max)
    rec.scene(frame(over_max), 1.6)


# ---------- cards ---------------------------------------------------------------------


def _title_scene() -> str:
    return (
        f'<div class="card"><img src="{LOGO.as_uri()}" alt="">'
        '<h1>idea <span class="arrow">&rarr;</span> governed app '
        '<span class="arrow">&rarr;</span> <em>Snowflake</em></h1>'
        "<p>All in Claude: build, review, preview, ship. CI does the deploy.</p></div>"
    )


def _end_scene() -> str:
    prompt = "Read the install prompt in github.com/kyle-chalmers/streamsnow and follow it."
    return (
        f'<div class="card"><img src="{LOGO.as_uri()}" alt="" style="height:84px">'
        "<h1>Every path to production passes<br>the <em>validate-app</em> gate. "
        "Only CI deploys.</h1>"
        f"<p>Get started: tell your agent<br><code>{html.escape(prompt)}</code></p></div>"
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
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="streamsnow-gif-") as tmp:
        work = (args.work or Path(tmp)).resolve()
        frames_dir = work / "frames"
        if frames_dir.exists():
            shutil.rmtree(frames_dir)
        frames_dir.mkdir(parents=True)
        stage = work / "stage.html"
        stage.write_text(STAGE_HTML % {"w": STAGE_W, "h": STAGE_H, "extra": CSS}, encoding="utf-8")

        with sync_playwright() as pw:
            browser = launch(pw)
            page = browser.new_page(
                viewport={"width": STAGE_W, "height": STAGE_H}, device_scale_factor=1
            )
            route_web_fonts(page)
            page.goto(stage.as_uri(), wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            rec = Recorder(page, frames_dir)
            rec.scene(_title_scene(), 2.2)
            build, review, preview, ship = STEPS
            feed = play(rec, build, [])
            feed = play(rec, review, feed)
            feed = play(rec, preview, feed)
            record_preview(rec, preview.cap)
            play(rec, ship, feed)
            record_pr(rec, ship.cap)
            record_snowflake(rec)
            rec.scene(_end_scene(), 3.2)
            browser.close()

        stitch(rec.frames, work, args.out, args.width)
    size = args.out.stat().st_size
    print(
        f"wrote {args.out.name}: {len(rec.frames)} scenes, {rec.seconds:.1f}s, {size / 1e6:.2f} MB"
    )


if __name__ == "__main__":
    main()
