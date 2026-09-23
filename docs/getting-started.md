# Getting started

StreamSnow helps you build, govern, and ship Streamlit-in-Snowflake apps. Three
ways in, from fastest to most complete:

- **[Path A — see it work in 2 minutes](#path-a--run-the-example-no-snowflake)**
  with the bundled example dashboard. No Snowflake account, no config.
- **[Path B — with Claude Code](#path-b--with-claude-code)**: install the plugin
  and let `/start-app --setup` drive the machine and repo setup, one confirmed
  step at a time.
- **[Path C — CLI only](#path-c--cli-only)**: the same governed repo, built by
  hand with the `streamsnow` CLI.

New to this stack? Each step says what it does and what you should see. Official
Snowflake pages for every fact below are collected in
[Official Snowflake docs, by topic](snowflake-docs.md).

## Prerequisites

| Tool | Why | Check |
|------|-----|-------|
| **Python 3.11+** | The runtime StreamSnow and your apps target | `python3 --version` |
| **uv** (recommended) | Fast Python/dependency manager; runs `streamsnow` with no install via `uvx` | `uv --version` |
| **git** | Version control | `git --version` |
| **Snowflake CLI (`snow`)** | Local preview against live Snowflake + deploy (Paths B and C) | `snow --version` |
| **pre-commit** | Runs the governance checks before each commit in a scaffolded repo | `pre-commit --version` |
| **Claude Code** *(Path B)* | Drives the StreamSnow plugin skills (`/start-app`, `/validate-app`, …) | — |

Install uv with `brew install uv` (macOS) or see [astral.sh/uv](https://docs.astral.sh/uv/);
`uv tool install snowflake-cli` and `uv tool install pre-commit` cover the other
two ([Snowflake CLI installation](https://docs.snowflake.com/en/developer-guide/snowflake-cli/installation/installation)).
The container runtime supports **Python 3.11 only**, so apps pin `>=3.11,<3.12`
([runtime environments](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/runtime-environments)).

`uvx streamsnow doctor` reports all of this in one pass, plus whether the
`snow` connection your config names exists yet.

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

## Path B — with Claude Code

Inside Claude Code, in the directory that will hold your apps:

```
/plugin marketplace add kyle-chalmers/streamsnow
/plugin install streamsnow@streamsnow
/start-app --setup
```

What `--setup` does, in order, confirming each fix before it runs it:

1. Installs the `streamsnow` CLI (`uv tool install streamsnow`) if it is not on
   your PATH — every later skill calls it, so this is required, not optional.
2. Runs `streamsnow doctor` and walks each missing prerequisite (Python, uv,
   git identity, `snow`, `pre-commit`) one at a time.
3. Runs `streamsnow init --no-starter-app`: five questions (runtime, account
   locator, the database apps query, allowed schemas, deploy source), then the
   governed repo files (`AGENTS.md`, `CLAUDE.md`, pre-commit hooks, CI and deploy
   workflows, `.gitignore`, `README.md`, `deploy/tombstones.yml`). No example
   app: `/start-app` scaffolds your real one. Everything else is a commented
   default in `streamsnow.config.yaml`.
4. Prints the one-time `snow connection add … --default` command for your
   account and, once you have run it, confirms the connection exists.
5. Hands you to `/start-app` to spec, scaffold, build, preview, validate,
   review and ship your first app.

If the directory already has Streamlit apps, `--setup` switches to **adopt
mode**: it inventories what exists, pre-answers the configure questions from
your deploy scripts and CI, and writes a `MIGRATION.md` checklist instead of
scaffolding over anything.

You will see the plugin's SessionStart line the next time you open Claude Code
in the repo: the plugin version, the skills, and which guards are active. If it
says `CLI not on PATH`, re-open your shell (or run the install it names).

## Path C — CLI only

### 1. Check your machine

```bash
uvx streamsnow doctor
```

Reports whether Python 3.11+, uv, git, the `snow` CLI and `pre-commit` are
present. Fix anything it flags before continuing. `pre-commit` is optional here
and **required** once a `streamsnow.config.yaml` exists — the scaffolded hooks
need the executable, and a first commit without it fails.

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
`streamsnow init --no-starter-app` writes the repo files without the example
app (then `streamsnow new <domain> <function>` adds your first real one).

`init` reuses an existing config and silently skips repo-level files it already
wrote, but it **errors on the starter app's files** if that app already exists —
re-run with `--force` to overwrite them, or `--app <slug>` to name a different
starter app (default `example-dashboard`). Pass `--reconfigure` to re-run the
wizard. Its closing `Next:` block is the rest of this section; its first step,
installing the Claude Code plugin, is optional on this path (step 6 below).

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
`.gitignore` that excludes `.streamsnow/preview/` (local preview state and logs)
along with `secrets.toml`.

### 3. Connect to Snowflake (one store)

`init` prints the exact `snow connection add` command for your account. It looks
like:

```bash
snow connection add --connection-name <name> --account <locator> \
  --user <you> --authenticator externalbrowser --default
```

That writes the `snow` CLI's `connections.toml`, which every Snowflake developer
tool shares — and `st.connection("snowflake")` reads its **default** connection
when an app has no `secrets.toml`, so this is the only place you type your
account details ([configure connections](https://docs.snowflake.com/en/developer-guide/snowflake-cli/connecting/configure-connections),
[st.connection](https://docs.streamlit.io/develop/api-reference/connections/st.connection)).

Two things to get right:

- Use the account **locator** (e.g. `ab12345.us-east-1`), not the full
  `*.snowflakecomputing.com` hostname — the connector appends the suffix itself,
  and the full hostname double-suffixes and 404s on auth.
- Authenticate with SSO (`externalbrowser`) or a [programmatic access token](https://docs.snowflake.com/en/user-guide/programmatic-access-tokens),
  never a password: Snowflake's [MFA rollout](https://docs.snowflake.com/en/user-guide/security-mfa-rollout)
  retires password-only sign-in (final phase Aug–Oct 2026; dates are
  account-specific).

The per-app `apps/<slug>/.streamlit/secrets.toml` (gitignored) is an **optional
override** for an app that needs a different role or warehouse. If you do copy
`secrets.toml.example`, keep `role` at your config's `snowflake.roles.viewer_role`
— deployed apps run under the CI role's grants, and a broad personal role hides
grant gaps locally that then ship as empty dashboards.

### 4. Install the hooks, validate, preview

```bash
uv tool install pre-commit && pre-commit install
streamsnow validate-app example-dashboard          # PASS proves the scaffold is whole
uv venv && uv pip install -e apps/example-dashboard # the app's deps, in a repo venv
streamsnow preview example-dashboard               # run locally vs live Snowflake
```

`preview` launches the `streamlit` from your repo's `.venv` when one exists
(the `uv venv` line above creates it without activating it), then falls back
to PATH. It polls the health endpoint and translates the common launch failures
(no Snowflake connection, a bad account locator, a missing package) into
actionable hints; `preview status`, `preview logs`, and `preview stop` manage it
from there.

`validate-app` is the deterministic gate: required files, manifest contents,
naming, and the governance checks (`schema-refs`, `security`,
`bind-predicates`, `caching`, `sql-tokens`, `session-fallback`,
`page-imports`, `artifacts`, `path-leaks`, `requirements` — the same names you
pass to `streamsnow check`). Any **FAIL** must be fixed before shipping. Run an
individual check while iterating with, e.g., `streamsnow check caching
apps/<slug>`.

Every query under `apps/<slug>/queries/` also gets a **paste-runnable audit
copy** under `apps/<slug>/sql_review/`, so a reviewer can re-run each visual's
SQL in Snowsight. `streamsnow sql-review discover | generate | check` keeps it
generated and fresh. Drift and hand edits always fail `check`; whether an
*uncovered* query fails or only warns is `sql_review.coverage` in your config
(`warn` by default).

Reproducing CI locally, job by job:

| CI job | Local command |
|---|---|
| Governance gate | `for d in apps/*/; do streamsnow validate-app "$(basename "$d")"; done` |
| SQL-review audit trail | `streamsnow sql-review check` |
| Tombstones | `streamsnow check tombstones --base-ref origin/main` |
| Dependency vulnerabilities | `streamsnow check dependency-vulns` |

### 5. Add another app

```bash
streamsnow new marketing campaign-dashboard
```

Then add it to the README's Apps table — the CLI reminds you, because it is the
step teams forget.

### 6. Add the Claude Code plugin

```
/plugin marketplace add kyle-chalmers/streamsnow
/plugin install streamsnow@streamsnow
```

This adds the skills that wrap the CLI — `/start-app` (the front door),
`/preview-app`, `/validate-app`, `/review-app`, `/ship-app`, and more — plus the
hooks described in the README.

**The review gate will nudge you.** When a Claude Code turn ends with a
substantive app change that no review covers, a one-line message suggests
`/review-app <slug> --auto`. It's advisory only — it never blocks a turn or a
ship, and coverage is per-change (a reviewed file stays reviewed until its
logic actually changes). Silence it with `REVIEW_GATE_OFF=1`, an
`apps/<slug>/.review/SKIP` marker, or `review_gate: {enabled: false}` in
`streamsnow.config.yaml`.

## Upgrading

The plugin does not pick up hook or skill changes on its own; an installed copy
stays at the version it was installed at. `claude plugin list` shows what you
have; the SessionStart line shows it too.

```
/plugin uninstall streamsnow@streamsnow
/plugin install streamsnow@streamsnow      # then restart Claude Code
```

```bash
uv tool upgrade streamsnow
streamsnow update            # dry-run: what the new templates would change
streamsnow update --apply    # re-render AGENTS.md, hooks, CI, deploy.yml
```

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
| `deploy.source` | `stage-copy` (default) or `git-repository`; `deploy.artifact_exclude` names non-code files your pipeline ships by another step |
| `sql_review.coverage` | `warn` (default) or `fail` — whether an uncovered query fails `validate-app`, pre-commit and CI |

See [`streamsnow.config.example.yaml`](../streamsnow.config.example.yaml) for an
annotated template.

## What's next

- **[Data discovery](data-discovery.md)** — find tables and wire queries inside
  the schema-access guardrails.
- **[Deploying](deploying.md)** — ship apps to Snowflake on merge to `main`.
- **[Deploy setup](deploy-setup.md)** — the one-time Snowflake objects + CI
  secrets the pipeline needs.
- **[Troubleshooting](troubleshooting.md)** — symptom / cause / fix for the
  failures people actually hit.
