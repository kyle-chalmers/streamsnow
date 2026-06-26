---
name: migrate-app
description: Port an external Streamlit app into a StreamSnow repo in two steps — lift-and-shift into apps/<slug>/, then conform to repo conventions (local branding.py + sql_loader.py, dashboard SQL into queries/*.sql with headers, snowflake.yml, runtime connection pattern) until `streamsnow validate-app` passes. Use when the user says "migrate a streamlit app", "port this dashboard into the repo", or "bring an external app in".
---

# migrate-app

Bring an external Streamlit app into the repo, then conform it to StreamSnow conventions until the deterministic gate passes.

## Steps

### Step 1 — lift-and-shift (get it in the tree)

1. Confirm prereqs: `streamsnow doctor`. If anything fails, point the user at /onboard first.
2. Settle the `<slug>` with the user (`<domain>-<function>` kebab-case) and the source path of the external app.
3. Copy the source into `apps/<slug>/` verbatim. Rename the entrypoint to `streamlit_app.py` (filename is not configurable). Do not refactor yet — this step only moves files.
4. Commit the unmodified lift so the conform diff is reviewable on its own.

### Step 2 — conform (make it a StreamSnow app)

5. Read the repo `streamsnow.config.yaml` for the source-schema allowlist, default runtime, and deploy target. Pick the runtime with the user (container is the default).
6. Source the canonical helpers: run `streamsnow new <domain> <function>` in a scratch dir (or copy from a sibling `apps/*/`) to get the local `branding.py` and `sql_loader.py`; copy both into `apps/<slug>/`. Always `from branding import ...` and `from sql_loader import ...` — never `from shared...`; each app ships its own copy.
7. Externalize dashboard SQL: move every query that feeds a UI element (chart, table, KPI, filter, export) into `apps/<slug>/queries/<name>.sql`, each opening with the required header block (`Query` / `Feeds` / `Schemas` / `Params` / `Tokens`). Load via `load_sql(name)` or `render_sql(name, **tokens)`. Leave only plumbing/discovery SQL inline.
8. Swap connections to the chosen runtime pattern: container → `st.connection("snowflake").query(...)`; warehouse → `get_active_session()` with an `st.connection` fallback. Wrap every data fetch in `@st.cache_data(ttl=...)` and pass filters as args, not closures.
9. Replace optional `(:N IS NULL OR col = :N)` bind-predicates with `{TOKEN}` SQL fragments via `render_sql` (deployed warehouse NULL-binds every param when any is `None`).
10. Write `apps/<slug>/snowflake.yml` for the chosen runtime (container declares `runtime_name` + `pyproject.toml`; warehouse omits it + uses `environment.yml`, never pinning `python`). Add an app `AGENTS.md` noting any non-default TTLs and the runtime.
11. Lint as you go: `pre-commit run --files apps/<slug>/**`. Isolate gates while iterating: `streamsnow check schema-refs|security|caching|bind-predicates apps/<slug>`. Scrub any out-of-allowlist schema refs and personal absolute paths.
12. Preview against live Snowflake via /preview-app (`streamsnow preview <slug>`) so the user confirms each page still renders.
13. Gate: run `streamsnow validate-app <slug>` (the deterministic PASS/FAIL ship gate — see /validate-app). Fix every FAIL and re-run until PASS.

## Hand-offs

- PASS the gate → run /ship-app to open the PR.
- Want qualitative judgment (SQL efficiency, UI patterns, spec drift)? That's the review tier (/review-app) — separate from this gate, never blocking. First-time account may need a one-time `streamsnow deploy-setup`.

## Done when

`apps/<slug>/` holds the conformed app (local `branding.py` + `sql_loader.py`, dashboard SQL in `queries/*.sql` with headers, `snowflake.yml` for the chosen runtime, runtime-correct connection), and `streamsnow validate-app <slug>` exits PASS.
