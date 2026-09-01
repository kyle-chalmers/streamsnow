# Build phase — add one page to an app

Scaffold one page so its charts, KPIs, filters, and queries match the spec. Additive and
idempotent: never overwrite an existing page or query; leave the app lint-clean and previewable
with TODO placeholders the developer fills next. This is also the path for adding a page to an app
that's already live — `/start-app <slug>` resumes into this phase when §4 has an unbuilt page.

The spec is the contract: read §4 for the page's sections and the Charts/KPIs/Filters/Caching
sections for its visuals. Don't invent visuals that aren't specced — if the page isn't in §4 yet,
run the spec phase first ([spec.md](spec.md)) and resume.

## Steps

1. **Resolve target.** Confirm `apps/<slug>/` and its `REQUIREMENTS.md` exist. No spec → backfill
   one first (spec phase, automatic backfill mode). Page already exists as `pages/<page>.py` → stop;
   overwriting risks losing in-progress work.
2. **Detect the runtime** (anchored `runtime_name:` in `snowflake.yml`) — it decides the loader's
   connection pattern below.
3. **Scaffold the SQL stubs.** For each query the page needs, create `queries/<name>.sql` with the
   required header block and a loadable placeholder body:
   ```sql
   -- Query: <name>
   -- Feeds: <Page title> (<sections>)
   -- Schemas: <TODO: fill from §3 — must be on governance.schema_allow>
   -- Params: <TODO: :1 start_date … — or omit>
   -- Tokens: <TODO — or omit>
   SELECT 1 AS placeholder;
   ```
   The header is what the checks parse; the placeholder keeps the page rendering during preview.
   Leave `<TODO>` rather than guessing an object name; never pre-fill a denied schema. If two
   sections share a query, reuse the existing `.sql` — don't scaffold a duplicate.
4. **Generate the page module** `pages/<page>.py`: title + one-line caption, one branded
   metric/chart stub per §4 section, filters per §7, and one `@st.cache_data(ttl=...)`-wrapped
   loader per query calling the app's `sql_loader`. Match a sibling page's patterns. TTL = repo
   default unless §8 says otherwise (then cite it in a comment). Imports of app-root modules
   (`branding`, `sql_loader`) stay bare; anything you factor out into `pages/` is imported
   package-qualified (`from pages._header import ...`) — see Gotchas.
5. **Register the page**: add an `st.Page(...)` entry to the existing `st.navigation` structure in
   `streamlit_app.py`. Show the diff before applying and use multi-line `Edit` context so the match
   is unambiguous. One nav group → add to it; several → ask which.
6. **Run the checks on the new files** (`streamsnow check schema-refs|caching|bind-predicates
   apps/<slug>`) and fix anything flagged while it's cheap.
7. **Log it:** append a §11 session line (`page <name> scaffolded — queries TODO. Next: fill stubs,
   then /preview-app <slug>`). Don't commit yet — the page is a reviewable stub; the commit happens
   in step 8, once the real SQL lands.
8. **Fill the stubs, then land the page WITH its audit trail — one commit.** After the page's real
   query bodies replace the placeholders:
   1. `streamsnow sql-review discover <slug> --write` — proposes (and persists) a skeleton manifest
      under `apps/<slug>/sql_review/manifests/` for each query no manifest claims yet. It never
      overwrites an existing manifest, and exit 1 here just means gaps existed — that's why you ran it.
   2. **Improve the skeleton's dispatcher literals.** `discover` fills each `{TOKEN}` dispatcher with
      a `-- TODO: sample fragment for <TOKEN>` placeholder; replace every one with a real sample
      fragment the page actually renders (e.g. `AND region = 'West'` for a region filter) so the
      review SQL exercises the query the way the dashboard does. Fix the `description`/`pages`
      fields if the header's `Feeds` line was still a TODO.
   3. `streamsnow sql-review generate <slug>` — renders the paste-runnable
      `sql_review/<feature>.review.sql` files with provenance lines.
   4. Commit the page module, its `queries/*.sql`, the manifest(s), and the rendered `.review.sql`
      **together** — a page and its audit trail land together. Splitting them leaves a window where
      `streamsnow sql-review check` reads uncovered queries or drift, and a reviewer can't re-run
      the numbers behind the new visuals.
9. **End of the build phase** (all §4 pages built): `streamsnow sql-review index <slug>` rebuilds
   the `sql_review/README.md` coverage table so it reflects every page's queries; include the
   refreshed README in the final build commit.

## Connection pattern by runtime

(Per [_shared/runtime-decision.md](../_shared/runtime-decision.md); match what sibling pages do.)

```python
# container
conn = st.connection("snowflake")
return conn.query(sql, params=[start, end], ttl=0)   # ttl=0: outer cache is the source of truth

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
  keep a generic `<TODO>` or use an allowed schema.
- **Preview errors loading a query** — the placeholder body was replaced with invalid SQL, or a
  param/token in the loader isn't declared in the `.sql`. Restore the placeholder until the real
  query is ready.
