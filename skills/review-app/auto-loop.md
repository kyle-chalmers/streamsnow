# `--auto` — loop review → fix until findings converge

The unattended path: the same review → bucket → fix → re-review cycle a person runs by hand, cycling
until the app stops producing mechanical findings, then a render smoke. Costs several minutes — and
some Snowflake credits when the lineage pass joins — so say so up front and offer `--no-lineage`
to stay static-only.

**Reach for the loop** after a big change that likely seeded several mechanical findings, once the
app is already past a clean `streamsnow validate-app`. **Stay manual** (default review + `--fix`)
when you expect mostly judgment calls — the loop never auto-applies those — or when the user wants
to inspect each finding before a commit lands.

The loop's deterministic pieces are CLI verbs, not prose — prose loops drift, and a loop that
re-derives its dedup each cycle re-reports findings it already resolved:

| Step | Verb |
|---|---|
| Parse a report into findings | `streamsnow review-loop parse-findings <report.md>` |
| Filter against prior Resolutions (+ repeat detection) | `streamsnow review-loop dedup-findings apps/<slug>/.review --new <report.md> --with-repeats` |
| Record what happened to each finding | `streamsnow review-loop write-resolutions <report.md> --applied … --deferred-b … --bucket-c …` |
| Decide continue/stop and why | `streamsnow review-loop exit-condition --iter N --max-iter 5 --applied N --block N --flag N [walk flags]` |
| Merge cross-agent reports | `streamsnow review-loop merge-findings --inputs claude:<a>,<agent>:<b>` |
| Mark the tree state reviewed | `streamsnow review-gate stamp <report.md> --slug <slug>` |

## Steps

1. Resolve the slug; warn about duration (and credits if live lineage will run).
2. **Check the working tree** — the loop commits one fix at a time, and a dirty tree muddies
   attribution. Offer stash/commit first.
3. **Detect the connection context** (`snow connection list`, or the `snowflake.*` blocks in
   config). Present → `/audit-lineage` joins each cycle (bounded read-only live-DB checks). Absent
   or `--no-lineage` → static-only; say so once and continue — no connection is not a failure.
4. **Cycle:** run the review pass (and the lineage pass, in parallel Task subagents when in scope),
   writing `apps/<slug>/.review/review-<ts>.md`. Merge multi-reviewer output with `merge-findings`;
   filter re-reports with `dedup-findings --with-repeats`. **A non-empty `repeats_of_applied` is
   no-convergence** — the same finding returned after its own applied fix, meaning the recipe is
   wrong for this case: stop the loop, don't re-apply, hand that finding to the user. (Plain dedup
   alone would hide the repeat — it filters everything previously resolved.)
5. **Apply Bucket A findings** (from `kept`) per [fixes.md](fixes.md) — one atomic commit per
   finding — then `write-resolutions` so the next cycle's dedup sees them; collect
   judgment/informational items into the running punch list.
6. **Ask `exit-condition`** with this cycle's counts (no walk flags yet); `continue` → back to
   step 4. Any terminal verdict except `clean` → skip to step 8's stamp decision and report.
7. **A pre-walk `clean` earns the smoke, and the walk gets the LAST word:** run the browser
   walkthrough across all pages when the tooling is loaded
   ([_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md)), otherwise
   `/preview-app` and a manual click-through. The walk is a **finding source, not confirmation** —
   re-run `exit-condition` WITH the `--walk-*` flags from its report: `walk-reentry` loops back to
   step 4 (bounded by `--max-walk-reentries` plus the already-attempted set — a flapping page
   cannot ping-pong); a walk that cannot be trusted (missing browser, un-seeded auth) is
   **DEGRADED and terminal**: zero findings, never a re-entry, and the UI reads UNVERIFIED.
8. **Stamp LAST — and only on a genuinely reviewed tree:** after the final fix commit,
   `streamsnow review-gate stamp apps/<slug>/.review/review-<ts>.md --slug <slug>` (stamping
   earlier records a tree state the commits immediately invalidate, and the gate would nag after
   every successful run). Skip the stamp on a no-convergence stop — an unresolved repeat is not a
   reviewed state.
9. **Report:** cycles run, commits (with SHAs), the punch list, the smoke outcome, the exit reason,
   and the hand-off — /validate-app then /ship-app.

## Exit conditions (from `exit-condition`, in priority order)

- **max-iterations** — the ceiling hit while work remained; say so, never report clean.
- **walk-degraded** — the UI is UNVERIFIED; terminal, hand over as-is.
- **clean** — a full cycle yields zero mechanical findings and the walk is clean.
- **walk-reentry** — the walk found mechanically-fixable defects; loop again (bounded).
- **plateau** — only judgment items remain; hand over the punch list.

## Notes

- **The loop is not the gate.** It polishes; only `streamsnow validate-app` passes/fails a ship —
  always finish with it, since a mechanical fix can't see everything the gate can.
- **Plateaus at cycle 1 with no commits** — everything was judgment; there's nothing to loop. Walk
  the punch list with `--fix` interactively.
- **Loop feels slow** — the browser walkthrough is the long pole; drop to a manual smoke and/or
  pass `--no-lineage`.
- Cross-agent reviewers ride inside the review/lineage passes per their own config; the loop
  inherits, never configures them.
