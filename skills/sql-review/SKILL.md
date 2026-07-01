---
name: sql-review
description: Bootstrap or extend an app's sql_review/ directory — for each query in queries/*.sql, write a paste-and-runnable .review.sql (EXPLAIN + bounded row-count + freshness) and a README mapping each query to its upstream object. Read-only. Use when the user says "bootstrap sql_review", "make a SQL review directory", "fill the NOT covered yet gap", "extend the sql_review folder", or "/sql-review".
---

# sql-review

Author or extend `apps/<slug>/sql_review/` so every UI-feeding query has an audit-ready, paste-and-runnable companion plus a lineage README. **Read-only** — this never mutates Snowflake and never deploys. It is a generator: the qualitative judgment about whether the data is *right* lives in /deep-dive-data, the static code review in /review-app, and the deterministic ship gate in `streamsnow validate-app`.

## When to use this vs. its siblings

- `/sql-review` (here) — you want *just* the paste-and-runnable companions + lineage index for an app. Mechanical, single-pass, no opinions.
- /review-app and /deep-dive-data run this same bootstrap automatically when they detect a `sql_review/` gap, so you rarely need to invoke it by hand. Reach for it directly when you want the scaffolding without the surrounding multi-dimension review.
- If you need a verdict on data correctness (real column sets, view-vs-base-table, predicate pushdown, cost), that is /deep-dive-data, not this skill.

## Steps

1. **Resolve `<slug>`.** If omitted, list `apps/*/` and ask which one. Confirm `apps/<slug>/queries/` exists. If there are no `queries/*.sql`, stop — there is nothing to review (see Edge cases for apps that inline all SQL).
2. **Compute the gap.** Enumerate sources with `ls apps/<slug>/queries/*.sql`, then existing companions with `ls apps/<slug>/sql_review/*.review.sql 2>/dev/null`. The queries with no matching `.review.sql` are the work. Leave existing companions untouched unless the user explicitly asks to refresh them.
3. **Read each gap query.** Parse its header block (Query / Feeds / Schemas / Params) and its body to learn the upstream `db.schema.object`(s), the bind params (`:1`, `:2`, …), and any `{TOKEN}` fragments the app substitutes at render time.
4. **Confirm a connection before any live check.** Run `streamsnow doctor` (or `snow connection list`). If none resolves, you can still write everything from static analysis — see step 7 — so this is a branch, not a hard stop.
5. **Write `apps/<slug>/sql_review/<name>.review.sql`** for each gap query — paste-and-runnable in Snowsight, three bounded read-only sections (see Companion shape). Substitute concrete sample literals for every `:N` bind and `{TOKEN}` so the file runs top-to-bottom as-is. Never leave a placeholder that errors on paste.
6. **Verify upstream lineage (connection present).** For each upstream object: confirm it resolves with a zero-row probe `snow sql -q "SELECT COUNT(*) FROM <db>.<schema>.<object> WHERE 1=0"`, and capture type + columns from `INFORMATION_SCHEMA.TABLES`/`COLUMNS`. Cap any sampling with `LIMIT`; never run an unbounded `SELECT *`. No DDL, no writes. Mark each row **verified**.
7. **Verify-or-defer (no connection).** With no connection, write the companions and README from static analysis only, mark every upstream lineage row **unverified**, and tell the user to re-run after `streamsnow configure` + `snow connection add`. Do not fabricate column lists.
8. **Write/refresh `apps/<slug>/sql_review/README.md`** as the full index: one row per query → upstream `db.schema.object` → feeds (from the header) → review-file name → lineage status (verified / unverified). Carry over rows for pre-existing companions so the README stays complete, not just the delta.
9. **Report the coverage delta:** queries total, companions now present, any still uncovered, and whether lineage was live-verified or static-only.

## Companion shape (`<name>.review.sql`)

Open with a comment naming the source query and its upstream object(s). Then three labelled, bounded, read-only sections — paste the whole file into a Snowsight worksheet and run top to bottom:

1. **EXPLAIN** — `EXPLAIN USING TEXT <the query with sample literals substituted>;` to inspect pruning and join order without scanning rows.
2. **Row-count** — `SELECT COUNT(*) …` over the same predicate window to sanity-check cardinality. Keep the window narrow so it stays cheap.
3. **Freshness** — `SELECT MAX(<date/ts col>) …` per upstream object so the reviewer sees how current the data is.

## Decision guidance

- **Sample literals must satisfy the predicates.** A bind that feeds `WHERE created_at >= :1` needs a date that actually returns rows in the row-count section, or the reviewer can't tell a correct query from an empty one. Pick literals from the freshness window, not arbitrary constants.
- **One companion per source query, not per upstream object.** A query that joins three tables gets one `.review.sql` with three freshness checks — mirror the query's own shape so the lineage is obvious.
- **Pick the freshness column the query filters on.** If the query prunes on `event_date`, the `MAX()` should be on `event_date`, not on a load-timestamp column — that's the column whose currency actually affects the UI.
- **Verified beats unverified, but unverified still ships value.** A static-only `sql_review/` with an honest "unverified" column is useful and auditable. Don't block on a missing connection; just be explicit about what wasn't checked.

## Notes

- **READ-ONLY always:** EXPLAIN, COUNT, MAX, `INFORMATION_SCHEMA`. Refuse to emit INSERT / UPDATE / MERGE / CREATE / DROP into a review file.
- **Respect governance.** Companions read the same approved objects the query does — never reach past the schema allowlist (`governance.schema_allow` / `schema_deny` in `streamsnow.config.yaml`). If unsure a referenced object is allowed, run `streamsnow check schema-refs apps/<slug>`.
- **`sql_review/` is review scaffolding, not app code.** It is not deployed and not loaded by the app's `sql_loader`. Editing it never changes what ships.
- **Qualified names follow config.** Companions resolve against `governance.database` and the app's configured warehouse/role — refer to them generically (`<database>.<schema>.<object>`), never hardcode environment-specific values.

## Edge cases

- **Empty `queries/` directory** (legacy app that inlines all SQL in Python). There's nothing to bootstrap. Stop and suggest the user externalize queries into `queries/*.sql` first, then re-run.
- **Query has no `{TOKEN}` placeholders.** Fine — the rendered companion is byte-identical to the source SQL (no substitutions). The EXPLAIN/COUNT/freshness sections and the lineage README are still worth having; no special handling.
- **Existing `sql_review/` plus newly added queries** (the common extend case). Only fill the gap. Don't regenerate companions the user already reviewed unless they ask.
- **Auth expires mid-run.** The first failing `snow sql` probe returns non-zero. Stop live verification there, finish the remaining companions static-only, mark their rows **unverified**, and tell the user to re-run after `snow connection test` to upgrade them to verified.
- **Object resolves but has zero rows in the predicate window.** That's a finding, not an error — note it in the README so a reviewer knows the UI feature may render empty. It is not this skill's job to fix it; hand to /deep-dive-data.

## Troubleshooting

- **Probe returns "does not exist or not authorized."** Either the object is genuinely missing or the configured role lacks grants. Run `streamsnow check schema-refs apps/<slug>` to confirm the reference is allowed, then check the role's grants. Mark the row **unverified** until resolved — don't guess columns.
- **`streamsnow doctor` passes but `snow sql` fails.** doctor checks local prerequisites, not live auth. Run `snow connection test` for the live check; refresh the connection if it's expired.
- **Companion errors on paste** (`{TOKEN}` or `:N` left in). A placeholder slipped through step 5. Substitute a concrete sample literal that satisfies the predicate and re-verify the EXPLAIN parses.

## Hand-offs

- /review-app and /deep-dive-data run this bootstrap automatically when they detect a gap; invoke /sql-review directly to fill the gap in isolation.
- Lineage findings here feed /deep-dive-data's live-DB tracing — the qualitative judgment lives there, not in this generator. If a probe surfaces a real correctness concern, route it to /deep-dive-data, which emits `BLOCK`/`FLAG`/`NICE-TO-HAVE` findings that /apply-review and /auto-review-app consume.
- /validate-app (`streamsnow validate-app <slug>`) remains the only deterministic ship gate; a populated `sql_review/` supports a review but does not replace that gate.

## Done when

Every `queries/*.sql` has a paste-and-runnable READ-ONLY `<name>.review.sql` (EXPLAIN + bounded row-count + freshness, with concrete sample literals), `sql_review/README.md` maps each query to its upstream object with a verified / unverified status, and the coverage delta is reported.
