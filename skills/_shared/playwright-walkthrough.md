# Playwright walkthrough

Purpose: drive a browser smoke walk, with the Playwright CLI, of every page in a running StreamSnow app, capturing a screenshot and console errors per page, with the `Data as of:` caption (or app-loaded state) as the success sentinel. This is a recipe other skills read and follow — not an invocable skill.

Consumed by: /validate-app and /preview-app (their optional UI-smoke sections), /review-app (incl. `--auto`), /feedback-app.

Pinned version: `@playwright/cli@0.1.22`. This is the only place the version is written; every
other mention says "the pinned version in _shared/playwright-walkthrough.md". Below, `P` means
`npx -y @playwright/cli@0.1.22` and `S` means `-s=streamsnow-<slug>-<ts>` (one session per walk:
session names are machine-wide, and `open` on a name already in use closes that browser).

## Preconditions

- The Playwright CLI runs through `npx`, so it needs Node.js 20+ with `npx` (doctor's `node`
  row). If that is missing, **degrade**: emit the one-liner below and return control to the
  caller with `ui_walk: skipped`. Never block, never error.
  > Playwright CLI unavailable (Node.js 20+ with npx is needed), so the UI walkthrough was skipped. Static checks still ran. To enable it, run `/onboard`.
- The app must already be serving locally. The caller owns launch via `streamsnow preview <slug>` (see /preview-app); this recipe assumes a reachable base URL. If none was passed, ask the caller for the local URL rather than launching one.
- Ignore the CLI's update and install banners: never install `@latest` or install globally, since
  that would break the pin.

## Inputs

- `slug`: the app under `apps/<slug>/`.
- `base_url`: the running app's root URL (from /preview-app's launch output).
- `pages`: optional list to limit scope; default is all pages. Take titles and order from
  `streamsnow nav <slug>` (do not hardcode). Diff-scoped callers pass only the diff-affected pages.

## Steps

1. Make `D = <repo>/apps/<slug>/.review/walkthrough-<ts>/` (an absolute path; `.review/` is
   gitignored). Open the browser from inside it, so the CLI's own `.playwright-cli/` snapshot and
   log files land there too: `cd D && P S open <base_url> --browser=chromium --idle-timeout=600000`.
   Always start at the app **root**, never a `/<page>` deep link (Streamlit serves the navigation
   shell from root; a direct page URL can render a stale or unbranded fallback). Then
   `P S resize 1280 4000`: Streamlit scrolls inside its own container, so a full-page screenshot
   of a normal-height window captures only the first screen. Every later command writes to
   absolute paths under `D`, so your working directory never matters again.
2. If `open` fails because the browser is not installed (the error names `install-browser`), run
   `P install-browser chrome-for-testing` once (no sudo needed) and retry. Still failing: degrade
   as above, with the error's first line.
3. For each page in scope:
   - The first page is the root you opened: no click (an app with one page has no sidebar). For
     later pages, click the sidebar link:
     `P S click "getByTestId('stSidebarNav').getByRole('link', { name: '<label>' })"`. If that
     fails (top navigation), retry with `getByRole('link', { name: '<label>' })` alone. Escape any
     `'` in the label. With more than about 10 pages, click "View more" in the sidebar first.
   - Wait for the success sentinel, the `Data as of:` caption:
     `P S run-code "async page => { await page.getByText('Data as of').first().waitFor({ timeout: 30000 }); }"`.
     For pages without a freshness caption, wait for the first heading the same way.
   - Screenshot: `P S screenshot --full-page --filename=D/<page-stem>.png`.
   - Console: `P S console error > D/<page-stem>-console.log`, then `P S console --clear`, so each
     page's errors stay with that page. Read the log; record `error`-level entries with the page
     name (an analytics call blocked by a proxy is noise, not an app error).
   - Note any visibly empty section, render exception, or missing `column_config` formatting (raw
     numbers without separators, unformatted dollars).
4. Bound the walk: the 30s sentinel wait is the cap per page; if it times out, record the page as
   `timeout` and move on rather than hanging.
5. Always finish with `P S close`, also after a failure.

## Output contract

Write `apps/<slug>/.review/walkthrough-<ts>/report.md` (gitignored) and return a short summary to the caller:

- Per page: `ok` | `console-errors` | `render-error` | `empty` | `timeout`, plus screenshot path.
- Aggregate: pages walked, pages with issues, total console errors.
- Any finding here is **advisory/qualitative** — it informs /review-app judgment; it does NOT override `streamsnow validate-app`, which remains the deterministic PASS/FAIL ship gate. A walkthrough issue never flips validate to FAIL on its own.

## Notes

- Artifacts live under `apps/<slug>/.review/` (gitignored) — never commit screenshots or `report.md`.
- The walk is read-only: navigate, click sidebar entries, screenshot, read console. Do not submit forms that mutate state or trigger writes.
- Reuse the one session `S` across pages so a single Snowflake-authenticated browser covers the whole walk; a walk that dies before `close` shuts itself down after the 10-minute idle timeout.
