---
name: deep-dive-data
description: Live-DB lineage and surface-fidelity check for a StreamSnow app — for each Snowflake object the app queries, trace upstream/downstream and verify the columns the app expects actually exist, via bounded read-only `snow sql`. Use when the user says "trace the data", "deep-dive this dashboard", "lineage for <slug>", or "deep-dive-data".
---

# deep-dive-data

Confirm the live Snowflake objects an app queries actually match what the code assumes — columns, lineage, filtering, cost — using bounded read-only queries. This is the LIVE-DB tier of review: it sees what static reviewers can't (real column sets, view definitions, predicate pushdown). It is qualitative and never blocks the ship — `streamsnow validate-app` is the only deterministic gate. Pairs with /review-app (static-only sibling) for full coverage.

## Preflight

1. **Resolve the `<slug>`** (ask if absent); confirm `apps/<slug>/` exists. Read `apps/<slug>/AGENTS.md` and `REQUIREMENTS.md` (if present) for the app's data-access conventions and the runtime it targets.
2. **Confirm a live connection** — this skill is useless without one:
   - `streamsnow doctor` verifies prerequisites, or `snow connection list` shows the connection named in `snowflake.connection_name` (read it from `streamsnow.config.yaml` via `streamsnow config-get snowflake.connection_name`).
   - If no connection resolves, STOP. Point the user at `streamsnow configure` + `snow connection add`, and offer the static-only path via /review-app instead.
3. **Detect the runtime mode** from `apps/<slug>/snowflake.yml`. Warehouse-runtime and container-runtime apps get different materialization advice (see step 8) — note which one this app uses before tracing.

## Discovery — what does the app query?

4. **Build the object → expected-columns map.** Grep `apps/<slug>/queries/**/*.sql` and `apps/<slug>/**/*.py` for `FROM`/`JOIN` references (fully-qualified `<database>.<schema>.<object>`) and the columns each query selects, filters, joins, or groups on. SQL files carry header blocks (`-- Feeds:`, `-- Schemas:`, `-- Params:`) — read them to learn the downstream surface and bind params. Dedupe into one object set; if the app touches an unusually large number of objects, that breadth is itself a FLAG-worthy architectural smell.
5. **Governance-gate before touching the DB.** Run `streamsnow check schema-refs apps/<slug>`. Anything resolving to `governance.schema_deny` is a BLOCK — report it and do NOT query it live. Only trace objects inside `governance.schema_allow` (plus any configured read exceptions).

## Per-object analysis (live, read-only, bounded)

For each governance-legal object:

6. **Surface fidelity.** Run a bounded `snow sql -c <conn>` query against `<database>.INFORMATION_SCHEMA.COLUMNS` (filter `table_schema` + `table_name`) to list the object's real columns and types. Diff against the expected set from step 4:
   - A column the app **selects/filters/joins on** that is absent → **BLOCK** (wrong-numbers or runtime-error risk).
   - A type or nullability mismatch that breaks a cast or comparison the app performs → **FLAG**.
   - A soft-delete / test-data column the object emits but the app ignores (e.g. an `IS_TEST` / `ACTIVE` / `IS_DELETED` style flag) → **FLAG** (counts may silently include rows the app should exclude).
7. **Lineage.** Run `SELECT GET_DDL('VIEW', '<fqn>')` (or `'TABLE'`) to read the object's definition. From the DDL:
   - Name upstream sources; recurse a bounded depth (2–3 levels). A view chain three or more levels deep → **FLAG** (fragile, hard to reason about).
   - Flag stale/renamed upstream references and wide multi-view joins that feed only a handful of columns downstream.
   - Record the downstream surface — which page/section consumes the object, from the query header's `-- Feeds:` line.
8. **Filtering & cost.** From the DDL and the app's predicates:
   - Flag pruning traps — a join or function inside a view that blocks predicate pushdown, or a missing partition/date filter that forces a full scan.
   - Flag `SELECT *` passthroughs (in the view body or the app query) — they freeze the column contract and pull columns nobody uses. When you flag an app-side `SELECT *`, enumerate the object's real columns from the step 6 lookup and put the explicit list in the finding's fix — that turns it into a mechanical change /apply-review can auto-apply rather than a judgment call.
   - Surface **materialization candidates**: a heavy aggregation or window function recomputed on every load, or an object several apps read, is better pre-computed in an allowed schema. Tailor the suggestion to the runtime from step 3 — container-runtime apps can lean on app-side caching for some of this, whereas warehouse-runtime apps benefit more from a pre-aggregated table/dynamic table in an allowed schema. Frame these as proposals, never as applied DDL.

## Read-only discipline (non-negotiable)

9. Every live query is **READ-ONLY and bounded**: `SELECT` / `GET_DDL` / `INFORMATION_SCHEMA` lookups only. No DDL, no DML (no `CREATE`/`ALTER`/`DROP`/`INSERT`/`UPDATE`/`DELETE`/`MERGE`/`GRANT`/`USE`). Always put a `LIMIT` on any row-returning probe; prefer `WHERE 1=0` or `COUNT(*)` over a narrow window to confirm an object resolves without scanning it. Never widen past `governance.schema_allow`. Keep the run cheap — these queries hit metadata, not data, so the credit cost is trivial; if you find yourself wanting a large or unbounded scan, stop and reframe the question.

## Cross-agent reviewers (optional, OFF by default)

10. You may optionally fan out external CLI reviewers (`agy`, `codex`) in parallel via the Task tool, following `_shared/cross-agent-review.md`, **only if** they are on PATH and enabled in config (`review.cross_agent: true`). This is OFF by default in OSS. Degrade silently to Claude-only when absent or disabled — never prompt to install anything. Tag findings `(Claude)` / `(Agy)` / `(Codex)`; collapse byte-identical citations to one line with `(also flagged by …)`.

## Output

11. Emit findings in the **same Markdown schema as /review-app** so /apply-review and /auto-review-app consume them unchanged. One bullet per finding, severity-prefixed `BLOCK` / `FLAG` / `NICE`, each citing `apps/<slug>/<file>` (or the fully-qualified object) plus a one-line fix. Group by severity. Write the report under `apps/<slug>/.review/` (gitignored) and surface a short summary.

### Severity rubric

- **BLOCK** — a surface-vs-source mismatch that produces *wrong numbers* or a runtime error: a selected/filtered/joined column the object doesn't emit, a join key that doesn't exist, or a missing governance-required filter that materially changes counts. Also: any reference to a `schema_deny` object.
- **FLAG** — measurable cost or coordination risk: deep view chain (≥3), ignored soft-delete column, pruning trap, missing date/partition filter, an object several apps read (shared-materialization opportunity), or a heavy recomputed aggregation.
- **NICE** — stylistic or cross-cutting: `SELECT *` in a view body, DDL hygiene, a single-app object with healthy shape — note only.

## Gotchas & edge cases

- **No connection ≠ no value.** If the DB is unreachable, don't fabricate column lists. Either stop (per Preflight) or, if the user wants a pass anyway, run the static slice via /review-app and mark every lineage claim *unverified*.
- **Don't claim upstream is broken.** A metric that looks off is far more often a cadence or definition mismatch than a corrupt source. Frame anomalies that way and let the human declare a regression — assert breakage only with strong, cited evidence.
- **`INFORMATION_SCHEMA` is role-scoped.** The columns/objects you can see depend on the connection's role. If an object the code references returns nothing, distinguish "doesn't exist" from "not visible to this role" before calling it a BLOCK.
- **Inline plumbing SQL is not app data.** `INFORMATION_SCHEMA` calls embedded in `.py` are infrastructure, not consumed surfaces — log them but don't trace them as data objects.
- **`GET_DDL` needs the right object domain.** A `'VIEW'` call against a base table (or vice versa) errors; if unsure, check `INFORMATION_SCHEMA.TABLES.TABLE_TYPE` first, then call `GET_DDL` with the matching domain.
- **Large DDL.** If a view definition is too long to inline in the report, summarize it in one line and stash the full text under `apps/<slug>/.review/` rather than dumping it into the finding.

## Troubleshooting

- **`snow connection list` / `streamsnow doctor` shows no connection** → `streamsnow configure` to set `snowflake.connection_name`, then `snow connection add`. Re-run preflight.
- **A query you expected to be legal is blocked by `check schema-refs`** → it resolves to `governance.schema_deny`. That's the intended block; surface it and stop tracing that object. If the schema *should* be allowed, that's a `streamsnow.config.yaml` governance change, not something this skill works around.
- **`INFORMATION_SCHEMA.COLUMNS` returns 0 rows for an object the code references** → wrong database/schema casing, an object that was renamed/dropped, or the role can't see it. Confirm with a `SELECT 1 FROM <fqn> WHERE 1=0` resolve check before concluding it's missing.
- **Findings don't flow into /apply-review** → the schema drifted. Match /review-app exactly: `BLOCK`/`FLAG`/`NICE` prefixes, one bullet per finding, a real `apps/<slug>/<file>` or object citation, and an explicit column list on any `SELECT *` fix.

## Hand-offs

- Findings feed /apply-review (auto-applies the mechanical BLOCK/FLAG fixes, walks the judgment ones) and the /auto-review-app loop.
- Pairs with /review-app — the no-DB qualitative pass. Run both for full coverage; /validate-app is the deterministic gate to run first.
- /sql-review is the read-only generator for an app's `sql_review/` companions; the lineage judgment lives here, the paste-and-runnable scaffolding lives there. If this trace surfaces a `sql_review/` gap, point the user at /sql-review.
- Optional issue-tracker filing of confirmed BLOCKs is OFF by default; only when the user asks and the integration is configured.

## Done when

Every governance-legal object the app queries has been traced upstream/downstream and column-verified against live Snowflake via bounded read-only `snow sql`, denied-schema references are flagged as BLOCKs without being queried, and the findings are written in the BLOCK/FLAG/NICE schema that /apply-review consumes.
