# `--auto` — loop review → fix until findings converge

The unattended path: the same review → bucket → fix → re-review cycle a person runs by hand, cycling
until the app stops producing mechanical findings, then a render smoke. Costs several minutes — and
some Snowflake credits when the lineage pass joins — so say so up front and offer `--no-lineage`
to stay static-only.

**Reach for the loop** after a big change that likely seeded several mechanical findings, once the
app is already past a clean `streamsnow validate-app`. **Stay manual** (default review + `--fix`)
when you expect mostly judgment calls — the loop never auto-applies those — or when the user wants
to inspect each finding before a commit lands.

## Steps

1. Resolve the slug; warn about duration (and credits if live lineage will run).
2. **Check the working tree** — the loop commits one fix at a time, and a dirty tree muddies
   attribution. Offer stash/commit first.
3. **Detect the connection context** (`snow connection list`, or the `snowflake.*` blocks in
   config). Present → `/audit-lineage` joins each cycle (bounded read-only live-DB checks). Absent
   or `--no-lineage` → static-only; say so once and continue — no connection is not a failure.
4. **Cycle:** run the review pass (and the lineage pass, in parallel Task subagents when in scope).
   Merge and dedupe both reports.
5. **No mechanical (Bucket A) findings this cycle → exit to step 7.** Otherwise apply them per
   [fixes.md](fixes.md) — one atomic commit per finding — collect every judgment/informational item
   into a running deduped punch list, and return to step 4.
6. **Stop early on no-convergence:** the same finding reappearing after its "fix" means the recipe
   is wrong for this case — stop, don't re-apply, hand that finding to the user.
7. **Final smoke:** confirm the app still renders — Playwright walkthrough across all pages when the
   MCP is loaded ([_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md)),
   otherwise `/preview-app` and a manual click-through. A render/console error becomes a new finding
   in the report, not something to silently patch.
8. **Report:** cycles run, commits (with SHAs), the punch list, the smoke outcome, and the hand-off —
   /validate-app then /ship-app.

## Exit conditions

- **Clean** — a full cycle yields zero mechanical findings. The healthy outcome.
- **Plateau** — only judgment items remain; hand over the punch list.
- **No convergence** — a finding survives its own fix; stop and escalate (see step 6).

## Notes

- **The loop is not the gate.** It polishes; only `streamsnow validate-app` passes/fails a ship —
  always finish with it, since a mechanical fix can't see everything the gate can.
- **Dedup keys on citation + summary.** A fix that shifts line numbers can make an old finding look
  "new" — treat a repeat as no-convergence, not fresh work.
- **Plateaus at cycle 1 with no commits** — everything was judgment; there's nothing to loop. Walk
  the punch list with `--fix` interactively.
- **Loop feels slow** — the Playwright walkthrough is the long pole; drop to a manual smoke and/or
  pass `--no-lineage`.
- Cross-agent reviewers ride inside the review/lineage passes per their own config; the loop
  inherits, never configures them.
