# streamlit-performance

Purpose: the defaults that keep a page fast to open and cheap to run. Each one says why, so a repo
can override it knowingly. The repo's overlay or `AGENTS.md` wins over anything here. These are
defaults, not gates: `/review-app` raises departures, nothing blocks on them. The gated rules
(caching TTLs, bind predicates, page imports) stay in `streamsnow validate-app`.

**What "fast" means here:**
- The page under its default filters shows its first real numbers quickly on a warm cache.
- A filter change does not refetch data it already has.
- The warehouse runs no query a viewer never sees.

## Fetch less

- **Aggregate in SQL, not pandas.** Return the rows the chart draws, at the grain it draws them:
  daily totals for a daily line, not the line items behind them.
  - *Why:* the warehouse does the work in parallel near the data. Pandas does it one viewer at a
    time after the bytes cross the wire.
  - *Also:* results must fit the message cap, 32 MB on the warehouse runtime and 200 MB by default
    on the container runtime ([snowflake-docs.md](../../docs/snowflake-docs.md) D7).
- **Cap detail tables.** A drill-down table gets an explicit `LIMIT` and a caption saying so
  ("Top 500 by balance").
  - *Why:* an uncapped detail query is the one that grows until the page times out.
- **Name the columns.** No `SELECT *`.
  - *Why:* a wide view grows over time, and every new column is shipped to every viewer.
- **Prefer a narrow governed view** to joining wide ones in the page's query. If none exists, ask
  for one rather than building it in the app ([production-gotchas.md](production-gotchas.md)).

## Cache by what changes the answer

- **One `@st.cache_data(ttl=...)` loader per query,** with every filter that changes the result as
  a function argument.
  - *Why:* the cache key is the arguments. A filter read from a closure or session state serves
    one viewer's slice to another.
  - The TTL follows the source's refresh cadence (REQUIREMENTS.md §8). `streamsnow check caching`
    enforces that the decorator and TTL exist; the right TTL value is a judgment.
- **Pass hashable, minimal arguments:** ISO date strings and tuples, not DataFrames or widget
  objects.
  - *Why:* Streamlit hashes every argument on every rerun.
- **Know where the cache lives.** Cross-session caching is container-only (D7).
  - **Container runtime:** one process serves every viewer, so a warm cache is shared and the
    first viewer after expiry pays for everyone.
  - **Warehouse runtime:** each session starts cold, so query cost per viewer matters more there.
- **`st.cache_resource` for clients and models, never for data.**
  - *Why:* it returns the same object to every caller, so a mutated DataFrame leaks across
    viewers.

## Share across pages

When two pages need the same data, they should share one loader, not each have their own copy.

- **Put shared loaders in one module** (e.g. `pages/_data.py`, imported package-qualified): the
  date bounds, a base aggregate several pages slice, the dimension lists that feed filters.
  - *Why:* `st.cache_data` keys on the function and its arguments. Two loaders with identical SQL
    are two cache entries and two warehouse queries. One loader called from both pages with the
    same arguments is one query.
- **Fetch once at the coarsest grain several pages need, then slice in Python** when the slice is
  small. For example, one daily-by-region aggregate feeds both the trend page and the regional
  breakdown.
  - *Don't over-share:* a "fetch everything" loader that every page filters down defeats
    "aggregate in SQL".
- **Share query files too.** Two pages reading the same `queries/<name>.sql` keep one definition
  of the metric, which is also what keeps their numbers identical.
  - *Why:* duplicated SQL drifts, and two pages then disagree about the same number.
- **Look for duplicates whenever a page is added.** Check whether its data is already loaded
  elsewhere before writing a new query: compare the `Feeds` and `Schemas` header lines across
  `queries/*.sql`.

## Rerun less

Every widget interaction reruns the whole page script.

- **Batch multi-filter pages in `st.form`.** One rerun per "Apply", not one per widget touched.
- **Wrap an independent widget and the chart it drives in `@st.fragment`.** Only that fragment
  reruns: for example, a metric picker that re-draws one chart without refetching the page's
  KPIs.
- **Put expensive, rarely opened views behind a tab or expander, with their loader called
  inside.**
  - *Why:* code at the top level runs on every rerun whether or not the viewer opens it.
- **Load the date bounds once.** `MAX(<date_col>)` from its own cached query, per
  [page-conventions.md](page-conventions.md), rather than inside each loader.

## Keep module state safe

- **Make no `st.*` call at import time,** and give shared modules (`branding.py`,
  `pages/_glossary.py`) idempotent setup.
  - *Why:* the container runtime imports a module once for every viewer.
- **Per-viewer state lives in `st.session_state`,** never in a module-level variable.

## Signs a page is slow, and the usual cause

| Sign | Usual cause |
|---|---|
| First open slow, later opens fast | Cold cache: expected; check the TTL isn't shorter than the data's refresh |
| Every filter click slow | Loader not keyed on the filter (refetches everything), or widgets not in a form |
| One page slow, others fine | A detail query with no cap, or pandas aggregation of row-level data |
| Slow only deployed | Warehouse size or queueing. Compare the query's time in Snowsight query history with the local run |
| Gets slower month by month | `SELECT *` on a growing view, or an unbounded date range |
