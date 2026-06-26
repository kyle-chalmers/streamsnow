---
name: ship-app
description: Stage, commit, push, and open a PR for a StreamSnow app, gated on a passing validation. Use when the user says "ship it", "ship this app", "open a PR", "deploy <slug>", or follows /preview-app and wants to publish. Runs /validate-app as a hard gate first and watches PR checks.
---

# ship-app

Take a built app from working tree to open PR, gated on validation, then watch CI to a terminal state.

## Steps

1. Resolve the `<slug>` (ask if absent). Confirm `apps/<slug>/` has the working-tree changes to ship; `git status --short apps/<slug>` to see them.
2. **Hard gate:** run `/validate-app <slug>` (which runs `streamsnow validate-app <slug>`). On any FAIL, stop — report the failures and do not stage, commit, or push. Only continue on PASS.
3. Branch off `main` if on `main`: `git switch -c ship/<slug>-<short-desc>`. Never reuse an already-merged branch.
4. Stage only the app's changes: `git add apps/<slug>`. Include sibling files (config, docs) only if they're part of this change.
5. Commit with a conventional message, e.g. `feat(<slug>): <summary>`.
6. Push: `git push -u origin HEAD`.
7. Open the PR with `gh pr create --fill` (or a written title/body summarizing SQL added/changed and caching choices). Capture the PR number/URL.
8. Note to the user: merge to `main` triggers CI, which deploys the app to Snowflake (one-time DDL via `streamsnow deploy-setup` must already be applied — see /onboard). No local `snow streamlit deploy`.
9. Watch PR checks to a terminal state: `gh pr checks <num> --watch` plus `gh pr view <num> --json state`. Run the watch loop in the background and report once on exit.
10. Report: a check fails → name the failed check and stop. Checks pass but unmerged → PR needs a teammate approval (you can't approve your own). Merged → confirm and report the deploy run outcome.

## Done when

The PR is open, validation passed, and PR checks have reached a terminal state with the outcome (failed-check name, awaiting-approval, or merged + deploy result) reported to the user.
