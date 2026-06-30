# Examples

## Run a sample app right now — [`sample-dashboard/`](sample-dashboard/)

A complete StreamSnow-shaped app wired to **deterministic sample data**, so you
can see the shape and run it immediately with **no Snowflake connection**:

```bash
pip install -r examples/sample-dashboard/requirements.txt
streamlit run examples/sample-dashboard/streamlit_app.py
```

It demonstrates the `st.navigation` entrypoint, branding, and `@st.cache_data`
loaders — the same patterns `streamsnow init` scaffolds. See its
[README](sample-dashboard/README.md) for details.

## Generate a real, governed app — `streamsnow init`

The sample above is a local demo (no `snowflake.yml`, no queries). For a real,
deployable app, `streamsnow init` **generates** a complete one for you (and the
generated output is exercised in CI by the `wheel-smoke` job, so it stays valid).

A freshly generated app looks like:

```
apps/<slug>/
  streamlit_app.py         # st.navigation entrypoint, apply_branding()
  pages/overview.py        # branded metric + Plotly chart + a cached loader
  queries/example_metric.sql
  branding.py  sql_loader.py
  .streamlit/config.toml   .streamlit/secrets.toml.example
  snowflake.yml            pyproject.toml (container) | environment.yml (warehouse)
  AGENTS.md
```

Generate and run one:

```bash
uvx streamsnow init                # setup wizard + scaffold
streamsnow validate-app <slug>     # PASS/FAIL gate
streamsnow preview <slug>          # run locally vs Snowflake
```
