# README media

Scripts that produce the images the top-level README and the GitHub social preview use.
They are maintainer tools, not part of the `streamsnow` package, and they are not run in
CI. Every image is regenerated from real sources (the real CLI, a real end-to-end run, the
hand-authored logo), so rerun them when the CLI output or a skill's output changes.

| Output (in `docs/images/`) | Produced by | Source |
| --- | --- | --- |
| `logo-light.svg`, `logo-dark.svg` | hand-authored, no script | the SVGs themselves |
| `social-preview.png` (1280x640) | `render_social.py` | `social_preview.html` |
| `demo-terminal.svg` | `render_terminal.py` | the real offline CLI flow |
| `demo.gif` | `render_gif.py` + `claude_flow.py` | a real Claude run, with its app screenshots in `assets/sales-performance/` |
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

Run from the repo root after `uv sync --extra dev`. Playwright is pulled in for the one run
with `uv run --with`, so nothing is added to `pyproject.toml`.

```bash
# Terminal transcript SVG: only the dev environment is needed.
uv run python scripts/readme_media/render_terminal.py

# Social preview PNG.
uv run --with playwright python scripts/readme_media/render_social.py

# Demo GIF: needs ffmpeg on PATH as well.
uv run --with playwright python scripts/readme_media/render_gif.py

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
- `render_gif.py` plays the StreamSnow journey in a Claude desktop chat: `/build-app` (with
  its checkpoints and subagents), `/review-app`, `/preview-app` (the app in a local browser
  window) and `/ship-app`, then the pull request's checks, the merge, the deploy run's steps,
  and the app live in a Snowsight frame. It stitches title card, scenes and end card into a
  looping 960x600 GIF at 10 fps with an ffmpeg palette (about 48 seconds, about 1.3 MB).
  Pass `--work DIR` to keep the frames, or `--width` to scale.
- `claude_flow.py` holds those scenes and their words. The words are condensed from one real
  end-to-end run over the TPC-H sample (`SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`), with account, user
  and repository names replaced by the fictional `acme-analytics` ones; the app pictures in
  `assets/sales-performance/` are that run's preview screenshots, cropped and quantized. Edit
  the `STEPS` list when a skill's output changes. Every scene passes `check_clean()`, which
  refuses home paths, Snowflake account URLs and email addresses outside `example.com`.
  Replacing a screenshot means checking it by eye for account names, hostnames and emails.
- `export_excalidraw.py` renders each `.excalidraw` file to a PNG beside it with
  Excalidraw's own exporter (`@excalidraw/utils`) in headless Chromium at 2x, in the
  diagrams' Space Grotesk face. It downloads the library from jsDelivr; where that is
  blocked, `npm install @excalidraw/utils@0.1.5` somewhere and pass `--lib-dir` to its
  folder. A PNG over `--max-kb` (default 500) is re-saved as a palette image.
  The diagrams use the logo's palette on the deep navy `#07182A` canvas: Snowflake blue
  `#29B5E8` for the main steps and arrows, its tints `#BFE6F6` and `#9CCFE6` for secondary
  boxes and labels, ice `#E8F4FA` for titles and body text, and coral `#D97757` only for
  what you do or own (your input, checkpoints, your files).
  Each diagram embeds `logo-dark.svg` as an image element in its top-right corner; when the
  logo changes, re-embed it in the three files and re-export.

## Record the full Claude Code to Snowflake demo

`demo.gif` draws the whole path from a real run's words and screenshots. A screen recording
of the same path (an idea typed into Claude Code, the governed app it builds, the pull
request, and the live app in Snowsight) shows the real interface instead, and it needs a real
account, so it is recorded by hand.
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
names before committing. Writing it to `docs/images/demo.gif` replaces the rendered GIF;
update the README's alt text to describe the recording.
