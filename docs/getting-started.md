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
| **Claude Code** *(optional)* | Drives the StreamSnow plugin skills (`/start-app`, `/validate-app`, …) | — |

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
uvx --with pandas --with plotly \
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
uv tool install streamsnow   # persistent `streamsnow` on your PATH (a bare uvx run is one-shot)
streamsnow init              # scaffolds into the current directory — cd to your repo root first, or pass --dir
```

`init` runs an interactive wizard that writes
[`streamsnow.config.yaml`](#the-config-file) (your Snowflake account, objects,
roles, governance schemas, runtime, and deploy source), then scaffolds a
governed repo with a starter app under `apps/<slug>/`. To split the steps, run
`streamsnow configure` first (config only), then `streamsnow init` to scaffold.

`init` reuses an existing config and silently skips repo-level files it already
wrote, but it **errors on the starter app's files** if that app already exists —
re-run with `--force` to overwrite them, or `--app <slug>` to name a different
starter app (default `example-dashboard`). Pass `--reconfigure` to re-run the
wizard.

A scaffolded app looks like:

```
apps/<slug>/
  streamlit_app.py         # st.navigation entrypoint, apply_branding()
  pages/overview.py        # branded metric + Plotly chart + a cached loader
  queries/example_metric.sql
  sql_review/              # human-runnable SQL audit trail (streamsnow sql-review)
    manifests/example_metric.json   # the editing surface
    example_metric.review.sql       # generated: paste-runnable in Snowsight
  branding.py  sql_loader.py
  .streamlit/config.toml   .streamlit/secrets.toml.example
  snowflake.yml            pyproject.toml (container) | environment.yml (warehouse)
  AGENTS.md
```

At the repo level, `init` also writes `deploy/tombstones.yml` (the registry
the deploy pipeline uses to drop retired apps — empty until your first
rename; see [Deploying](deploying.md#retiring-or-renaming-an-app)) and a
`.gitignore` that excludes `.streamsnow/` (local preview state and logs —
runtime artifacts, never committed) along with `secrets.toml`.

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
uv venv && uv pip install -e apps/marketing-campaign-dashboard   # install the app's deps locally
streamsnow preview marketing-campaign-dashboard   # run locally vs live Snowflake
streamsnow validate-app marketing-campaign-dashboard   # PASS/FAIL ship gate
```

App dependencies are not installed automatically — `preview` runs the
`streamlit` on your PATH, so install the app's `pyproject.toml` deps into a
local venv first (the missing-package launch failure is one of the hints
`preview` translates).

`preview start` launches the app in the background, polls its health endpoint,
and translates the common launch failures (missing `secrets.toml`, a bad
account locator, a missing package) into actionable hints; `preview status`,
`preview logs`, and `preview stop` manage it from there. A bare
`streamsnow preview <slug>` is shorthand for `preview start <slug>`.

`validate-app` is the deterministic gate: required files, manifest contents,
naming, and the governance checks (`schema-refs`, `security`,
`bind-predicates`, `caching`, `sql-tokens`, `session-fallback`,
`page-imports`, `artifacts`, `path-leaks`, `requirements` — the same names you
pass to `streamsnow check`). Any **FAIL** must be fixed before shipping. Run an
individual check while iterating with, e.g., `streamsnow check caching
apps/<slug>`.

One convention worth knowing on day one: every query under
`apps/<slug>/queries/` — the directory the validate gate pushes UI-feeding SQL
into — also gets a **paste-runnable audit copy** under
`apps/<slug>/sql_review/`, so a reviewer can re-run each visual's SQL in
Snowsight. `streamsnow sql-review discover | generate | check` keeps it
generated and fresh (the scaffold ships a starter manifest, so the pattern is
live from commit 1); the `check` fails closed in pre-commit and the generated
CI, and warns only inside `validate-app` in 0.6.

### 5. (Optional) Claude Code plugin

Inside Claude Code:

```
/plugin marketplace add kyle-chalmers/streamsnow
/plugin install streamsnow@streamsnow
```

This adds the skills that wrap the CLI — `/start-app` (the front door),
`/preview-app`, `/validate-app`, `/review-app`, `/ship-app`, and more — plus a SessionStart hook
scoped to StreamSnow repos.

**The review gate will nudge you.** When a Claude Code turn ends with a
substantive app change that no review covers, a one-line message suggests
`/review-app <slug> --auto`. It's advisory only — it never blocks a turn or a
ship, and coverage is per-change (a reviewed file stays reviewed until its
logic actually changes). Silence it with `REVIEW_GATE_OFF=1`, an
`apps/<slug>/.review/SKIP` marker, or `review_gate: {enabled: false}` in
`streamsnow.config.yaml`.

## The config file

`streamsnow.config.yaml` is the single source of truth the CLI, the checks, CI,
and the scaffold templates all read. **No secrets live here** (those go in CI
secrets / `secrets.toml`). The load-bearing sections:

| Section | What it controls |
|---------|------------------|
| `runtime` | `container` (default) or `warehouse` |
| `snowflake.objects` | where apps deploy (app database/schema), the warehouse, and container `compute_pool` + `external_access_integration` |
| `snowflake.roles` | `ci_role` (deploy) and `viewer_role` (preview + deployed access) |
| `governance` | `database`, `schema_allow`, `schema_deny`, `read_exceptions` — the data guardrails. `schema_deny` is what the `schema-refs` check enforces (a denylist); `schema_allow` is the convention the scaffolded queries and docs point at, not an enforced gate |
| `deploy.source` | `stage-copy` (default) or `git-repository` |

See [`streamsnow.config.example.yaml`](../streamsnow.config.example.yaml) for an
annotated template.

## What's next

- **[Data discovery](data-discovery.md)** — find tables and wire queries inside
  the schema-access guardrails.
- **[Deploying](deploying.md)** — ship apps to Snowflake on merge to `main`.
- **[Deploy setup](deploy-setup.md)** — the one-time Snowflake objects + CI
  secrets the pipeline needs.
