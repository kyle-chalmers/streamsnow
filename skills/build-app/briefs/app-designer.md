# Brief: app-designer

## Role

Turn the spec and the data profile into a page-by-page design: what each page answers, which
visual answers it and why, the words on the page, and the data several pages share. Design for
the use case and the people who read the page.

## Inputs

- `apps/<slug>/REQUIREMENTS.md` (§2 audience and `Style:`, §4 pages and their `Question:`,
  §5–§8), and the data-scout result.
- The design layers, highest first: §2 `Style:`, the house design guide `.streamsnow/design.md`
  when present ([overlays.md](../../_shared/overlays.md#house-design-guide)), then the defaults in
  [visualization-guide.md](../../_shared/visualization-guide.md),
  [explainability.md](../../_shared/explainability.md) and
  [streamlit-performance.md](../../_shared/streamlit-performance.md).

## Owns

Nothing. Return the plan; the orchestrator writes it into REQUIREMENTS.md and shows it at CP1b.

## Steps

1. Per page: restate its question in the reader's words (the page caption, about 20 words).
2. Per section: pick the form from the question (visualization-guide's table), with a one-line
   reason that cites the guide row or house rule it follows or departs from. A departure that
   would mislead says how the page discloses it.
3. Per KPI: the comparison it names, whether up is good, and its glossary entry (label,
   definition, formula; ratios are `SUM ÷ SUM`).
4. Copy within the text budget: page caption, subheader captions (grain and population),
   optional "how to read" lines (at most three, only for non-obvious pages).
5. **Shared-data plan:** list every page's data needs side by side. Data two pages read (same
   object at the same or a coarser grain, date bounds, filter value lists) becomes one shared
   query and one cached loader in `pages/_data.py`, at the coarsest grain the pages need.
6. `show_sql`: yes for sections whose readers verify numbers themselves (per §2), else no.

## Returns

```json
{"pages": [{"file": "pages/<page>.py", "title": "...", "question": "...",
  "sections": [{"title": "...", "caption": "...", "form": "...", "reason": "...",
                "metric_keys": ["..."], "query": "queries/<name>.sql", "show_sql": false}],
  "how_to_read": ["..."]}],
 "glossary": [{"key": "...", "label": "...", "definition": "...", "formula": "..."}],
 "shared_data": [{"loader": "load_...", "query": "queries/<name>.sql", "grain": "...",
                  "pages": ["pages/<a>.py", "pages/<b>.py"]}],
 "page_queries": {"pages/<page>.py": ["queries/<name>.sql"]}}
```

## Verify

The orchestrator checks that every §4 page appears once, every metric key has a glossary entry,
every query belongs to exactly one page or to `shared_data`, and every caption fits the budget.

## Degrade

No data profile: design from the spec, mark grain-dependent choices `(inferred)` for CP1b.
