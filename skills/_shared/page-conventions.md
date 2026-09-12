# page-conventions

Purpose: the two page-level patterns a sixteen-app production fleet converged on, so every page a
skill builds reads the same way and every number can be traced. Prose, not a check — `/review-app`
flags departures, nothing blocks on them. Skills link here instead of restating it.

## The four-block page contract

Every page, in this order:

1. **Title + caption.** `st.title(...)` then one `st.caption(...)` saying what the page answers.
2. **A caption under every subheader.** `st.subheader(...)` is always followed by `st.caption(...)`
   naming the grain and the population ("daily, funded loans only"). A definition belongs where the
   number is, not three inches above it in a paragraph.
3. **Time controls in one place.** One shared helper (e.g. `pages/_time_controls.py`, imported
   package-qualified) renders the period picker and a `Selected: … · Comparing to: …` line, so
   two pages never disagree about what "last month" means.
4. **Footer: sources and freshness.** `st.divider()`, then `st.caption("Sources: <db>.<schema>.<object> · …")`
   and `st.caption(f"Data as of: {freshness}")`. A reviewer can go from the footer to Snowsight.

When editing a page that does not follow this, fix it in the same PR — atomic, no separate cleanup.

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
