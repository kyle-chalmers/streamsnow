---
name: onboard
description: Get this machine, this repo and its Snowflake account ready for StreamSnow. Detects what is already done and does only what is missing, so it is safe to re-run to ask "am I set up?". Use when the user says "set me up", "onboard", "I just cloned this repo", "is my machine ready", "add StreamSnow to this repo", or "set up Snowflake for StreamSnow", and when /build-app finds the machine or repo isn't ready.
argument-hint: "(none: detects what this machine and repo need)"
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep, AskUserQuestion]
---

# /onboard

> **Repo overlay:** if `.streamsnow/overlays/onboard.md` exists in this repo, read it first: committed, repo-specific additions and overrides ([_shared/overlays.md](../_shared/overlays.md)). Outside Claude Code, also read [_shared/other-agents.md](../_shared/other-agents.md).

Gets this machine, this repo and its Snowflake account ready to build and ship apps.
Everything that needs no answers runs first, then one round of questions, then the build,
then the steps that wait on other people. Re-running re-checks and does only what is missing.
The trust rules at the top of [setup.md](setup.md) apply throughout.

## Narrate every step

- Open each stage with one line: what it does, and whether it will ask anything.
- Before each action, one line of what and why; after it, one line with the result. Actions
  that need no approval are narrated too.
- Every question says what it decides and what each answer changes. Ask with
  `AskUserQuestion`, batched.
- Close each stage with what got done and what is next. Plain words, no walls of text.

## Stage 1 · Detect and prepare (no questions except one install approval)

1. Native Windows outside WSL: stop and follow setup.md §0.
2. Detect: `streamsnow doctor --format json` (no CLI: setup.md §0b first), whether
   `streamsnow.config.yaml` exists, whether `apps/*/streamlit_app.py` exists, whether
   `.claude/settings.json` enables `streamsnow@streamsnow`, and the Snowflake access
   inventory (setup.md §2a). Report the state in one line.
3. With any Snowflake access: the read-only investigation (setup.md §2b), including the
   admin check (setup.md §2d step 1).
4. One batched approval for every missing tool, then install one at a time and re-run
   doctor after each (setup.md §0b and §1). If `snow` was just installed or fixed, redo step 3.
5. The project Python environment (setup.md §1c) and the browser check (setup.md §1b).
6. Add the plugin to the repo's Claude settings when missing (setup.md §2e).

Nothing in Stage 1 writes `streamsnow.config.yaml` or the governed repo files.

## Stage 2 · One round of questions

Only what Stage 1 could not settle:
- no Snowflake connection: set it up for them, or guide them (setup.md §2a); then redo Stage 1 step 3;
- the five wizard answers marked found or needs you, and the defaults it does not ask (setup.md §2c);
- git name and email, only if missing;
- apps but no config: confirm the adopt plan ([adopt.md](adopt.md));
- admin setup not confirmed: who runs it, the user or someone else (setup.md §2d).

## Stage 3 · Build (after the user confirms)

- No config, no apps: `streamsnow configure` with the confirmed flags, then
  `streamsnow init --no-starter-app` (setup.md §2c).
- Apps, no config: the adopt flow ([adopt.md](adopt.md)).
- Config present and doctor's `repo-files` lists missing files: `init --no-starter-app` writes only those.
- A non-default connection: choose how local preview reads it (setup.md §3).
- Then `pre-commit install`, and doctor until every required row passes. Show the tree and the result.

## Stage 4 · Finish what waits on others

1. Snowflake admin setup when not confirmed (setup.md §2d): `streamsnow ci-key create`, the
   admin file in `.internal/`, the hand-off. Never run the admin SQL.
2. Once the admin objects are confirmed: `streamsnow ci-key push` (setup.md §2e).
3. Offer to commit the new repo files and `.claude/settings.json`.

## Done when

Name the end state:
- **Ready to build and preview:** Stages 1 to 3 pass. Next: `/build-app`.
- **Ready to deploy:** Stage 4 passes too.

With only the first, name what is pending ("waiting on your Snowflake admin") and say that
re-running `/onboard` re-checks.

## Out of scope

Building an app: `/build-app`. Porting one: `/migrate-app`. Deploying: `/ship-app`.
