# SQL review redesign, phase 3: screen comparison

Spec: `docs/superpowers/specs/2026-10-04-sql-review-redesign.md` (decision 5, "Verified facts"
on screen readability, `compare` verb). Requires phase 2 merged. One PR, version **0.9.x**
(additive).

## Goal

The review log's "screen match" column is filled: the value each visual actually received is
compared to the `run` result for its metric, deterministically via the `review_value` capture,
with a no-code Playwright cross-check.

## Task 1: capture in `review_value`

- `apps/{slug}/review.py`: when `STREAMSNOW_REVIEW_CAPTURE=<dir>` is set, write
  `<dir>/<page_stem>__<key>.json` containing `{key, page, kind, row_count, columns, headline}`
  where `headline` uses the same aggregation as `run` (row count, sums of numeric columns) so
  `compare` matches like for like. Scalars record `{value}`. DataFrame handling without a
  top-level pandas import (duck-type `.shape`, `.columns`, `.select_dtypes`).
- Never write rows. Overwrite per rerun. Swallow and log its own errors; a capture failure must
  never break the page.
- Disabled path unchanged; the phase-1 benchmark test still passes. Add a test that capture is
  impossible in Streamlit in Snowflake (env var absent there; document that SiS never sets it).

## Task 2: review preview mode

- `streamsnow/tools/preview_app.py`: `start --review-capture <dir>` passes
  `env={**os.environ, "STREAMSNOW_REVIEW_CAPTURE": dir}` to `Popen` (`:492-498`), and
  `--port 0` picks a free port (state file already records the port).
- Tests: env reaches the child (fake Popen), free-port selection.

## Task 3: Playwright walk with capture

- Extend `skills/_shared/playwright-walkthrough.md` with a review variant: start preview with
  capture, visit pages in `app_nav` order, wait for `Data as of:`, and for each visual read
  no-code values: `st.metric`/`st.table` DOM text; `st.dataframe` hidden accessible table plus
  `aria-rowcount`; Vega-Lite ARIA labels. Save to `<run_dir>/screen.json`. Plotly: skip with
  `unsupported` (out of scope).
- Playwright not loaded → skip the walk; capture JSON alone still drives `compare`.

## Task 4: `compare` verb

- `sql-review compare <slug> --run <run_id> --capture <dir> [--screen screen.json]`.
- Per metric: `match | mismatch | not_captured | unsupported`, with tolerance: relative 0.5%
  or the displayed rounding (derive decimals from the screen string), whichever is looser;
  integers exact. IDs `compare:<page>#<n>` for agents to cite.
- `log` fills the "screen match" column and lists mismatches as evidence-backed findings
  candidates for the page reviewer.
- Tests: rounding cases (`$12.3K` vs 12345.67, `45%` vs 0.4512), row-count match on virtualized
  dataframes via `aria-rowcount`, missing capture.

## Task 5: skill, docs, release

- `/sql-review` pipeline gains: preview with capture → walk → `compare` between `run` and the
  reviewers. `--no-screen` skips it.
- Docs and CHANGELOG; version bump in the four files.
- Done when CI is green and a manual run on a real app shows matches in the log.

## As built (deviations from this plan)

Recorded when phase 3 landed. The plan was checked by two sub-agents first (one against the spec,
the phase plans and the phase-2 code, one adversarial on correctness, privacy, Windows and the
test rules), and their findings are folded in below.

- **Version:** no bump in the feature PR; the maintainer releases separately.
- **Playwright CLI, not the MCP:** the walk is a review variant of
  `skills/_shared/playwright-walkthrough.md`, with its DOM snippet in `skills/sql-review/screen.md`.
  Without the CLI the user opens each page once; the capture alone still drives `compare`.
- **The capture is the evidence; the walk a cross-check.** `compare` writes `compare.json` with
  `compare:NN#n` ids (citable through `_RESULT_FILE_RE`). `screen.json` is agent-written:
  `compare` keeps only its counts and displayed numbers, records whether they agree, and never
  lets them change a status, reach the log, or become evidence.
- **Capture location and identity:** `<run_dir>/capture/` by default (`--capture DIR`). Files are
  `<stem>__<key>.json`, the page found as the innermost app file on the call stack (`check`
  requires the call in the page file), so any `st.Page` path works; `compare` matches by page
  path, then stem, then a key-only file only when the key is unique in the app.
- **Privacy:** captures hold a row count, sha256-hashed column names (a pivoted frame's columns
  can be data values) and column totals; text is kept only when it is a displayed number. The
  walk's snippet reduces everything to counts in the browser and the walk folder is deleted.
- **Safety in Streamlit:** `review.py` classifies values by type, never by attribute lookups
  (a Snowpark DataFrame's `__getattr__` and Snowpark pandas' `.shape` can run queries), has no
  annotations (the warehouse runtime allows Python 3.9), and swallows and logs its own errors.
- **Matching:** scalars only against a one-row single total or a row count (anything else is
  `unsupported`); integers exact give or take the displayed rounding; `%` as a ratio, and as
  points only when the run value is above 1; frame totals by hashed name, then by value, with a
  `values-ambiguous` rule when a value pairing could go two ways; a null total equals a zero.
- **Log:** the Screen match column shows status words only, or `stale` when a page's
  `run-NN.json` changed after the comparison; `log` lists uncited screen mismatches but never
  logs them.
- **Preview:** `--port 0` retries once on a fresh port if the free port is taken before
  Streamlit binds it; a preview running with another capture setting is `capture_mismatch`.
- **No users before 1.0:** the maintainer confirmed there are no users yet, so there is no path
  to refresh `review.py` in existing apps; the policy is now written in `CONTRIBUTING.md` and
  `docs/versioning.md`.
- **Real check (2026-10-05, Streamlit 1.59.2, Playwright CLI at the pinned version):** a freshly
  scaffolded Acme app on `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS` with four marked visuals (a KPI
  frame, a formatted `$34.46B` text metric, a 5-row dataframe with lower-cased columns, a 13-bar
  Vega chart). Preview on `--port 0` with capture, a walk, `compare` against a live `run`: 4 of 4
  `match`, helper `current`, the walk agreeing on all three visuals it can judge. It confirmed
  that `aria-rowcount` counts the header row (6 for 5 rows), and found two things the recipe
  now handles: Vega axes are `graphics-symbol`s too (the snippet counts only bars and points),
  and the `Data as of:` caption renders before the data, so the walk also waits for
  `data-test-script-state=notRunning`. Slicing the 5-row table to 3 rows in the page made
  `compare` exit 1 with a row-count mismatch, as it should.
- **Code review before the PR** (a fresh sub-agent on the full diff) found, and this PR fixes:
  column names that collide once normalised (`Orders`, `Orders %`) overwrote a total, so repeats
  are now numbered and marked `collided` and pair by value only; the display-number regex
  backtracked on long whitespace, so text is stripped and capped at 40 characters and the
  pattern has no adjacent `\s*`; a NaN on screen matched a zero; malformed captures crashed
  `compare`; a re-run page left the log `stale` with no prompt to compare again.
