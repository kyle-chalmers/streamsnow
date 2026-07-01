---
name: add-page
description: Add a page to an existing StreamSnow app from its REQUIREMENTS.md — generates pages/<page>.py with branded metric/chart stubs, scaffolds queries/*.sql with the required header block, and registers the page in streamlit_app.py's st.navigation. Use when the user says "add a page", "scaffold a page", "add page to <app>", or after /refine-requirements adds an entry to §4 Pages & Sections.
---

# add-page

Scaffold one new page into an existing app so its charts, KPIs, filters, and queries match the spec. This skill is **additive and idempotent**: it never overwrites an existing page or query, and it leaves the app in a lint-clean, previewable state with TODO placeholders the developer fills in next.

`REQUIREMENTS.md` is the contract — `/add-page` reads §4 (Pages & Sections) and the Charts/KPIs/Filters/Caching sections to generate code that `/review-app` and `/validate-app` later audit against. Do not invent visuals that aren't in the spec.

## When to use vs. siblings

- **/new-app** creates the app and its first page. Use **/add-page** for the second / Nth page after the app exists.
- **/refine-requirements** owns §4. If the page isn't specced yet, send the user there first, then resume.
- **/preview-app** is the immediate next step — confirm the page renders in the sidebar (with placeholders) before filling queries.
- **/validate-app** is the ship gate that must pass once the page is filled in.

## Steps

1. **Resolve the target.** Determine `<slug>` and the page name from the request. Confirm `apps/<slug>/` and `apps/<slug>/REQUIREMENTS.md` exist. If the slug is ambiguous, list `apps/*/` and ask. If `REQUIREMENTS.md` is missing, hand off to /refine-requirements (backfill mode against the existing source) first, then resume.

2. **Read the page spec.** In `apps/<slug>/REQUIREMENTS.md` §4, find the target page and extract its sections, metric cards, charts, filters, and the source schema(s) per visual. If the page isn't in §4, hand off to /refine-requirements to add it, then resume. If §4 lists the page but its sections are sparse, generate a minimal page (heading + one TODO placeholder) and note the gap in the output.

3. **Reject conflicts early.** If `apps/<slug>/pages/<page>.py` already exists, stop and tell the user to delete it manually or pick a different name. This skill does not overwrite — that risks losing in-progress work. Same rule for any `queries/<name>.sql` that already exists (keep it, note it in the summary).

4. **Detect the runtime.** This decides the connection pattern in the generated loaders (see "Runtime: container vs warehouse" below). Check `apps/<slug>/snowflake.yml` for an anchored `runtime_name:` key, or cross-check `streamsnow.config.yaml` for the configured default runtime.

5. **Scaffold the SQL stubs.** For each unique query the page needs, create `apps/<slug>/queries/<name>.sql` with the **required 5-line header block** and a valid placeholder body (see "SQL header contract"). Copy the header shape from an existing file under `apps/<slug>/queries/`. Source schemas must come from `governance.schema_allow` in `streamsnow.config.yaml` — leave a `<TODO>` for the real object name; never pre-fill a denied schema.

6. **Generate the page module.** Create `apps/<slug>/pages/<page>.py`: title + one-line caption, one branded metric/chart stub per §4 section, filters per the spec, and one `@st.cache_data(ttl=...)`-wrapped loader per query that calls the per-app `sql_loader`. Match the patterns in a sibling page under `apps/<slug>/pages/`. Default the TTL to the repo default unless the Caching section of REQUIREMENTS.md specifies otherwise for this page (then cite it in a comment).

7. **Register the page in navigation.** Add a `st.Page(...)` entry to the existing `st.navigation(...)` structure in `apps/<slug>/streamlit_app.py`. Do **not** introduce a `pages/` auto-discovery convention — StreamSnow uses explicit `st.navigation`. Show the user the diff (the `st.Page` declaration + the group-list insertion) before applying, and use multi-line `old_string` context so the `Edit` match is unambiguous. If only one user-facing group exists, add to it automatically; if there are several, ask which group.

8. **Run the governance checks on the new files** to catch problems while they're cheap:
   ```bash
   streamsnow check schema-refs apps/<slug>
   streamsnow check caching apps/<slug>
   streamsnow check bind-predicates apps/<slug>
   ```
   `schema-refs` blocks references to denied schemas; `caching` requires `@st.cache_data(ttl=...)` on data-fetching functions; `bind-predicates` blocks the `:N IS NULL OR` bind trap. Optionally run `streamsnow check security apps/<slug>` too. Fix anything they flag before handing off.

9. **Update §11 Build Progress** in `apps/<slug>/REQUIREMENTS.md`: mark the new page's status `scaffolded` (Notes: `queries TODO`), bump the last-updated/last-action line, and refresh the resume hint. If there's no §11, skip this step — don't synthesize one (that's /start-app or /refine-requirements' job).

10. **Hand off.** Run /validate-app on `<slug>` as the deterministic gate, and /preview-app to eyeball the page locally. Do **not** auto-commit — the page is still a stub.

## Runtime: container vs warehouse

The loader's connection pattern depends on the app's runtime (set in `snowflake.yml` / `streamsnow.config.yaml`):

- **Container runtime** (the default for newly scaffolded apps): use the Streamlit connection, and disable its internal cache so the outer `@st.cache_data` is the single source of truth.
  ```python
  conn = st.connection("snowflake")
  # inside a loader:
  return conn.query(sql, params=[start_date, end_date], ttl=0)
  ```
  The `ttl=0` matters: without it you get double caching and confusing staleness.
- **Warehouse runtime** (legacy): use the active Snowpark session.
  ```python
  from snowflake.snowpark.context import get_active_session
  session = get_active_session()
  # inside a loader:
  return session.sql(sql, params=[start_date, end_date]).to_pandas()
  ```

Match whatever pattern the app's existing pages already use — don't mix the two within one app.

## SQL header contract

Every generated `apps/<slug>/queries/<name>.sql` must carry the StreamSnow header block, then a valid placeholder body:

```sql
-- Query: <name>
-- Feeds: <Page title> page (<comma-separated section names>)
-- Schemas: <TODO: ANALYTICS.<OBJECT> — fill in from REQUIREMENTS.md §3, must be on governance.schema_allow>
-- Params: <TODO: :1 start_date, :2 end_date — or omit if no params>
-- Tokens: <TODO: TOKEN_A — or omit if not using render_sql>

-- TODO: write the query body
SELECT 1 AS placeholder;
```

The header is what `streamsnow check schema-refs` and `validate-app` parse — a query missing it fails the gate. The placeholder `SELECT` keeps the file loadable during preview so the page renders instead of erroring.

## Gotchas & edge cases

- **Optional "All" filters — never bind Python `None`.** The deployed warehouse driver mishandles `None` positional binds and can silently NULL-bind every param, so a page shows 0/0 KPIs deployed while working locally. For optional filters, compose a `{TOKEN}` SQL fragment via `render_sql` instead of binding a `None` sentinel. Related: `streamsnow check bind-predicates` blocks the sibling `:N IS NULL OR` trap.
- **Denied schemas.** Source schemas must be on `governance.schema_allow` in `streamsnow.config.yaml`. Leave `<TODO>` placeholders rather than guessing a real object; `check schema-refs` will reject a denied schema (e.g. a raw/landing schema) before commit.
- **Cast COUNT-type metrics to int** before formatting so a card reads `23` not `23.0` (e.g. `f"{int(count):,}"`).
- **Shared queries.** Two sections may legitimately feed off one query. Reuse the existing `.sql` rather than scaffolding a duplicate; note the reuse in the summary.
- **Default landing page.** `default=True` is rare and judgment-driven — don't auto-set it. If the user wants the new page to be the landing page, have them flip the existing default in a one-line manual edit after scaffolding.
- **§4 group label drifts from the live nav.** If the spec's group name doesn't match an existing `st.navigation` dict key, the spec drifted. Ask which is canonical and recommend updating REQUIREMENTS.md to match the implementation.
- **Expected lint noise.** Freshly scaffolded stubs often have unused-import warnings (a branding helper imported but not yet called). Those are expected and resolve once the page is filled in — don't auto-strip imports the developer is about to use.

## Troubleshooting

- **`check schema-refs` flags a TODO line** → you left a denied/real schema in the `-- Schemas:` header. Replace it with an allowed schema from `governance.schema_allow` or keep a generic `<TODO>` placeholder.
- **`check caching` flags a loader** → the data-fetching function is missing `@st.cache_data(ttl=...)`. Wrap it; container-runtime loaders also need `ttl=0` on the `conn.query` call.
- **Page doesn't appear in the sidebar after preview** → the `st.Page(...)` line was added but not inserted into a `st.navigation` group list, or the `Edit` matched the wrong block. Re-check `streamlit_app.py` and reapply with more surrounding context.
- **`Edit` aborts as ambiguous** → the same closing bracket/line appears multiple times in the nav dict. Widen the `old_string` to a unique multi-line block, or print the diff and ask the user to apply manually.
- **Preview errors loading a query** → the placeholder body was edited to invalid SQL before the real query was written, or a param/token referenced in the loader isn't in the `.sql`. Restore the `SELECT 1 AS placeholder;` body until the real query is ready.

## Done when

`pages/<page>.py` and its `queries/*.sql` exist with valid 5-line headers, the page is registered in `st.navigation` and shows in the sidebar, `streamsnow check schema-refs|caching|bind-predicates apps/<slug>` pass on the new files, §11 reflects the new page as scaffolded, and the flow has handed off to /validate-app and /preview-app. No commit yet — the page is a reviewable stub.

## References

- Page spec / §4 / §11 schema: /refine-requirements
- Scaffolding the app itself: /new-app
- Verify it renders: /preview-app · Ship gate: /validate-app · Qualitative audit: /review-app
- Governance checks: `streamsnow check schema-refs|security|caching|bind-predicates`
