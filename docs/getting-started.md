# Getting started

StreamSnow helps you build, govern, and ship Streamlit-in-Snowflake apps. This
guide takes you from zero to a running app two ways:

- **[Path A — see it work in 2 minutes](#path-a--run-the-example-no-snowflake)**
  with the bundled example dashboard. No Snowflake account, no config.
- **[Path B — set up your own governed repo](#path-b--set-up-a-governed-repo)**
  with the `streamsnow` CLI: a scaffolded monorepo, governance guardrails, local
  preview against live Snowflake, and a deploy pipeline.

New to this stack? Each step says what it does and what you should see.

## Prerequisites

| Tool | Why | Check |
|------|-----|-------|
| **Python 3.11+** | The runtime StreamSnow and your apps target | `python3 --version` |
| **uv** (recommended) | Fast Python/dependency manager; runs `streamsnow` with no install via `uvx` | `uv --version` |
| **git** | Version control | `git --version` |
| **Snowflake CLI (`snow`)** | Local preview against live Snowflake + deploy (Path B only) | `snow --version` |
| **Claude Code** *(optional)* | Drives the StreamSnow plugin skills (`/new-app`, `/validate-app`, …) | — |

Install uv with `brew install uv` (macOS) or see [astral.sh/uv](https://docs.astral.sh/uv/).
The container runtime supports **Python 3.11 only**, so apps pin `>=3.11,<3.12`.

## Path A — run the example (no Snowflake)

The repo ships a complete, StreamSnow-shaped app wired to deterministic sample
data, so it renders anywhere with **no Snowflake connection**.

```bash
git clone https://github.com/kyle-chalmers/streamsnow.git
cd streamsnow
pip install -r examples/sample-dashboard/requirements.txt
streamlit run examples/sample-dashboard/streamlit_app.py
```

Or, with uv and no install step:

```bash
uvx --with streamlit --with pandas --with plotly \
  streamlit run examples/sample-dashboard/streamlit_app.py
```

Streamlit opens at <http://localhost:8501>. You'll see KPI cards, a trend line,
and a channel breakdown — the same structure (`st.navigation` entrypoint,
branding, `@st.cache_data` loaders) that `streamsnow init` scaffolds, just with
mock data instead of `conn.query(...)`. See
[`examples/sample-dashboard/README.md`](../examples/sample-dashboard/README.md).

## Path B — set up a governed repo

### 1. Check your machine

```bash
uvx streamsnow doctor
```

Reports whether Python 3.11+, uv, git, and the `snow` CLI are present. Fix
anything it flags before continuing.

### 2. Configure + scaffold

```bash
uvx streamsnow init
```

`init` runs an interactive wizard that writes
[`streamsnow.config.yaml`](#the-config-file) (your Snowflake account, objects,
roles, governance schemas, runtime, and deploy source), then scaffolds a
governed repo with a starter app under `apps/<slug>/`. To split the steps, run
`streamsnow configure` first (config only), then `streamsnow init` to scaffold.

`init` reuses an existing config, so re-running it is safe. Pass
`--reconfigure` to re-run the wizard, or `--app <slug>` to name the starter app
(default `example-dashboard`).

A scaffolded app looks like:

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

### 3. Connect to Snowflake (for local preview)

`init` prints the exact `snow connection add` command for your account. It looks
like:

```bash
snow connection add --connection-name <name> --account <locator> \
  --user <you> --authenticator externalbrowser
```

Use the account **locator** (e.g. `ab12345.us-east-1`), not the full
`*.snowflakecomputing.com` hostname — the connector appends the suffix itself,
and the full hostname double-suffixes and 404s on auth.

Then create local preview secrets from the scaffolded template:

```bash
cp apps/<slug>/.streamlit/secrets.toml.example apps/<slug>/.streamlit/secrets.toml
```

Set `role` to your config's **`snowflake.roles.viewer_role`** — deployed apps
run under that role, so matching it locally surfaces grant gaps before deploy.
`secrets.toml` is gitignored; never commit it.

### 4. Build, preview, validate

```bash
streamsnow new marketing campaign-dashboard      # scaffold another app
streamsnow preview marketing-campaign-dashboard   # run locally vs live Snowflake
streamsnow validate-app marketing-campaign-dashboard   # PASS/FAIL ship gate
```

`validate-app` is the deterministic gate: required files, manifest contents,
naming, and the governance checks (`schema-refs`, `app-security`,
`bind-predicates`, `caching`). Any **FAIL** must be fixed before shipping. Run
an individual check while iterating with, e.g.,
`streamsnow check caching apps/<slug>`.

### 5. (Optional) Claude Code plugin

Inside Claude Code:

```
/plugin marketplace add kyle-chalmers/streamsnow
/plugin install streamsnow@streamsnow
```

This adds the skills that wrap the CLI — `/new-app`, `/preview-app`,
`/validate-app`, `/ship-app`, `/start-app`, and more — plus a SessionStart hook
scoped to StreamSnow repos.

## The config file

`streamsnow.config.yaml` is the single source of truth the CLI, the checks, CI,
and the scaffold templates all read. **No secrets live here** (those go in CI
secrets / `secrets.toml`). The load-bearing sections:

| Section | What it controls |
|---------|------------------|
| `runtime` | `container` (default) or `warehouse` |
| `snowflake.objects` | where apps deploy (app database/schema), the warehouse, and container `compute_pool` + `external_access_integration` |
| `snowflake.roles` | `ci_role` (deploy) and `viewer_role` (preview + deployed access) |
| `governance` | `database`, `schema_allow`, `schema_deny`, `read_exceptions` — the data guardrails the checks enforce |
| `deploy.source` | `stage-copy` (default) or `git-repository` |

See [`streamsnow.config.example.yaml`](../streamsnow.config.example.yaml) for an
annotated template.

## What's next

- **[Data discovery](data-discovery.md)** — find tables and wire queries inside
  the schema-access guardrails.
- **[Deploying](deploying.md)** — ship apps to Snowflake on merge to `main`.
- **[Deploy setup](deploy-setup.md)** — the one-time Snowflake objects + CI
  secrets the pipeline needs.
