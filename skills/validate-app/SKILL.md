---
name: validate-app
description: The pass/fail check that must be clean before an app ships. Runs `streamsnow validate-app <slug>` (required-files, manifest, artifacts, naming, schema-refs, app-security, bind-predicates, sql-tokens, session-fallback, page-imports, caching, path-leaks, the §11 requirements contract, the offline sql-review check of the app's sql_review/ files, whose coverage severity follows sql_review.coverage in config, and placeholders) and explains how to fix anything that fails. Use when the user says "validate", "is this ready", "check my app", or before /ship-app.
argument-hint: "<slug>"
allowed-tools: [Bash, Read, Edit]
---

# /validate-app

> **Repo overlay:** if `.streamsnow/overlays/validate-app.md` exists in this repo, read it first: committed, repo-specific additions/overrides ([_shared/overlays.md](../_shared/overlays.md)). Outside Claude Code, also read [_shared/other-agents.md](../_shared/other-agents.md).

Run the pass/fail gate on one app and report exactly what fails and how to fix it. `streamsnow
validate-app <slug>` is the single source of truth: offline (no Snowflake, no network), deterministic,
and what CI runs per app. CI also runs `dependency-vulns` and `tombstones`; pre-commit runs a subset.

## What it covers

Names as the gate prints them. How to fix each: [fixing-checks.md](fixing-checks.md).

- **required-files, manifest, artifacts, naming:** the runtime's files exist, `snowflake.yml` is
  valid for that runtime, its `artifacts:` list matches disk, and the slug is lowercase-hyphenated.
- **schema-refs:** no reference into a `governance.schema_deny` schema (exact `read_exceptions` aside). Names outside `governance.sources` and app data, and two-part `SCHEMA.OBJECT` names, are warnings under `governance.boundary: warn` and failures under `enforce`.
- **app-security:** no network egress, code execution, write SQL or string-built SQL. Apps are
  read-only by contract; maintained DDL in `sql_review/app_specific_reporting_objects/` is exempt.
- **bind-predicates:** none of the `:N IS NULL OR` deployed-driver trap.
- **sql-tokens:** no `{TOKEN}` placeholders inside SQL comments.
- **session-fallback:** `get_active_session()` sits in a broad `try/except`.
- **page-imports:** subdirectory helpers (`pages/_layout.py`, `pages/_data.py`) imported
  package-qualified, since only the app root is on `sys.path` deployed. **A local boot and a full UI
  walkthrough cannot catch this**, which is the whole reason it's static.
- **caching:** data-fetching functions carry `@st.cache_data(ttl=...)`.
- **path-leaks:** no personal home-directory paths in `.py` or `.md` files.
- **requirements:** the §11 Build Progress block in `REQUIREMENTS.md` that `/build-app` resumes from.
- **sql-review (coverage policy: warn|fail):** the offline `streamsnow sql-review check` of
  `sql_review/`. Coverage gates only under `sql_review.coverage: fail`; `advisory` never gates.
- **placeholders:** no scaffold `YOUR_TABLE` or starter sample block. A fresh scaffold fails it on purpose. **starter-text** only warns (`!`): an app `AGENTS.md` still describing the starter, or a repo README Apps table with no row for the app.

## Steps

1. **Resolve the slug** (list `apps/*/` and ask if omitted).
2. **Run it:** `streamsnow validate-app <slug>`; read its output, don't re-derive the checks
   (`--format json` to parse; match the sql-review entry by its `sql-review` name prefix).
3. **PASS →** report per check. A `!` mark with `~` lines is a PASS carrying sql-review warnings
   (uncovered pages or queries, advisories): report them, never call the app clean. `/build-app`
   needs zero coverage warnings when its build phase ends.
4. **On FAIL,** the gate already prints `file:line`, up to 10 per check. Past that, re-run the
   focused check: `streamsnow check schema-refs|security|caching|bind-predicates|artifacts|sql-tokens|page-imports|path-leaks|requirements apps/<slug>`,
   `streamsnow check session-fallback --all apps/<slug>`, or `streamsnow sql-review check <slug>`.
   required-files, manifest, naming and placeholders have no sub-check: cite what the gate named.
5. **Fix per [fixing-checks.md](fixing-checks.md):** apply only mechanical, unambiguous fixes;
   surface judgment calls to the user rather than guessing.
6. **Re-run until PASS** (or the only remaining failures are documented human deferrals), then
   report a terse per-check summary.

## Gotchas

- **Per-app, deterministic, offline:** it catches contract violations, not slow SQL, wrong numbers
  or awkward UI. Quality is `/review-app`; numbers against live Snowflake are `/sql-review`.
- **Never "fix" by weakening governance.** Editing the deny list, deleting a check, or
  string-escaping past the dynamic-SQL rule is a regression. Route through allowed schemas,
  parameterize, or remove the capability.
- **Local PASS is necessary, not final:** CI is authoritative and re-runs after push.
- **Trust the aggregate for the verdict.** A focused check can pass while the gate fails: four checks
  exist only in the gate, `session-fallback` defaults to new calls only, and `sql-review check`
  reads the coverage policy from the repo's config rather than `--config`.

## Troubleshooting

- **`no app at apps/<slug>` (exit 2):** the slug must be a directory under `apps/`; run from the
  repo root or pass `--dir` (and `--config <path>` when the config is elsewhere). An ungoverned
  repo is an `/onboard` problem, not a validate problem.
- **Schema looks allowed but fails:** a denied schema fails whatever the boundary. A bare deny entry (`RAW`) blocks every database, a qualified one (`FINANCE.RAW`) only its own; a three-part name outside the sources fails only under `governance.boundary: enforce`. Quoted names keep their case: `"analytics_db"."reporting"` is not the source `ANALYTICS_DB.REPORTING`.
- **Checks disagree with reality after a config change:** the gate reads config live, so re-run it.

## Optional UI smoke

The gate can't see a page that fails to render: add (never substitute) a walkthrough per
[_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md). A clean walkthrough is no evidence against `page-imports`.

## Done when

The gate exits PASS on every check, or each remaining FAIL is handed back with a specific, named
reason. Hand-offs: a sql-review fix that needs real sample values or new `index.yaml` entries →
`/sql-review <slug> --offline` (inside `/build-app`, its build step owns `index.yaml`); quality depth →
/review-app; see it render → /preview-app; PASS → /ship-app, or back to `/build-app` when it called.
