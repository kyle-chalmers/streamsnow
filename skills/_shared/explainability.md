# explainability

Purpose: defaults that let someone open the app cold and know what a page is for, what each number
means, and what to do next, **with as few words as that takes**. Builds on the four-block contract
and glossary in [page-conventions.md](page-conventions.md); framing in
[page-conventions.md § Related defaults](page-conventions.md#related-defaults).

## Text budget

Pages are read in seconds, so every line of text costs attention. Fit the copy to the audience,
but start from these limits:

| Text | Default limit |
|---|---|
| Page caption | One sentence, about 20 words: the question the page answers |
| Subheader caption | Grain and population, about 12 words ("daily, active accounts only") |
| `help=` / glossary entry | The definition and its formula. No examples or history |
| Takeaway line | At most one per section, computed from the data, never hard-coded |
| Body text | No `st.markdown` paragraphs. If a chart needs a paragraph, change the chart |

Prefer the reader's words to the table's ("customers", not `DIM_CUST`). Cut any sentence a reader
could skip without losing a fact.

## Name the page's question

- **The page caption is the decision the page supports** ("Which regions are behind plan this
  month?"), not its topic ("Regional dashboard"). The reader can tell at once whether this is the
  right page.
- **Order the page by that question:** the number that answers it comes first. A visual that
  doesn't serve the question moves to another page or goes.
- **The spec records the question.** REQUIREMENTS.md §2 holds the decision the app drives. Keep
  each page's question in its §4 entry, so a reviewer can check the page against it.

## Definitions live at the number

Every number has a definition the reader can reach from where it appears: `help=` on a metric or
column, the tooltip on a chart, or the glossary expander ([page-conventions.md](page-conventions.md)).
When a definition departs from the obvious ("average of store rates", not an overall rate), the
definition says so.

## How to read the page (only when needed)

When the encoding or the workflow isn't obvious (a cohort grid, a target band, a drill-down
path), add up to three lines to the page's existing glossary expander:
- what the page shows
- what good or bad looks like
- where to go next

A page has one expander for this, not two. An obvious chart gets none.

## Empty, stale and broken states

| State | Default |
|---|---|
| Filters return no rows | `st.info` naming what to widen. No empty axes |
| Data older than its refresh cadence | `st.warning` above the KPIs naming the "as of" date |
| A query fails | `st.error` in plain words and what to do next. No stack trace |
| A partial period (today, this month) | Marked in the label ("Oct, to date") |

A silent empty or stale page reads as "zero" or "no change", and both lead to wrong decisions.

## Cold-reader check

Before calling a page done, have someone show a screenshot of it, under default filters, to a
reader (a person or a subagent) who hasn't seen the spec or the code. The reader answers three
questions:
1. What decision is this page for?
2. What does each number mean, and is it good or bad right now?
3. Where would you look next?

A wrong or missing answer is a finding against the page, not the reader. The fix is usually
better copy or layout, not more text. The UI walkthrough
([playwright-walkthrough.md](playwright-walkthrough.md)) produces the screenshots.

## Further reading

- Nielsen Norman Group: [How users read on the web](https://www.nngroup.com/articles/how-users-read-on-the-web),
  [Tooltip guidelines](https://www.nngroup.com/articles/tooltip-guidelines),
  [Empty states in complex applications](https://www.nngroup.com/articles/empty-state-interface-design)
- GOV.UK: [Sentence length: why 25 words is our limit](https://insidegovuk.blog.gov.uk/2014/08/04/sentence-length-why-25-words-is-our-limit)
- Streamlit: [st.metric](https://docs.streamlit.io/develop/api-reference/data/st.metric) and
  [st.column_config](https://docs.streamlit.io/develop/api-reference/data/st.column_config)
  (`help=` tooltips)
