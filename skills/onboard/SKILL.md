---
name: onboard
description: First-time machine setup for a StreamSnow repo — runs `streamsnow doctor`, walks the user through installing any missing prereqs (Python 3.11+, uv, git, Snowflake CLI, streamlit), then hands off to Snowflake configuration. Use when the user says "set me up", "onboard", "onboard me", "first time setup", or when prereq detection finds something missing during normal work.
---

# Onboard

Get a fresh machine ready to build and preview StreamSnow apps locally.

## Steps

1. Run `streamsnow doctor` and read the per-check status. This is the source of truth for what's missing — do not re-derive prereqs by hand.
2. For each FAIL or missing tool, propose the platform-appropriate install command and run it only after the user confirms:
   - **Python 3.11+** — point at the user's preferred installer (pyenv, official installer, system package manager).
   - **uv** — `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `pip install uv`).
   - **git** — system package manager; confirm `git config user.name` / `user.email` are set.
   - **Snowflake CLI (`snow`)** — `uv tool install snowflake-cli` (or `pipx install snowflake-cli-labs`).
   - **streamlit** — installed per-app via `uv`; flag only if `doctor` reports it absent globally.
3. Re-run `streamsnow doctor` until every check passes. Surface any check that stays red with the exact remediation, don't paper over it.
4. (Optional) Install git hooks so commits get gated locally: `pre-commit install`.
5. Hand off to Snowflake setup — tell the user to run `streamsnow configure` to write `streamsnow.config.yaml` (account, role, warehouse, auth). Don't author that file by hand.
6. Point at the next step: scaffold an app with `/new-app`, then `/preview-app` to run it against live Snowflake.

## Done when

`streamsnow doctor` reports all checks passing and the user knows to run `streamsnow configure` next.
