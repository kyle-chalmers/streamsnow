<!-- markdownlint-disable MD041 -->
<h1 align="center">StreamSnow ❄️</h1>

<p align="center">
  <a href="https://github.com/kyle-chalmers/streamsnow/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/kyle-chalmers/streamsnow/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://pypi.org/project/streamsnow/"><img alt="PyPI" src="https://img.shields.io/pypi/v/streamsnow.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Claude Code plugin" src="https://img.shields.io/badge/Claude%20Code-plugin-d97757">
</p>

<p align="center">
  <strong>An open-source toolkit for building, governing, and shipping
  Streamlit-in-Snowflake apps with Claude Code.</strong>
</p>

<p align="center">
  <em>Scaffold a governed monorepo, build dashboards inside enforced
  data-governance guardrails, and deploy them to Snowflake — without
  learning the rules by hand.</em>
</p>

---

> **Status: beta, functional.** The CLI (configure / init / new / doctor /
> validate-app / preview / check / sql-review / review-gate / review-loop /
> migrate / nav / deploy-sql / deploy-setup / verify-deploy / update) and the
> Claude Code plugin (8 skills + shared recipes, with deprecated aliases for the
> pre-0.3 names) are implemented and CI-green for both runtimes and both deploy
> sources. Published on PyPI (`uvx streamsnow` / `pip install streamsnow`); APIs
> may still evolve toward 1.0.

## Mission

**StreamSnow aims to let a data team build and ship Streamlit-in-Snowflake apps
quickly and safely, by turning production lessons into scaffolding and checks
that people and AI sessions can follow without having to remember them.**

## Vision

**A data professional, with or without an AI assistant, can take a dashboard
from idea to a governed, verified deployment in Snowflake without learning the
platform's traps the hard way, and a reviewer with Snowsight can re-run the SQL
behind the numbers it shows.**

**This is for you if:**

- ✅ you run, or will run, more than one Streamlit app in Snowflake with more
  than one author
- ✅ you want Claude Code sessions and humans held to the same governance rules
- ✅ you want a reviewer to re-run a dashboard's SQL in Snowsight without
  reading Python
- ❌ you host Streamlit outside Snowflake, or you want a BI tool, a scheduler,
  or a data catalog — StreamSnow sits beside those

**Principles** every change is judged against (the rules were already in the
repo; collecting them here is what keeps future edits aligned):

1. **One implementation, many consumers.** CLI, plugin, pre-commit, and CI call
   the same code.
2. **Detection is automated and total; destruction requires explicit committed
   consent.**
3. **The backstop asks; it never decides.** The gates are `validate-app` and CI,
   not the review nudge.
4. **Org knowledge lives in `streamsnow.config.yaml` and `.streamsnow/overlays/`,
   never in skills.**
5. **Every rule names the incident that created it and the mechanism that
   enforces it.**
6. **Degrade, don't die.** A missing enabler is named, not refused.
7. **Faithful to a real fleet.** A check that fails a well-run production app is
   a defect in the check until proven otherwise; `tests/fixtures/fleet/` is the
   regression net.
8. **Leaving should be cheap.** Everything StreamSnow writes into a repo is a
   plain file the repo keeps; the checks are the only dependency, and the exit
   path is documented as it actually is ([Distribution → Ownership and
   exit](docs/distribution.md#ownership-and-exit)).

## What it is

StreamSnow is a **hybrid** of two things that work together:

1. **A `streamsnow` CLI** (PyPI) — scaffolds a governed Streamlit-in-Snowflake
   monorepo, runs an interactive setup wizard, and vendors the validation
   tools, CI, pre-commit hooks, and branding your repo needs.
2. **A Claude Code plugin** (marketplace) — ships the skills, subagents, and
   hooks that turn Claude Code into a domain expert for this stack:
   `/start-app` (the front door), `/preview-app`, `/validate-app`,
   `/review-app`, `/ship-app`, and more.

Think **a Claude Code skill pack fused with an installable system + setup**.
The CLI gives you the substrate; the plugin gives Claude the playbook. A single
`streamsnow.config.yaml` is the source of truth both read from.

## Why

Building Streamlit apps on Snowflake well means getting a hundred small things
right: caching with TTLs, parameterized SQL that survives the deployed Go
driver, runtime selection (container vs. warehouse), schema access guardrails,
a deploy pipeline, branding, and review discipline. StreamSnow encodes those as
**executable guardrails** — pre-commit + CI gates, scaffolding templates, and
Claude Code skills — so every developer (and every Claude session) follows the
same rules and ships safely.

## Two things you choose

StreamSnow treats two axes as first-class, configurable options:

| Axis | Options |
|------|---------|
| **Runtime** | **Container** (default — GA since March 2026, full PyPI, local preview matches deploy) or **Warehouse** (instant start, Anaconda channel, no compute-pool cost). Snowflake's own comparison: [runtime environments](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/runtime-environments) |
| **Deploy source** | **Stage-copy** (default — CI uploads to an internal stage) or **Snowflake `GIT REPOSITORY`** (Snowflake pulls from your Git repo) |

## Quickstart

Two lanes; pick the one that matches how you work. Both end at the same governed
repo, and both need Python 3.11+, `uv`, and `git` (`uvx streamsnow doctor` tells
you what is missing).

### With Claude Code (recommended)

```
/plugin marketplace add kyle-chalmers/streamsnow
/plugin install streamsnow@streamsnow
/start-app --setup
```

`/start-app --setup` installs the `streamsnow` CLI if it is missing, runs the
doctor, walks each missing prerequisite one confirmation at a time, runs
`streamsnow init --no-starter-app` (the five-question wizard plus the governed
repo files: `AGENTS.md`, pre-commit hooks, CI, `.gitignore`, README; no example
app), and hands you to `/start-app` to build your first app. In a repo that already has Streamlit apps it switches to adopt
mode (maps onto what exists, writes `MIGRATION.md`, never scaffolds over you).

### CLI only

```bash
uv tool install streamsnow           # persistent `streamsnow` on your PATH
mkdir my-snowflake-apps && cd my-snowflake-apps
streamsnow init                      # 5-question wizard, then a governed scaffold
snow connection add --connection-name <name> --account <locator> \
  --user <you> --authenticator externalbrowser --default   # init prints the exact command
uv tool install pre-commit && pre-commit install
streamsnow validate-app example-dashboard                 # PASS proves the scaffold is whole
uv venv --python 3.11 && uv pip install -e apps/example-dashboard   # container runtime
streamsnow preview example-dashboard
```

On the warehouse runtime an app has `environment.yml` instead of `pyproject.toml`,
so install its packages directly (`init` and `streamsnow preview` print the exact
line).

One connection store: `st.connection("snowflake")` reads the `snow` CLI's default
connection locally, so the per-app `secrets.toml` is an optional override, not a
second place to type the same values.

### Upgrading

The two halves upgrade separately, and the plugin half does **not** pick up
hook or skill changes on its own — an installed copy stays at the version it
was installed at until you reinstall it.

```
claude plugin list                       # shows the installed plugin version
/plugin uninstall streamsnow@streamsnow
/plugin install streamsnow@streamsnow    # then restart Claude Code
```

```bash
uv tool upgrade streamsnow               # the CLI
streamsnow update                        # dry-run: governance files the new templates would change
streamsnow update --apply                # re-render AGENTS.md, hooks, CI, deploy.yml
```

Generated CI pins `streamsnow>=0.7,<0.8`; bump the pin with `update --apply`
when you move majors. `claude plugin details streamsnow@streamsnow` lists 16
skills: 8 real ones plus 8 deprecated aliases for the pre-0.3 names.

## The skills

One front door plus focused verbs — each skill's `SKILL.md` stays under 80
lines, with depth in per-skill reference files:

| Skill | What it does |
|---|---|
| `/start-app` | The front door: spec (incl. backfill from existing source) → scaffold → build pages → ship, with checkpoints. Also `--setup` (machine + repo) and `adopt` (existing repos — maps, doesn't scaffold, writes `MIGRATION.md`) |
| `/preview-app` | Run an app locally against live Snowflake |
| `/validate-app` | The pass/fail check that must be clean before shipping |
| `/review-app` | Senior-reviewer-grade review; `--fix` applies findings, `--auto` loops to clean (executable loop primitives + per-change coverage stamping), `--sql` authors the audit-trail manifests |
| `/audit-lineage` | Live-warehouse column + lineage verification (read-only, bounded) |
| `/feedback-app` | Turn user feedback into classified, atomic-commit fixes |
| `/ship-app` | Validate-gated stage → commit → push → PR → watch CI |
| `/migrate-app` | Port an external Streamlit app in (lift, then conform) |

Pre-0.3 names (`/new-app`, `/refine-requirements`, `/add-page`, `/onboard`,
`/auto-review-app`, `/sql-review`, `/apply-review`, `/deep-dive-data`) still
work as deprecated aliases and will be removed in the next major release.

## The audit trail (new in 0.6)

Every query under `apps/<slug>/queries/` gets a **human-runnable proof**: a
fully-rendered, paste-runnable `.review.sql` under `apps/<slug>/sql_review/`,
generated from a per-feature manifest and verified by an import-free freshness
+ coverage gate (`streamsnow sql-review check`). Coverage is keyed to the
`queries/` convention — the same place the validate gate pushes UI-feeding SQL
— so SQL inlined in Python sits outside its reach. Drift, hand edits, unbound
binds and write statements always fail the gate; whether an *uncovered* query
fails or warns is your repo's call — `sql_review: {coverage: warn | fail}` in
`streamsnow.config.yaml` (default `warn`, so an adopting fleet backfills on its
own schedule; new in 0.7, replacing the 0.6 "warn now, FAIL later" promise). A
person with nothing but Snowsight can trace a covered visual back to the data
and confirm it — see **[Auditing a visual](docs/auditing-a-visual.md)**. For
dashboards whose visuals aggregate differently than any single query,
`"mode": "metrics"` manifests (0.6.1) render one authored block per visual.

## Make it yours — repo overlays (new in 0.6.1)

The skills are generic procedures; your org's knowledge layers on top without
forking them. Commit `.streamsnow/overlays/<skill>.md` files and every skill
reads its overlay first — extra steps, local failure signatures, environment
specifics, explicit overrides. Plugin upgrades never touch them; overlays may not
skip mandatory gate invocations, and the coded gates (hooks, CI, pre-commit)
run outside skill prose entirely. See
[skills/_shared/overlays.md](skills/_shared/overlays.md).

## Hooks, in full

Trust demands transparency: this plugin runs hooks, so here is every one of them. All are
stdlib-only, make **no network calls**, never write outside the repo (plus one best-effort
dedupe state file in `$TMPDIR`, so the review nudge fires once per state, not every turn),
and fail open — a hook
error never blocks your session; the guards only ever *add* a confirmation or a note.

| Event | Script | What it does |
|---|---|---|
| PreToolUse (Bash) | `hooks/deploy_safety.py` | Pauses before destructive Streamlit/SQL commands (`snow streamlit deploy/drop`, `CREATE OR REPLACE / DROP / ALTER STREAMLIT`, stage `REMOVE`, destructive SQL incl. `-f` files / stdin) — `/ship-app` is the sanctioned deploy path |
| SessionStart | `hooks/session_start.sh` | One line inside a StreamSnow repo (plugin version, skills, which guards are active), a one-line `/start-app --setup` nudge in a repo that has Streamlit apps but no config, silence everywhere else |
| Stop | `hooks/review_gate_stop.py` | Warn-only nudge (a `systemMessage`, never a turn continuation) when a substantive app change ends with no review covering it — points at `/review-app <slug> --auto`. Off-switches: `REVIEW_GATE_OFF=1`, `apps/<slug>/.review/SKIP`, or `review_gate: {enabled: false}` in config |

All hooks are repo-gated on `streamsnow.config.yaml` — zero cost in unrelated repos — and
declare explicit timeouts so a hung hook can never stall a session. To turn them off, disable
the plugin (`claude plugin disable streamsnow`). Hook additions do not reach installed copies
automatically — see [Upgrading](#upgrading).

## How it's organized

```
streamsnow/            the PyPI package — CLI, config, policy, scaffolder, tools
  ├── cli.py           configure / init / new / doctor / check
  ├── config.py        typed + validated streamsnow.config.yaml model
  ├── policy.py        schema allow/deny single source of truth
  ├── scaffolder.py    renders a governed repo from config
  ├── _templates/      the Jinja scaffold templates (repo/ + app/)
  └── tools/           governance checks + engines (schema refs, security,
                       caching, dependency vulns, tombstones, path leaks,
                       sql_review generator, review gate/loop, migrate, doctor)
.claude-plugin/        Claude Code plugin manifest + marketplace
skills/  agents/  hooks/   Claude Code plugin surface (skills + SessionStart hook)
docs/  examples/            guides + a runnable no-Snowflake example app
```

> Active scaffolding lives in `streamsnow/` (templates under `streamsnow/_templates/`).

The `streamsnow` Python package is the **single source of truth** for tool
logic: the CLI, the Claude Code plugin, pre-commit, and CI all call the same
code — one implementation, many consumers.

## Documentation

- **[Getting started](docs/getting-started.md)** — run the example with no
  Snowflake, then scaffold and preview your own governed app.
- **[Data discovery](docs/data-discovery.md)** — find tables and wire queries
  inside the schema-access guardrails.
- **[Deploying](docs/deploying.md)** — ship apps to Snowflake on merge, for both
  deploy sources.
- **[Deploy setup](docs/deploy-setup.md)** — the one-time Snowflake objects and
  CI secrets the pipeline needs.
- **[Auditing a visual](docs/auditing-a-visual.md)** — the five-minute runbook
  for confirming any dashboard number against the warehouse, no code required.
- **[Production lessons](docs/production-lessons.md)** — the incidents behind
  the guardrails, genericized.
- **[Troubleshooting](docs/troubleshooting.md)** — numbered symptom / cause /
  fix entries for the first-run and deploy failures people actually hit.
- **[Official Snowflake docs, by topic](docs/snowflake-docs.md)** — every
  external docs link the toolkit relies on, with scope, retrieved date, and
  where StreamSnow deliberately differs.
- **[Distribution](docs/distribution.md)** — how StreamSnow ships (PyPI CLI +
  plugin) and why there's no separate copy-paste kit.
- **[Migrating a consumer repo](docs/migrating-a-consumer-repo.md)** — bring a
  repo with home-grown skills onto the plugin (skill map + incremental path).

## License

[MIT](LICENSE) © Kyle Chalmers

> StreamSnow is an independent open-source project and is not affiliated with or
> endorsed by Snowflake Inc., Streamlit, or Anthropic.
