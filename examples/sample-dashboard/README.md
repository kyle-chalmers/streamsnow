# Sample dashboard (runnable, no Snowflake)

A complete StreamSnow-shaped Streamlit app wired to **deterministic sample data**,
so you can see what `streamsnow init` produces and run it immediately — no
Snowflake account, connection, or credentials needed.

```bash
pip install -r examples/sample-dashboard/requirements.txt
streamlit run examples/sample-dashboard/streamlit_app.py
```

(Or with uv, no install step: `uvx --with streamlit --with pandas --with plotly \
  streamlit run examples/sample-dashboard/streamlit_app.py`.)

## What it shows

- **`st.navigation` entrypoint** (`streamlit_app.py`) with two pages.
- **Branding** (`branding.py`) — brand colors, a registered Plotly template, and
  the `branded_metric` card. This is the module `streamsnow init` generates from
  your brand config; the values here are the defaults.
- **Cached loaders** (`data.py`) decorated with `@st.cache_data(ttl=...)` — the
  exact shape `streamsnow check caching` enforces. In a real app each loader
  calls `conn.query(load_sql("name"), ...)` against an allowed schema; here they
  return fixed sample frames.
- **Pages** (`pages/overview.py`, `pages/trends.py`) — KPIs, a trend line, a
  channel breakdown, and a retention curve.

## How it differs from a real StreamSnow app

This demo is intentionally **not** a deployable Snowflake app: it has no
`snowflake.yml`, no `queries/`, and no database access. To generate a real,
governed app (with the manifest, governance checks, and deploy wiring), run:

```bash
uvx streamsnow init        # setup wizard + scaffold a real app under apps/
streamsnow validate-app <slug>
```

See the top-level [`examples/README.md`](../README.md) for the full picture.
