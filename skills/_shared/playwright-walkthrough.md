# Playwright walkthrough

Purpose: drive a browser smoke walk, with the Playwright CLI, of every page in a running StreamSnow app, capturing a screenshot and console errors per page, with the `Data as of:` caption (or app-loaded state) as the success sentinel. This is a recipe other skills read and follow — not an invocable skill.

Consumed by: /validate-app and /preview-app (their optional UI-smoke sections), /review-app (incl. `--auto`), /build-app (verify phase and `--feedback`), /sql-review (the review variant below).

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
   log files land there too, in a subshell so your own working directory does not move:
   `(cd D && P S open <base_url> --browser=chromium --idle-timeout=600000)`.
   Always start at the app **root**, never a `/<page>` deep link (Streamlit serves the navigation
   shell from root; a direct page URL can render a stale or unbranded fallback). Then
   `P S resize 1280 4000`, a tall window, so most pages fit before step 3 grows it for the rest.
   Every later command writes to absolute paths under `D`, so they work from wherever you are.
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
     For pages without a freshness caption, wait for the first heading the same way. The caption
     can render before the data does, so then wait for the page's script run to finish:
     `P S run-code "async page => { await page.locator('[data-test-script-state=notRunning]').waitFor({ timeout: 60000 }); }"`.
   - Screenshot. Streamlit scrolls the page inside its own container, so `--full-page` captures
     the window, not the page: a page taller than the window loses its bottom, and a container
     left scrolled shows the middle. Reset every inner scroll to the top and grow the window by
     what the page's full-height container still overflows (not a table's own scroller, whose
     height covers all its rows), up to 16000 px, then capture the viewport and restore the window
     for the next page:
     `P S run-code "async page => { const extra = await page.evaluate(() => { let extra = 0; for (const e of document.querySelectorAll('*')) { if (!/auto|scroll/.test(getComputedStyle(e).overflowY)) continue; e.scrollTop = 0; if (e.clientHeight >= window.innerHeight * 0.9) extra = Math.max(extra, e.scrollHeight - e.clientHeight); } return extra; }); const v = page.viewportSize(); await page.setViewportSize({ width: v.width, height: Math.min(v.height + extra, 16000) }); await page.waitForTimeout(500); }"`,
     then `P S screenshot --filename=D/<page-stem>.png`, then `P S resize 1280 4000`.
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

## Review variant (/sql-review)

/sql-review's screen step ([sql-review/screen.md](../sql-review/screen.md)) walks a preview that
was started with `--review-capture`. Steps 1 to 5 apply, with these differences:

- `D` is `<run_dir>/walk/` (`<run_dir>` = `.streamsnow/sql-review/<slug>/<run_id>`), not
  `apps/<slug>/.review/`, and you delete `D` after `close`: the CLI's own `.playwright-cli/`
  snapshots there hold cell text, which must not outlive the walk.
- Never touch a widget, and take no screenshots: the page must render at its default filters,
  and a screenshot is a copy of the data.
- After step 3's two waits (the caption, then the finished script run), run screen.md's snippet
  once, from the file screen.md says to save it in:
  `P S eval "$(cat D/snippet.js)"`.
  It returns counts and displayed numbers only. Write the readings to `<run_dir>/screen.json`
  as screen.md says, and never repeat them in chat.
- The report is `ok` or `timeout` per page; there is no `report.md`.

## Notes

- Artifacts live under `apps/<slug>/.review/` (gitignored) — never commit screenshots or `report.md`.
- The walk is read-only: navigate, click sidebar entries, screenshot, read console. Do not submit forms that mutate state or trigger writes.
- Reuse the one session `S` across pages so a single Snowflake-authenticated browser covers the whole walk; a walk that dies before `close` shuts itself down after the 10-minute idle timeout.
