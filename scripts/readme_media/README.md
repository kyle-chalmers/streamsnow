# README media

Scripts that produce the images the top-level README and the GitHub social preview use.
They are maintainer tools, not part of the `streamsnow` package, and they are not run in
CI. Every image is regenerated from real sources (the real CLI, the real sample dashboard,
the hand-authored logo), so rerun them when the CLI output or the dashboard changes.

| Output (in `docs/images/`) | Produced by | Source |
| --- | --- | --- |
| `logo-light.svg`, `logo-dark.svg` | hand-authored, no script | the SVGs themselves |
| `social-preview.png` (1280x640) | `render_social.py` | `social_preview.html` |
| `demo-terminal.svg` | `render_terminal.py` | the real offline CLI flow |
| `demo.gif` | `render_gif.py` | the same CLI flow plus `examples/sample-dashboard` |
| `skills-flow.png`, `repos-flow.png`, `secrets-flow.png` | `export_excalidraw.py` | the `.excalidraw` file next to each PNG |

## The logo

`logo-light.svg` (navy wordmark, for light backgrounds) and `logo-dark.svg` (near-white
wordmark, for dark backgrounds) are a snowflake whose lower arm runs off into a stream,
next to the "StreamSnow" wordmark. The wordmark is outlined to paths (from the Outfit Bold
typeface, SIL Open Font License), so the files need no font and render the same
everywhere. Colors: Snowflake blue `#29B5E8`, navy `#11567F`, ice `#E8F4FA`, and one
accent dot in `#D97757`. Edit the SVGs directly; keep each under 30 KB. In a README,
switch between them with a `<picture>` element and `prefers-color-scheme`.

## Regenerate

Run from the repo root after `uv sync --extra dev`. Playwright, Streamlit, pandas and
Plotly are pulled in for the one run with `uv run --with`, so nothing is added to
`pyproject.toml`.

```bash
# Terminal transcript SVG: only the dev environment is needed.
uv run python scripts/readme_media/render_terminal.py

# Social preview PNG.
uv run --with playwright python scripts/readme_media/render_social.py

# Demo GIF: needs ffmpeg on PATH as well.
uv run --with playwright --with streamlit --with "pandas>=2,<3" --with "plotly>=5,<6" \
    python scripts/readme_media/render_gif.py

# Diagram PNGs, after editing the .excalidraw JSON (at excalidraw.com or by hand).
uv run --with playwright --with pillow python scripts/readme_media/export_excalidraw.py \
    docs/images/skills-flow.excalidraw docs/images/repos-flow.excalidraw
```

The browser scripts drive headless Chromium through Playwright. They use the browser named
by the `STREAMSNOW_CHROMIUM` environment variable if it is set, then any preinstalled
Playwright Chromium under `PLAYWRIGHT_BROWSERS_PATH` or `/opt/pw-browsers`, and otherwise
Playwright's own download. On a laptop, fetch that once with
`uv run --with playwright playwright install chromium`. Web fonts (Inter, JetBrains Mono)
come from Google Fonts and fall back to the system fonts when offline.

What each script does:

- `render_terminal.py` creates a throwaway directory and runs, for real and offline,
  `streamsnow init` (all wizard answers given as flags, Acme sample values),
  `streamsnow new sales pipeline-dashboard`, and
  `streamsnow validate-app sales-pipeline-dashboard`. The last step is expected to fail:
  a fresh scaffold still has starter placeholders, and the gate refusing them is the point.
  Temp paths are rewritten to `~/acme-analytics`, long tips are trimmed with a marked
  ellipsis, and the transcript is saved with rich's SVG exporter. The script refuses to
  write an SVG that still contains a home or temp path.
- `render_social.py` screenshots `social_preview.html` at 1280x640. Upload the PNG under
  the repository's Settings, General, Social preview.
- `render_gif.py` replays the same transcript as typed commands in a dark terminal scene,
  then starts `examples/sample-dashboard` with Streamlit on a free local port, screenshots
  its Overview and Trends pages (with a chart hover on each), and stitches title card,
  terminal, dashboard and end card into a looping 960x600 GIF at 10 fps with an ffmpeg
  palette (about 28 seconds, about 1.5 MB). Pass `--work DIR` to keep the frames,
  `--width` to scale, or `--skip-dashboard` for the terminal part only.
- `export_excalidraw.py` renders each `.excalidraw` file to a PNG beside it with
  Excalidraw's own exporter (`@excalidraw/utils`) in headless Chromium at 2x, in the
  diagrams' Space Grotesk face. It downloads the library from jsDelivr; where that is
  blocked, `npm install @excalidraw/utils@0.1.5` somewhere and pass `--lib-dir` to its
  folder. A PNG over `--max-kb` (default 500) is re-saved as a palette image.

## Record the full Claude Code to Snowflake demo

The offline GIF stops where Snowflake starts. A recording of the whole path (an idea typed
into Claude Code, the governed app it builds, the pull request, and the live app in
Snowsight) is the stronger demo, and it needs a real account, so it is recorded by hand.
Use a scratch Snowflake account or a sandbox database with sample data only, and nothing
from a real company.

1. Prepare. Create an empty scratch repository on GitHub and clone it. Set the screen to
   a clean desktop with notifications off, a large terminal font (16 to 18 pt), and a
   window of about 1440x900. Have the `snow` connection and the deploy secrets ready so
   no credential is ever typed on screen.
2. Start recording, open Claude Code in the scratch repository and run `/onboard`. Let it
   set up the repo, the config and the one-time Snowflake objects. Cut the waiting in
   the edit, not the steps.
3. Run `/build-app` with a one-line idea, for example
   `/build-app "Acme weekly sales pipeline dashboard by region"`. Show each checkpoint
   (the spec, the preview, the validate and review result) and your OK at each.
4. Run `/ship-app`. Show the pull request it opens and the CI checks going green, then
   the merge.
5. Show the deploy workflow finishing, then open Snowsight and the live app.
6. Stop recording.

Tools: the macOS screen recorder (Shift-Command-5) or Kap for the whole screen; for a
terminal-only take, `vhs` (charmbracelet) scripts a reproducible recording from a tape
file. Trim the recording to 30 to 60 seconds, then convert it to a GIF at about 960 px
wide and 10 fps with a generated palette:

```bash
ffmpeg -i demo.mov -vf "fps=10,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=192[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" -loop 0 docs/images/demo.gif
```

Keep the result under 5 MB (lower the fps to 8, the width to 800, or `max_colors` to 128
if it is larger) and check every frame for account names, hostnames, emails and table
names before committing. Writing it to `docs/images/demo.gif` replaces the offline GIF,
and the README needs no change because it already points at that path.
