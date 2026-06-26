# deploy-error-translator

Purpose: map a failing Snowflake deploy (from the generated `deploy.yml` run or local `snow`/`streamsnow` output) to a plain-English cause and a concrete fix. A contract that /ship-app reads and follows post-merge when a deploy run ends `conclusion: failure` — not an invocable skill. It diagnoses and proposes; it never runs DDL or deploys itself.

## When it runs

/ship-app calls this after it watches a merge-to-`main` deploy run to a terminal state and the run failed (or a local `streamsnow deploy-sql <slug>` errors). The goal: turn the raw error into one actionable line for the user rather than echoing a stack trace.

## Inputs

- The failing step's log (e.g. `gh run view <run-id> --log-failed`, or `snow` stderr).
- Config names so messages name the right objects, not placeholders. Read from `streamsnow.config.yaml`:
  - `snowflake.roles.ci_role` (deploy/CI role) · `snowflake.objects.stage_database/stage_schema/stage_name` (code stage; or `streamsnow stage-path`) · `snowflake.objects.compute_pool` · `snowflake.objects.external_access_integration` · `deploy.git_repository_fqn` (if Git-backed).
  - Substitute these into the fixes below as `<ROLE>`, `<STAGE>`, `<POOL>`, `<EAI>`, `<GIT_REPO>`. If a key is absent, say so and point at /onboard `streamsnow deploy-setup` for the one-time DDL.

## Translate the error

Match the failed log against these patterns (case-insensitive — Snowflake identifiers fold case). First match wins; report cause + fix + the named object.

| Log signature (substring) | Cause | Fix (one-time, account owner) |
|---|---|---|
| `Insufficient privileges`, `does not exist or not authorized`, `Object ... not authorized` on a `BUSINESS_*`/data object | `<ROLE>` lacks a grant on the schema/object the app queries | `GRANT USAGE ON SCHEMA ...; GRANT SELECT ON ... TO ROLE <ROLE>;` — re-run via `streamsnow deploy-setup` output. Then re-deploy by re-running the CI job. |
| `Compute pool ... does not exist`, `pool ... not found`, `COMPUTE_POOL` invalid | Container runtime declared in `snowflake.yml` but `<POOL>` was never created | `CREATE COMPUTE POOL <POOL> ...` (see `streamsnow deploy-setup`). Per-invocation infra op — owner runs it. |
| `External access integration ... does not exist`, `EAI ... not authorized`, PyPI install / network blocked during image build | `<EAI>` missing or not granted; container can't reach PyPI | Create + grant `<EAI>` to `<ROLE>`; ensure the app's `snowflake.yml` references it. `streamsnow deploy-setup` emits the DDL. |
| `Stage ... does not exist`, `@<STAGE>` not found, `snow stage copy` target error | Code stage `<STAGE>` not created, or wrong FQN in config | Create `<STAGE>` (one-time, `streamsnow deploy-setup`) or correct `snowflake.objects.stage_*` in `streamsnow.config.yaml`. Confirm with `streamsnow stage-path`. |
| `live version` … `NULL`, `no live version`, app serves stale/empty after a Git-backed deploy | Git-backed STREAMLIT didn't advance its live version (fetch alone doesn't) | Owner runs the 3-statement refresh on the app: `ALTER STREAMLIT <app> ADD LIVE VERSION FROM LAST;` preceded by an `ALTER GIT REPOSITORY <GIT_REPO> FETCH;` + commit pull — see the deploy doc. |
| `Git Repository ... fetch`, `Failed to connect`, `IP ... not allowed`, network/403 on `<GIT_REPO>` | Git repo secret/API integration or network policy blocks the fetch | Verify the API integration + secret on `<GIT_REPO>`; if IP-allowlisted, add the runner egress range. Re-run `ALTER GIT REPOSITORY <GIT_REPO> FETCH;`. |
| `Cannot perform CREATE STREAMLIT` / `ALTER STREAMLIT` … `not authorized` | Deploy ran under the wrong role — non-owner `ALTER STREAMLIT` is a silent no-op or hard error | Ensure the CI step does `USE ROLE <ROLE>;` (the owner role) before any STREAMLIT DDL. |
| `pool ... is suspended` / cold-start timeout / health check failed but Bootstrap+Promote succeeded | Container cold start (1–3 min) outran the verify step — app likely shipped fine | Distinguish for the user: deploy created the object; only `Verify` flaked. Re-run verify or wait for warm process — usually not a real failure. |

No pattern matches → report the failed **step name** and the verbatim error tail, and say it's unrecognized so the user can triage. Never guess a fix you can't ground in the log.

## Output

Hand /ship-app a single block:

```
Deploy failed at step <step-name>.
Cause: <plain-English cause, naming <OBJECT>>.
Fix: <one-time DDL / config edit — who runs it and where>.
Shipped anyway? <yes if object created and only Verify failed; else no>.
```

## Contract for callers

- /ship-app's post-merge monitor calls this on a failed deploy run, passes the failed-step log + config-resolved names, and relays the block to the user.
- This recipe is **read-only**: it reads logs and config, proposes DDL/config changes, and points at `streamsnow deploy-setup` and /onboard. It never runs `snow` DDL, never deploys, never edits config — the account owner applies infra fixes per-invocation.
- Reporting the failed-step name and the shipped-anyway distinction is mandatory: a deploy can be `conclusion: failure` while the STREAMLIT was successfully created (Verify flaked), and that tells the user whether to re-run or to fix infra.
