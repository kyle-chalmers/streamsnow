# The report format and the review stamp

Three tools read the review report: `streamsnow review-gate stamp` marks it reviewed, `--fix` and
the `--auto` loop parse its findings, and `/ship-app` counts what it left open. They all depend on
the same shape, so write it exactly.

## Report headings

```markdown
# Review: <slug>

Slug, timestamp, runtime, scope, and the top-3 summary go here, above the first `##`.

## SQL

### BLOCK
- [apps/<slug>/queries/revenue_daily.sql:12] SELECT * over a wide view -- name the columns.

### FLAG
- _none_

### NICE-TO-HAVE
- _none_

## UI
...
```

- One `## <Dimension>` section per reviewer (SQL, Data, UI, Runtime, Docs), and in each, all three
  severity headings: `### BLOCK` (critical), `### FLAG` (should-fix), `### NICE-TO-HAVE`.
- One finding per bullet: `- [file:line] <summary> -- <why>`. The parser also accepts an em dash
  as the separator. An empty bucket is `- _none_`.
- Keep the summary free of `### BLOCK`, `### FLAG` and `### NICE-TO-HAVE` headings, or its items
  count twice.
- Don't bold or rename the severity headings. A report the parser can't read gives
  `parsed: false` in `streamsnow review-loop open-findings`, and `/ship-app` then writes
  "Open critical: unknown" in the PR body.

## Stamping after a default pass

`streamsnow review-gate classify` only treats a change as reviewed once a report is stamped.
Before the default pass stamped, a full five-reviewer review still read `needs_review: true` and
`/ship-app` offered a review that had already run.

1. **Step 2:** run `streamsnow review-gate baseline <slug>` and keep the digest. It identifies the
   tree the reviewers are about to read.
2. **Step 8b, after the report is written:**
   `streamsnow review-gate stamp apps/<slug>/.review/review-<ts>.md --slug <slug> --expect-baseline <digest>`.
3. **Stamp even with critical findings open.** Reviewed means reviewed, not clean. `/ship-app`
   writes the open critical count into the PR body, so the approver still sees it.
4. **Exit 2, "app changed since dispatch":** the app changed while the reviewers ran (an edit, or
   a commit of work that was uncommitted at step 2). The report describes an older tree, so the
   stamp is refused and nothing is written. Tell the user the review is stale and offer to re-run
   it. Never drop `--expect-baseline` to force the stamp: the stamp records the tree as it is now
   and never reads the report, so it would mark code nobody reviewed as reviewed. Commit before a
   review, not during one.

The stamp also records `Reviewed-head:`, the commit it saw. `classify` uses it to list the commits
that touched the app since the review (`commits_since_review`).

## Which modes stamp

- **Default and `--sql`:** step 8b, as above.
- **`--auto`:** once, at the end of the loop ([auto-loop.md](auto-loop.md) step 8). Its review
  cycles don't run step 8b, so a no-convergence stop stays unstamped.
- **`--fix`:** never. Its commits change code after the review; the next review pass covers them.

## In the /ship-app PR body

`/ship-app` collects the review state at its step 2, before its step 7 rebase rewrites the commit
SHAs, and writes it into the PR body at step 9:

- **`Open critical: N`**, where N is `counts.BLOCK` from
  `streamsnow review-loop open-findings apps/<slug>/.review` (findings recorded under
  `### Applied` are already subtracted). Write `Open critical: unknown` when `parsed` is false or
  the command exits 2 (no report). Never write 0 for an unknown.
- **Commits since review**, one `<short sha> <subject>` line per entry in
  `.apps[0].commits_since_review`, or `none` when the list is empty. When
  `.apps[0].reviewed_head_status` is not `ancestor`, write `unknown` instead: `not-ancestor` means
  the reviewed commit was rebased or amended away, and `none` means no stamp recorded a commit.
