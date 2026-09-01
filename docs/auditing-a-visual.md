# Auditing a visual — trace any number on a dashboard back to the data

Every StreamSnow app carries a human-runnable audit trail: for each UI-feeding
query there is a fully-rendered, paste-runnable SQL file under
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

3. **Run the SET block first.** The top of the file declares session
   variables (`SET start_date = …`). Paste and run those lines once; every
   section below references them. To audit a different date window, edit the
   SET lines — never the queries.

4. **Run the section for your visual.** Each section is one statement,
   labeled `[Page: <page>] <query>.sql`, with a one-line `-- <metric_name>`
   tag directly above the SQL so your editor names the result tab after the
   on-screen visual. Paste, run, compare the aggregates against what the
   dashboard shows **with the same filters** — the combo in the file header
   is the filter state the SQL was rendered for.

5. **Check freshness before crying foul.** A mismatch is very often a date
   window or a filter, not a data bug: confirm the dashboard's selected range
   matches your SET block, and its filter widgets match the combo. If the
   numbers still disagree, you have a real finding — file it via
   `/feedback-app <slug>` (quoting the review file and combo you ran), or
   route deeper lineage questions to `/audit-lineage <slug>`.

## What you can trust about these files

- **They are exactly what the app runs.** The files are generated from the
  same query templates and token fragments the app renders at runtime —
  not a hand-written approximation. A provenance line at the bottom pins the
  generation to the exact manifest, templates, and app code that produced it;
  CI fails when any of those drift without a regenerate, and when the
  rendered file itself is edited by hand.
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
`check` verb (pre-commit, CI, and `streamsnow validate-app`) keeps coverage
complete and renders fresh: a new page cannot ship without its audit trail,
and a changed query cannot ship with a stale one. `/review-app --sql` is the
assisted path that authors manifests well and live-verifies the README.
