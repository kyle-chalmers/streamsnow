---
name: validate-app
description: Deterministic PASS/FAIL ship gate for a StreamSnow app — runs `streamsnow validate-app <slug>` (files, schema-refs, app-security, bind-predicates, caching) and explains how to fix any failing check. Use when the user says "validate", "is this ready", "check my app", "validate <slug>", or before /ship-app.
---

# validate-app

Run the deterministic PASS/FAIL gate on one app and report exactly what fails and how to fix it. `streamsnow validate-app <slug>` is the single source of truth — it bundles the same checks the governance hooks and CI enforce, so a clean run here means a clean run there. Treat this as the fast local pre-flight; the repo's CI re-runs the authoritative version after you push.

## What the gate covers

`validate-app` aggregates these checks for one app under `apps/<slug>/`:

- **files / layout** — required files exist for the app's runtime mode (see below); the slug is well-formed.
- **schema-refs** — no references to schemas denied by `governance.schema_deny`; at least one reference into an allowed schema (`governance.schema_allow`, e.g. ANALYTICS / REPORTING). Schemas live in `streamsnow.config.yaml`, never hardcoded here.
- **security** — no egress, no code-exec, no write-SQL, no dynamic (string-built) SQL. Apps are read-only by contract.
- **bind-predicates** — none of the `:N IS NULL OR` deployed-runtime trap.
- **caching** — data-fetching functions are wrapped with `@st.cache_data(ttl=...)`.

## Steps

1. **Resolve the slug.** If omitted, list `apps/*/` and ask which app; confirm `apps/<slug>/` exists.
2. **Run the gate:** `streamsnow validate-app <slug>`. Do not re-derive its checks by hand — read its output. (Add `--format json` if you want to parse the result programmatically; the default `md` is best for reading.)
3. **If it exits PASS,** report PASS per check and stop. Nothing else to do here.
4. **On FAIL,** for each failing check re-run the matching focused gate to surface the exact offending file and line:
   - schema-refs → `streamsnow check schema-refs apps/<slug>`
   - security → `streamsnow check security apps/<slug>`
   - caching → `streamsnow check caching apps/<slug>`
   - bind-predicates → `streamsnow check bind-predicates apps/<slug>`
   - file / layout failures → no sub-gate; cite the path the validator named.
5. **For each failure, give a one-line fix** tied to the cited file and line. Apply only mechanical, unambiguous fixes (see "Fixing each check"). Surface anything judgment-bound for the user to decide rather than guessing.
6. **Re-run `streamsnow validate-app <slug>`** to confirm it flips to PASS. Repeat until clean or a finding needs a human call.
7. **Report a terse summary:** each check PASS/FAIL, fixes applied, and anything left for the user.

## Detect runtime mode first (it changes "required files")

Before reasoning about file/layout failures, determine the app's runtime from its `snowflake.objects` / app manifest in config:

- **Container runtime** — the manifest declares a container runtime. Its dependency manifest is the project file the scaffold generated for container apps.
- **Warehouse runtime** — no container runtime declared. Its dependency manifest is the warehouse environment file.

The two modes expect *different* dependency manifests, and the file check fails if the wrong one is present (or both). If a file/layout check fails, first confirm you're checking against the mode the manifest actually declares — a common false alarm is judging a container app against warehouse expectations. When unsure how a fresh app should be laid out, compare against a freshly scaffolded one from /new-app or /start-app rather than guessing.

## Fixing each check

**schema-refs.** A finding means code touches a schema in `governance.schema_deny` (or never touches an allowed one). Fix by routing the query through an allowed schema — typically a curated REPORTING/ANALYTICS view — not by editing the deny list. The allow/deny lists are governance, set in `streamsnow.config.yaml`; changing them to pass the gate defeats the gate. If a denied reference is genuinely required, that's a human governance decision, not a mechanical fix.

**security.** Four classes, all mechanical to locate, some judgment-bound to fix:
- *egress* — networking/exfil imports. Remove them; an in-Snowflake app should not reach the network.
- *exec* — `eval` / `exec` / `os.system` / `subprocess` / `pickle` and friends. Remove or replace with safe equivalents.
- *write-sql* — `DROP`/`DELETE`/`INSERT`/`UPDATE`/`MERGE`/`CREATE`/`ALTER`/`GRANT`/`REVOKE` etc. in SQL or inline constants. Apps are read-only; the write does not belong in app code.
- *dynamic-sql* — SQL assembled by f-string / `.format` / `%` / `+`. Fix with bind parameters, or render a `{TOKEN}` fragment validated against an allowlist. Do not paper over it by string-escaping.

**bind-predicates.** The `:N IS NULL OR col = :N` pattern (an "All" sentinel that binds `None` to position `:N`) works locally but breaks once deployed: the warehouse driver NULL-binds the *whole* parameter list when any one value is `None`. Classic symptom — KPIs render fine in `streamsnow preview` but show 0/0 once deployed. Fix by branching the SQL: build the predicate fragment only when a real value is supplied (e.g. a `{TOKEN}` fragment rendered in), so `None` never reaches a bound position.

**caching.** Every function that fetches data must carry `@st.cache_data(ttl=<positive int>)`. If a query *should not* cache (e.g. a connection/heartbeat check where a stale cached result would hide a broken session), that's an intentional exception to document in the app's `AGENTS.md`, not a blanket reason to drop caching everywhere. An app with no data fetches at all (static/sample content) legitimately has nothing to cache.

## Gotchas

- **The gate is per-app and deterministic.** It will not catch slow SQL, awkward UI, or spec drift — only contract violations. Quality is a separate pass (see Hand-offs).
- **Don't "fix" by weakening governance.** Editing `schema_deny`, deleting a check, or escaping a dynamic-SQL string to slip past the gate is a regression, not a fix. Route through allowed schemas / parameterize / remove the offending capability instead.
- **Local PASS is necessary, not always final.** This mirrors CI's checks but CI is authoritative — it re-runs after push. If CI fails on something this gate missed, fix and push again rather than assuming the local run was wrong.
- **A focused `check` can pass while `validate-app` fails.** The aggregate also enforces files/layout and slug naming, which the four `check` subcommands don't. Always trust the aggregate for the final verdict.

## Troubleshooting

- **"app not found" / wrong directory** — the slug must be a directory under `apps/`. Run from the repo root, or pass `--dir <repo-root>`. If the repo isn't governed yet, that's an /onboard problem, not a validate problem.
- **Config not found** — pass `--config <path>` to point at `streamsnow.config.yaml` if it isn't at the repo root.
- **Schema-refs fails but the schema looks allowed** — re-read `governance.schema_allow` / `schema_deny` and `governance.database` in config; the gate compares against those exact values, and a fully-qualified name resolving into a denied schema still trips it.
- **A check disagrees with reality after a config change** — governance files are rendered from config + templates. If you changed config, re-render with `streamsnow update --apply` so hooks/CI match what `validate-app` enforces, then re-run the gate.

## Optional UI smoke

The static gate can't see a page that fails to render, raw column headers, or console errors. For that, drive a browser walkthrough following the shared recipe `skills/_shared/playwright-walkthrough.md` (it owns the tool sequence and failure mapping). Treat it as a complement, not a substitute — a UI smoke does not replace a PASS from `validate-app`.

## Hand-offs

- Deeper qualitative concerns (SQL efficiency, UI patterns, spec drift) → run /review-app or /sql-review. This gate does not judge quality.
- Once PASS → run /ship-app to open the PR. This skill is the inline safety gate /ship-app runs before pushing.
- To see the app actually render against live Snowflake before shipping → /preview-app.

## Done when

`streamsnow validate-app <slug>` exits PASS for every check, and any FAIL has either been fixed-and-reverified (gate re-run, now clean) or handed back to the user with a specific, named reason.
