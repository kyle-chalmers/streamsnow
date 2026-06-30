---
name: migrate-app
description: Port an external Streamlit app into a StreamSnow repo in two steps — lift-and-shift into apps/<slug>/, then conform to repo conventions (local branding.py + sql_loader.py, dashboard SQL into queries/*.sql with headers, snowflake.yml, runtime connection pattern) until `streamsnow validate-app` passes. Use when the user says "migrate a streamlit app", "port this dashboard into the repo", "bring an external app in", "upgrade the scaffold", or "modernize this dashboard".
---

# migrate-app

Bring an external Streamlit app into the repo, then conform it to StreamSnow conventions until the deterministic gate passes. Two commits: the unmodified lift, then the conform diff — each reviewable on its own.

## Why two steps

Mixing the file move and the convention rewrite into one diff makes review impossible — you can't tell a behavior change from a layout change. Land the verbatim port first (it should run identically to the source), then layer conventions on top. If the conformed app misbehaves, you can bisect against the known-good lift.

## Step 1 — lift-and-shift (get it in the tree)

1. Confirm prereqs: `streamsnow doctor`. If anything fails, send the user to /onboard before continuing.
2. Settle the `<slug>` with the user — `<domain>-<function>`, kebab-case (e.g. `marketing-campaign-dashboard`). Confirm the source path of the external app and that nothing under `apps/<slug>/` already exists (don't clobber a sibling).
3. Copy the source into `apps/<slug>/` verbatim. Rename the entrypoint to `streamlit_app.py` (the filename is not configurable). Copy supporting modules, `pages/`, and image assets too, preserving relative import paths. Do NOT refactor — this step only moves files.
4. Scrub blockers the port can't ship with, but keep it minimal:
   - **Secrets in code.** Move any hardcoded credentials out of `.py` into `.streamlit/secrets.toml` (gitignored). Never read or commit an existing source secrets file — note its presence and have the user copy values by hand.
   - **Denied-schema references.** Re-point any reference to a schema outside the config allowlist (`governance.schema_allow` in `streamsnow.config.yaml`) to the governed equivalents (e.g. ANALYTICS / REPORTING). `streamsnow check schema-refs apps/<slug>` flags these.
5. Commit the unmodified lift: a single commit so reviewers see "the port" as one changeset.

## Step 2 — conform (make it a StreamSnow app)

6. Read `streamsnow.config.yaml` for the schema allowlist (`governance.schema_allow` / `schema_deny`), the governance database, and the default runtime. Decide the runtime with the user (see "Runtime choice" below).
7. Source the canonical helpers. Run `streamsnow new <domain> <function>` in a scratch dir, or copy from a sibling `apps/*/`, to get the local `branding.py` and `sql_loader.py`; copy both into `apps/<slug>/`. Always `from branding import ...` and `from sql_loader import ...` — never `from shared...`. Each app ships its own copy because the deployed runtime cannot reach a repo-level `shared/`.
8. Externalize dashboard SQL. Move every query that feeds a UI element (chart, table, KPI card, filter dropdown, CSV export) into `apps/<slug>/queries/<name>.sql`, each opening with the required header block (`Query` / `Feeds` / `Schemas` / `Params` / `Tokens`). Load via `load_sql(name)` or `render_sql(name, **tokens)`. Leave only plumbing/discovery SQL inline (e.g. `SELECT CURRENT_TIMESTAMP()`, INFORMATION_SCHEMA lookups). The header metadata needs human judgment — walk candidates with the user one at a time; do not auto-extract, since the Feeds/Schemas mapping cannot be guessed from code alone.
9. Swap connections to the chosen runtime pattern (see below). Wrap every data fetch in `@st.cache_data(ttl=...)` and pass filters as function arguments, not closures — closures defeat the cache key.
10. Replace optional `(:N IS NULL OR col = :N)` bind-predicates with `{TOKEN}` SQL fragments via `render_sql`. The deployed warehouse driver NULL-binds *every* param when *any* bound param is `None`, so an "All" sentinel silently zeroes the result set — invisible locally, broken once deployed. `streamsnow check bind-predicates apps/<slug>` catches the trap.
11. Write `apps/<slug>/snowflake.yml` for the chosen runtime and add an app `AGENTS.md` noting any non-default cache TTLs and the runtime decision.
12. Lint and gate-check as you go. Run `streamsnow check schema-refs|security|caching|bind-predicates apps/<slug>` to isolate each governance gate while iterating, and scrub any out-of-allowlist schema refs or personal absolute paths.
13. Preview against live Snowflake via /preview-app (`streamsnow preview <slug>`) so the user confirms each page still renders.
14. Gate: run `streamsnow validate-app <slug>` (the deterministic PASS/FAIL ship gate — see /validate-app). Fix every FAIL and re-run until PASS.
15. Commit the conform pass as its own changeset.

## Runtime choice — container vs warehouse

The runtime decides the connection pattern, the dependency manifest, and what packages are even available. Confirm it with the user against `streamsnow.config.yaml`'s default; don't assume.

- **Container** (the usual default): full PyPI via the runtime image. `snowflake.yml` declares `runtime_name` and ships a `pyproject.toml`. Connect with `st.connection("snowflake").query(...)`.
- **Warehouse**: packages come from the Snowflake Anaconda channel only — narrower, and it lags PyPI. `snowflake.yml` omits `runtime_name` and uses `environment.yml` (never pin `python` — a pinned interpreter is a warehouse landmine). Connect with `get_active_session()`, keeping an `st.connection` fallback for local parity.

Pick container unless a hard constraint forces warehouse. Reach for warehouse only when the deploy target or governance config mandates it; if the source carries cross-viewer module-level mutable state that is unsafe on container's shared-process model, that's the other reason to consider warehouse — document why in the app `AGENTS.md`.

## Dependencies

- **Container:** most packages ship as-is, provided the package and its transitive deps are PyPI-installable. List them in `pyproject.toml`.
- **Warehouse:** every dep must exist on the Snowflake Anaconda channel. Map source pins to channel names, drop the `python` pin, and for anything unavailable either swap for a channel-available alternative or move the app to container. When the source has no manifest at all, infer candidates from imports but treat them as suggestions — confirm with the user before adding; never auto-add a guessed dependency.

## Gotchas

- **`from shared...` imports.** The deployed runtime has no repo-level `shared/`. Every app needs its own local `branding.py` and `sql_loader.py`. A `from shared.branding import ...` runs locally and then fails at deploy.
- **`None` in `params=` (the bind-predicate trap).** See step 10. A `(:N IS NULL OR col = :N)` "All" filter looks fine locally but returns nothing once deployed to warehouse. Convert to a `{TOKEN}` fragment via `render_sql`.
- **`SELECT *` in externalized SQL.** Pin explicit columns; a star couples the app to schema drift and bloats the result. If you can't enumerate columns without a live session, defer with a note rather than guessing — never hallucinate column names.
- **Uncached data fetches.** Every function that hits Snowflake needs `@st.cache_data(ttl=...)`, or the dashboard re-queries on every rerun. The intentional exceptions (smoke-test calls where caching would mask a dead session) should be documented in the app `AGENTS.md`.
- **Pinned `python` in warehouse `environment.yml`.** Strip it — the warehouse runtime supplies the interpreter, and a pin breaks the manifest.
- **Personal absolute paths / machine-specific config** copied in with the source. Scrub them; they fail for everyone else and leak local detail.

## Troubleshooting

- **Local `streamlit run` fails but the deployed app works (warehouse).** Expected: `get_active_session()` raises outside Snowflake. Use /preview-app's local-parity fallback, or accept that the local run can't fully exercise a warehouse app and verify in Snowflake instead.
- **`streamsnow validate-app` FAILs on schema refs.** A reference points outside `governance.schema_allow` (or hits `schema_deny`). Re-point to the governed schema; re-run `streamsnow check schema-refs apps/<slug>` to confirm.
- **`streamsnow check caching` flags a function.** It fetches data without `@st.cache_data(ttl=...)`. Wrap it, or — if it's an intentional uncached call — document the rationale in `AGENTS.md`.
- **Manifest validation FAILs.** Container apps need a valid `pyproject.toml` + `runtime_name` in `snowflake.yml`; warehouse apps need `environment.yml` with no `python` pin and no `runtime_name`. Re-read the FAIL message — it names the offending field.
- **A warehouse dep isn't on the Anaconda channel.** Swap for an available alternative or migrate the app to container. The channel lags PyPI — check availability before assuming a package is present.
- **A deploy step errors.** Translate the Snowflake error via skills/_shared/deploy-error-translator.md; a first-time account may need a one-time `streamsnow deploy-setup`.

## Hand-offs

- Need to drive each page in a browser during preview? See skills/_shared/playwright-walkthrough.md.
- PASS the gate → run /ship-app to deploy and open the PR. First-time account may need a one-time `streamsnow deploy-setup`.
- Want qualitative judgment (SQL efficiency, UI patterns, spec drift)? That's the review tier — /review-app and /sql-review, plus /deep-dive-data to validate the live numbers. These are separate from the gate and never block it.
- Refactoring an app that's already in the repo (not an external port)? You can skip Step 1 and run the conform work plus the gate directly.

## Done when

`apps/<slug>/` holds the conformed app — local `branding.py` + `sql_loader.py`, dashboard SQL in `queries/*.sql` with headers, `snowflake.yml` matching the chosen runtime, a runtime-correct connection pattern, every data fetch cached, and no denied-schema or bind-predicate findings — and `streamsnow validate-app <slug>` exits PASS. The lift and the conform land as two separate commits.
