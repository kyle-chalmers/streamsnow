# Examples

StreamSnow doesn't check in a sample app — `streamsnow init` **generates** a
complete, working one for you (and the generated output is exercised in CI by
the `wheel-smoke` job, so it stays valid).

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
