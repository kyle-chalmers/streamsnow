# Feedback mode — `/build-app <slug> --feedback "<feedback>"`

Turn feedback on a live app (text and/or screenshots) into classified items, spec updates where
scope changes, and fixes built the same way new pages are, so a fix meets the same bar as the
page it changes. Chatty by design: every classification, plan and commit gets visible
confirmation, because the user is checking your reading of *their* words.

## Preflight

1. Confirm `apps/<slug>/streamlit_app.py` exists. No `REQUIREMENTS.md` → backfill it first
   ([spec.md](spec.md), automatic backfill mode) so scope changes have a spec to land in: one
   confirmation, then proceed.
2. Confirm a clean working tree (`git status --porcelain apps/<slug>`); otherwise ask to commit or
   stash first. One commit per item only works on a clean tree.

## Collect and classify

3. **Split the feedback into items**: each clause that names a different problem is its own item.
   "Make it better" is not actionable; ask for concrete observations. If an item points at a visual
   and the Playwright CLI is available, offer a screenshot of the page
   ([_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md)) and confirm what the
   user means; skip silently without it.
4. **Classify each item** into exactly one bucket, show the numbered table, and **lock it with the
   user before planning**. Misreading feedback is this mode's main failure.

| Bucket | Definition | Route | Commit type |
|---|---|---|---|
| **BUG** | The output is wrong: `23.0` for `23`, a broken link, NaN where 0 belongs | Fast path | `fix` |
| **POLISH** | Copy, formatting, captions, spacing | Fast path | `feat` |
| **UX** | How a page works: filter behavior, control layout, page flow | Pipeline | `refactor` |
| **NEW-FEATURE** | Adds scope: a column, chart, filter or page | Spec first, then pipeline | `feat` |
| **CROSS-CUTTING** | Applies to every page of the app | Pipeline, all pages in one pass | `feat` |

- **Wrong output beats everything.** An item that is both a UX gripe and a wrong number is a BUG,
  and wrong numbers may be the warehouse's doing: offer `/sql-review <slug>` before patching
  Python.
- **Scope smell test.** A fix that needs a table, chart or filter the spec doesn't mention is
  NEW-FEATURE, however it was phrased.
- **CROSS-CUTTING stays in this app.** Promoting a convention repo-wide is a separate, named
  follow-up; never slip template changes into a feedback round.
- **Two plausible readings?** Show both in the table and let the user pick.
- **Screenshots** show layout, chart type, labels and widgets; not column names, filter state or
  bind semantics. Ask for the data half.

## Plan, then apply

5. **Spec first** for NEW-FEATURE (and any UX item that changes §4–§7): update REQUIREMENTS.md, then
   show the plan for every item (files and fixes for the fast path; a before/after sketch in about
   five lines for UX). Confirm before touching code.
6. **Fast path** (BUG, POLISH confined to one page or one shared file): edit, run the matching
   `streamsnow check` commands and `ruff check` / `ruff format` on the app, stage exactly the
   touched files, commit `<type>(<slug>): <summary> (feedback #N)` with the user's words quoted in
   the body. A pre-commit failure is a real finding: fix it and make a fresh commit; never
   `--no-verify`, never `--amend` after a hook failure.
7. **Pipeline** (UX, NEW-FEATURE, CROSS-CUTTING): re-run the design step for the affected pages
   only ([design.md](design.md); CP1b shows just those pages), then the parallel build with
   page-builders in `fix` mode, each given its items
   ([pages.md § Parallel build](pages.md#parallel-build)), then [verify.md](verify.md) for those
   pages. A whole new page is ordinary build work: it re-enters at design.
8. **Log the round**: one §11 Sessions line per round
   (`feedback: applied N items (<summary>). Commits: <range>. Next: ...`).

## Follow-up review

9. If a code-touching commit landed, ask the gate whether a review is owed:
   `streamsnow review-gate classify <slug> --format json`. `.apps[0].needs_review == true` → offer
   `/review-app <slug> --auto` and wait for a yes (it spends minutes, and Snowflake credits on
   lineage); declined → one static diff-scoped `/review-app <slug>` pass. `verdict == "trivial"` or
   `reviewed` → skip and say why. The gate decides whether review is owed; never re-derive it by
   hand. A pipeline round already ran the verify reviewers; the gate still decides `/review-app`.

## Done when

Every confirmed item is an atomic commit (or an explicit deferral), §11 records the round, the
follow-up review ran or was deliberately skipped, and the user knows the next command
(`/preview-app <slug>` to look, `/ship-app <slug>` to open the PR).
