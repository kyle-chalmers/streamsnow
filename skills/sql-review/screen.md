# Screen: hold what the page shows to the reviewed SQL

`run` proves each section's aggregates; this step proves the page shows them. In review preview
mode the app's `review_value` calls record what each visual received (a row count, hashed column
names and column totals, or the number a metric shows; never a row), and `streamsnow sql-review
compare` holds each one to its `run` result. A browser walk adds a cross-check. Skip the whole
step with `--no-screen`.

Below, `<run_dir>` is `.streamsnow/sql-review/<slug>/<run_id>`.

## Steps

1. **Start the preview in review mode**, on any free port:
   `streamsnow preview start <slug> --port 0 --review-capture <run_dir>/capture --json`. Keep its
   `url`. `capture_mismatch` means the user already has this app's preview running: ask before
   `streamsnow preview stop <slug>`, never stop it on your own. A launch failure is reported by
   `streamsnow preview logs <slug>`; fix the environment or skip with `--no-screen`.
2. **Open every page once, at its default filters.** Never touch a widget: a changed filter
   overwrites the capture with numbers the review window does not describe.
   - With the Playwright CLI: follow the review variant in
     [playwright-walkthrough.md](../_shared/playwright-walkthrough.md), running the snippet below
     on each page.
   - Without it (no Node.js 20+): ask the user to open the `url` and click through each page
     once without changing a filter. Pages that never render capture nothing.
3. **Stop the preview**, always, also after a failure: `streamsnow preview stop <slug>`.
4. **Compare:** `streamsnow sql-review compare <slug> --run <run_id>`. It reads `<run_dir>/capture/`
   and, when present, `<run_dir>/screen.json`, and writes `compare.json` with one `compare:NN#n`
   id per metric. Exit `1` means at least one `mismatch`: hand them to the page reviewers as
   candidate findings, never log them yourself. Exit `2` with "nothing was captured" means no
   page rendered, or the app's `review.py` predates capture. A page that shares one loader and
   groups it shows fewer rows than the SQL: when every total it shows equals the SQL's and its
   group keys are non-numeric columns of the result that never repeat and keep every distinct
   value the run counted, that is a `match` with the rule `aggregated` (`match (aggregated)` in
   the log). A head or filtered slice stays a `mismatch`, even when its dropped rows sum to zero.
   Grouped visuals need a `run` and a `review.py` that record key counts: an older one reads as
   `mismatch`, so rerun `run` and refresh `review.py` from the current scaffold.

## Reading a page (the walk)

Save the snippet as `<run_dir>/walk/snippet.js` (it holds both quote styles, so it cannot be
pasted inside shell quotes), then run `P S eval "$(cat <run_dir>/walk/snippet.js)"` once the page's
script run has finished (the review variant says how to wait for it). It reduces every visual to counts in the browser, so no cell
value, label or chart point leaves the page:

```js
() => {
  const num = /^(?=.*\d)\(?[+\-\u2212]?[$\u20ac\u00a3\u00a5]?[+\-\u2212]?(?:\d{1,3}(?:,\d{3})+|\d{1,15})?(?:\.\d+)?\s?[kKmMbBtT%]?\)?$/;
  const kinds = '[data-testid="stMetric"],[data-testid="stDataFrame"],[data-testid="stTable"],' +
    '[data-testid="stVegaLiteChart"],[data-testid="stPlotlyChart"]';
  return [...document.querySelectorAll(kinds)].map((el) => {
    const kind = el.getAttribute('data-testid');
    if (kind === 'stMetric') {
      const v = (el.querySelector('[data-testid="stMetricValue"]')?.innerText ?? '').trim();
      return { source: 'metric', observed: v.length <= 40 && num.test(v) ? v : null };
    }
    if (kind === 'stDataFrame') {
      const grid = el.querySelector('[aria-rowcount]');
      return { source: 'dataframe', aria_rowcount: grid ? Number(grid.getAttribute('aria-rowcount')) : null };
    }
    if (kind === 'stTable') return { source: 'table', rows: el.querySelectorAll('tbody tr').length };
    if (kind === 'stVegaLiteChart') {
      // One mark per row for bars and points; axes are symbols too, and lines are not per row.
      const marks = [...el.querySelectorAll('[role="graphics-symbol"]')]
        .filter((m) => !m.closest('[aria-roledescription="legend"]'))
        .map((m) => m.getAttribute('aria-roledescription') || '')
        .filter((d) => !['axis', 'legend', 'title'].includes(d));
      const perRow = marks.length > 0 && marks.every((d) => !/line|area|trail|rule|text/.test(d));
      return { source: 'vega', marks: perRow ? marks.length : null };
    }
    return { source: 'plotly' };
  });
}
```

It returns the page's visuals in on-screen order. Tie each one to its metric with the page's
source: the visual that wraps `review_value("<key>", ...)` is that key's metric, and its number is
the key's position on the page in `index.yaml`. Visuals that wrap no `review_value` are skipped.
Write every page's readings into one `<run_dir>/screen.json`, and never repeat a reading in chat:

```json
{"visuals": [
  {"page": "01", "n": 1, "source": "metric", "observed": "$12.3K"},
  {"page": "01", "n": 3, "source": "dataframe", "aria_rowcount": 1001}
]}
```

`aria_rowcount` counts the header row, and `marks` counts bars or points (a line or area chart
gives `null`). `compare` keeps only `page`, `n`, `source`, `observed` (when it is a displayed
number), `rows`, `aria_rowcount` and `marks`. A disagreement there is a note in `compare.json` for the reviewers;
it never changes a status or reaches the log. Plotly charts are not read.

## What can make a screen differ from the SQL

- The SQL is wrong, or the page transforms the result (a slice, a rename, a division, a filter).
- The page's default filters no longer match the index's tokens and review window.
- The local preview reads as the user's local role, not the CI role, so a row access policy or
  a missing grant can change the numbers.
- The data changed between `run` and the walk. Rerun both when in doubt.
