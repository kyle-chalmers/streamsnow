---
name: refine-requirements
description: Build or refine an app's REQUIREMENTS.md spec (pages, sections, charts, KPIs, filters, source schemas, caching TTLs, runtime, deploy source) from a free-form description, a Jira ticket, screenshots, or existing code — before scaffolding. Use whenever the user says "spec out a dashboard", "refine requirements", "I have a Jira for an app", "write a REQUIREMENTS doc", or wants a contract before /new-app.
---

# refine-requirements

Turn an idea, ticket, screenshots, or existing app code into a structured `REQUIREMENTS.md` that downstream skills (`/new-app`, `/add-page`, `/validate-app`, `/review-app`) build and audit against.

## Steps

1. Determine mode from the input: **new** (free-form description / `--new`), **ingest** (Jira `DI-XXXX` — fetch the ticket), or **backfill** (an existing `apps/<slug>/` — reverse-engineer from `streamlit_app.py`, `pages/*.py`, `queries/*.sql` headers, `snowflake.yml`, and the app `AGENTS.md`).
2. Read the repo `streamsnow.config.yaml` for the source-schema allowlist, default warehouse/runtime, and deploy target. Every source schema in the spec must come from the allowlist.
3. Collect any screenshots/sketches the user pasted or dropped under `apps/<slug>/screenshots/`; use them to fill layout, chart types, widgets, and branding — but still ask for table/column/TTL facts vision can't infer.
4. Interview the user for the gaps: pages and their sections, each section's chart/KPI/table, filters and time controls, the source schema + object per query, per-query caching TTL (justify any deviation from the repo default), runtime, and deploy source. Recommend a default and move on rather than quizzing.
5. Write `apps/<slug>/REQUIREMENTS.md` with these sections: Overview, Source Schemas, Pages & Sections, Charts & KPIs, Filters, Caching, Runtime, Deploy, and §11 Build Progress (phase + page status + resume hint). Use the slug `<domain>-<function>` it'll scaffold under.
6. Echo a one-screen summary (pages → sections → source objects → TTLs) and confirm with the user.
7. Hand off: for a new app, run `/new-app` (then `/add-page` per page); for an existing app, run `/validate-app` then `/review-app` against the freshly written spec.

Done when `apps/<slug>/REQUIREMENTS.md` exists, every source schema is on the `streamsnow.config.yaml` allowlist, and §11 records the build phase.
