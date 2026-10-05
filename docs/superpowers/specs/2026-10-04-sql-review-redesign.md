# SQL review redesign: design spec

Status: design agreed with Kyle on 2026-10-04. Implementation plans for the three phases below are
NOT written yet; the next session writes them (superpowers:writing-plans) from this spec, one plan
per phase, saved under `docs/superpowers/plans/`.

## Purpose

Every Streamlit app built with this plugin ships SQL a person can open (DataGrip, a text editor,
any IDE, or Snowsight), read, run, and trace to every metric on every page. App-specific reporting
objects created to feed the app are discoverable in the same structure. A live review ties each
metric's SQL to the warehouse and to what the screen shows, and records results in a standard,
committed log. A human signs off on correctness; Claude never fills the sign-off.

## Decisions (all confirmed by Kyle)

1. **One SQL file per app page**, named `NN_page_name.sql` (two-digit on-screen order, matching
   the page order from `streamsnow/tools/app_nav.py`). No `.review.sql` suffix.
2. **One section per metric** inside the page file. The tag line `--N_short_description`
   (N = on-screen metric number on that page, description five words or fewer, snake_case) sits
   alone on the line directly above that section's SQL. Each section is runnable on its own
   (cursor + Cmd+Enter in DataGrip or Snowsight): it carries its own `params` CTE for the review
   window instead of a file-level `SET` block.
3. **Renumbering is allowed.** Each metric has a stable key (its snake_case description) and a
   position number the tool assigns from on-screen order; the tool renumbers files and tags.
4. **`index.yaml` per app replaces `sql_review/manifests/`.** The app runtime never reads the
   manifests (verified: no app template reads `sql_review/`); they exist only so the review SQL is
   provably the SQL the app runs. The index holds pages in order, metrics with stable keys, the
   app query feeding each metric, objects read, token sample values, and the review window. It is
   the single source for the README table, "Used by" lines, and `/sql-review` agents.
5. **Visual marker = helper call** in page code, e.g. `review_value("revenue_by_region", df)`.
   It is the ID tying a visual to its index entry, powers the "every visual is covered" check, and
   in review preview mode saves the exact data the visual received to JSON. Hard requirement: no
   measurable load-time cost. Disabled mode must be a cheap no-op (env flag read once at import),
   never issue queries, never write files, and be safe in Streamlit in Snowflake. Plan must include
   a benchmark test for the disabled path.
6. **`app_specific_reporting_objects/`** (no leading underscore) under `sql_review/` holds the
   maintained DDL for objects created to make the app work, one file per object, with a header:
   Object, Purpose, Used by (tool-generated from the index), Grants. `check` enforces: every DDL
   file is used by at least one page; every app-owned object a page reads has a DDL file. These
   files are exempt from the read-only guard and never run by the review flow.
7. **Applying DDL:** a human applies it by default. Claude deploys it only when the user
   explicitly says to. The live review flags drift between committed DDL and live `GET_DDL`.
8. **`sql_review/AGENTS.md` + `sql_review/CLAUDE.md`** (`@AGENTS.md`, same pattern as
   `_templates/repo/CLAUDE.md.j2`). AGENTS.md holds folder rules (format, tag rule, comment rule,
   "edit the index, never generated sections", commands) and points to the app-level AGENTS.md for
   data notes (grain, definitions, quirks, freshness), which stay in one place. README.md is
   created at init and is the human-facing index.
9. **Lint** with sqlfluff, Snowflake dialect: `fix` at generate time, `lint` inside `check`.
   Lint the app's own `queries/*.sql` too.
10. **Comment rule:** every CTE gets a one-line business-purpose comment above it; every
    non-obvious filter, join, or CASE gets a one-line "why" comment; each comment one line of 100
    characters or fewer; never restate the SQL; roughly one comment per 5 to 10 SQL lines, comments
    under about 25% of the body. CTE-comment presence and line length are machine-checked.
11. **Hard switch with a version bump** (no migration path; no known users of the old format).
12. **`/sql-review` skill** replaces `/audit-lineage` (retire it) and takes over
    `/review-app --sql`. This deliberately reverses the 0.7.3 retirement of the `sql-review` name:
    update `RETIRED_NAMES` in `tests/test_plugin_surface.py` accordingly.
13. **Two speeds, one review.** Structural `check` runs offline on every commit (pre-commit, CI,
    `validate-app`). The live review runs on request and before release or deploy.
14. **Full live run**: execute every section with a per-query timeout and the review window;
    record row count, columns, headline totals, and the Snowflake query ID per metric.
15. **Committed review log** with a standard workflow: one file per run at
    `sql_review/review_log/YYYY-MM-DD_<shortsha>.md`; README links the latest. Fixed sections:
    reviewed commit, connection, role, warehouse; per-page table (metric | SQL status | rows |
    headline | screen match | findings); verified findings by severity; human sign-off block.
    Records pass/fail, row counts, top-level totals only: never row-level data or small-group
    breakdowns. Committed alone as `chore(sql-review): <slug> review <date>`; fixes go in separate
    commits.
16. **Optimizer agent**: makes SQL faster by improving the SQL itself (pruning, predicate
    pushdown, removing `SELECT *`, pre-aggregation proposed as app-specific reporting objects),
    never by recommending a bigger warehouse. Every change must pass a deterministic
    result-equivalence check (same row count and same order-insensitive result hash) and a
    benchmark with the result cache disabled (elapsed time, bytes and partitions scanned from query
    history), before and after. Edits land in the app's `queries/*.sql`; the human approves.

## Architecture: facts from scripts, judgment from agents

Every fact (numbers, existence, diffs, pass/fail) comes from a deterministic tool emitting compact
JSON. Agents make only judgment calls, and every finding must cite the ID of a tool result; the
`log` verb rejects findings that do not. The orchestrator reads only JSON verdicts, never raw
result sets.

### Tools (`streamsnow sql-review <verb>`)

| Verb | Live | Produces |
|---|---|---|
| `generate` | no | Page SQL files, README table, "Used by" lines, all from `index.yaml` |
| `check` | no | index, markers, files, app queries aligned; read-only guard; drift; sqlfluff lint; checkable comment rules; DDL folder rules |
| `probe` | yes | objects resolve, columns match, grants reach the deploy role, committed DDL vs live `GET_DDL` |
| `run` | yes | per-section execution with timeout: row count, columns, headline totals, query ID |
| `bench` | yes | elapsed, bytes and partitions scanned with result cache off; result-equivalence hash |
| `compare` | no | screen-captured values vs `run` results, with rounding tolerance |
| `log` | no | standard log entry from the JSON above plus verified findings; validates the finding schema |

Live verbs respect `governance.schema_deny` (reuse `streamsnow check schema-refs`) and the
read-only allowlist.

### Agents (plugin `agents/`, empty today)

| Agent | Count | Job |
|---|---|---|
| Orchestrator (`/sql-review` skill, main session) | 1 | runs tools in a fixed order, dispatches, reads JSON only |
| Page reviewer | 1 per page, parallel | business logic, grain, row-duplicating joins, test or soft-deleted rows, nulls, comment quality |
| Object reviewer | 1 per app-specific reporting object | DDL drift, view-chain depth, grants, pre-aggregation |
| Optimizer | on request, or per slow metric | faster SQL with equivalence and benchmark proof (decision 16) |
| Finding verifier | per finding or batched, fresh context | adversarial check of each finding against evidence; drops unsupported ones |

In agent tools without subagents, the same recipe files run sequentially
(`skills/_shared/other-agents.md` convention).

Live pipeline order: `check` → `probe` → `run` → Playwright walk with capture → `compare` →
page and object reviewers (optimizer when requested) → verifier → `log`.

## Verified facts the plans must account for

- **Screen readability without code** (tested 2026-10-04 on a local Streamlit app, Streamlit
  1.59.x via uv; inferred version from the uv cache): `st.metric` and `st.table` values are plain
  DOM text. `st.dataframe` renders on canvas but exposes a hidden accessible `<table>` with exact
  values; it is virtualized, so only visible rows are in the DOM (13 of 1001 in the test), while
  `aria-rowcount` gives the exact total. Altair/Vega-Lite charts (`st.bar_chart`, `st.line_chart`)
  render SVG with an ARIA label per bar or point carrying exact values (e.g.
  `region: West; revenue: 12345.67`), plus axis labels. Plotly and Streamlit-in-Snowflake versions
  were not tested. Conclusion: the capture hook (decision 5) is still the deterministic path;
  the Playwright walk adds a no-code cross-check for metrics, small tables, and Vega charts.
- **SQL execution today:** the only executor is `run_query_snow` in `streamsnow/verify.py:63-86`,
  which shells out to `snow sql` and does not pass a connection name, role, or warehouse;
  `snowflake.connection_name` from config is only used in hints and `doctor`. Live verbs must pass
  the configured connection explicitly.
- **`streamsnow preview`** sets no environment variables and picks no free port; review mode needs
  an env flag added there (e.g. `STREAMSNOW_REVIEW_CAPTURE=<path>`).
- No `QUERY_TAG` or statement-timeout convention exists; live verbs should set a query tag
  (e.g. `streamsnow:sql-review:<slug>`) and a statement timeout.
- Playwright MCP is bundled in `.mcp.json` (`@playwright/mcp@0.0.83`); the walk recipe is
  `skills/_shared/playwright-walkthrough.md`; page order comes from `streamsnow/tools/app_nav.py`.
- Current engine: `streamsnow/tools/sql_review.py` (2255 lines, `GENERATOR_SCHEMA = 1`), tests in
  `tests/test_sql_review.py` (131 tests). Bump `GENERATOR_SCHEMA` to 2.
- Known issues in the current docs/skills that the redesign must not carry forward: skeletons
  without `generate` fail `check` as `provenance`; contradictory `-- TODO` literal guidance between
  `skills/audit-lineage/SKILL.md:66` and `skills/audit-lineage/tracing.md:56`; README not created
  at init; stale "coverage is a hard failure" wording; `/review-app` claims `--sql` runs
  automatically but no step runs it; app `AGENTS.md.j2:15` omits the rendered file on delete.

## Phases (one PR each, each releasable)

1. **Layout and offline check:** page files, `index.yaml`, `review_value` helper and markers,
   `app_specific_reporting_objects/`, `check` with sqlfluff and comment rules, sql_review
   AGENTS.md/CLAUDE.md, README at init, templates, docs, `GENERATOR_SCHEMA = 2`, version bump.
2. **Live review:** `probe`, `run`, `bench`, `log`, the five agents, the `/sql-review` skill,
   retire `/audit-lineage`, fold `/review-app --sql`, review log workflow.
3. **Screen comparison:** capture during review preview, Playwright walk, `compare`, log column.

## Out of scope

Applying DDL automatically; warehouse sizing advice; migration of old-format apps; Plotly-specific
screen parsing.
