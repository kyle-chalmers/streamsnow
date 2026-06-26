---
name: start-app
description: End-to-end playbook for building a StreamSnow app from spec to PR — walks the human through refine, scaffold, build, preview, validate, review, and ship with checkpoints. Use when the user says "build a full app end to end", "do the whole pipeline", "ship a new dashboard start to finish", or "run the whole chain".
---

# /start-app

One playbook that walks a new StreamSnow app from requirements through a shipped PR, pausing at three checkpoints for human sign-off.

> Skills can't invoke each other programmatically. This is a human-walked chain: at each step you tell the user the next skill to run, wait for them to run it, then continue.

## Steps

1. **Prereqs.** Run `streamsnow doctor`; if anything fails, fix it (or run `streamsnow configure` for Snowflake env) before proceeding.
2. **Requirements.** Tell the user to run `/refine-requirements` (Jira `DI-XXXX`, `--new`, or existing slug). Confirm `apps/<slug>/REQUIREMENTS.md` exists with §4 Pages and §11 Build Progress.
3. **Scaffold.** Run `streamsnow new <domain> <function>` to create `apps/<slug>/`. Derive `<domain>`/`<function>` from the spec.
4. **Build pages.** For each page in §4, tell the user to run `/add-page <slug> <page>`. Fill in the generated query/chart/KPI stubs against the spec.
5. **CHECKPOINT 1 — post-scaffold.** Suggest the user run `/review-app <slug>` and `/apply-review <slug>` for an early structural pass. Wait for confirmation before continuing.
6. **Preview.** Run `streamsnow preview <slug> [--port N]` (or `/preview-app <slug>`) against live Snowflake.
7. **CHECKPOINT 2 — browser verify.** User opens the local URL and confirms each page renders, charts populate, and filters work. Block until they confirm.
8. **Validate gate.** Run `streamsnow validate-app <slug>` (or `/validate-app <slug>`). Any FAIL aborts — fix and re-run. Use `streamsnow check schema-refs|security|caching|bind-predicates <paths>` to isolate a single failing gate.
9. **Final review.** Tell the user to run `/auto-review-app <slug>` (or `/review-app` + `/apply-review`) until findings are clean.
10. **CHECKPOINT 3 — ship-readiness.** Confirm validate passes, review is clean, and the user is ready. Then hand off to `/ship-app <slug>` (commit, push, PR). First-time apps may need a one-time `streamsnow deploy-setup` (may be a stub — note it to the user).
11. **Resume.** §11 Build Progress is the resume contract — re-read it to pick up an interrupted run at the last completed step.

**Done when** `/ship-app` has opened the PR, `streamsnow validate-app <slug>` passes, and §11 reflects the shipped state.
