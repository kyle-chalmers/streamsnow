# Per-object tracing — surface, lineage, filtering, cost

For each governance-allowed object the app queries:

## 1 · Surface fidelity

Bounded lookup against `<database>.INFORMATION_SCHEMA.COLUMNS` (filter `table_schema` +
`table_name`) for the real columns and types. Diff against the expected set from discovery:

- A column the app **selects / filters / joins on** that is absent → **critical** (wrong numbers or
  a runtime error).
- A type or nullability mismatch that breaks a cast or comparison the app performs → **should-fix**.
- A soft-delete / test-data flag the object emits but the app ignores (`IS_TEST`, `ACTIVE`,
  `IS_DELETED`-style) → **should-fix** (counts may silently include rows the app should exclude).

## 2 · Lineage

`SELECT GET_DDL('VIEW', '<fqn>')` (or `'TABLE'`) to read the definition. Check
`INFORMATION_SCHEMA.TABLES.TABLE_TYPE` first if the object domain is unclear — a `'VIEW'` call
against a base table errors. From the DDL:

- Name upstream sources; recurse a bounded 2–3 levels. A view chain ≥3 deep → **should-fix**
  (fragile, hard to reason about).
- Flag stale/renamed upstream references and wide multi-view joins feeding only a handful of
  downstream columns.
- Record the downstream surface — which page/section consumes the object, from the query header's
  `-- Feeds:` line.
- A definition too long to inline → summarize in one line, stash the full text under
  `apps/<slug>/.review/`.

## 3 · Filtering & cost

From the DDL plus the app's predicates:

- **Pruning traps:** a join or function inside a view that blocks predicate pushdown, or a missing
  partition/date filter forcing a full scan.
- **`SELECT *` passthroughs** (view body or app query): freeze the column contract and pull unused
  columns. For an app-side `SELECT *`, inline the object's real columns (from step 1) in the
  finding's fix — that makes it mechanical for `/review-app --fix` instead of a judgment call.
- **Materialization candidates:** a heavy aggregation or window function recomputed on every load,
  or an object several apps read, is better pre-computed in an allowed schema. Tailor to runtime —
  container apps can lean on app-side caching for some of this; warehouse apps benefit more from a
  pre-aggregated / dynamic table. Proposals only, never applied DDL. Dynamic-table proposals must
  respect the platform rules (refresh runs as the owner's primary role only; a DT can't read
  DT-backed views) — see [_shared/production-gotchas.md](../_shared/production-gotchas.md).
- **Grant reachability:** the deployed app reads with the **ci_role's** grants, not the previewer's.
  For each traced source, note whether the ci_role can SELECT it (a source readable only by your
  role is a deployed-app failure waiting to happen) and whether a schema-level future grant covers
  new objects.

## 4 · Hand-back into `sql_review/`

The lineage pass ends holding exactly what the app's audit trail needs — traced upstream objects
and live confirmation per object — so spend it before it goes cold:

1. `streamsnow sql-review check <slug>` — freshness + coverage gate for the rendered companions.
   Clean → skip straight to the index step.
2. **On gaps** (uncovered queries, missing files): interactively, offer
   `streamsnow sql-review discover <slug> --write` followed by
   `streamsnow sql-review generate <slug>`, flagging that the skeleton manifests carry `-- TODO`
   dispatcher literals a person (or the `/review-app --sql` recipe) still has to replace with real
   sample fragments. Inside `/review-app --auto` there is nobody to ask — bootstrap silently with
   the static skeleton defaults and add a punch-list item to author the manifests properly.
   **On DRIFT**: `streamsnow sql-review generate <slug>` regenerates; never edit a `.review.sql`
   body — the manifest under `sql_review/manifests/` is the editing surface.
3. `streamsnow sql-review index <slug>` rebuilds the README coverage table between its markers
   (the tool owns the skeleton; it preserves the Upstream and Verified cells per query and every
   line outside the markers). Then edit the two human columns from this pass's results:
   - **Upstream object(s)** — the fully-qualified object(s) the lineage walk (section 2 above)
     traced for that query, replacing the `_(fill via /review-app --sql)_` placeholder.
   - **Verified** — today's date, only on rows whose objects THIS pass confirmed live (resolve
     probe + `INFORMATION_SCHEMA.COLUMNS` both answered). Anything untraced or unreachable stays
     `no` — a Verified date you didn't earn this pass is fabrication, not paperwork.

## Troubleshooting

- **No connection resolves** → `streamsnow configure` sets `snowflake.connection_name`, then
  `snow connection add`; re-run preflight.
- **`check schema-refs` blocks an object you expected to trace** → that's the intended block;
  changing the allowlist is a governance decision in `streamsnow.config.yaml`, not something this
  skill works around.
- **`INFORMATION_SCHEMA.COLUMNS` returns 0 rows for a referenced object** → wrong database/schema
  casing, a renamed/dropped object, or the role can't see it. Confirm with
  `SELECT 1 FROM <fqn> WHERE 1=0` before concluding it's missing.
- **Findings don't flow into `/review-app --fix`** → the report schema drifted. Match it exactly:
  severity-prefixed bullets, one finding per bullet, a real `apps/<slug>/<file>` or object citation,
  and an explicit column list on any `SELECT *` fix.
