---
name: validate-app
description: The pass/fail check that must be clean before an app ships — runs `streamsnow validate-app <slug>` (files, schema references, security, bind parameters, caching) and explains how to fix anything that fails. Use when the user says "validate", "is this ready", "check my app", or before /ship-app.
argument-hint: "<slug>"
allowed-tools: [Bash, Read]
---

# /validate-app

Run the pass/fail check on one app and report exactly what fails and how to fix it.
`streamsnow validate-app <slug>` is the single source of truth — it bundles the same checks the
governance hooks and CI enforce, so a clean run here means a clean run there. This is the fast local
pre-flight; CI re-runs the authoritative version after push.

## What it covers

- **files / layout** — required files exist for the app's runtime; the slug is well-formed.
- **schema-refs** — no references to denied schemas; at least one into an allowed schema.
- **security** — no network egress, code execution, write SQL, or string-built SQL. Apps are
  read-only by contract.
- **bind-predicates** — none of the `:N IS NULL OR` deployed-driver trap.
- **caching** — data-fetching functions carry `@st.cache_data(ttl=...)`.
- **artifacts** — `snowflake.yml`'s `artifacts:` list matches the files on disk.
- **sql-tokens** — no `{TOKEN}` placeholders inside SQL comments.
- **session-fallback** — `get_active_session()` sits in a broad `try/except`.
- **page-imports** — subdirectory helpers imported package-qualified, since only the app root is on
  `sys.path` deployed. **A local boot and a full UI walkthrough cannot catch this** — that is the
  whole reason it's static.

## Steps

1. **Resolve the slug** (list `apps/*/` and ask if omitted).
2. **Run it:** `streamsnow validate-app <slug>` — read its output, don't re-derive the checks
   (`--format json` to parse programmatically).
3. **PASS →** report per check and stop.
4. **On FAIL,** re-run the matching focused check to get the exact file and line:
   `streamsnow check schema-refs|security|caching|bind-predicates|artifacts|sql-tokens|session-fallback|page-imports apps/<slug>`
   (files/layout, manifest, and naming failures have no sub-check — cite the path the validator named).
5. **Fix per [fixing-checks.md](fixing-checks.md):** apply only mechanical, unambiguous fixes;
   surface judgment calls to the user rather than guessing.
6. **Re-run until PASS** (or the only remaining failures are documented human deferrals), then
   report a terse per-check summary.

## Runtime changes "required files"

Determine the runtime the way the checker does — from the app's own `snowflake.yml` (anchored
`runtime_name:` key; config's top-level `runtime` is only the fallback). Container expects
`pyproject.toml`; warehouse expects `environment.yml`; the file check fails on the wrong one (or
both). A common false alarm is judging a container app against warehouse expectations — see
[_shared/runtime-decision.md](../_shared/runtime-decision.md).

## Gotchas

- **Per-app and deterministic:** it catches contract violations, not slow SQL, awkward UI, or spec
  drift — that's `/review-app`.
- **Never "fix" by weakening governance.** Editing the deny list, deleting a check, or
  string-escaping past the dynamic-SQL rule is a regression. Route through allowed schemas,
  parameterize, or remove the capability.
- **Local PASS is necessary, not final** — CI is authoritative and re-runs after push.
- **A focused check can pass while the aggregate fails** — the aggregate also enforces files/layout
  and slug naming. Trust the aggregate for the verdict.

## Troubleshooting

- **"app not found"** — the slug must be a directory under `apps/`; run from the repo root or pass
  `--dir`. An ungoverned repo is a `/start-app --setup` problem, not a validate problem.
- **Config not at the root** — pass `--config <path>`.
- **Schema looks allowed but fails** — compare against the exact `governance.schema_allow` /
  `schema_deny` / `governance.database` values; a fully-qualified name resolving into a denied
  schema still trips it.
- **Checks disagree with reality after a config change** — re-render governed files with
  `streamsnow update --apply`, then re-run.

## Optional UI smoke

The static check can't see a page that fails to render. Complement (never substitute) with a
browser walkthrough per [_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md).
The reverse also holds: a clean walkthrough is not evidence against `page-imports`, which flags
exactly the class of bug a walkthrough is blind to. Never wave a check off because the app ran.

## Done when

`streamsnow validate-app <slug>` exits PASS on every check, or each remaining FAIL is handed back
with a specific, named reason. Hand-offs: quality depth → /review-app; see it render → /preview-app;
PASS → /ship-app.
