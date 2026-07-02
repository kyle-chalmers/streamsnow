# Feedback classification — the five buckets

Every feedback item lands in exactly one bucket; the bucket decides what a fix looks like and what
gets updated first.

| Bucket | Definition | Fix shape | Commit type |
|---|---|---|---|
| **BUG** | The output is wrong — `23.0` where `23` belongs, a broken link, NaN where 0 should show | Code-only; usually one file, one commit | `fix` |
| **POLISH** | Surface-level improvement — copy, formatting, captions, spacing | Possibly several files; one commit per kind of change | `feat` |
| **UX** | Interaction redesign — filter behavior, control layout, page flow | Often a shared helper; restructures how a page works. Sketch before/after in ~5 lines before editing | `refactor` |
| **NEW-FEATURE** | Adds scope — a new column, chart, filter, or page | Spec sections (§4–§7) updated **first**, then code. A whole new page → route to `/start-app <slug>` (build phase) instead | `feat` |
| **CROSS-CUTTING** | Applies to every page of the app (or every app in the repo) — "every page needs a one-line description" | Apply across this app's pages in one pass; flag the convention for promotion to the repo templates in a follow-up PR | `feat` |

## Classification rules

- **Wrong output beats everything:** if an item is both a UX gripe and a wrong number, it's a BUG
  first — and wrong numbers may be the warehouse's doing, so consider `/audit-lineage` before
  patching Python.
- **Scope smell test:** if fixing it requires a table/chart/filter the spec doesn't mention, it's
  NEW-FEATURE regardless of how the user phrased it.
- **CROSS-CUTTING stays scoped:** apply to *this* app now; promoting the convention repo-wide is a
  separate, named follow-up — never sneak template changes into a feedback PR.
- When two readings are plausible, present both in the classification table and let the user pick —
  that's what the lock-in step is for.

## Vision limits (screenshots)

A screenshot tells you layout, chart type, KPI labels, filter widgets, branding. It does **not**
tell you real column names, whether the view is default or filtered state, or bind-parameter
semantics — ask for the data half rather than inferring it.

## Example session shape

```
1. [BUG]           Payment Count shows "23.0" — should be an integer
2. [CROSS-CUTTING] Every page needs a one-line description under the title
3. [UX]            Custom Range filter is confusing — make it a selectbox mode
```

→ lock classification → plan (files per item) → confirm → three atomic commits + one §11 log commit
→ follow-up `/review-app` on the diff.
