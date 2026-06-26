# Contributing to StreamSnow

Thanks for your interest! StreamSnow is in early, active development.

## Ground rules

- **One implementation, many consumers.** Validation/scaffolding logic lives in
  the `streamsnow` Python package. The CLI, the Claude Code plugin, pre-commit,
  and CI all call that same code — never fork logic into a skill or a workflow.
- **Tools are CLI-first and structured.** Each tool is a small program with
  `--format=md|json` output and meaningful exit codes (`0` pass, `1` finding,
  `2` tool error). Skills shell out to them; they never embed prompt text.
- **No org-specific values in committed code.** Anything Snowflake-, company-,
  or brand-specific belongs in `streamsnow.config.yaml`, not hardcoded.
- **Secrets never go in the repo.** Not in config, not in tests, not in docs.

## Dev setup

```bash
git clone https://github.com/kyle-chalmers/streamsnow.git
cd streamsnow
uv venv --python 3.11
uv pip install -e ".[dev]"
pre-commit install        # once available
```

## Before you open a PR

```bash
ruff check . && ruff format --check .
pytest
```

CI runs the same checks. Keep changes focused; describe what tool/skill/template
you touched and why.

## Reporting issues

Open a GitHub issue with the StreamSnow version (`streamsnow --version`), your
runtime/deploy-source config (redact secrets), and steps to reproduce.
