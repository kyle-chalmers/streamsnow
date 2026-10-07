# Build phase — the parallel build, and how one page is built

## Parallel build

The orchestrator builds every §4 page at once and owns every file pages share; each
**page-builder** ([briefs/page-builder.md](briefs/page-builder.md)) owns one page.

1. **Owners.** The orchestrator: `streamlit_app.py`, `sql_review/` (`index.yaml` and everything
   `generate` writes), `pages/_*.py`, `pages/about.py`, shared queries and REQUIREMENTS.md. Each
   page-builder: `pages/<page>.py` and the queries the design lists for that page alone.
   `index.yaml` and the navigation are shared on purpose: every generated review file hashes the
   whole index, and its `NN` comes from the navigation order.
2. **Dispatch** one page-builder per page in one message (`mode: build`), each with its design
   entry, its `page_queries` and the shared loaders available.
3. **Merge** the returns: reject any write outside a builder's own files; add `nav_entry` to
   `st.navigation` in §4 order (About stays last); add `index_entry` under `pages:`; add
   `glossary_entries` to `pages/_glossary.py`; resolve `data_requests` by adding the loader to
   `pages/_data.py` and re-dispatching the pages that asked.
4. **Generate once:** `streamsnow sql-review generate <slug>`, then `streamsnow sql-review check
   <slug>` and the page-builder brief's five `check` commands. Fix shared files yourself; send page
   findings back to that page's builder in `fix` mode.
5. **Commit the round** (pages, their queries, `index.yaml`, the generated review files, the
   shared files you changed) and log it in §11 (`Next: verify`).

Without subagents, build the pages one at a time with the steps below; you then own every file.

## One page

Scaffold one page so its charts, KPIs, filters, and queries match the spec. Additive and
idempotent: never overwrite an existing page or query; leave the app lint-clean and previewable
with TODO placeholders the developer fills next. This is also the path for adding a page to an app
that's already live — `/build-app <slug>` resumes into this phase when §4 has an unbuilt page.

The spec is the contract: read §4 for the page's sections and the Charts/KPIs/Filters/Caching
sections for its visuals. Don't invent visuals that aren't specced — if the page isn't in §4 yet,
run the spec phase first ([spec.md](spec.md)) and resume.

### Steps

In a parallel build, steps 5, 7, 8.2 and 8.4–9 are the orchestrator's (above): the page-builder
returns their inputs instead of editing shared files.

1. **Resolve target.** Confirm `apps/<slug>/` and its `REQUIREMENTS.md` exist. No spec → backfill
   one first (spec phase, automatic backfill mode). Page already exists as `pages/<page>.py` → stop;
   overwriting risks losing in-progress work. The one exception is the scaffold's own starter
   `pages/overview.py` (it still contains `YOUR_TABLE` or the `STREAMSNOW_STARTER_PLACEHOLDER`
   sample block): the first page replaces it, see
   [Replace the starter trio](#replace-the-starter-trio).
2. **Detect the runtime** (anchored `runtime_name:` in `snowflake.yml`) — it decides the loader's
   connection pattern below.
3. **Scaffold the SQL stubs.** For each query the page needs, create `queries/<name>.sql` with the
   required header block and a loadable placeholder body:
   ```sql
   -- Query: <name>
   -- Feeds: <Page title> (<sections>)
   -- Schemas: <TODO: fill from §3, each a governance.sources entry>
   -- Params: <TODO: :1 start_date … — or omit>
   -- Tokens: TOKEN_NAME (what it filters), no braces; omit the line when there are none
   SELECT 1 AS placeholder;
   ```
   The header is what the checks parse; the placeholder keeps the page rendering during preview.
   Leave `<TODO>` rather than guessing an object name; never pre-fill a denied schema. If two
   sections share a query, reuse the existing `.sql` — don't scaffold a duplicate.
4. **Generate the page module** `pages/<page>.py` following the four-block page contract and the
   single-source metric definitions in [_shared/page-conventions.md](../_shared/page-conventions.md):
   title + caption, a caption under every subheader, filters per §7, a sources + "Data as of"
   footer; one branded metric/chart stub per §4 section and one `@st.cache_data(ttl=...)`-wrapped
   loader per query calling the app's `sql_loader`. Match a sibling page's patterns. TTL = repo
   default unless §8 says otherwise (then cite it in a comment). Imports of app-root modules
   (`branding`, `sql_loader`) stay bare; anything you factor out into `pages/` is imported
   package-qualified (`from pages._header import ...`) — see Gotchas. Use the scaffold's shared
   modules rather than writing your own: each metric gets a `pages/_glossary.py` entry and its
   `help=`; the page ends with `definitions_expander(<its keys>)` and `sources_footer(...)` from
   `pages/_layout.py`; the period picker is `date_range` from `pages/_time_controls.py`; data
   another page also reads goes in `pages/_data.py`. Add `show_sql(...)` under a visual when §2
   says the readers check numbers themselves (analysts), passing the same SQL and binds the loader
   runs.
5. **Register the page**: add an `st.Page(...)` entry to the existing `st.navigation` structure in
   `streamlit_app.py`. Show the diff before applying and use multi-line `Edit` context so the match
   is unambiguous. One nav group → add to it; several → ask which.
6. **Run the checks on the new files** — `streamsnow check schema-refs apps/<slug>`, then
   `streamsnow check caching apps/<slug>`, then `streamsnow check bind-predicates apps/<slug>`,
   then `streamsnow check sql-tokens apps/<slug>` (four invocations, not a pipeline), and fix
   anything flagged while it's cheap.
7. **Log it:** append a §11 session line (`page <name> scaffolded — queries TODO. Next: fill stubs,
   then /preview-app <slug>`). Don't commit yet — the page is a reviewable stub; the commit happens
   in step 8, once the real SQL lands.
8. **Fill the stubs, then land the page WITH its review SQL: one commit.** After the page's real
   query bodies replace the placeholders:
   1. **Mark each visual.** Wrap the value each visual shows in
      `review_value("<metric_key>", value)` (`from review import review_value`), e.g.
      `st.metric("Revenue", review_value("total_revenue", total))`. Keys are snake_case, five
      words or fewer, and name the value shown. It is a no-op at runtime.
   2. **List the page in `apps/<slug>/sql_review/index.yaml`.** Add the page's `path` (as in its
      `st.Page(...)`) with one entry under `metrics:` per marked visual, in on-screen order: `key`,
      `query` (`queries/<name>.sql`), `tokens` (a sample value for every `{TOKEN}` the page
      renders, mirroring the page's default filter state: an optional "All" is `""`, so
      `REGION_FILTER: ""` when the page opens on All regions), `binds` (every `:1`/`:name`, usually
      `params.start_date`), `reads` (the objects it reads), and `notes` when a definition needs
      one. Keep `review_window` anchored to the data's latest date, never today:
      `end_date: "(SELECT MAX(<date_col>) FROM <db>.<schema>.<table>)::DATE"`. Leave
      `review_window` out entirely when no metric binds a `params.*` value: an unused window
      is dead weight in every generated section. A view or table you
      built for this app goes under `objects:` with its DDL in
      `sql_review/app_specific_reporting_objects/` (see that folder's rules in
      `sql_review/AGENTS.md`).
   3. **Comment the queries.** Every CTE gets a one-line comment directly above its name; every
      non-obvious filter, join or CASE gets a one-line "why". `check` enforces the CTE comments and
      the 100-character limit, and lints each query with the repo's `.sqlfluff`.
   4. `streamsnow sql-review generate <slug>`: writes `sql_review/NN_<page>.sql` (one runnable
      section per metric) and refreshes the README tables. Then `streamsnow sql-review check <slug>`
      must be clean.
   5. Commit the page module, its `queries/*.sql`, `index.yaml`, and the generated page file and
      README **together**: a page and its review SQL land together. Splitting them leaves a window
      where `check` reads drift or uncovered pages, and a reviewer can't re-run the numbers behind
      the new visuals.
9. **End of the build phase** (all §4 pages built): `streamsnow sql-review check <slug>` reports no
   `coverage` warning, so every page in the nav is in `index.yaml`. A warning for a helper query that
   shows no value on screen (a date-bounds or filter-options loader in `pages/_data.py`) is expected:
   leave it, and never invent a metric to clear it. Then finish the app's own documents:
   - **App `AGENTS.md`:** rewrite the Pages and Queries sections to list the real pages and
     queries (the scaffold's text names `pages/overview.py` and `queries/example_metric.sql` and
     tells the reader to replace them), and fill the Data notes the design recorded.
   - **Repo `README.md`:** add the app's row to the Apps table (title and `apps/<slug>/`),
     replacing the `_(none yet)_` row when it is still there.

## Replace the starter trio

`streamsnow new` leaves three placeholder files. The **first** page built replaces all three, in the
same commit as that page (step 8), whatever the page is called:

1. **`pages/overview.py`** (sample numbers). If the first §4 page is the landing or overview page,
   write it into `pages/overview.py`, replacing the starter content entirely. Otherwise create
   `pages/<page>.py`, put its `st.Page(...)` entry in `streamlit_app.py` in place of the starter's
   `st.Page("pages/overview.py", ...)` entry (keeping `default=True`: the starter's default was a
   placeholder, so the default-page gotcha below does not apply), and delete `pages/overview.py`.
2. **`queries/example_metric.sql`** (reads `YOUR_TABLE`). Delete it once the page's real queries
   exist. Never repoint it into a real query under the example name.
3. **The `example_metric` entry in `sql_review/index.yaml`**, and the `YOUR_TABLE` in its
   `review_window`. Replace them with the real page's entry and window (step 8.2), then run
   `streamsnow sql-review generate <slug>`: it removes the stale `sql_review/01_overview.sql`
   when no page needs it, and `check` reports a leftover page file as an orphan. Keep the
   `pages/about.py` entry (`metrics: []`): the About page stays in the navigation.

The About page is not part of the trio. When the build phase ends, fill its `ABOUT` constants
from REQUIREMENTS.md (§1 purpose, §2 audience, the owner, §4 each page and its question, known
caveats); its definitions and data sources fill themselves from the glossary and query headers.

Then `streamsnow validate-app <slug>` must PASS: its `placeholders` check FAILS while any query,
page or `index.yaml` still carries `YOUR_TABLE` or the starter page's sample block. Do not grep the
app folder for the token instead: the app's own `AGENTS.md` names `YOUR_TABLE` in its
instructions, so a correct app still matches.

The starter's text lives in one more place. The app `AGENTS.md` Pages and Queries sections still
describe the starter page and `example_metric.sql`: rewrite them in the same commit as the first
page (the end-of-build step below finishes them for every page).

## Connection pattern by runtime

(Per [_shared/runtime-decision.md](../_shared/runtime-decision.md); match what sibling pages do.)

```python
# container
conn = st.connection("snowflake")
return conn.query(sql, params=[start, end], ttl=0)  # ttl=0: outer cache is the source of truth

# warehouse
from snowflake.snowpark.context import get_active_session

return get_active_session().sql(sql, params=[start, end]).to_pandas()
```

## Gotchas

- **Optional "All" filters:** never bind Python `None` — compose a `{TOKEN}` fragment via
  `render_sql` instead. Deployed, the driver NULL-binds every param when one is `None`; the page
  shows 0/0 KPIs deployed while working locally.
- **Shared helper in `pages/`:** import it package-qualified (`from pages._header import ...`).
  Bare (`from _header import ...`) resolves under `streamlit run`, which also puts the executing
  page's own directory on `sys.path`, then `ModuleNotFoundError`s on every page deployed, where only
  the app root is. A local boot and a full click-through both pass — `streamsnow check page-imports`
  is the only thing that catches it. Don't name the helper after an app-root module either.
- **`st.navigation` runs before any data call.** A loader above it (a date-bounds query in
  `streamlit_app.py`, say) leaves Streamlit's fallback menu of every `pages/*.py` helper on screen
  for the whole first run, because the real navigation does not exist until the loader returns.
  Build the `st.navigation(...)` call first, then load data.
- **Cast COUNT-style metrics to int** before formatting (`f"{int(n):,}"`) so a card reads `23`, not `23.0`.
- **Don't auto-set `default=True`** on the new page; if it should be the landing page, the user
  flips the existing default in a one-line manual edit.
- **§4 group label vs. live nav drift:** if the spec's group doesn't match an `st.navigation` key,
  ask which is canonical and update the spec to match the implementation.
- **Fresh-stub lint noise** (unused imports about to be used) is expected — don't strip them.

## Troubleshooting

- **Page missing from the sidebar after preview** — the `st.Page` entry didn't land inside a
  `st.navigation` group list; re-check `streamlit_app.py` with wider `Edit` context.
- **`check schema-refs` flags a TODO line** — a real or denied schema was left in the header;
  keep a generic `<TODO>` or use a governance source.
- **Preview errors loading a query** — the placeholder body was replaced with invalid SQL, or a
  param/token in the loader isn't declared in the `.sql`. Restore the placeholder until the real
  query is ready.

## Build loop

`/build-app` runs these itself between CP1 and CP3; the user only types at the checkpoints.

1. **Preview:** `streamsnow preview start <slug>` (`--json` for the URL and status). It prefers the
   repo's `.venv/bin/streamlit`; on exit 1 read the classified hint (no default `snow` connection,
   bad account locator, missing package) before improvising, then `streamsnow preview logs <slug>`.
   Hand over the root URL and ask for the click-through — that is CP2, not something to skip.
2. **Validate:** `streamsnow validate-app <slug>`; on FAIL, follow `/validate-app`'s fixing guide
   and re-run until PASS. Never weaken a rule to get there.
3. **Review:** follow `/review-app`'s instructions in full for this app ("run" a skill means follow
   its procedure — there is no skill-invocation tool). Read its overlay first if the repo has one.
   Report the verdict at CP3; `--auto` only when the user asked for the hands-off loop, because it
   spends minutes and, with lineage, Snowflake credits.
4. **Stop the preview** (`streamsnow preview stop <slug>`) once the user is done clicking through.
