# Brief: data-scout

## Role

Profile the data a new app will read, so the design and the queries rest on facts: grain, date
range, size and the values behind each filter. Read-only: never run DDL, DML or anything that
writes, and never widen a grant.

## Inputs

- `slug`, and `apps/<slug>/REQUIREMENTS.md` §2–§7 (what the app must show).
- `streamsnow.config.yaml`: `governance.database`, `schema_allow`, `schema_deny`,
  `read_exceptions`, and the connection name.
- [docs/data-discovery.md](../../../docs/data-discovery.md) for the two `INFORMATION_SCHEMA` queries.

## Owns

Nothing. Return the profile; the orchestrator writes §3.

## Steps

1. List candidate objects in the allowed schemas (`INFORMATION_SCHEMA.TABLES`, with comments),
   run through `snow sql` on the configured connection. Skip anything under `schema_deny`.
2. For each object the spec needs: columns and types (`INFORMATION_SCHEMA.COLUMNS`), the grain
   (what one row is), the date column with `MIN`/`MAX`, the row count (`ROW_COUNT` for tables;
   for views, a `COUNT(*)` over the last 30 days of the date column, never a full scan).
3. For each filter in §7: up to 20 distinct values, ordered by frequency.
4. Flag what the spec assumes but the data doesn't hold (no date column, a grain finer than the
   charts need, an object only readable through a denied schema).

## Returns

```json
{"objects": [{"fqn": "DB.SCHEMA.OBJECT", "kind": "table|view", "grain": "one row per ...",
  "date_column": "...", "min_date": "YYYY-MM-DD", "max_date": "YYYY-MM-DD",
  "row_estimate": 0, "columns": [{"name": "...", "type": "..."}],
  "filter_values": {"<filter>": ["..."]}}],
 "gaps": ["..."], "queries_run": ["SELECT ..."]}
```

## Verify

The orchestrator checks every `fqn` against the allowlist (`streamsnow check schema-refs` runs on
the queries later) and copies `max_date` into the review window and the page defaults.

## Degrade

No connection or no `snow` CLI: return the objects named in the spec with `"unverified": true`
and say so; §3 stays marked unverified.
