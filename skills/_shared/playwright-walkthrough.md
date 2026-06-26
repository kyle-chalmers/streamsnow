# Playwright walkthrough

Purpose: drive a browser smoke walk of every page in a running StreamSnow app, capturing a screenshot and console errors per page, with the `Data as of:` caption (or app-loaded state) as the success sentinel. This is a recipe other skills read and follow — not an invocable skill.

Consumed by: /validate-app (`--ui`), /review-app, /auto-review-app, /preview-app (`--smoke`).

## Preconditions

- A Playwright MCP must be loaded in the session (its `browser_*` tools are visible). If not, **degrade**: emit the one-liner below and return control to the caller with `ui_walk: skipped`. Never block, never error.
  > Playwright MCP not loaded — skipping UI walkthrough. Static checks still ran. To enable, add a Playwright MCP to `.mcp.json` and restart the session.
- The app must already be serving locally. The caller owns launch via `streamsnow preview <slug>` (see /preview-app); this recipe assumes a reachable base URL. If none was passed, ask the caller for the local URL rather than launching one.

## Inputs

- `slug` — the app under `apps/<slug>/`.
- `base_url` — the running app's root URL (from /preview-app's launch output).
- `pages` — optional list to limit scope; default is all pages. Derive from `streamlit_app.py`'s `st.navigation` page list (read the file; do not hardcode). `--diff` callers pass only diff-affected pages.

## Steps

1. Resolve `base_url`. Always start at the app **root** — never a `/<page>` deep link (Streamlit serves the navigation shell from root; a direct page URL can render a stale/unbranded fallback).
2. Read `apps/<slug>/streamlit_app.py` and extract the ordered page list from `st.navigation`. Map each to its sidebar label for clicking.
3. For each page in scope:
   - Navigate to root (first page) or click the page's sidebar entry; wait for network/render to settle.
   - Wait for the success sentinel: the `Data as of:` caption is visible, or — for pages without a freshness caption — the page's title/first heading has rendered and no spinner remains.
   - Capture a full-page screenshot to `apps/<slug>/.review/walkthrough-<ts>/<page-stem>.png`.
   - Collect console messages; record any `error`-level entries with the page name.
   - Note any visibly empty section, render exception, or missing `column_config` formatting (raw numbers without separators, unformatted dollars).
4. Bound the walk: cap at ~30s wait per page; if the sentinel never appears, record the page as `timeout` and move on rather than hanging.

## Output contract

Write `apps/<slug>/.review/walkthrough-<ts>/report.md` (gitignored) and return a short summary to the caller:

- Per page: `ok` | `console-errors` | `render-error` | `empty` | `timeout`, plus screenshot path.
- Aggregate: pages walked, pages with issues, total console errors.
- Any finding here is **advisory/qualitative** — it informs /review-app and /auto-review-app judgment; it does NOT override `streamsnow validate-app`, which remains the deterministic PASS/FAIL ship gate. A walkthrough issue never flips validate to FAIL on its own.

## Notes

- Artifacts live under `apps/<slug>/.review/` (gitignored) — never commit screenshots or `report.md`.
- The walk is read-only: navigate, click sidebar entries, screenshot, read console. Do not submit forms that mutate state or trigger writes.
- Reuse one browser context across pages so a single Snowflake-authenticated session covers the whole walk.
