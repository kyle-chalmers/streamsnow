# Data judgment: surface, lineage, filtering, cost

How the reviewers judge an object and the sections that read it. The facts come from
`streamsnow sql-review probe` and `run` (never your own queries); this is how to read them.
Denied objects are reported and never queried: `probe` refuses them before anything is sent.

For each governance-allowed object the app queries:

## 1 · Surface fidelity

`probe` reports each section's real columns and types (`probe:NN#n`); a section that does not
compile fails there with Snowflake's message. Diff against what the app expects:

- A column the app **selects / filters / joins on** that is absent → **critical** (wrong numbers or
  a runtime error).
- A type or nullability mismatch that breaks a cast or comparison the app performs → **should-fix**.
- A soft-delete / test-data flag the object emits but the app ignores (`IS_TEST`, `ACTIVE`,
  `IS_DELETED`-style) → **should-fix** (counts may silently include rows the app should exclude).

## 2 · Lineage

For objects built for this app, `probe` compares the committed DDL with the live definition
(`drift`). For shared sources, read the definition when a person has given you access to it.
From the DDL:

- Name upstream sources; recurse a bounded 2–3 levels. A view chain ≥3 deep → **should-fix**
  (fragile, hard to reason about).
- Flag stale/renamed upstream references and wide multi-view joins feeding only a handful of
  downstream columns.
- Record the downstream surface — which page/section consumes the object, from the query header's
  `-- Feeds:` line.
- A definition too long to inline → summarize it in one line.

## 3 · Filtering & cost

From the DDL plus the app's predicates:

- **Pruning traps:** a join or function inside a view that blocks predicate pushdown, or a missing
  partition/date filter forcing a full scan.
- **`SELECT *` passthroughs** (view body or app query): freeze the column contract and pull unused
  columns. For an app-side `SELECT *`, inline the object's real columns (from `probe`) in the
  finding's fix: that makes it mechanical instead of a judgment call.
- **Materialization candidates:** a heavy aggregation or window function recomputed on every load,
  or an object several apps read, is better pre-computed in an allowed schema. Tailor to runtime —
  container apps can lean on app-side caching for some of this; warehouse apps benefit more from a
  pre-aggregated / dynamic table. Proposals only, never applied DDL. Dynamic-table proposals must
  respect the platform rules (refresh runs as the owner's primary role only; a DT can't read
  DT-backed views) — see [_shared/production-gotchas.md](../_shared/production-gotchas.md).
- **Grant reachability:** the deployed app reads with the **ci_role's** grants, not the previewer's.
  The live commands run as that role with secondary roles off, so a `run:` pass proves access.
  `probe` lists direct grants only; note whether a schema-level future grant covers new objects.

## 4 · Hand-back into `sql_review/`

What the review learned belongs in the index, so the next review starts from it:

1. **Record what was traced** in `sql_review/index.yaml`: each metric's `reads:` lists the
   fully-qualified objects its query reads (`probe` checks every one). A view or table built for
   this app goes under `objects:` with its DDL in `sql_review/app_specific_reporting_objects/`.
   Data facts a reviewer needs (grain, a quirk, a load cadence) go in the app `AGENTS.md` Data
   notes, not in `index.yaml`. Then `streamsnow sql-review generate <slug>`, committed on its own.
2. **On coverage gaps** (a nav page or query `index.yaml` does not cover): interactively, offer to
   add the missing pages and metrics, with real sample tokens a person confirms; never generate
   from placeholder samples, because a section that runs on a made-up value proves nothing.
   Inside `/review-app --auto` there is nobody to ask: add a punch-list item instead.
3. **On drift**: `streamsnow sql-review generate <slug>` regenerates; never edit a page file:
   `index.yaml` and `queries/*.sql` are the editing surface.
4. Report which objects this run confirmed live and which were unreachable. Never claim a
   confirmation the run did not earn.

## Troubleshooting

- **No connection resolves** → `streamsnow configure` sets `snowflake.connection_name`, then
  `snow connection add`; re-run preflight.
- **A live command refuses a denied schema** → that's the intended block; changing the allowlist
  is a governance decision in `streamsnow.config.yaml`, not something this skill works around.
- **`probe` says an object is not found** → wrong database or schema casing, a renamed or dropped
  object, or the role cannot see it. The detail names the role; check its grants before calling
  it missing.
- **`USE ROLE` fails** → you do not hold the app's `ci_role`; ask which role to review with and
  pass `--role`. Say in the report that the review ran under a different role.
