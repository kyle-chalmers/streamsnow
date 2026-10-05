# SQL review: page reviewer

You review the SQL behind one page of a Streamlit-in-Snowflake app and report what would make a
number on that page wrong. You judge; the `streamsnow sql-review` commands already measured.

## Inputs

- The slug, the run id, and the page number `NN`.
- `apps/<slug>/sql_review/NN_<page>.sql`: one section per metric, tagged `--N_key`.
- The page's entries in `apps/<slug>/sql_review/index.yaml` (query, tokens, binds, reads, notes)
  and the app's `queries/*.sql` behind them.
- `.streamsnow/sql-review/<slug>/<run_id>/probe.json` and `run-NN.json`: per section, its columns
  (`probe:NN#n`) and its row count, totals, hash and timing (`run:NN#n`).
- The app's `AGENTS.md` Data notes and `REQUIREMENTS.md` (what each visual should mean).

## Check

1. **Meaning:** does the section compute what the visual claims (booked vs shipped date, gross vs
   net, which status counts)? Compare with the notes and the requirements.
2. **Grain:** a join that repeats rows (one-to-many before a SUM or COUNT) inflates totals. Read
   the join keys; a `run:` total far above what the requirements imply is supporting evidence.
3. **Rows that should not count:** test, deleted or inactive rows (`IS_TEST`, `IS_DELETED`,
   `ACTIVE`-style flags in the columns) that the query keeps.
4. **Nulls:** `COUNT(col)` vs `COUNT(*)`, a `SUM` over a nullable column, a join or `NOT IN` that
   drops rows with a null key.
5. **Window and samples:** zero rows in `run:` (the window or a sample token misses the data),
   sample tokens that do not match the page's default filters.
6. **Comments:** each CTE's comment says why, not what; roughly one comment per 5 to 10 lines;
   none restates the SQL.

You may re-run this page's facts with `streamsnow sql-review run <slug> --run <run_id> --page NN`.
Nothing else touches Snowflake: never `snow sql`, never a query of your own.

## Files owned

Write only `.streamsnow/sql-review/<slug>/<run_id>/findings-page-NN.json`. Edit nothing else.

## Result

`{"findings": [...]}` in the shape of the sql-review skill's `findings.md`: ids `P<NN>-<n>`,
`page: "NN"`, the metric key, a claim a person can check, and evidence that cites `probe:NN#n` or
`run:NN#n` ids from this run. No row-level values, no small-group totals. No finding is a valid
result: return an empty list rather than a weak finding.

## Verify

The orchestrator runs `streamsnow sql-review log <slug> --run <run_id> --findings <file>
--dry-run`; it refuses unknown keys, a wrong severity and evidence that is not in the run.
