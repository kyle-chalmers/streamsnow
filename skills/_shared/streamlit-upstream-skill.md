# streamlit-upstream-skill

Purpose: let the agents that write or review Streamlit code use the Streamlit team's own agent
skill, `developing-with-streamlit`, without letting it pull an app off the runtime it is pinned to
or around StreamSnow's gates. It is the best current source on widget choice, layout, state and
caching idioms, and it changes every few days; StreamSnow does not copy it.

**The rule:** read it at the latest Streamlit release, check every call against the app's pin, and
let StreamSnow's conventions and checks win every conflict.

## Read it at the latest release tag

1. Resolve the tag: `gh api repos/streamlit/streamlit/releases/latest --jq .tag_name` (or the same
   path on `api.github.com` without `gh`).
2. Read from
   `https://github.com/streamlit/streamlit/tree/<tag>/lib/streamlit/.agents/skills/developing-with-streamlit`.
   Never the `develop` branch: it is Streamlit's default branch and documents unreleased APIs. The
   repository has no `main`.
3. Read only what the job needs; the whole skill is over 300 KB.

| Who | Read |
|---|---|
| page-builder, and the orchestrator editing shared files | `references/api-reference.md`, then `selection-widgets.md`, `layouts.md`, `data-display.md`, `session-state.md` as the page needs them |
| streamlit-reviewer, and `/review-app`'s UI reviewer | the same, plus `SKILL.md`'s "Best practices quick reference" |
| perf-reviewer | `references/performance.md` (after [streamlit-performance.md](streamlit-performance.md)) |
| viz-critic | `references/dashboards.md`, `references/data-display.md` (after [visualization-guide.md](visualization-guide.md)) |

Unreachable (offline, rate-limited): say so in one line and work from StreamSnow's own guides.

## Pin guard: every call must exist in the app's Streamlit

The latest release is ahead of every pinned app, and the warehouse runtime further still. Before
writing any `st.*` command or parameter the upstream skill suggested, confirm it in the app's pin
(`streamlit==` in `pyproject.toml` for container, `streamlit=` in `environment.yml` for warehouse):

- **App environment installed:** use the interpreter preview uses (the repo's `.venv`, then the
  app's): `.venv/bin/python -c "import inspect, streamlit as st; print(st.__version__, inspect.signature(st.metric))"`
  (`.venv\Scripts\python.exe` on Windows). Works on every version. **The printed version must equal
  the pin**; a repo whose apps pin different versions shares one `.venv`, so a mismatch means use
  the next bullet. (`streamlit docs st.<command>` also prints the docstring, but only from
  Streamlit 1.59.0; on older pins it just opens a browser.)
- **Not installed, or the version differs:** read the function in the Streamlit source at the
  pinned tag, `https://github.com/streamlit/streamlit/tree/<pin>/lib/streamlit/`.

Not in the pin → use what the pin has and say so; never raise the pin to fit a suggestion (a pin
change is its own decision). Examples at the time of writing: `alt=` (1.65.0),
`on_change="ignore"` (1.63.0) and `st.echarts_chart` (1.64.0) are missing from the 1.59.2
container pin; `st.menu_button` (1.56.0), alert `title=` (1.57.0) and `bind="query-params"`
(1.55.0) exist there but not in the 1.52.2 warehouse pin.

## StreamSnow wins these conflicts

The upstream skill is written for a Streamlit app anywhere. A Streamlit-in-Snowflake app governed
by StreamSnow differs in these places; follow the StreamSnow side.

| Upstream says | StreamSnow does | Why |
|---|---|---|
| It is "[REQUIRED] for ALL Streamlit tasks" | It is a reference consulted through this recipe; `/build-app` and the other StreamSnow skills run the work | Its own workflow skips the checkpoints and gates. If `streamlit skills` installed a copy into the repo, the same rule applies to it |
| Start a dashboard from its `assets/templates/apps/` | `streamsnow new`, then [build-app scaffold](../build-app/scaffold.md) | The templates pass APIs newer than either pin and leave `streamlit` unpinned. Borrow a pattern, never the file |
| Run with `streamlit run` and rely on hot reload | `streamsnow preview start <slug> --restart-if-stale` | It uses the repo environment, classifies connection errors and restarts a server still running old shared modules or cached SQL |
| `app_pages/` and helpers in `utils/` | `pages/` with `pages/_*.py` helpers | Part of the stable surface: `streamsnow check page-imports`, `snowflake.yml` artifacts, `sql_review/` paths and the fleet depend on it |
| Prefer Vega charts (Altair, native `st.*_chart`) over Plotly | Plotly with the brand template, one chart library per app ([visualization-guide.md](visualization-guide.md)) | Fleet consistency and `hover_definition`; Altair's 5,000-row default cap breaks real dashboards, which is why `/migrate-app` swaps it out |
| Copy a theme file from its templates (web fonts, light and dark sections) | Keep the scaffold's `.streamlit/config.toml`, generated from `streamsnow.config.yaml` branding | One brand across the fleet, set in config |
| `?` placeholders, or `:name` with a dict | `:1`, `:2` binds with a list; optional filters through `render_sql` `{TOKEN}` fragments, never a `None` bind | `streamsnow check bind-predicates`; the deployed driver NULL-binds every parameter when one is `None` |
| Set `ttl` on `conn.query` | `ttl=0` on `conn.query` inside one outer `@st.cache_data(ttl=...)` loader, with `max_entries=` when it is keyed on high-cardinality filters | One cache layer, with the TTL the config sets (`streamsnow check caching`) |
| Cache the whole source, filter in pandas | Filters are loader arguments; aggregate in SQL | Warehouse-scale data, the 32 MB warehouse message limit (`docs/snowflake-docs.md` D7) and query spend. Small lookup tables may still be cached whole |
| Always `st.connection("snowflake")`; `st.secrets` for credentials | Per [runtime-decision.md](runtime-decision.md): `get_active_session()` with a broad fallback on warehouse (`streamsnow check session-fallback`), `st.connection` on container; never a password | `docs/snowflake-docs.md` D2, D5, D6 |
| Caller's-rights connections; `write_pandas` and INSERT examples | Owner's rights, read-only apps | `docs/snowflake-docs.md` D10, D13; `streamsnow check security` blocks write SQL |
| Never `st.components.v1`; use `components.v2` and `st.iframe` | Container: follow upstream when the pin has it. Warehouse: v1 is the only option | Custom components v2 are container-only (`docs/snowflake-docs.md` D7); `st.iframe` needs 1.56.0 |
| Remove dividers | The `st.divider()` before the sources footer stays | [page-conventions.md](page-conventions.md) footer contract |
| Install the latest Streamlit, unpinned | Exact pins | The container base image sets a floor; the warehouse runtime offers a fixed list (`docs/snowflake-docs.md` D12) |

Not applicable to a Streamlit-in-Snowflake app: `st.login` (Snowsight signs viewers in),
`st.App` and ASGI server configuration, and `[server]` options.

Forms, fragments, tabs and expanders follow [streamlit-performance.md](streamlit-performance.md)
first (an `st.form` only when each rerun is expensive); upstream adds detail where it is silent.
Selection widgets, layout and alignment, labels and casing, Material icons, and state and keys come
from the upstream skill, subject to the pin guard. A `streamsnow check` finding always beats
upstream advice.
