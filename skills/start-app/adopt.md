# Adopt mode — bring StreamSnow to a repo that already has Streamlit apps

Triggered when preflight finds an existing repo: Streamlit apps not born from `streamsnow new`
(an `apps/`-like tree, `streamlit_app.py` files, deploy scripts), an existing `AGENTS.md`/`CLAUDE.md`,
or custom `.claude/commands` / `.claude/skills`. The rule: **map onto what exists — never scaffold
over it, never demand a rewrite.**

## 1 · Inventory what's there (read-only)

- **App layout:** find the app directories and how they're organized (one app per dir? a flat
  repo-is-the-app?). Infer the slug convention from directory names.
- **Tools in use:** CI configs, deploy scripts, `snow` connections, existing secrets examples —
  these pre-answer most of the `streamsnow configure` questions.
- **Custom commands/skills:** list everything in `.claude/commands/` and `.claude/skills/` and
  classify each against the plugin's skills: **shadows** (does what a plugin skill does), **extends**
  (a domain-specific variant — e.g. company branding checks, a tracker-specific spec flow), or
  **unrelated** (keep as-is).
- **Existing rules file:** if `AGENTS.md`/`CLAUDE.md` exists, do NOT overwrite it.

## 2 · Configure from observed reality

Run `streamsnow configure` with the inventory pre-answering its ≤5 questions (account and database
names from existing deploy scripts/CI, runtime from how apps currently connect). Confirm inferences
with the user in one round rather than re-asking what the repo already shows.

## 3 · Non-destructive scaffold

- Skip any scaffold step whose target already exists; merge new `.gitignore` rules in, never replace.
- `AGENTS.md` exists → render the StreamSnow template to `AGENTS.streamsnow.md` instead and note the
  sections worth merging (the governance table, the query/caching conventions). The human merges.
- **Backfill specs, don't demand them:** for each existing app, offer `/start-app --spec <slug>`
  (automatic backfill mode) so the review and validate phases have a contract to audit against.
  One app first, not a bulk rewrite.
- Don't force `streamsnow new` conformance (local `branding.py`/`sql_loader.py`, `queries/*.sql`
  externalization) as part of adoption — that's per-app conform work, listed in MIGRATION.md and
  handled through `/migrate-app`'s conform step when the team is ready.

## 4 · Write `MIGRATION.md` (the adoption checklist)

One row per finding, with a recommendation:

| Item | Classification | Recommendation |
|---|---|---|
| custom `build-dashboard.md` command | shadows `/start-app` | **replace** — plugin covers it; delete after one trial app |
| company branding-parity skill | extends `/review-app` | **keep local** — domain-specific; note it in AGENTS.md |
| hand-maintained rules file | overlaps rendered AGENTS.md | **merge** — adopt the governance table + conventions |
| app with inlined SQL | needs conform pass | **later** — `/migrate-app` conform step, one app at a time |

Include: what was auto-configured, what needs a human decision, and the suggested trial — run ONE
real change through `/start-app → /validate-app → /review-app → /ship-app` before deleting anything
custom.

## 5 · Verify & report

Run `streamsnow doctor` and `streamsnow validate-app` against one existing app to see where it
stands (expect FAILs — they're the conform backlog, not emergencies; a red gate on an adopted app
blocks nothing until the team opts in). The report leads with the MIGRATION.md path and the
trial-run suggestion — adoption is incremental by design.
