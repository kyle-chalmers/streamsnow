## What and why

<!-- One or two sentences. Link the issue this closes. Feature PRs need an issue labeled status:triaged or ready-for-agent first. -->

Closes #

## Area

<!-- e.g. a check, the scaffolder, a skill, a hook, docs -->

## Checks run locally

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run pytest -q -rs`
- [ ] `uv run python -m streamsnow.tools.check_export_clean .`
- [ ] Added or updated fixture tests (fictional Acme domain, no network)
- [ ] Added a line under `## [Unreleased]` in CHANGELOG.md

## Stable surface

- [ ] This PR does **not** rename or remove a command, flag, config key, check result, skill name or generated file ([docs/versioning.md](../docs/versioning.md)).
      If it does, explain the migration path here and expect the `breaking-change` label (from 1.0.0 it also needs a deprecation period).

## AI assistance

<!-- AI-assisted contributions are welcome when disclosed (CONTRIBUTING.md, "AI-assisted contributions"). -->

- [ ] No AI tools were used, **or** they were: <!-- tool and what it did -->
- [ ] I have read every line of this diff and can explain it, and I ran it.
