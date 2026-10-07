# streamlit-performance

Purpose: defaults that keep a page fast to open and cheap to run. Fit them to the app's data, runtime
and audience; the framing is in [page-conventions.md § Related defaults](page-conventions.md#related-defaults).
The gated rules (caching TTLs, bind predicates, page imports) stay in `streamsnow validate-app`.

**The goal:**
- The default view shows real numbers quickly on a warm cache.
- A filter change refetches only what it changes.
- No query runs for something nobody sees.

## Fetch less

- **Aggregate in SQL**, at the grain the chart draws. The warehouse does it in parallel next to
  the data; pandas does it per session after the transfer. Everything a page renders must fit the
  message cap: 32 MB on the warehouse runtime, 200 MB by default on the container runtime
  ([snowflake-docs.md](../../docs/snowflake-docs.md) D7).
- **Cap detail tables** with `ORDER BY … LIMIT n`, and say so in the caption ("top 500").
  An uncapped detail query grows until the page times out. The `ORDER BY` makes the cap
  deterministic.
- **Name columns instead of `SELECT *`.** Wide views keep gaining columns, and every one ships to
  every session.
- **Prefer a narrow governed view to wide joins.** When none exists, ask for one
  ([production-gotchas.md](production-gotchas.md)).

## Cache by what changes the answer

- **One `@st.cache_data(ttl=…)` loader per query.** Make every filter that changes the result an
  argument. The arguments are the cache key; a filter read from a closure or session state serves
  stale data, and on the container runtime another viewer's.
  - Set the TTL from the source's refresh cadence (REQUIREMENTS.md §8).
  - Pass small arguments (dates, tuples), not DataFrames, which are re-hashed on every call.
- **Know where the cache lives.**
  - **Container runtime:** one process serves every viewer, so the cache is shared. Add
    `max_entries=` to loaders keyed on high-cardinality filters, because the cache sits in
    compute-pool memory.
  - **Warehouse runtime:** every session starts cold. Snowflake's own result cache still answers
    a repeat of identical SQL and binds without warehouse compute, so keep query text stable.
  - Cross-session caching is container-only (D7). The loader's outer cache is the source of truth,
    so `conn.query(..., ttl=0)` ([runtime-decision.md](runtime-decision.md)).
- **`st.cache_resource` holds shared objects** such as clients and models. It returns the same
  object to every caller; `cache_data` returns a copy. Don't put data you mutate in it.

## Share across pages

- **Give data that several pages use one loader** in a shared module (e.g. `pages/_data.py`,
  imported package-qualified), backed by one `queries/*.sql`. Typical cases: date bounds, a base
  aggregate, the lists behind filters.
  - The cache keys on the function, so two copies of the same SQL are two queries. They also
    drift into two definitions of one number.
- **Fetch at the coarsest grain the pages share, then slice in Python when the slice is small.**
  A "fetch everything" loader defeats aggregating in SQL.
- **Before adding a query, check whether another page already loads that data:** compare the
  `Feeds` and `Schemas` header lines across `queries/*.sql`.

## Rerun less

Every widget interaction reruns the whole script.
- **Batch filter widgets in `st.form` only when each rerun is expensive** (an uncached query, heavy
  pandas work): one rerun per Apply.
  - Over cached data a rerun is cheap, so plain widgets are fine and a missing form is at most
    nice-to-have. A form costs the reader an Apply click on every filter change.
- **Wrap a self-contained widget and the chart it drives in `@st.fragment`,** so only that part
  reruns.
- **Tab and expander bodies run on every rerun by default, open or not.**
  - From Streamlit 1.55, `on_change="rerun"` plus the container's `.open` lets a page skip a
    closed one.
  - On an older runtime pin, gate the expensive view behind a widget (a toggle, a segmented
    control, a button) and call its loader only when it is selected.

## Slow page: usual causes

| Sign | Usual cause |
|---|---|
| First open slow, later fast | Cold cache. Check the TTL isn't shorter than the source's refresh |
| Every interaction slow | Uncached work on each rerun (pandas over row-level data, a loader without a cache), or, when the rerun itself is costly, filters outside a form |
| Slow only deployed | Warehouse resume or queueing, or a cold container start. Compare query history in Snowsight with the local run |
| Slower month by month | `SELECT *` on a growing view, or an unbounded date range |

## Further reading

- Streamlit: [Caching overview](https://docs.streamlit.io/develop/concepts/architecture/caching),
  [Working with fragments](https://docs.streamlit.io/develop/concepts/architecture/fragments),
  [Using forms](https://docs.streamlit.io/develop/concepts/architecture/forms),
  [st.tabs](https://docs.streamlit.io/develop/api-reference/layout/st.tabs) (lazy tabs)
- Snowflake: [Runtime environments](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/runtime-environments)
  (per-runtime caching),
  [Using persisted query results](https://docs.snowflake.com/en/user-guide/querying-persisted-results)
