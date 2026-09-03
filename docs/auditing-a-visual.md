# Auditing a visual — trace any number on a dashboard back to the data

Every StreamSnow app carries a human-runnable audit trail: for each query under
`apps/<slug>/queries/` — the directory the validate gate pushes UI-feeding SQL
into (SQL inlined in Python sits outside the trail) — there is a fully-rendered,
paste-runnable SQL file under
`apps/<slug>/sql_review/`. You do not need the app's code, a local Python
environment, or StreamSnow itself to use it — just Snowsight (or any SQL
editor) and a role that can read the app's schemas. This page is the runbook
for the person who looks at a chart and asks *"is that number right?"*

## The five-minute audit

1. **Find the visual's review file.** Open
   `apps/<slug>/sql_review/README.md`. The coverage table maps every query →
   the page it feeds → its review file → whether its lineage was live-verified
   (and when). Find the row for the page/visual you're questioning.

2. **Open the review file** (`<feature>.review.sql`, or
   `<feature>.<combo>.review.sql` when the feature ships several filter
   combinations — each combo mirrors one way the dashboard can be filtered).
   The header banner tells you which combo you're holding and what every
   `{TOKEN}` was substituted with.

3. **Run the SET block first, if the file has one.** When the sections take
   bind params, the top of the file declares session variables
   (`SET start_date = …`); paste and run those lines once, and to audit a
   different window edit the SET lines — never the queries. A `set_block_note`
   above them, when present, records *why* the defaults are what they are:
   which source the bounds derive from, and any mechanics that bite when you
   change them. Read it before widening a window.

   Some files legitimately have **no** SET block — the header says so — because
   no section references a session variable or takes a bind param. There the
   range lives inside each query, so there is nothing global to edit: read the
   section to see how it bounds itself. The generator prunes SET lines nothing
   references precisely so this step never lies to you; a SET block you can
   edit with no effect on the numbers is worse than none.

4. **Run the section for your visual.** Each section is one statement,
   labeled `[Page: <page>] <query>.sql`, with a one-line `-- <metric_name>`
   tag directly above the SQL so your editor names the result tab after the
   on-screen visual. Paste, run, compare the aggregates against what the
   dashboard shows **with the same filters** — the combo in the file header
   is the filter state the SQL was rendered for.

5. **Check freshness before crying foul.** A mismatch is very often a date
   window or a filter, not a data bug: confirm the dashboard's selected range
   matches your SET block (or, with no SET block, the range the section sets
   for itself), and its filter widgets match the combo. If the
   numbers still disagree, you have a real finding — file it via
   `/feedback-app <slug>` (quoting the review file and combo you ran), or
   route deeper lineage questions to `/audit-lineage <slug>`.

## What you can trust about these files

- **What the provenance digest pins depends on the manifest's
  `token_strategy`.** With `manifest`, the files are rendered by calling the
  same token dispatchers the app calls, and the digest pins the manifest, the
  query templates, *and* the app modules those dispatchers live in — the SQL
  is exactly what the app runs. With `static` (the default), the digest pins
  the manifest and templates only: the token literals are the manifest
  author's assertion of what the app renders, kept honest by code review
  rather than by the digest. Under either strategy, CI fails when any pinned
  input drifts without a regenerate, and when the rendered file itself is
  edited by hand.
- **They are read-only by construction.** The generator refuses to emit
  anything but `SELECT`/`WITH…SELECT`/`SHOW`/`DESCRIBE`/`EXPLAIN` plus the
  `SET` session variables, and CI re-verifies committed files. Running a
  review file cannot write anything.
- **"Verified" means someone (or the lineage pass) actually confirmed it.**
  The README's Verified column carries a date only when the upstream objects
  were live-probed on that date. An `unverified` row is still useful — it
  just means the lineage narrative came from static analysis, honestly
  labeled.

## For the developer on the other side of this

The rendered files are **not** the editing surface — the manifest at
`apps/<slug>/sql_review/manifests/<feature>.json` is. Edit the manifest (or
the query templates), then `streamsnow sql-review generate <slug>`. The
`check` verb keeps coverage complete and renders fresh: every
`queries/*.sql` must be claimed by a manifest, and an unregenerated change
reads as DRIFT. It fails closed in pre-commit and the generated CI where
those configs are adopted; inside `streamsnow validate-app` it warns only in
0.6 (FAIL planned for 0.7). `/review-app --sql` is the
assisted path that authors manifests well and live-verifies the README.
