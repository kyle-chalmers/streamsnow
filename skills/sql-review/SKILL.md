---
name: sql-review
description: Bootstrap or extend an app's sql_review/ directory — for each query in queries/*.sql, write a paste-and-runnable .review.sql (EXPLAIN + bounded row-count + freshness) and a README mapping each query to its upstream object. Read-only. Use when the user says "bootstrap sql_review", "make a SQL review directory", "fill the NOT covered yet gap", "extend the sql_review folder", or "/sql-review".
---

# sql-review

Author or extend `apps/<slug>/sql_review/` so every UI-feeding query has an audit-ready, paste-and-runnable companion plus a lineage README. Read-only — this never mutates Snowflake.

## Steps

1. Resolve `<slug>`. If omitted, list `apps/*/` and ask which one; confirm `apps/<slug>/queries/` exists. If there are no `queries/*.sql`, stop and tell the user there is nothing to review.
2. Enumerate the source queries: `ls apps/<slug>/queries/*.sql`. Then list what already exists: `ls apps/<slug>/sql_review/*.review.sql 2>/dev/null`. The gap (queries with no `.review.sql`) is the work; existing companions are left alone unless the user asks to refresh them.
3. For each gap query, read its header block (Query / Feeds / Schemas / Params) and body to learn the upstream objects, bind params, and `{TOKEN}` fragments.
4. Write `apps/<slug>/sql_review/<name>.review.sql` — paste-and-runnable in Snowsight, READ-ONLY, three bounded sections (see Companion shape). Substitute concrete sample literals for `:1`/`:2` binds and `{TOKEN}` fragments so it runs as-is; never leave a placeholder that errors on paste.
5. Confirm a connection is configured before any live check: `streamsnow doctor` (or `snow connection list`). If none, write the companions and README from static analysis only, mark upstream lineage **unverified**, and tell the user to re-run after `streamsnow configure`.
6. With a connection, validate each upstream object read-only and bounded: `snow sql -q "SELECT COUNT(*) FROM <db>.<schema>.<object> WHERE 1=0"` to confirm it resolves, and an `INFORMATION_SCHEMA.TABLES`/`COLUMNS` lookup to capture type + columns. Never run an unbounded `SELECT *`; cap any sampling with `LIMIT`. No DDL, no writes.
7. Write/refresh `apps/<slug>/sql_review/README.md`: one row per query → upstream `db.schema.object` → feeds (from the header) → review-file name → lineage status (verified/unverified). Carry over rows for existing companions so the README stays the full index.
8. Report the coverage delta: queries total, companions now present, any still uncovered, and whether lineage was live-verified or static-only.

## Companion shape (`<name>.review.sql`)

Three labelled, bounded, read-only sections — paste the whole file into a Snowsight worksheet and run top to bottom:

1. **EXPLAIN** — `EXPLAIN USING TEXT <the query with sample literals substituted>;` to inspect the plan (pruning, joins) without scanning rows.
2. **Row-count** — `SELECT COUNT(*) ...` over the same predicate window to sanity-check cardinality; keep the window narrow.
3. **Freshness** — `SELECT MAX(<date/ts col>) ...` per upstream object so the reviewer sees how current the data is.

Open with a comment naming the source query and its upstream object(s).

## Notes

- READ-ONLY always: EXPLAIN, COUNT, MAX, `INFORMATION_SCHEMA`. Refuse to emit INSERT/UPDATE/MERGE/CREATE/DROP into review files.
- Respect the schema allowlist — companions read the same approved objects the query does; never reach past them. Run `streamsnow check schema-refs apps/<slug>` if unsure.
- `sql_review/` is review scaffolding, not app code; it is not deployed and not loaded by `sql_loader`.

## Hand-offs

- /review-app and /deep-dive-data run this bootstrap automatically when they detect a gap; invoke /sql-review directly to fill the gap in isolation.
- Lineage findings here feed /deep-dive-data's live-DB tracing — the qualitative judgment lives there, not in this generator.

## Done when

Every `queries/*.sql` has a paste-and-runnable READ-ONLY `<name>.review.sql`, `sql_review/README.md` maps each query to its upstream object with a verified/unverified status, and the coverage delta is reported.
