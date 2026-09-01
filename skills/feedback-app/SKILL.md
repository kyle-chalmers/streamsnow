---
name: feedback-app
description: Turn a user's feedback on a live app into applied fixes — classify each point, update the spec where scope changes, and land one commit per item. Use when the user says "here's feedback on <app>", "users said the numbers look wrong", "the filter is confusing", "polish this dashboard", or pastes review notes/screenshots about an existing app.
argument-hint: "<slug> <feedback>"
allowed-tools: [Bash, Read, Edit, Glob, Grep]
---

# /feedback-app

The user-driven counterpart to `/review-app`: instead of reviewers finding issues, the human brings
them. Free-form feedback (text and/or screenshots) becomes classified items, spec updates where
scope changes, and atomic per-item commits — so feedback doesn't rot in chat. Chatty by design:
every classification, plan, and commit gets visible confirmation, because the user is checking your
interpretation of *their* words in real time.

## Preflight

1. Confirm `apps/<slug>/streamlit_app.py` exists. If `REQUIREMENTS.md` is missing, offer to
   backfill it first (`/start-app --spec <slug>`, automatic backfill mode) so scope changes have a
   spec to land in — one confirmation, then proceed.
2. Confirm a clean working tree (`git status --porcelain apps/<slug>`); otherwise ask to
   stash/commit first — one commit per feedback item only works on a clean tree.

## Collect & classify

3. **Re-read the feedback carefully** and split it into distinct items (each paragraph or clause
   that names a different problem is its own item). "Make it better" is not actionable — ask for
   concrete observations. If feedback references a visual and a Playwright MCP is loaded, offer to
   capture the page (per [_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md))
   and confirm what the user is pointing at; skip silently without the MCP.
4. **Classify each item** per [classification.md](classification.md) — BUG / POLISH / UX /
   NEW-FEATURE / CROSS-CUTTING — and show the numbered table. **Lock the classification with the
   user before planning.** Misreading feedback is this skill's main failure mode.

## Plan & apply

5. **Plan the edits** per bucket (see classification.md): name files and fixes for BUG/POLISH,
   sketch before/after for UX, update the spec **first** for NEW-FEATURE (a whole new page →
   route to `/start-app <slug>` instead), list every affected file for CROSS-CUTTING. Show the full
   plan; confirm before touching code.
6. **Apply confirmed items in order, one commit each:** edit → lint (`ruff check` + `ruff format`
   on the app) → stage exactly the touched files → commit
   `<type>(<slug>): <summary> (feedback #N)`, quoting the user's words in the body. A pre-commit
   hook failure is a real finding — fix the cause and make a fresh commit; never `--no-verify`,
   never `--amend` after a hook failure.
7. **Log the session:** append one §11 Sessions line to `REQUIREMENTS.md`
   (`/feedback-app: applied N items (<summary>). Commits: <range>. Next: /preview-app <slug>`),
   committed separately as `docs(<slug>): record feedback session`.

## Follow-up review (default on)

8. If at least one code-touching commit landed, ask the gate what the edits amount to:
   `streamsnow review-gate classify <slug> --format json`. `.apps[0].needs_review == true` →
   OFFER `/review-app <slug> --auto` (feedback batches routinely seed several mechanical
   findings) and wait for a yes — the loop takes minutes and, with a connection present, spends
   Snowflake credits on lineage each cycle; never auto-run it (same stance as /ship-app). Declined
   → a single static diff-scoped `/review-app <slug>` pass. `.apps[0].verdict == "trivial"` or
   `.apps[0].reviewed` → no review is owed; skip the follow-up and say why. Skip also when
   `--no-followup-review` is passed, when only docs/captions changed, or when `/start-app` is
   orchestrating (it reviews at its own checkpoint). The gate decides *whether* review is owed —
   never re-derive substantive-vs-trivial by hand.

## Close out

9. Report: items applied per bucket, commits, files touched, follow-up review outcome, and next
   steps (`/preview-app` to verify, `/validate-app` before shipping, `/ship-app` to open the PR).
   List any CROSS-CUTTING conventions worth promoting to the repo's templates as a separate PR.

## Guardrails

- **Never expand scope silently.** A NEW-FEATURE item updates the spec before any code.
- **The user's words are the spec for BUG items** — quote them in the commit body so the reviewer
  sees the why.
- Feedback about wrong *numbers* usually needs evidence, not a guess: offer `/audit-lineage <slug>`
  before "fixing" a calculation the warehouse may be driving.

## Done when

Every confirmed item is an atomic commit (or an explicit deferral), §11 records the session, the
follow-up review ran or was deliberately skipped, and the user knows the next command.
