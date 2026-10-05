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

- `streamsnow sql-review compare <slug> --run <run_id> --capture <dir> [--screen screen.json]`.
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
