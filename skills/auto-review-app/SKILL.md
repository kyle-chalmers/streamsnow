---
name: auto-review-app
description: Self-closing review→fix loop for a StreamSnow app — repeatedly runs /review-app (plus /deep-dive-data when a Snowflake connection is configured) and applies the mechanical fixes via /apply-review until no new auto-fixable findings remain, then a final preview smoke. Use when the user says "loop the review", "auto-fix until clean", "self-clean the app", or "auto-review-app".
---

# auto-review-app

Loop the qualitative review and mechanical-fix skills until the app stops producing auto-fixable findings, then confirm with a smoke pass. This is the unattended path: it inlines the same review → classify → fix → re-review cycle a person would run by hand with /review-app and /apply-review, but keeps cycling on its own until findings converge.

Costs many minutes and (when /deep-dive-data runs) some Snowflake credits — say so before a long run, and offer `--no-deep-dive` to stay static-only.

## When to reach for this vs. the manual path

- **Use this loop** when the app is already past a clean `streamsnow validate-app` and you want it polished without babysitting each cycle — or after a big change that likely seeded several mechanical findings.
- **Use /review-app + /apply-review by hand** when you expect mostly judgment calls (this loop never auto-applies those), or when you want to inspect each finding before any commit lands.
- **Do not** use this in place of the gate. These review skills are **qualitative judgment, never a gate** — they never block. The deterministic PASS/FAIL gate is `streamsnow validate-app <slug>` via /validate-app, which this loop neither runs nor replaces.

## What gets auto-applied vs. deferred

/apply-review classifies every finding into buckets. This loop only acts on the mechanical ones:

- **Auto-fixable (Bucket A / mechanical)** — deterministic edits with one correct form (missing `@st.cache_data(ttl=...)`, a denied-schema reference, a write/dynamic-SQL pattern, a `:N IS NULL OR` bind-predicate trap). The loop applies these and re-reviews.
- **Judgment-required + NICE-TO-HAVE** — anything needing a human decision (restructuring a query, UX trade-offs, naming). The loop **never** auto-applies these; it collects them into a single end-of-run punch list.

## Steps

1. **Resolve the slug.** If omitted, ask, or infer from the current `apps/<slug>/` if you are inside one. Verify `apps/<slug>/` exists. Warn the run may take several minutes (and credits if /deep-dive-data is in scope), then proceed.
2. **Check the working tree.** If `apps/<slug>/` has uncommitted changes, surface them first — the loop commits one fix at a time and a dirty tree muddies attribution. Offer to let the user stash or commit before starting.
3. **Detect the connection context.** Look for a configured Snowflake connection (`snow connection list`, or the `snowflake.objects` / `snowflake.roles` block in `streamsnow.config.yaml`). Present → /deep-dive-data joins the loop (live-DB lineage against the app's queried objects). Absent → skip it and tell the user the loop is static-only.
4. **Begin a cycle.** Run /review-app, and /deep-dive-data when step 3 found a connection, in parallel (Task subagents). Live-DB queries are READ-ONLY and bounded — never write SQL, never run an unbounded scan. /review-app stays DB-free.
5. **Merge the reports.** Combine both into one finding list, deduped. If neither produced an auto-fixable (Bucket A) finding this cycle, exit the loop and go to step 7.
6. **Apply and re-review.** Run /apply-review to commit the Bucket A auto-fixes — one atomic commit per finding. Add every judgment-required / NICE-TO-HAVE item to a running, deduped punch list, then return to step 4 for the next cycle. **Stop early** if a cycle re-surfaces a finding it already "fixed" (no convergence) and hand that finding to the user rather than looping on it.
7. **Final smoke.** Confirm the app still renders. If a Playwright walkthrough is available, follow `_shared/playwright-walkthrough.md` across all pages; otherwise run /preview-app and have the user click through. Capture any render error or console error as a new finding.
8. **Report.** Cycles run, fixes committed (with shas), the single deduped punch list of judgment items, and the smoke outcome. End with the hand-off: /validate-app then /ship-app.

## Runtime note (container vs. warehouse)

The review dimensions are the same regardless of how the app runs in Snowflake, but two findings classes care about runtime:

- **Caching** (`streamsnow check caching` / @st.cache_data) matters most on a **warehouse runtime**, where every uncached query reads bill credits — the loop will keep auto-applying TTL caching there.
- A **container runtime** changes compute economics but not the security/schema/bind-predicate rules. Don't suppress caching findings just because the app runs in a container; the check is still the right default.

Both runtimes come from `snowflake.objects`/`snowflake.roles` in `streamsnow.config.yaml` — refer to them generically; never hardcode a warehouse, role, or database name into a fix.

## Convergence and exit conditions

The loop ends on whichever comes first:

- **Clean** — a full cycle produces zero auto-fixable findings. The healthy outcome.
- **Plateau** — only judgment-required / NICE-TO-HAVE items remain (nothing mechanical left to apply). Hand the punch list to the user.
- **No convergence** — the same finding keeps reappearing after a "fix." Stop, do not keep cycling, and surface that finding for human eyes (the fix recipe is likely wrong for this case, or the finding is really a Bucket B in disguise).

## Gotchas and edge cases

- **A fix that breaks `streamsnow validate-app`.** The loop optimizes qualitative findings; it does not run the gate. If a mechanical fix were to violate governance (e.g. touch a denied schema), /apply-review's recipes refuse it upstream — but always finish with /validate-app to catch any regression the review layer cannot see.
- **Denied-schema references must stay green.** Schemas come from `governance.schema_allow` / `governance.schema_deny` (and `governance.database`) in `streamsnow.config.yaml`. No fix should introduce a reference to a denied schema; if `streamsnow check schema-refs` would fail, the fix is wrong.
- **Deep-dive credits.** Each cycle that includes /deep-dive-data spends some credits on bounded read-only queries. On a long loop this adds up — say so, and offer `--no-deep-dive` (static-only) for offline or cost-sensitive runs.
- **Cross-agent reviewers** (optional external CLIs) ride *inside* /review-app and /deep-dive-data, off by default. This loop inherits whatever those skills decide — it does not configure them itself. See `_shared/cross-agent-review.md`.
- **Smoke ≠ gate.** A clean smoke confirms the app renders; it is not a PASS. Only `streamsnow validate-app <slug>` is.

## Troubleshooting

- **Plateaus at cycle 1 with no commits** — every finding was judgment-required. There is nothing to auto-fix; walk the punch list with /apply-review interactively.
- **Same finding every cycle** — dedup keys on the citation + summary; if a fix shifts line numbers the citation may drift and look "new." Treat a repeat as no-convergence (above), stop, and hand it to the user rather than re-applying.
- **Smoke shows a render error** — record it as a new finding and report it; the loop has done its job by catching it. Do not silently patch render logic outside the review recipes.
- **Loop feels slow** — the Playwright walkthrough is the long pole. Drop the final walk to /preview-app (manual smoke), and/or pass `--no-deep-dive` to drop the live-DB branch.

## Done when

A full cycle yields no new auto-fixable findings, the smoke pass is clean, and the user has the single deduped punch list of remaining judgment calls — plus the explicit next steps: /validate-app then /ship-app.

## References

- /review-app — the five qualitative reviewer dimensions (SQL, data, UI, runtime, docs); source of truth for the per-dimension prompts each cycle runs.
- /deep-dive-data — live-DB lineage and column-fidelity check; the Snowflake-touching branch of the loop.
- /apply-review — Bucket A/B/C classification and the mechanical fix recipes this loop applies.
- /validate-app — the deterministic PASS/FAIL gate; run after the loop, before shipping.
- /preview-app — manual smoke when Playwright is unavailable.
- /ship-app — open the PR once the gate passes.
- `_shared/playwright-walkthrough.md` — the per-page walkthrough recipe for the final smoke.
- `_shared/cross-agent-review.md` — optional external-reviewer detection and merge contract (inherited from the review skills).
