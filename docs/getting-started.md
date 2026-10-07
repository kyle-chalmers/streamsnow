# Getting started

StreamSnow helps you build, govern, and ship Streamlit-in-Snowflake apps. Three
ways in, from fastest to most complete:

- **[Path A — see it work in 2 minutes](#path-a--run-the-example-no-snowflake)**
  with the bundled example dashboard. No Snowflake account, no config.
- **[Path B — with Claude Code](#path-b--with-claude-code)**: install the plugin
  and let `/onboard` drive the machine, repo and Snowflake setup, explaining
  each step as it goes.
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
| **Claude Code** *(Path B)* | Drives the StreamSnow plugin skills (`/build-app`, `/validate-app`, …) | — |
| **Node.js 20+** *(Path B, recommended)* | Runs the Playwright browser tool that clicks through your app and screenshots each page | `node --version` |

**On Windows**, run StreamSnow inside [WSL](https://learn.microsoft.com/en-us/windows/wsl/install)
(Windows Subsystem for Linux, Microsoft's built-in Linux layer): run `wsl --install` in an
administrator PowerShell, restart, and do everything below inside the Ubuntu app it adds. The
CLI, local preview and the safety guards run on native Windows since 0.7.7, but the plugin's
SessionStart hook is still a bash script and `/onboard` sends native Windows to WSL, so WSL is
the supported route for now.

Install uv with `brew install uv` (macOS) or see [astral.sh/uv](https://docs.astral.sh/uv/);
`uv tool install snowflake-cli` and `uv tool install pre-commit` cover the other
two ([Snowflake CLI installation](https://docs.snowflake.com/en/developer-guide/snowflake-cli/installation/installation)).
The container runtime supports **Python 3.11 only**, so apps pin `>=3.11,<3.12`
([runtime environments](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/runtime-environments)).
You do not need 3.11 as your system Python: `uv venv --python 3.11` downloads
one (or run `uv python install 3.11` ahead of time). If Homebrew's `snow`
crashes on start, `uv tool install snowflake-cli` gives you a working one;
`streamsnow doctor` reports a broken `snow` as `BROKEN`. A `snow` that is
only slow to start (the first run after the machine idles can take 25 s)
is a `warn` that asks you to re-run doctor, not a reinstall.

`uvx streamsnow doctor` reports all of this in one pass, plus whether the
`snow` connection your config names exists yet.

### What to ask your Snowflake admin for

Building and previewing an app needs only a Snowflake login that can read the
data. Shipping one through CI needs one-time objects most people cannot create
themselves. Run `streamsnow deploy-setup --admin` after `init` and hand the
output to your admin; it asks for, in plain terms:

- a database, schema and `XSMALL` warehouse for the apps (auto-suspending);
- two roles: a **CI role** that deploys and owns the apps, and a **viewer role**
  that opens them;
- a **CI service user** with key-pair auth (`streamsnow ci-key create` makes the
  key pair; `--public-key-file` puts the public key in the script);
- `CREATE STREAMLIT` (and `CREATE STAGE`) on the app schema for the CI role, and
  read access to the schemas your apps query;
- container runtime only: `USAGE` on a PyPI external access integration and on
  the compute pool (`SYSTEM_COMPUTE_POOL_CPU` exists already).

See [Deploy setup](deploy-setup.md#0-the-admin-bootstrap-deploy-setup---admin)
for the section-by-section breakdown.

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

From a shell in the directory that will hold your apps, install the plugin at
project scope (recorded in the repo's `.claude/settings.json`, not your user
settings):

```bash
claude plugin marketplace add --scope project kyle-chalmers/streamsnow
claude plugin install --scope project streamsnow@streamsnow
```

Then, in a Claude Code session in that directory (an already-open one is fine; the
skills are normally available straight away), run:

```
/onboard
```

If `/onboard` isn't listed, run `/reload-plugins` (no restart needed) and try again.

`/onboard` works in four stages and says what it is doing, and why, at every step.

1. **Check and prepare.** It installs the `streamsnow` CLI if it is not on your
   PATH, runs `streamsnow doctor`, and lists every Snowflake access you already
   have (`snow` connections, a Snowflake MCP server, a dbt profile, `SNOWFLAKE_*`
   variable names), reading names only, never values. It reads your account with
   read-only `SHOW` queries to propose the setup answers, then installs whatever
   is missing after one approval: Python, uv, `pre-commit`, `snow`, `gh`, and
   Node.js, which runs the Playwright browser tool that clicks through your app
   and screenshots each page. It downloads that tool and its browser now, creates
   the repo's Python environment (`.venv`), and saves the plugin into the repo's
   Claude settings so teammates are offered it.
2. **One round of questions.** Clickable choices, each explained first so you
   learn what is being set up. Whether to set up your Snowflake connection for
   you or guide you through it, then one question each for the five setup answers
   (runtime, the database apps query, allowed schemas, schemas apps may not
   query, deploy source), a bulleted explanation of the defaults it does not ask
   about followed by one keep-or-change question, your git name and email if
   they are missing, and who runs the Snowflake admin script, which it describes
   before asking, plus which of your Snowflake roles can open the apps. What it detects is the recommended answer, never a substitute
   for the question.
3. **Build.** After you confirm, `streamsnow init --no-starter-app` writes
   `streamsnow.config.yaml` and the governed repo files (`AGENTS.md`, `CLAUDE.md`,
   pre-commit hooks, CI and deploy workflows, `.sqlfluff`, `.gitignore`, `README.md`,
   `deploy/tombstones.yml`, plus `osv_allowlist.json` on the warehouse runtime). No example app: `/build-app` scaffolds your real one.
   Then it installs the pre-commit hook for your clone.
4. **Finish.** It runs `streamsnow ci-key create` (save a copy of the private key
   somewhere safe, such as a password manager), prepares the Snowflake admin
   script in `.internal/admin-setup.sql`, and once your admin has run it, sets the
   deploy workflow's GitHub secrets with `streamsnow ci-key push`. If the repo is on
   GitHub, it checks whether GitHub deletes a branch once its pull request merges and,
   if not, asks before turning that repo setting on.

It ends at "ready to build and preview" (next: `/build-app`) or "ready to deploy".
Re-run `/onboard` any time to check.

### If you are the Snowflake admin

`/onboard` copies `.internal/admin-setup.sql` to your clipboard. In Snowsight,
open a new SQL worksheet, paste, and choose Run All, then tell Claude it ran.
Claude never runs the admin script itself.

### If someone else is

`/onboard` writes a short note naming the file, with a link to
[Deploy setup](deploy-setup.md), for you to forward. You can build and preview
now; deploys wait until your admin has run it and you re-run `/onboard`.

### Joining a repo someone already set up

Clone it, open Claude Code in it, accept the StreamSnow plugin when prompted, and
run `/onboard`. It sees the existing `streamsnow.config.yaml` and governed files,
skips scaffolding, and only sets up your machine: tools, your git identity, the
pre-commit hook for your clone, your Snowflake connection, and the browser check.
Your session's first line tells you if your clone still needs it.

### Repos that already have Streamlit apps

If the directory already has Streamlit apps but no `streamsnow.config.yaml`,
`/onboard` switches to **adopt mode**: it inventories what exists, suggests
answers to the configure questions from your deploy scripts and CI (it still asks you
each one), and writes a
`MIGRATION.md` checklist instead of scaffolding over anything.

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
  pages/overview.py        # starter placeholder: sample numbers, a Plotly chart, a cached loader
  pages/about.py           # "About this app": purpose, pages, metric definitions, data sources
  pages/_glossary.py       # one definition per metric, reused by tooltips, expanders and About
  pages/_layout.py         # shared page pieces: definitions, empty state, sources, "show the SQL"
  pages/_time_controls.py  # the period picker every page shares, bounded by the data
  pages/_data.py           # cached loaders for data more than one page uses
  queries/example_metric.sql   # starter placeholder: reads YOUR_TABLE
  sql_review/              # runnable SQL per page (streamsnow sql-review)
    index.yaml             # the editing surface: pages, metrics, review window
    01_overview.sql        # generated: one runnable section per metric
    README.md  AGENTS.md  CLAUDE.md
  branding.py  sql_loader.py  review.py (the review_value marker)
  .streamlit/config.toml   .streamlit/secrets.toml.example
  snowflake.yml            pyproject.toml (container) | environment.yml (warehouse)
  AGENTS.md
```

Every app ships an **About this app** page, last in its navigation. It is
built from the app itself, not hand-written: metric definitions from
`pages/_glossary.py` and data sources from the header block of each
`queries/*.sql` file, plus a short `ABOUT` block (purpose, audience, owner,
pages, caveats) that `/build-app` fills from `REQUIREMENTS.md`. The `pages/_*.py`
modules are shared by every page; import them package-qualified
(`from pages._glossary import metric_help`), because a bare import works under
`streamlit run` and fails once deployed.

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
`secrets.toml.example`, use a role whose data reads match the CI role's, since
deployed apps run under the CI role's grants: either the viewer role with the
opt-in data grants from `deploy-setup --admin` uncommented, or a developer role
with the same reads. A broad personal role hides grant gaps locally that then
ship as empty dashboards.

### 4. Install the hooks, validate, preview

```bash
uv tool install pre-commit && pre-commit install
streamsnow validate-app example-dashboard          # FAILS on the starter placeholders until replaced
```

Then create a local environment with the app's dependencies. The command
depends on the runtime, because the two runtimes ship different manifests:

```bash
# container runtime: the app has a pyproject.toml (Python 3.11 only)
uv venv --python 3.11 && uv pip install -e apps/example-dashboard

# warehouse runtime: the app has an environment.yml for Snowflake's Anaconda
# channel and no pyproject.toml, so install its packages directly (translate
# a conda pin like streamlit=1.52.2 to streamlit==1.52.2)
uv venv --python 3.11 && uv pip install 'streamlit==1.52.2' pandas plotly snowflake-snowpark-python
```

`init` prints the exact line for your app, and so does `streamsnow preview` if
it cannot find `streamlit`. Then:

```bash
streamsnow preview example-dashboard               # run locally vs live Snowflake
```

`preview` launches the `streamlit` from your repo's `.venv` when one exists
(the `uv venv` line above creates it without activating it), then falls back
to PATH. It polls the health endpoint and translates the common launch failures
(no Snowflake connection, a bad account locator, a missing package) into
actionable hints; `preview status`, `preview logs`, and `preview stop` manage it
from there.

`validate-app` is the deterministic gate: required files, manifest contents,
naming, the governance checks (`schema-refs`, `app-security`,
`bind-predicates`, `caching`, `sql-tokens`, `session-fallback`,
`page-imports`, `artifacts`, `path-leaks`, `requirements`; each has a
`streamsnow check` subcommand of the same name, except `app-security`, which is
`streamsnow check security`), the app's SQL review, and `placeholders`. The
[CLI reference](cli-reference.md#validate-app) lists every step. Any **FAIL**
must be fixed before shipping. The
`placeholders` check **fails** while any app file still reads the starter's
`YOUR_TABLE`: the example query, the review window in
`sql_review/index.yaml`, and `pages/overview.py`, whose numbers are samples. Replace or
repoint all three (or delete the example app) before you merge, because CI
deploys every app under `apps/`; every other check passing is what proves the
scaffold itself is whole. Run an
individual check while iterating with, e.g., `streamsnow check caching
apps/<slug>`.

Every page of an app also gets a **runnable SQL file** under
`apps/<slug>/sql_review/` (`01_overview.sql`, …), one section per metric, so a
reviewer can re-run each visual's SQL in DataGrip or Snowsight
([Auditing a visual](auditing-a-visual.md)). `sql_review/index.yaml` lists the
metrics; `streamsnow sql-review generate` writes the files and
`streamsnow sql-review check` keeps them fresh, checks each page's
`review_value` markers, and lints the app's queries with the repo's
`.sqlfluff`. Drift, hand edits and lint always fail `check`; whether an
*uncovered* page or query fails or only warns is `sql_review.coverage` in your
config (`warn` by default).

Reproducing CI locally, job by job:

| CI job | Local command |
|---|---|
| Lint | `ruff check apps/` |
| Governance gate | `for d in apps/*/; do streamsnow validate-app "$(basename "$d")"; done` |
| SQL review | `streamsnow sql-review check` |
| Tombstones | `streamsnow check tombstones --base-ref origin/main` |
| Dependency vulnerabilities | `streamsnow check dependency-vulns` |

### 5. Add another app

```bash
streamsnow new marketing campaign-dashboard
```

Then add it to the README's Apps table — the CLI reminds you, because it is the
step teams forget.

### 6. Add the Claude Code plugin

From the repo root, at project scope:

```bash
claude plugin marketplace add --scope project kyle-chalmers/streamsnow
claude plugin install --scope project streamsnow@streamsnow
```

If Claude Code is already open in the repo, the skills normally appear straight
away; if `/build-app` isn't listed, run `/reload-plugins` there to load the plugin
without restarting.

This adds the skills that wrap the CLI — `/build-app` (the front door),
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

```bash
claude plugin uninstall --scope project streamsnow@streamsnow
claude plugin install --scope project streamsnow@streamsnow    # then /reload-plugins in Claude Code if the new skills don't appear
```

```bash
uv tool upgrade streamsnow
streamsnow update            # dry-run: what the new templates would change
streamsnow update --apply    # re-render AGENTS.md, CLAUDE.md, hooks, CI, deploy.yml
```

`update` stops at the repo-level governance files. It never rewrites app files
(`apps/<slug>/review.py`, `pages/_glossary.py`) and never rewrites an existing `.sqlfluff`.
Refresh a stale `review.py` with `streamsnow sql-review helper <slug> --apply` (a dry run
without `--apply`; `--force` as well when it reports `modified`).

Two small template fixes reach new repos only, so an existing repo makes them by hand:

- In `apps/<slug>/pages/_glossary.py`, change the last line of `hover_definition` from
  `return metric_help(key).replace("%", "%%")` to `return metric_help(key)`.
- In `.sqlfluff` (optional: a comment only, no rule or setting changes), replace the two
  comment lines `# rule set below. Run the same lint by hand with:` and the one after it
  (`#   sqlfluff lint apps/<slug>/queries --templater placeholder`) with the current text:

  ```text
  # rule set below. Lint through it, not through a bare sqlfluff run:
  #   streamsnow sql-review check <slug>
  # The query files hold {TOKEN} placeholders, which sqlfluff's placeholder templater
  # (param_style colon) cannot parse. sql-review check substitutes the index's sample
  # tokens first, so the same files lint cleanly there.
  ```

A plugin update does not apply to a session that is already running: restart the
session (or run `/reload-plugins`) before relying on the new skill text.

## The config file

`streamsnow.config.yaml` is the single source of truth the CLI, the checks, CI,
and the scaffold templates all read. **No secrets live here** (those go in CI
secrets / `secrets.toml`). The load-bearing sections:

| Section | What it controls |
|---------|------------------|
| `runtime` | `container` (default) or `warehouse` |
| `project` | `name` and `slug` for the generated README and AGENTS.md |
| `snowflake.account`, `snowflake.connection_name` | the account locator (not the full hostname) and the `snow` CLI connection the CLI and preview use |
| `snowflake.objects` | where apps deploy (app database/schema), the stage for stage-copy deploys, the warehouse, and container `compute_pool` (default `SYSTEM_COMPUTE_POOL_CPU`, which Snowflake pre-provisions) + `external_access_integration`; optional `runtime_name` (default `SYSTEM$ST_CONTAINER_RUNTIME_PY3_11`) and `container_python` (default `3.11`) |
| `snowflake.roles` | `ci_role` (deploys and owns the apps, reads the data) and `viewer_role` (opens deployed apps; data reads are opt-in) |
| `governance` | `database`, `schema_allow`, `schema_deny`, `read_exceptions` — the data guardrails. `schema_deny` is what the `schema-refs` check enforces (a denylist); `schema_allow` is the convention the scaffolded queries and docs point at, not an enforced gate |
| `deploy.source` | `stage-copy` (default) or `git-repository`; `deploy.artifact_exclude` names non-code files your pipeline ships by another step |
| `deploy.git_*` | git-repository only: `git_repository_fqn`, `git_origin` (the GitHub HTTPS URL Snowflake fetches; `deploy-setup` needs it), `git_branch` (default `main`), `api_integration_name`, `secret_name`, and `github_auth_mode` (`pat` default, `github-app` set up the same way, or `public` for no secret). See [Git repository deploys](git-repository.md) |
| `sql_review.coverage` | `warn` (default) or `fail` — whether a page or query missing from `sql_review/index.yaml` fails `validate-app`, pre-commit and CI |
| `review_gate` | the warn-only review nudge: `enabled` (default `true`), and optionally `apps_dir` and `base_ref` |
| `brand` | optional theme for scaffolded apps: `theme.primary`, `theme.background`, `theme.text_color`, `theme.secondary_background`, `font` (default `Inter, sans-serif`) and `chart_sequence` |
| `cache_ttl` | top-level, optional: the default `@st.cache_data` TTL in seconds (default `1800`) written into AGENTS.md and the starter page |

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
