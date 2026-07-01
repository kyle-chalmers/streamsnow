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

> **Status: alpha, functional.** The CLI (configure / init / new / validate-app /
> preview / check / deploy-sql / deploy-setup) and the Claude Code plugin (14
> skills + shared recipes) are implemented and CI-green for both runtimes and
> both deploy sources. Published on PyPI as **v0.1.0**
> (`uvx streamsnow` / `pip install streamsnow`); APIs may still evolve toward 1.0.

## What it is

StreamSnow is a **hybrid** of two things that work together:

1. **A `streamsnow` CLI** (PyPI) — scaffolds a governed Streamlit-in-Snowflake
   monorepo, runs an interactive setup wizard, and vendors the validation
   tools, CI, pre-commit hooks, and branding your repo needs.
2. **A Claude Code plugin** (marketplace) — ships the skills, subagents, and
   hooks that turn Claude Code into a domain expert for this stack:
   `/new-app`, `/preview-app`, `/validate-app`, `/ship-app`, `/start-app`, and
   more.

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
| **Runtime** | **Container** (default — cheaper, full PyPI, modern Streamlit) or **Warehouse** (legacy — instant start, Anaconda channel) |
| **Deploy source** | **Stage-copy** (default — CI uploads to an internal stage) or **Snowflake `GIT REPOSITORY`** (Snowflake pulls from your Git repo) |

## Quickstart (target experience)

```bash
# 0. Check your machine has the prerequisites (Python 3.11+, uv, git, snow CLI)
uvx streamsnow doctor

# 1. Configure your Snowflake environment + scaffold a governed repo with a starter app
uvx streamsnow init          # runs the config wizard, then scaffolds
#    (or split it: `streamsnow configure` to set up streamsnow.config.yaml first,
#     then `streamsnow init` to scaffold)

# 2. Connect to Snowflake + create local preview secrets
snow connection add --connection-name <name> --account <locator> \
  --user <you> --authenticator externalbrowser   # init prints the exact command
cp apps/<slug>/.streamlit/secrets.toml.example apps/<slug>/.streamlit/secrets.toml

# 3. Add the Claude Code plugin (inside Claude Code)
/plugin marketplace add kyle-chalmers/streamsnow
/plugin install streamsnow@streamsnow

# 4. Build, preview, validate, ship
streamsnow new marketing campaign-dashboard   # or /new-app
streamlit run apps/marketing-campaign-dashboard/streamlit_app.py
#    /preview-app  ->  /validate-app  ->  /ship-app
```

## How it's organized

```
streamsnow/            the PyPI package — CLI, config, policy, scaffolder, tools
  ├── cli.py           configure / init / new / doctor / check
  ├── config.py        typed + validated streamsnow.config.yaml model
  ├── policy.py        schema allow/deny single source of truth
  ├── scaffolder.py    renders a governed repo from config
  ├── _templates/      the Jinja scaffold templates (repo/ + app/)
  └── tools/           governance checks (e.g. check_schema_refs)
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
- **[Distribution](docs/distribution.md)** — how StreamSnow ships (PyPI CLI +
  plugin) and why there's no separate copy-paste kit.

## License

[MIT](LICENSE) © Kyle Chalmers

> StreamSnow is an independent open-source project and is not affiliated with or
> endorsed by Snowflake Inc., Streamlit, or Anthropic.
