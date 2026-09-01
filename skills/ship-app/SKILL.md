---
name: ship-app
description: Stage, commit, push, and open a PR for one app, gated on a passing validation, then watch CI to a terminal state. Use when the user says "ship it", "open a PR", "deploy <slug>", or after preview and review look good.
argument-hint: "<slug>"
allowed-tools: [Bash, Read]
disable-model-invocation: true
---

# /ship-app

Take a built app from working tree to open PR, gated on validation, then watch CI. Scope is
intentionally narrow: **one app at a time** (`apps/<slug>/`), and deployment is **CI-only on merge
to `main`** — this skill never runs a local Snowflake deploy. Repo-level changes (templates,
governance files, shared recipes, CI) do not belong in a `/ship-app` PR; commit those separately.

## Steps

1. **Resolve the slug**; `git status --short apps/<slug>` must show changes to ship — zero changes
   ahead of `main` → nothing to PR; stop and say so.
2. **Preflight 0 — review gate (asks, never blocks):**
   `streamsnow review-gate classify <slug> --format json`. Reviewed/trivial/skip-marker → proceed.
   `.apps[0].needs_review == true` → offer the choice: review first (`/review-app <slug> --auto`) or **ship
   as-is** — always available (ships can be time-critical; /validate-app + CI are the real publish
   gates), but note "shipped unreviewed" in the PR body so the approver sees it. Never auto-run a
   credit-spending review loop on the user's behalf here.
3. **Hard gate:** run /validate-app. Any FAIL → stop; report and do not stage, commit, or push.
   /validate-app is the fix-it path — don't auto-fix here.
4. **Branch hygiene.** On `main` → `git switch -c ship/<slug>-<desc>` first. **Never reuse a
   squash-merged branch** — Git's three-way merge can silently revert your own deletions. Check:
   `gh pr list --search "head:$(git branch --show-current) is:merged" --json number` — non-empty
   means the branch is spent; start fresh off `main` and re-apply (cherry-pick or copy edits, never
   `git merge` from the old branch).
5. **Stage only the app:** `git add apps/<slug>` (plus the repo README only if its app-index row
   changed). Show `git diff --cached --stat`; unstage anything else — don't widen scope to "fix one
   more thing".
6. **Commit** conventionally (`feat(<slug>): <summary>`), the summary matching the diff —
   underclaim, never overclaim. Pre-commit hooks block → **stop and surface the error**; never
   `--no-verify` (the hooks run the same checks CI does).
7. **Sync with `origin/main` before pushing** per
   [_shared/sync-with-main.md](../_shared/sync-with-main.md): rebase (never merge), then
   `git push --force-with-lease`. A rebase conflict stops with manual instructions — don't guess a
   resolution.
8. **Push** (`git push -u origin HEAD` if the sync didn't already) — refuse to push to `main`.
9. **Open the PR** (title/body: what changed, validation passed). Print the number and URL.
10. **Note the deploy path:** merging to `main` triggers CI, which deploys — no local deploy step.
11. **Watch checks to a terminal state** (`gh pr checks <num> --watch` in the background;
    `gh pr view <num> --json state,mergeStateStatus`) and report once on exit.

## Reporting the outcome

- **A check fails** → name it; translate deploy failures via
  [_shared/deploy-error-translator.md](../_shared/deploy-error-translator.md) (failure signatures
  differ by runtime — see [_shared/runtime-decision.md](../_shared/runtime-decision.md)); stop.
- **Green but unmerged** → it's waiting on a teammate's approval (you can't approve your own PR) —
  say so plainly rather than looping on the checks.
- **Merged** → confirm, then report the deploy run's outcome.

## Gotchas

- **Squash-merged branch reuse is the highest-severity trap** — it fails silently: CI passes, the
  deploy ships the wrong code. The step 4 check is non-negotiable.
- **Commit message must match the diff.** A claimed change with no matching hunk means a fix was
  lost (often in manual conflict resolution) — re-read the diff and correct one or the other before
  opening the PR.
- Most deploy-run failures resolve to one-time, admin-applied DDL (a grant, a compute pool, an
  external-access integration) emitted by `streamsnow deploy-setup` — surface the named fix; never
  run DDL from here.

## Troubleshooting

- **Push rejected (stale `--force-with-lease`)** → re-fetch, rebase, push — never plain `--force`.
- **PR opens "behind"** → `main` moved; re-run the sync step and let checks re-run.

## Done when

The PR is open, validation passed before staging, the branch is rebased on current `origin/main`,
and checks reached a terminal state with the outcome reported: a named failed check, "awaiting
approval," or merged + the deploy result.

## System-evolution retro (always, even on a clean ship)

One question before closing: did anything go wrong or get re-done this ship? If so, **which
layer was insufficient** — the config (`streamsnow.config.yaml` / governance rules), a skill,
a check (`streamsnow validate-app` / CI), or the deploy path? Propose the concrete fix to
*that* artifact. If the gap is in StreamSnow itself, file it against the plugin repo (issue or
a note the user can act on) rather than patching around it locally. Fixing the layer, not the
instance, is what compounds. (Ported from ticketwright's /ship Phase C.)
