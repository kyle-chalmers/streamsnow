# Auditing a visual — trace any number on a dashboard back to the data

Every StreamSnow app carries SQL you can run to check it: for each page of the
app there is one file under `apps/<slug>/sql_review/`, with one runnable
section per metric the page shows, in on-screen order. You do not need the
app's code, a local Python environment, or StreamSnow itself to use it: just
DataGrip, Snowsight or any SQL editor, and a role that can read the app's
schemas. This page is the runbook for the person who looks at a chart and asks
*"is that number right?"*

## The five-minute audit

1. **Find the page's file.** Files are named `NN_<page>.sql`, where `NN` is
   the page's position in the app's navigation: the third page in the sidebar
   is `03_<page>.sql`. `apps/<slug>/sql_review/README.md` lists every page
   file, every metric (with the app query behind it and the objects it reads),
   and any objects built just for this app.

2. **Find the metric's section.** Metrics are numbered in on-screen order, and
   each section starts with a tag line: `--2_orders_by_region` is the second
   metric on that page, whose key in `index.yaml` is `orders_by_region`. The
   first section always starts on line 9 (its tag) and line 10 (its SQL); the
   header above it names the page, the app, and the metrics.

3. **Run that section on its own.** In DataGrip or another SQL IDE, put the
   cursor inside the section and press Cmd/Ctrl+Enter. In Snowsight, paste the
   section (from its tag to its `;`) into a worksheet. Every section is
   self-contained: its `params` CTE at the top holds the review window, and
   the app's `{TOKEN}` filters and `:bind` values are already filled in with
   the sample values recorded in `index.yaml`, so nothing else needs running
   first.

4. **Compare like with like.** To audit a different date range, edit the
   `params` CTE of the section you are running; never anything else. Set the
   dashboard to the same range and filters (the section's tokens show the
   filter values it uses), then compare the aggregates.

5. **Check freshness before crying foul.** A mismatch is very often a date
   window or a filter, not a data bug: confirm the dashboard's selected range
   matches the section's `params` CTE, and its filter widgets match the
   section's filter values. The app's `AGENTS.md` has a Data notes section
   (grain, definitions, quirks, when sources load). If the numbers still
   disagree, you have a real finding: file it via `/build-app <slug> --feedback`
   (quoting the page file and section tag you ran), or run the live review,
   `/sql-review <slug>`, below.

## What you can trust about these files

- **Each section is generated from the app's own query.** The tool reads the
  query file the app loads (`queries/<name>.sql`), substitutes the sample
  token values and bind values from `index.yaml`, and adds the `params` CTE. A
  provenance digest at the end of every page file pins `index.yaml`, those
  query files, the page's navigation entry and the sqlfluff settings, and CI
  fails when any of them changes without a regenerate, or when the page file
  itself is edited by hand. The token samples are the index author's
  assertion of what the app renders, kept honest by code review.
- **Every visual is accounted for.** Page code marks each visual's value with
  `review_value("<key>", value)`, and CI fails when a marked visual has no
  section, or a section has no visual.
- **They are read-only by construction.** The generator refuses to emit
  anything but `SELECT`/`WITH…SELECT`/`SHOW`/`DESCRIBE`/`EXPLAIN`, and CI
  re-verifies committed files. Running a section cannot write anything.
- **The DDL folder is the exception, on purpose.**
  `sql_review/app_specific_reporting_objects/` holds the maintained
  `CREATE` statements for views or tables built to feed this app, one file per
  object, with its purpose, the sections that read it, and its grants. No
  review command ever runs them; a person applies them.

## For the developer on the other side of this

The page files are **not** the editing surface: `apps/<slug>/sql_review/index.yaml`
and the app's `queries/*.sql` are. Edit them, then
`streamsnow sql-review generate <slug>`. The folder's `AGENTS.md` has the
format and rules. A minimal index:

```yaml
schema_version: 2
app: acme-sales
review_window:            # anchor to the data's latest date, not today
  start_date: "(SELECT DATEADD('year', -1, MAX(order_date)) FROM ANALYTICS.REPORTING.ORDERS)::DATE"
  end_date: "(SELECT MAX(order_date) FROM ANALYTICS.REPORTING.ORDERS)::DATE"
pages:
  - path: pages/overview.py            # as in st.Page(...)
    metrics:                           # on-screen order
      - key: total_revenue             # matches review_value("total_revenue", ...)
        query: queries/total_revenue.sql
        tokens: {REGION_FILTER: "AND region = 'West'"}
        binds: {"1": params.start_date, "2": params.end_date}
        reads: [ANALYTICS.REPORTING.ORDERS]
        summary: |                     # optional: exactly what the visual shows
          -- The Revenue tile, rounded to whole dollars as shown
          SELECT ROUND(SUM(revenue), 0) AS revenue_shown
          FROM detail
objects: []                            # views/tables built for this app, with DDL files
```

`summary:` is for a visual that derives its number from the query's result (a
card that sums a wide query, a chart that regroups it). `generate` writes it
as a second section right after the metric's own, so a reader runs the
section the app's query feeds and then the one that returns the numbers on
screen:

```sql
-- The Revenue tile, rounded to whole dollars as shown
--1_total_revenue_summary
WITH detail AS (
    WITH params AS (
        ...the --1_total_revenue section above, verbatim...
    )
    SELECT ...
)

SELECT ROUND(SUM(revenue), 0) AS revenue_shown
FROM detail;
```

Its first line is a one-line description, printed above the tag. `detail` is
the section above, verbatim, so the summary cannot drift from what the app
runs. `compare` holds a shown number to the summary's first column. Name its
outputs apart from `detail`'s columns: Snowflake reads a bare `revenue` as the
column, never as a same-named alias.

`streamsnow sql-review check` keeps it honest: every finding carries a
`kind` (`index`, `provenance`, `marker`, `objects`, `lint`, `comments`,
`readonly`, `bind`, `coverage`, `advisory`). All but the last two always fail,
in pre-commit, the generated CI and `streamsnow validate-app` alike, because
each means the committed review SQL no longer matches what the app runs or
breaks the folder's rules (queries are linted with the repo's `.sqlfluff`, and
every CTE needs a one-line comment above it). Whether a *coverage* gap (a page
or query `index.yaml` does not account for) fails or only warns is your
repo's `sql_review.coverage` setting: `warn` by default so a fleet backfills
on its own schedule, `fail` once coverage is where you want it. `advisory`
never fails. `/sql-review` (or `/review-app --sql`) is the assisted path that
builds the index and marks the visuals, then reviews it live.

## The live review: `/sql-review`

`/sql-review <slug>` checks every number against Snowflake and leaves a record
a person signs. It runs on request and is recommended before a release or a
deploy; it never blocks one.

1. **Facts, from tools.** `streamsnow sql-review probe <slug>` checks that every
   object the index names exists, that the app's role has a direct grant on
   it, that each view in `app_specific_reporting_objects/` matches what is
   live, and that every section compiles. `streamsnow sql-review run <slug>`
   runs every section wrapped in an aggregate: the row count, a total per
   numeric column and an order-insensitive hash come back; rows never do. Both
   run as the app's CI role with secondary roles off, tag their queries
   `streamsnow:sql-review:<slug>`, and refuse anything that is not read-only
   or that reads a denied schema before it is sent.
2. **The screen, held to the SQL.** `streamsnow preview start <slug> --port 0
   --review-capture <dir>` runs the app in review preview mode: each
   `review_value` call records what its visual received (a row count, hashed
   column names and column totals, or the number a metric shows; never a
   row). After every page has been opened once at its default filters,
   `streamsnow sql-review compare <slug>` holds each capture to its `run`
   result: within 0.5% or the displayed rounding (`$12.3K` covers 12,250 to
   12,350), integers exactly. A mismatch (a sliced frame, a renamed and
   dropped column, a forgotten `* 100`, defaults that drifted from the index)
   goes to the reviewers as a candidate finding. A browser walk with the
   Playwright CLI adds a cross-check that never counts as evidence. Streamlit
   in Snowflake never captures: an app there cannot set the flag.
3. **Judgment, cited.** Reviewer agents read the page files and those results
   (one per page, one per reporting object, an optimizer for sections slower
   than 10 seconds). Every finding cites the ids of the results it rests on, and
   a verifier in a fresh context tries to refute each one.
4. **A record, signed.** `streamsnow sql-review log` refuses a finding whose
   evidence is not in the run, then writes
   `sql_review/review_log/YYYY-MM-DD_<sha>.md`: the commit, connection, role and
   warehouse; a table per page (metric, SQL status, rows, headline, screen
   match from `compare`, findings); the verified findings by severity; and a sign-off block a
   person fills in. Headlines show totals only for a single all-numeric row or
   a result of ten rows or more, never a small-group breakdown. The README's
   "Latest review" links the newest log.

The run's evidence stays on your machine under `.streamsnow/sql-review/`, which
ignores itself in git. An optimization is proposed only with
`streamsnow sql-review bench` showing the rewrite returns the same rows and
hash, with before and after timings; nobody is ever told to buy a bigger
warehouse.

**Upgrading from 0.7.** The 0.7 format (`sql_review/manifests/*.json` and
`*.review.sql` files) was removed in 0.8.0 with no automatic migration.
`check` reports an app still on it as an `index` finding. Write
`sql_review/index.yaml` (one entry per visual; a manifest's `set_block` becomes
`review_window`, its `token_dispatchers` literals become per-metric `tokens`,
its `param_bindings` become `binds`), wrap each visual in `review_value`, add
`review.py` to the app (`streamsnow sql-review helper <slug> --apply` writes it; list it in
`snowflake.yml` artifacts), run `streamsnow update` for the repo's `.sqlfluff`,
then `streamsnow sql-review generate <slug>`: it deletes the old
`*.review.sql` files. Delete `sql_review/manifests/` yourself.
