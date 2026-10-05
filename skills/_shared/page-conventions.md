# page-conventions

Purpose: the two page-level patterns a sixteen-app production fleet converged on, so every page a
skill builds reads the same way and every number can be traced. Prose, not a check — `/review-app`
flags departures, nothing blocks on them. Skills link here instead of restating it.

## The four-block page contract

Every page, in this order:

1. **Title + caption.** `st.title(...)` then one `st.caption(...)` saying what the page answers.
2. **A caption under every subheader.** `st.subheader(...)` is always followed by `st.caption(...)`
   naming the grain and the population ("daily, active accounts only"). A definition belongs where the
   number is, not three inches above it in a paragraph.
3. **Time controls in one place.** One shared helper (e.g. `pages/_time_controls.py`, imported
   package-qualified) renders the period picker and a `Selected: … · Comparing to: …` line, so
   two pages never disagree about what "last month" means.
4. **Footer: sources and freshness.** `st.divider()`, then `st.caption("Sources: <db>.<schema>.<object> · …")`
   and `st.caption(f"Data as of: {freshness}")`. A reviewer can go from the footer to Snowsight.

When editing a page that does not follow this, fix it in the same PR — atomic, no separate cleanup.

## The default date range comes from the data

A page's default period ends at the data's latest date, not at today. Load it once, cached, from
the object the page reads (`SELECT MAX(<date_col>) FROM <db>.<schema>.<table>` in its own
`queries/*.sql`), and derive the default start from it (for example one year before). Pass both
ends to the date picker's `value=`, and bound its `min_value` / `max_value` by the data too.
`date.today()` as a default makes every page over data that ends in the past (a historical
extract, a sample dataset, a feed that stopped loading) open empty, which reads as a broken
query. Show the anchor in the `Data as of:` footer, so a stale feed is visible rather than
silent. The app's `sql_review/index.yaml` anchors its `review_window` to the same
`MAX(<date_col>)`, so the review SQL covers the range the page shows by default.

## Mark every visual for SQL review

Wrap the value each data visual shows in `review_value("<metric_key>", value)` from the app's
`review.py` (`from review import review_value`):
`st.metric("Revenue", review_value("total_revenue", total))`,
`st.dataframe(review_value("orders_by_region", df))`. It returns its input and does nothing else
at runtime (no query, no file, no import), in Streamlit in Snowflake too. The key is the metric's
key in `sql_review/index.yaml` (snake_case, five words or fewer); one call per metric, with a
string literal key, because `streamsnow sql-review check` reads the calls without running the page
and fails on a visual the index does not list or a metric no visual marks.

## Single-source metric definitions (the glossary module)

Seven pages once carried ~500 lines of prose each defining the same metrics slightly differently.
The fix: one `pages/_glossary.py` per app, a table of definitions with four consumers, imported
package-qualified (`from pages._glossary import metric_help`) — never bare, which resolves under
`streamlit run` and raises `ModuleNotFoundError` deployed. The module imports `streamlit` but calls
nothing at import time (the container runtime shares one process across viewers).

```python
from typing import NamedTuple
import streamlit as st

class Metric(NamedTuple):
    key: str; label: str; definition: str; formula: str

_DEFINITIONS = (
    Metric("promise_kept_rate", "Promise-kept rate",
           "Share of payment promises honoured by their due date.",
           "SUM(kept_promises) ÷ SUM(promises_due)"),
)
_BY_KEY = {m.key: m for m in _DEFINITIONS}

def metric_help(key):          # -> st.metric(..., help=metric_help("promise_kept_rate"))
    m = _BY_KEY[key]; return f"{m.definition} Formula: {m.formula}"
def column_help(*keys):        # -> st.column_config help= dicts for tables
    return {k: metric_help(k) for k in keys}
def render_glossary(*keys):    # -> one expander per page listing what it shows
    with st.expander("Metric definitions"):
        for k in keys or _BY_KEY: st.markdown(f"**{_BY_KEY[k].label}** — {metric_help(k)}")
def hover_definition(key):     # -> %-escaped text for a Plotly hovertemplate
    return metric_help(key).replace("%", "%%")
```

House rules the table encodes: formulas use `÷`; every ratio is `SUM(numerator) ÷ SUM(denominator)`
at the rendered grain, never the average of a per-row percentage; the `Sources:` footer names the
object each metric reads. `tests/fixtures/fleet/apps/acme-collections-overview/pages/_glossary.py`
in the StreamSnow repo is a complete, validate-clean example.

## Related defaults

Design for the use case and the people who read the page. The three guides below are starting
points with their reasons, not rules. The repo's house style (`brand:` config, its overlay,
`AGENTS.md`) wins, and so does a judgment that fits this audience better. `/review-app` raises
departures; nothing blocks on them.
- [streamlit-performance.md](streamlit-performance.md): fetch less, cache by what changes the
  answer, share across pages, rerun less.
- [visualization-guide.md](visualization-guide.md): form from the question, labels, layout, KPI
  cards, color.
- [explainability.md](explainability.md): a text budget, the page's question, definitions at the
  number, empty and stale states, the cold-reader check.
