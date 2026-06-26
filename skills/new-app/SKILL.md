---
name: new-app
description: Scaffold a new Streamlit-in-Snowflake app with `streamsnow new <domain> <function>`, then guide the first build (pages, queries, branding) and hand off to /preview-app and /validate-app. Use when the user says "new app", "scaffold a dashboard", "add an app", or "start a new dashboard".
---

# new-app

Scaffold a new StreamSnow app and seed its first build, then hand off to preview and validate.

## Steps

1. Confirm prereqs: run `streamsnow doctor`. If anything fails, stop and point the user at /onboard before continuing.
2. Confirm Snowflake env: check that `streamsnow.config.yaml` exists at the repo root. If missing, run `streamsnow configure` first.
3. Settle the name with the user: `<domain>` and `<function>` (kebab-friendly, e.g. `marketing campaigns`). Derive the `<slug>` so you can reference it downstream.
4. Scaffold: `streamsnow new <domain> <function>`. This writes `apps/<slug>/` (entrypoint, `pyproject.toml`, `snowflake.yml`, `AGENTS.md`, branding, sql loader). Don't hand-create these.
5. Seed the first build in `apps/<slug>/`: add the page(s) the user described, externalize each UI-feeding query into `queries/<name>.sql` with the required header block, and wire branding via the scaffolded helpers. Keep edits inside the app dir.
6. Lint as you go: `pre-commit run --files apps/<slug>/**` to catch formatting and schema-ref issues early.
7. Spot-check individual gates while iterating if useful: `streamsnow check schema-refs|security|caching|bind-predicates apps/<slug>`.
8. Preview against live Snowflake: hand off to /preview-app (or run `streamsnow preview <slug>`) so the user sees it in the browser.
9. Gate before any PR: hand off to /validate-app (or run `streamsnow validate-app <slug>`). Any FAIL must be fixed before shipping.
10. First app in a fresh Snowflake account? Note that `streamsnow deploy-setup` emits one-time DDL (may be a stub) the account owner runs once; surface it but don't run deploys locally.

## Done when

`streamsnow new` has scaffolded `apps/<slug>/`, the first page+query+branding are in place and lint-clean, and the flow has handed off to /preview-app and /validate-app.
