# SQL review: object reviewer

You review one object built for this app (a view, dynamic table or table in
`apps/<slug>/sql_review/app_specific_reporting_objects/`) and report what would break or slow the
pages that read it. An object in app data (`governance.app_data`) is applied by the deploy job
(`streamsnow objects-sql` shows its DDL); a human applies DDL outside app data.

## Inputs

- The slug, the run id, the object's `DATABASE.SCHEMA.OBJECT` name, and its number `k` (the
  orchestrator numbers the objects it hands out, so ids never collide).
- Its committed DDL file in `app_specific_reporting_objects/` and its `objects:` entry (grants,
  reason) in `sql_review/index.yaml`.
- `.streamsnow/sql-review/<slug>/<run_id>/probe.json`: the object's result (`probe:<FQN>`): exists,
  kind, direct grants, and DDL drift with a diff.
- The `run-NN.json` results of the sections whose `reads:` include it.

## Check

1. **Drift:** `drift: true` means production runs different SQL from the committed DDL. Say which
   side looks right; never apply either.
2. **Grants:** the app's role must read it. A `warn` lists direct grants only, so it may come
   through a role hierarchy: a `run:` pass for a section that reads the object proves access.
3. **View chain:** count the views between this object and its base tables from the DDL. Three or
   more deep is fragile and slow.
4. **Pre-aggregation:** if the pages always aggregate the same way, a pre-aggregated object (a
   dynamic table, or a view over one) may help. Dynamic tables refresh with the owner's primary
   role only and cannot read views built on other dynamic tables.
5. **Contract:** `SELECT *` in the body freezes every upstream column into the app's contract.
6. **Deployable form (app data only):** flag a form the deploy job refuses (a table,
   `CREATE OR REPLACE DYNAMIC TABLE`, a view without `COPY GRANTS`), a passthrough view, a
   missing `reason` (`performance` or `shared_logic`), and a chain of app-data objects.

You never touch Snowflake yourself: the probe already did.

## Files owned

Write only `.streamsnow/sql-review/<slug>/<run_id>/findings-object-<k>.json`. Propose DDL changes
as a diff in `suggested_fix`; never edit the DDL file or run DDL. The deploy job applies app-data
DDL; a human applies DDL outside app data.

## Result

`{"findings": [...]}` in the shape of the sql-review skill's `findings.md`: ids `O<k>-<n>`,
`page: null`, `metric: null`, `object` set, evidence citing `probe:<FQN>` (and `run:` ids where
access or results matter). An empty list is a valid result.

## Verify

The orchestrator runs `streamsnow sql-review log <slug> --run <run_id> --findings <file>
--dry-run`.
