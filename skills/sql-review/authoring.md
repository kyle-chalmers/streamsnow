# Build or repair an app's `sql_review/` page files

Give every page of `apps/<slug>/` a runnable SQL file, one section per metric, driven by
`apps/<slug>/sql_review/index.yaml` and `streamsnow sql-review`. **Read-only**: never mutates
Snowflake, never deploys. The tool owns everything deterministic: rendering, the read-only guard,
sqlfluff layout, provenance digests, the README tables, the marker and coverage checks. Your
judgment work is the **index** (which metric each visual is, its sample tokens and binds) and the
query comments. Data-correctness judgment is the live review that follows
([SKILL.md](SKILL.md), from step 4), code judgment is `/review-app`, and the ship gate is
`streamsnow validate-app`.

**`index.yaml` and `queries/*.sql` are the editing surface; a page file never is.** Each
`sql_review/NN_<page>.sql` carries a provenance digest, and `sql-review check` flags a hand-edited
body (or an index, query, nav entry or `.sqlfluff` that changed since generation) until
regenerated. The folder's own rules are in `apps/<slug>/sql_review/AGENTS.md`.

## Steps

1. Resolve the slug; confirm `apps/<slug>/queries/` exists. No `queries/*.sql` (a legacy app that
   inlines its SQL) → nothing to generate; suggest externalizing queries first and stop.
2. **Compute the gap:** `streamsnow sql-review check <slug>`. `coverage` warnings name each nav
   page missing from `index.yaml` and each query no metric uses; `marker` findings name visuals
   whose `review_value` key and the index disagree. An app with no `index.yaml` yet gets one
   `coverage` warning: start the index from the template below.
3. **Mark the visuals.** For each data visual on a page, wrap the value it shows in
   `review_value("<metric_key>", value)` (`from review import review_value`; the scaffold ships
   `review.py`, a no-op at runtime). Keys are snake_case, five words or fewer, and name the value
   (`revenue_by_region`), so they stay stable when the layout moves.
4. **Write the index: the judgment work.** Per page (its `st.Page` path), one entry per marked
   visual under `metrics:`, **in on-screen order** (the order sets the section numbers):
   - `query`: the `queries/<name>.sql` that feeds the visual. Two visuals on one query each get an
     entry.
   - `tokens`: a **real sample value** for every `{TOKEN}` the query has, whose literals satisfy
     the predicates (values the data actually contains, inside the review window), and that
     mirror the page's **default filter state** (an optional "All" is `""`). Otherwise a correct query and an empty one look
     the same when run.
   - `binds`: a value for every `:1` / `:name`. `params.start_date` reads the section's own
     `params` CTE; anything else is inserted as a SQL literal (`"'West'"`). An unused or missing
     bind is an `index` finding.
   - `reads`: every object the query reads, `DATABASE.SCHEMA.OBJECT`.
   - `notes`: a definition a reviewer needs ("booked date, not ship date"), when there is one.
   - **Omit `review_window` when no metric binds `params.*`.** It only feeds the `params` CTE;
     without a bind that reads it, the window is dead weight in every generated section.
   - **Anchor `review_window` to the data, always** (when you keep one): end it at the source's
     own latest date,
     `end_date: "(SELECT MAX(<date_col>) FROM <db>.<schema>.<table>)::DATE"` and
     `start_date: "(SELECT DATEADD('year', -1, MAX(<date_col>)) FROM <db>.<schema>.<table>)::DATE"`,
     reading the object and date column the page's default range uses (see the default-date-range
     rule in [page-conventions](../_shared/page-conventions.md)). A window ending at
     `CURRENT_DATE` returns zero rows for data whose latest date is in the past (a historical
     extract, a sample dataset like TPC-DS, a paused feed); use it only when today really is the
     anchor.
   - `fragments`: `[{file: queries/_shared_ctes.sql, reason: "..."}]` for a query file that is a
     CTE inlined via a token: it is not runnable alone, so no metric can use it. The reason is
     required. Never rename a query to dodge coverage; declare it.
   - `objects`: query the sources directly first. Propose an app-data object only with a
     `reason`: `reason: performance` (a dynamic table pre-computes a slow or costly query) or
     `reason: shared_logic` (one view replaces logic that two or more queries, pages or apps
     repeat). It goes here with its `grants` and `reason`, and its maintained DDL goes in
     `sql_review/app_specific_reporting_objects/` as `<DATABASE>.<SCHEMA>.<OBJECT>.sql` with the
     `-- Object:` / `-- Purpose:` / `-- Used by:` / `-- Grants:` header. The file holds one
     CREATE in a deployable form, then only `GRANT SELECT` on it to the roles in `grants`; never a
     table. For a dynamic table write `CREATE OR ALTER DYNAMIC TABLE ... WAREHOUSE =
     <default_warehouse> INITIALIZE = ON_CREATE AS ...`. For a view write
     `CREATE OR REPLACE VIEW ... COPY GRANTS`, the default: a view holds no data, so replacing it
     is cheap and always takes the new query. `CREATE OR ALTER VIEW` is accepted but not proposed.
     The deploy job applies these objects; `streamsnow objects-sql` shows what it would run.
     Objects outside app data keep today's rule: a human applies DDL outside app data, and you
     deploy it yourself only when the user says to.
5. **Comment the queries.** Every CTE gets a one-line comment directly above its name (for the
   first CTE, `WITH` on its own line first); every non-obvious filter, join or CASE a one-line
   "why". Never restate the SQL. `check` enforces the CTE comments and the 100-character limit,
   and lints each query with the repo's `.sqlfluff` (`sqlfluff fix` fixes most layout findings).
6. **Render:** `streamsnow sql-review generate <slug>`. It substitutes tokens and binds, adds each
   section's `params` CTE, applies sqlfluff's layout and capitalisation fixes, verifies the output
   is read-only, stamps provenance, refreshes the README tables and `Used by` lines, and removes
   page files no page needs any more. Read-only is enforced in two independent layers: a
   statement-root allowlist (`SELECT` / `WITH…SELECT` / `SHOW` / `DESCRIBE` / `EXPLAIN`), plus a
   tripwire that refuses a write verb in command position even if the parser is fooled. An invalid
   index, an unresolved `{TOKEN}`, a surviving bind, more than one statement in a query, or a
   write-shaped statement stops it with nothing written. Fix the index or query, never the output.
7. `streamsnow sql-review check <slug>` must be clean.
8. **Commit the page files** (with `index.yaml`, the markers and the query comments) before the
   live review: `probe` and `run` refuse page files that do not match what the app runs. Never
   run sections by hand to test them; `probe` compiles every section and `run` measures it.
   Without a connection, say the sections were generated from static analysis only. Never
   fabricate column lists: `probe` reports the real ones.
9. **Report the coverage delta**: pages and metrics covered, pages and queries still uncovered
   (`check` names them), and declared fragments.

## Judgment calls

- **One section per visual, whatever its join width**: a three-table join is one section; its
  `reads:` lists all three objects.
- **No `{TOKEN}`s and no binds in a query** → no `tokens:` or `binds:` needed. That's fine.
- **Zero rows in the review window** is a finding to report (the UI may render empty), not an
  error to fix here: the page reviewer judges it from the `run` result.

## Edge cases

- **Auth expires mid-run:** the live command exits 2 and names the failure; reconnect
  (`snow connection test`) and re-run it with the same `--run <run_id>`.
- **"Does not exist or not authorized" on a probe:** either genuinely missing or the role can't see
  it: run `streamsnow check schema-refs apps/<slug>` to confirm the reference is allowed, check
  grants, and report it rather than guessing columns.
- **A section errors or returns nothing when run:** a sample token doesn't match real data: fix it
  in `index.yaml` and regenerate; never patch the page file.
- `sql_review/` is review scaffolding, not app code: it isn't deployed and isn't loaded by the
  app's `sql_loader`; editing it never changes what ships. The generator refuses to emit any write
  statement into a page file, and `check` re-verifies committed files stay read-only.
