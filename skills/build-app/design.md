# Discover and design — profile the data, plan every page, then CHECKPOINT 1b

Between the confirmed spec and the scaffold, two subagents turn intent into a buildable plan, so
the parallel build that follows has nothing left to guess. Dispatch each with its brief and the
inputs it names ([briefs/](briefs/)); without subagents, follow the brief yourself
([_shared/other-agents.md](../_shared/other-agents.md)).

## Discover (§11 phase `discover`)

1. Dispatch **data-scout** ([briefs/data-scout.md](briefs/data-scout.md)).
2. Write its profile into §3: each object's fully qualified name, grain, date column and range,
   and size. Record its `gaps` in §10. Every object must sit on the allowlist; a gap that blocks
   a page goes to the user now, not after the build.

## Design (§11 phase `design`)

3. Dispatch **app-designer** ([briefs/app-designer.md](briefs/app-designer.md)) with the spec,
   the profile and the design layers.
4. Check its plan: every §4 page appears once; every metric key has a glossary entry; every query
   belongs to exactly one page or to `shared_data`; captions fit the text budget
   ([_shared/explainability.md](../_shared/explainability.md)).
5. Write it into REQUIREMENTS.md: each page's `Question:` and sections in §4, forms in §5, KPIs
   and comparisons in §6, and a `Shared loaders:` line per `shared_data` entry in §8.

## CHECKPOINT 1b — the wireframe

6. Show one text wireframe per page, top to bottom, before any code exists:
   ```
   Sales trend — "Which regions are behind plan this month?"
   [KPI row] Net paid (vs. plan) · Orders (vs. prior 30 days) · Avg order value
   [Line] Net paid by day, plan as a labelled target          (trend → line)
   [Bar, sorted, labelled] Net paid by region                  (compare → sorted bar)
   [Definitions] [Sources · Data as of]
   ```
   Then the shared loaders and which pages use them. Ask: build it / change it / stop. Block until
   the user chooses; a change re-runs step 3 for the pages it touches.
