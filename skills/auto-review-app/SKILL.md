---
name: auto-review-app
description: Self-closing review→fix loop for a StreamSnow app — repeatedly runs /review-app (plus /deep-dive-data when a Snowflake connection is configured) and applies the mechanical fixes via /apply-review until no new auto-fixable findings remain, then a final preview smoke. Use when the user says "loop the review", "auto-fix until clean", "self-clean the app", or "auto-review-app".
---

# auto-review-app

Loop the qualitative review and mechanical-fix skills until the app stops producing auto-fixable findings, then confirm with a smoke pass. Costs many minutes and (when deep-dive-data runs) some Snowflake credits — say so before a long run.

These review skills are **qualitative judgment, never a gate** — they do not block. The deterministic PASS/FAIL gate is `streamsnow validate-app <slug>` via /validate-app, which this loop does not replace.

## Steps

1. Resolve the slug (ask if omitted); verify `apps/<slug>/` exists. Warn this may take several minutes and use credits, then proceed.
2. Detect a configured Snowflake connection (`snow connection list` / `streamsnow.config.yaml`). Present → /deep-dive-data is in the loop; absent → skip it and note the loop is static-only.
3. Begin a cycle. Run /review-app, and /deep-dive-data when step 2 found a connection, in parallel (Task subagents). Live-DB queries are READ-ONLY and bounded — never write SQL or unbounded scans.
4. Merge both reports. If neither produced an auto-fixable (Bucket A / mechanical) finding, exit the loop to step 6.
5. Run /apply-review to commit the Bucket A auto-fixes atomically. Collect judgment-required and NICE-TO-HAVE items into a running punch list, then return to step 3 for the next cycle. Stop early if a cycle re-surfaces the same finding (no convergence) and hand it to the user.
6. Final smoke: if a Playwright walkthrough is available, follow _shared/playwright-walkthrough.md across all pages; otherwise run /preview-app for a manual smoke. Capture any render error as a new finding.
7. Report: cycles run, fixes committed, the single deduped punch list of judgment items, and the smoke outcome.

## Notes

- This loop does NOT run /validate-app or open a PR — finish with /validate-app then /ship-app.
- Cross-agent reviewers (agy/codex) ride inside /review-app and /deep-dive-data; they are optional and off by default — this loop inherits whatever those skills decide.

## Done when

A full cycle yields no new auto-fixable findings, the smoke pass is clean, and the user has the punch list of remaining judgment calls.
