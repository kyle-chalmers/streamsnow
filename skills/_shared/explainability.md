# explainability

Purpose: the defaults that let someone open an app cold, with no demo and no author nearby, and
know what each page is for, what each number means, and what to do about it. They build on the
four-block contract and the glossary module in [page-conventions.md](page-conventions.md). These
are defaults with reasons, not rules: the repo's overlay or `AGENTS.md` and the app's audience
(REQUIREMENTS.md §2) can change the tone and depth, and `/review-app` raises departures.

## Every page answers a named question

- **The caption under the title states the decision the page supports, in the reader's words:**
  "Which regions are behind on collections this month, and by how much?", not "Collections
  dashboard".
  - *Why:* a reader who knows the question can tell in seconds whether this is the right page,
    and can judge whether the page answers it.
- **Order the page by that question.** The KPI that answers it comes first; the context that
  explains it comes after. A visual that doesn't serve the question moves to another page or goes.
- **REQUIREMENTS.md §4 records the question per page,** so `/review-app` and the next author can
  check the page against it.

## Every number carries its definition

- **Every KPI card and every metric column gets `help=`** from the app's glossary
  (`metric_help`, `column_help`). Every chart's hover repeats the metric's name and unit
  (`hover_definition`).
  - *Why:* the definition belongs where the number is. A reader hovering a surprising value is
    the reader who most needs to know how it was computed.
- **State the grain and the population in the subheader's caption:** "daily, active accounts
  only". Two numbers that look like they should match usually differ on exactly this.
- **Name every comparison:** "vs. prior 30 days", "vs. plan". A delta whose baseline the reader
  has to guess is a delta they will misread.

## A "How to read this page" expander

One `st.expander("How to read this page")` near the top, collapsed by default, in three or four
short lines:
- what the page shows and at what grain
- how the filters change it
- what a good or bad reading looks like ("below the dashed target line means behind plan")
- where to go next ("drill into an account on the Accounts page")

*Why:* collapsed, it costs a returning user one line; expanded, it replaces the walkthrough a new
user never got.

Put the full metric list in the glossary expander (`render_glossary`), not here.

## Empty, stale and broken states say what happened

| State | Default |
|---|---|
| Filters return no rows | `st.info` naming the filter that emptied it and what to widen. Never draw empty axes ([visualization-guide.md](visualization-guide.md)) |
| Data older than its refresh cadence | `st.warning` above the KPIs: "Data as of <date>; expected daily. Numbers may be behind." The footer's `Data as of` stays either way |
| A query fails | `st.error` in plain words ("Couldn't load collections data"), plus what the reader can do (retry, who to contact) from the overlay. No stack trace on the page |
| A number is partial (the current day or month still loading) | Mark it in the label or caption ("Oct (to date)") so a dip isn't read as a drop |

*Why:* a silent empty or stale page reads as "the numbers are zero" or "nothing changed". Both
are wrong, and both lead to a bad decision.

## Point out what matters

- **Annotate the one notable point on a chart** (a launch date, an outage, the peak) with a short
  label on the chart itself, not in a paragraph below.
- **Use emphasis, not more color,** when one series is the story ([visualization-guide.md](visualization-guide.md)).
- **When a page has a clear takeaway,** a one-line `st.caption` under the chart may say it, as
  long as it's computed from the data ("West is 12% behind plan, the largest gap"). A hard-coded
  sentence goes stale.

## The cold-reader check

Before a page is called done, test it the way a new viewer meets it.

**Who reads it:** someone, or a subagent, who has not seen the spec or the code. They see only a
screenshot of the page under its default filters and the page's text.

**They answer three questions:**
1. What decision is this page for?
2. What does each KPI mean, and is it good or bad right now?
3. What would you look at next?

**A wrong or missing answer is a finding against the page's copy, not against the reader.** Fix
the caption, the `help=`, the "How to read" lines or the layout, then ask again. The UI walkthrough
in [playwright-walkthrough.md](playwright-walkthrough.md) produces the screenshots.
