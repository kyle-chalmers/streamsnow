# Acme Collections Overview

App-specific context. Inherits the repo rulebook in the top-level `AGENTS.md`.

- **Runtime:** container · **Query warehouse:** `STREAMLIT_WH`
- **Data sources:** `ANALYTICS_DB` (schemas: ANALYTICS, REPORTING)
- **Caching strategy:** default TTL 1800s. Note any per-query deviations here with a reason.

## Pages

- **Overview** (`pages/overview.py`) — starter page; replace the example metric with your real KPIs.

## Queries

- see `queries/` — one file per UI-feeding query, header block on each.
