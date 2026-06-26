---
name: deep-dive-data
description: Live-DB lineage and surface-fidelity check for a StreamSnow app — for each Snowflake object the app queries, trace upstream/downstream and verify the columns the app expects actually exist, via bounded read-only `snow sql`. Use when the user says "trace the data", "deep-dive this dashboard", "lineage for <slug>", or "deep-dive-data".
---

# deep-dive-data

Confirm the live Snowflake objects an app queries actually match what the code assumes — columns, lineage, filtering — using bounded read-only queries. Qualitative review against live data; it never blocks the ship (that is /validate-app, the deterministic gate).

## Steps

1. Resolve the `<slug>` (ask if absent); confirm `apps/<slug>/` exists.
2. Confirm a connection: read `snowflake.connection_name` from `streamsnow.config.yaml` and check `snow connection list` shows it. If missing, stop and point the user at `streamsnow configure` + `snow connection add` — this skill needs a live DB.
3. Inventory the objects the app queries: grep `apps/<slug>/queries/**/*.sql` and `apps/<slug>/**/*.py` for `FROM`/`JOIN` references and the columns selected/filtered per object. Build the object → expected-columns map.
4. Confirm each object is governance-legal before touching the DB: `streamsnow check schema-refs apps/<slug>`. Anything in `governance.schema_deny` is a BLOCK; don't query it live.
5. **Surface fidelity** — per object, run bounded read-only `snow sql -c <conn>` against `INFORMATION_SCHEMA.COLUMNS` (filter `table_schema` + `table_name`) to list real columns. Diff vs the expected set: a column the app selects/filters that is absent → BLOCK; a type/nullability mismatch that breaks a cast or filter → FLAG.
6. **Lineage** — for each object run `SELECT GET_DDL('VIEW'|'TABLE', '<fqn>')` to read its definition: name upstream sources, flag wide multi-view joins feeding few columns, stale/renamed references, and downstream surfaces (which page/section consumes it, from the query header `-- Feeds:`).
7. **Filtering / cost** — from the DDL, flag pruning traps (a join inside a view that blocks predicate pushdown), missing partition/date filters, and `SELECT *` passthroughs that freeze columns. Surface materialization candidates (repeated heavy aggregations better pre-computed in an allowed schema).
8. Keep every live query READ-ONLY and bounded: `SELECT` / `GET_DDL` / `INFORMATION_SCHEMA` only — no DDL/DML, always a `LIMIT` on row-returning probes. Never widen beyond `governance.schema_allow` + `read_exceptions`.
9. Optionally fan out cross-agent reviewers (`agy`, `codex`) in parallel via the Task tool **only if** present on PATH and enabled in config — OFF by default in OSS. Degrade silently to Claude-only when absent; tag findings `(Claude)`/`(Agy)`/`(Codex)`.
10. Emit findings in the same Markdown schema as /review-app so /apply-review and /auto-review-app consume them unchanged: one bullet per finding, severity-prefixed `BLOCK` / `FLAG` / `NICE`, each citing `apps/<slug>/<file>` + the object + a one-line fix. Group by severity.

## Hand-offs

- Findings feed /apply-review (auto-applies mechanical BLOCK/FLAG, walks judgment ones) and the /auto-review-app loop.
- Pairs with /review-app (no-DB qualitative pass) — run both for full coverage.
- Optional Jira filing of confirmed BLOCKs is OFF by default; only when the user asks and the integration is configured.

## Done when

Every object the app queries has been traced upstream/downstream and column-verified against live Snowflake via bounded read-only `snow sql`, and findings are written in the BLOCK/FLAG/NICE schema /apply-review consumes.
