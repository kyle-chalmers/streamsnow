# `--sql` — write the audit-ready `sql_review/` companions

Author or extend `apps/<slug>/sql_review/` so every UI-feeding query has a paste-and-runnable
companion plus a lineage README. **Read-only** — never mutates Snowflake, never deploys. It's a
generator: data-correctness judgment lives in `/audit-lineage`, code judgment in the review pass,
the ship gate in `streamsnow validate-app`. Review and lineage passes run this automatically when
they detect a `sql_review/` gap; invoke `--sql` directly for the scaffolding alone.

## Steps

1. Resolve the slug; confirm `apps/<slug>/queries/` exists. No `queries/*.sql` (a legacy app that
   inlines its SQL) → nothing to generate; suggest externalizing queries first and stop.
2. **Compute the gap:** queries with no matching `sql_review/<name>.review.sql`. Leave existing
   companions untouched unless the user asks for a refresh.
3. **Read each gap query** — header block (Query / Feeds / Schemas / Params / Tokens), upstream
   `db.schema.object`(s), bind params, `{TOKEN}` fragments.
4. **Check for a connection** (`streamsnow doctor` / `snow connection list`). It's a branch, not a
   gate: with one, lineage rows get live-verified; without one, everything is written from static
   analysis and marked **unverified** — still useful, still honest. Never fabricate column lists.
5. **Write each companion** — paste-and-runnable in Snowsight, three bounded read-only sections,
   every `:N` and `{TOKEN}` substituted with a concrete sample literal (no placeholder that errors
   on paste):
   1. **EXPLAIN** — `EXPLAIN USING TEXT <query with sample literals>;` (pruning + join order, no scan)
   2. **Row-count** — `SELECT COUNT(*)` over the same narrow predicate window
   3. **Freshness** — `SELECT MAX(<the date column the query actually filters on>)` per upstream object
6. **Live-verify lineage when connected:** per upstream object, a zero-row resolve probe
   (`SELECT COUNT(*) FROM <fqn> WHERE 1=0`) and type/columns from `INFORMATION_SCHEMA`. `LIMIT`
   any row-returning probe; no DDL, no writes, nothing outside `governance.schema_allow`.
7. **Write/refresh `sql_review/README.md`:** one row per query → upstream object → feeds → review
   file → verified/unverified. Carry over pre-existing rows so the index stays complete.
8. **Report the coverage delta** — total, covered, still uncovered, live-verified vs. static.

## Judgment calls

- **Sample literals must satisfy the predicates** — pick them from the freshness window so the
  row-count section actually returns rows; otherwise a correct query and an empty one look the same.
- **One companion per source query, not per upstream object** — a three-table join gets one file
  with three freshness checks, mirroring the query's own shape.
- **No `{TOKEN}`s in a query** → the rendered companion is byte-identical to the source; that's fine.
- **Zero rows in the predicate window** is a finding for the README (the UI may render empty), not
  an error to fix here — route the judgment to `/audit-lineage`.

## Edge cases

- **Auth expires mid-run:** finish the remaining companions static-only, mark their rows
  unverified, and say how to upgrade them (`snow connection test`, re-run).
- **"Does not exist or not authorized" on a probe:** either genuinely missing or the role can't see
  it — run `streamsnow check schema-refs apps/<slug>` to confirm the reference is allowed, check
  grants, and leave the row unverified rather than guessing columns.
- **Companion errors on paste:** a `:N`/`{TOKEN}` slipped through — substitute a real literal and
  re-check the EXPLAIN parses.
- `sql_review/` is review scaffolding, not app code — it isn't deployed and isn't loaded by the
  app's `sql_loader`; editing it never changes what ships. Refuse to emit any write statement into
  a review file.
