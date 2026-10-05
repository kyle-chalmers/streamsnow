# SQL review redesign, phase 2: live review

Spec: `docs/superpowers/specs/2026-10-04-sql-review-redesign.md` (decisions 7, 12-16, the
Architecture section). Requires phase 1 merged. One PR, version **0.9.0** (skill rename is a
breaking surface change).

## Goal

`/sql-review <slug>` runs a live review against Snowflake: deterministic tools produce JSON
facts, agents make judgment calls that must cite those facts, a verifier drops unsupported
findings, and `log` writes a committed review log with a human sign-off block Claude never fills.

## Task 1: Snowflake executor with explicit connection

`streamsnow/verify.py:63` `run_query_snow` passes no connection, role or warehouse. Add a new
executor rather than changing deploy behaviour:

- `streamsnow/sf_exec.py`: `SnowExec(connection: str | None, role, warehouse, query_tag,
  timeout_s)` with `.query(sql) -> Result(rows, columns, query_id, elapsed_ms)`.
  Shells out to `snow sql -q … --format json -c <connection>`; prefixes the session with
  `ALTER SESSION SET QUERY_TAG = 'streamsnow:sql-review:<slug>'`,
  `STATEMENT_TIMEOUT_IN_SECONDS = <timeout>`, and `USE ROLE`/`USE WAREHOUSE` when configured.
  Gets the query ID with `SELECT LAST_QUERY_ID()` in the same invocation.
- Connection from `snowflake.connection_name` in config; `--connection` overrides.
- Every statement passes `_verify_read_only` and `check_schema_refs.find_denied_refs` (with
  `SchemaPolicy.from_governance`) before execution; a denied ref is a tool error, never sent.
- Injectable runner for tests (same pattern as `verify_app(run_query=…)`).

Tests: argv construction, session prefix, guard rejection before subprocess, timeout mapping,
JSON parse of multi-statement output, `LAST_QUERY_ID` capture. No live Snowflake in CI.

## Task 2: live verbs

All emit compact JSON to stdout (`--out <path>` to write a file under
`.streamsnow/sql-review/<slug>/<run_id>/`), each result with a stable `id` agents cite
(`probe:<object>`, `run:<page>#<n>`, `bench:<page>#<n>:before`).

- `probe <slug>`: every object in index `reads:` and DDL folder resolves
  (`SHOW OBJECTS LIKE … IN SCHEMA`), columns referenced by sections exist
  (`DESCRIBE`), grants reach the deploy role (`SHOW GRANTS ON`), committed DDL vs live
  `GET_DDL` with whitespace/case normalised → `drift: true|false` plus a unified diff
  truncated to 40 lines (decision 7).
- `run <slug> [--page NN] [--timeout S]`: execute each section with the review window; record
  row count, column names, headline totals (sum of numeric columns, count of rows; nothing
  row-level), query ID, elapsed. Never write result rows to disk.
- `bench <slug> --metric <page>#<n> [--sql-file F]`: `ALTER SESSION SET USE_CACHED_RESULT =
  FALSE`; run 3 times, take median; read `bytes_scanned`, `partitions_scanned`,
  `partitions_total` from `INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION()` by query ID;
  result-equivalence hash = `HASH_AGG(*)` over the result plus `COUNT(*)` (order-insensitive),
  computed in Snowflake so no rows leave the warehouse. `--sql-file` benchmarks a candidate
  rewrite against the current section and reports `equivalent: true|false`.
- `log <slug> --run <run_id> --findings findings.json`: validates findings schema (below);
  rejects any finding whose `evidence` IDs are not in the run's JSON; writes
  `sql_review/review_log/YYYY-MM-DD_<shortsha>.md` with fixed sections: reviewed commit,
  connection, role, warehouse; per-page table (metric | SQL status | rows | headline |
  screen match (`n/a` until phase 3) | findings); verified findings by severity; sign-off block
  (`Reviewer:`, `Date:`, `Decision: approve / changes needed`, left blank). Updates the README
  "Latest review" marker row.

Finding schema: `{id, severity: blocker|major|minor, page, metric, claim, evidence: [ids],
suggested_fix}`.

Add the `review_log/` rule to sql_review AGENTS.md: commit the log alone as
`chore(sql-review): <slug> review <date>`; fixes go in separate commits; never record row-level
data or small-group breakdowns.

## Task 3: agents (`agents/`, empty today)

Each a markdown agent file with frontmatter (name, description, tools), plus a recipe that the
same text serves for agent tools without subagents (`skills/_shared/other-agents.md`).

- `sql-review-page.md`: input = page file, index entries for that page, `run:`/`probe:` JSON for
  its metrics, app AGENTS.md data notes. Checks business logic, grain, row-duplicating joins,
  test or soft-deleted rows, null handling, comment quality (the judgment half of decision 10).
  Output = findings JSON only. Read-only tools; no Snowflake access beyond `streamsnow
  sql-review run --page`.
- `sql-review-object.md`: one per DDL file: drift, view-chain depth, grants, pre-aggregation.
- `sql-review-optimizer.md`: on request or for metrics over a slow threshold (default 10 s).
  Proposes edits to `queries/*.sql` or a new app-specific reporting object; must run `bench
  --sql-file` and include `equivalent: true` and before/after numbers; never recommends warehouse
  size (decision 16). Edits are presented as a diff for human approval, not applied.
- `sql-review-verifier.md`: fresh context; per finding (batched by page), re-reads cited
  evidence, tries to refute; outputs keep/drop with reason. Only kept findings reach `log`.

## Task 4: `/sql-review` skill; retire `/audit-lineage`

- `skills/sql-review/SKILL.md`: orchestrator. Order: `check` → `probe` → `run` → page and
  object reviewers in parallel (optimizer if `--optimize` or slow metrics) → verifier → `log`.
  Reads JSON only, never raw result sets. Flags: `--page NN`, `--offline` (check only),
  `--optimize`. Ends by telling the human to fill the sign-off block.
- Delete `skills/audit-lineage/`; move any still-useful tracing guidance into the skill.
- `tests/test_plugin_surface.py`: add `sql-review` to `EXPECTED_SKILLS`, remove it from
  `RETIRED_NAMES` (`:39`), add `"audit-lineage": "/sql-review"`.
- `skills/review-app/`: replace the `--sql` flag text and `sql-companions.md` with "run
  `/sql-review`"; add an explicit step that runs it when `--sql` is passed (fixes the claim
  that it runs but no step does).
- `skills/ship-app` / deploy docs: recommend `/sql-review` before release or deploy (decision
  13); do not block deploy on it.
- Docs: `docs/auditing-a-visual.md` rewritten around `/sql-review`; README skill table;
  CHANGELOG **Breaking**: `/audit-lineage` → `/sql-review`.

## Task 5: tests and release

- Unit tests for each verb with a fake runner fixture returning canned `snow` JSON.
- `log` tests: rejects uncited finding, rejects unknown evidence ID, never writes row data
  (assert only aggregates appear), stable output for snapshot.
- Skill-surface tests updated; `tests/test_skill_cli_parity.py` covers the new verbs.
- Version 0.9.0 in the four version files + template pins `>=0.9,<0.10`.
- Done when CI is green and one manual live run on a real app produced a log, recorded in the
  PR description (commit, connection name, counts; no data).

## As built (deviations from this plan)

Recorded when phase 2 landed, after a sub-agent review of this plan against the spec, the code
and read-only probes of a real Snowflake account.

- **Version:** no version bump in the feature PR (the maintainer's call); 0.9.0 ships in its own
  release PR, with the template pins.
- **Partitions:** `QUERY_HISTORY_BY_SESSION` has no partition columns. `bench` takes elapsed
  time and bytes from `SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER` by query id and the
  actual partitions scanned and total from `GET_QUERY_OPERATOR_STATS`.
- **Measuring without rows:** `run` and `bench` wrap a section as `COUNT(*)`, `HASH_AGG` over
  positional columns sorted by name, and per-column `SUM`, cast to text in Snowflake. Columns
  come from `SELECT * FROM (<section>) WHERE 1 = 0` plus `DESCRIBE RESULT`, which also replaces
  per-column `DESCRIBE` as the compile check. `bench` times that wrapper, not the bare query.
- **Session:** `USE SECONDARY ROLES NONE` after `USE ROLE` (secondary roles would hide a missing
  grant); the role defaults to `snowflake.roles.ci_role`. `snow sql` runs with
  `--enable-templating NONE` and reads the script from stdin.
- **Runs:** every verb writes into `.streamsnow/sql-review/<slug>/<run_id>/` (not optional
  `--out`), which carries its own `.gitignore`; later verbs take `--run <id>`.
- **Finding schema:** `page` and `metric` may be null and `object` was added, for object
  findings. `log --dry-run` validates without writing (the reviewers' verify command).
- **Privacy:** the committed log shows totals only for a single all-numeric row or 10+ rows.
- **Agent briefs** live in `skills/sql-review/reviewers/` (shipped by `agent-skills install`)
  and are mirrored word for word in the plugin's `agents/`, pinned by a test.
- **Done-when:** the manual live run on a real app could not happen in the implementation
  session (no Snowflake CLI access there); the maintainer waived it for the merge and does it
  after. Every SQL shape the verbs emit was validated against Snowflake through a read-only
  connection instead.
