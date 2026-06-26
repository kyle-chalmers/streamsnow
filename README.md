<!-- markdownlint-disable MD041 -->
<h1 align="center">StreamSnow ❄️</h1>

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

> **Status: early / under active construction.** The plan and architecture are
> set; the CLI and Claude Code plugin are being built out phase by phase. APIs
> and layout may move until the first tagged release.

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
# 1. Scaffold a governed monorepo + run the setup wizard
uvx streamsnow init

# 2. Add the Claude Code plugin
#    (inside Claude Code)
/plugin marketplace add kyle-chalmers/streamsnow
/plugin install streamsnow@streamsnow

# 3. Build, preview, validate, ship
streamsnow new marketing campaign-dashboard   # or /new-app
#    /preview-app  ->  /validate-app  ->  /ship-app
```

## How it's organized

```
.claude-plugin/   Claude Code plugin manifest + marketplace
skills/           model-invocable skills (the playbook)
agents/           reviewer subagents
hooks/            Claude Code hooks (load governance, gates)
streamsnow/       the PyPI package — CLI + config + policy + the validation tools
templates/        Copier template for scaffolding new apps (container + warehouse)
scaffold/         files `init` writes into a new user repo (CI, pre-commit, AGENTS.md, branding)
docs/             setup + usage guides
examples/         a runnable reference app
```

The `streamsnow` Python package is the **single source of truth** for tool
logic: the CLI, the Claude Code plugin, pre-commit, and CI all call the same
code — one implementation, many consumers.

## License

[MIT](LICENSE) © Kyle Chalmers

> StreamSnow is an independent open-source project and is not affiliated with or
> endorsed by Snowflake Inc., Streamlit, or Anthropic.
