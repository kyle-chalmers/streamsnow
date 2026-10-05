# visualization-guide

Purpose: the defaults for choosing and drawing a page's charts and KPIs, so a page reads as one
designed system and every visual is in the form that answers its question fastest. These are
defaults with reasons, not rules.
- **The house style wins:** the repo's `brand:` block in `streamsnow.config.yaml` (colors, font,
  `chart_sequence`), then its overlay or `AGENTS.md`, then the app's REQUIREMENTS.md §2 audience
  notes.
- **The check is review, not a gate:** `/review-app`'s UI reviewer raises departures from the
  default, and a stated house rule always beats one. Data, audience and style vary.

## Start from the question, then pick the form

Name what the reader must do with the visual before picking a chart type. Sometimes the answer is
not a chart.

| The reader needs to… | Default form | Not |
|---|---|---|
| Know one current number (plus its direction) | KPI card: value, delta, optional sparkline | A one-bar bar chart |
| Scan a few headline numbers | A KPI row of 3 to 5 cards | A grouped bar of unrelated measures |
| See a trend over time | Line (area only for a single series) | Bars for 50+ time points |
| Compare categories | Bar sorted by value, horizontal when labels are long | Alphabetical order, pie |
| See one series against the rest | Emphasis: that series in the brand color, the rest gray | Five equal hues |
| See a part of a whole | Stacked bar, or 100% stacked for shares | Pie with more than 3 slices |
| See above or below a target | Bar or line against a labelled target line | Two y-axes |
| See a distribution | Histogram or box plot | An average alone |
| See a relationship | Scatter, with the outlier labelled | A table of pairs |
| Look up exact values for many items | A table with `column_config` formats | A chart with a label on every mark |

*Why:* the right form lets the reader skip a step. A sorted bar answers "which is biggest" before
the reader reads an axis.

## Layout: one reading order

**Default order:**
1. The four-block contract from [page-conventions.md](page-conventions.md): title and caption, then
   one shared time control.
2. KPI row.
3. Main trend.
4. Breakdown.
5. Detail table.
6. Sources and freshness footer.

*Why:* a viewer who stops reading after the first screen still leaves with the answer, and every
page in the app reads the same way.

- **At most two charts side by side** in `st.columns` on a wide layout; a chart narrower than about
  a third of the page loses its axis labels.
- **One filter row,** above everything it scopes. Filters inside one chart's container imply they
  affect only that chart; say so in its caption if they do.

## KPI cards

- **A delta needs a named comparison:** "vs. prior 30 days", "vs. target". A bare "+5%" makes
  the reader guess.
- **Color the delta by meaning, not by sign.** A rise in cost is bad. `st.metric`'s
  `delta_color="inverse"` covers that case.
- **Format for reading:** `1.2M`, `$48.6k`, `34.5%`, and counts as integers (`f"{int(n):,}"`, never
  `23.0`).
  - Percentage-point changes say "pts", not "%".
- **Put the definition in the card's `help=`** from the glossary
  ([page-conventions.md](page-conventions.md)), not in a paragraph above the row.

## Color

- **Categorical colors follow the entity, never its rank.** Fix a color per entity (region,
  product, team) once, in a shared dict or the brand sequence order, and reuse it on every page.
  - *Why:* a filter that removes a series must not repaint the survivors, or the reader's "West is
    blue" becomes wrong.
- **Use the fewest hues that do the job.**
  - **Magnitude:** one hue, light to dark.
  - **Above or below a midpoint:** two opposite hues with a gray middle.
  - **Five or more series:** fold the tail into "Other" or split into small multiples rather than
    adding hues.
- **Keep status colors for status.** Green, amber and red mean good, warning and bad. Don't use
  them for series 3, 4 and 5, and pair them with a label or icon so color is never the only
  signal.
- **Check the palette for colorblind readers** (adjacent series must stay distinct under common
  color-vision deficiencies) and for contrast against both light and dark themes. A brand palette
  that fails gets direct labels or a table view alongside.

## Marks and chrome

- **Bars start at zero.** Lines may start elsewhere when the change is the point; say so in the
  axis title.
- **Never two y-axes on one chart.** Two measures on different scales become two charts, or both
  are indexed to 100 at the start.
  - *Why:* the alignment of two scales is arbitrary and invents a correlation.
- **Label directly and selectively:** the last point of a line, the largest bar, the one series
  that matters. Keep a legend for two or more series. Never put a number on every mark.
- **Keep gridlines and axes faint, solid and few.** Titles say what is measured and in what unit
  ("Revenue, $k"). Hover templates repeat the unit and use the same number format as the KPI
  cards.
- **Use one chart library per app,** with the brand Plotly template applied in the entrypoint
  (`apply_branding()`).
  - *Why:* two libraries mean two looks and two sets of formatting bugs.

## When a chart has nothing to show

An empty chart under default filters reads as a broken query
([playwright-walkthrough.md](playwright-walkthrough.md) treats a band of them as critical).
- **When a filter returns no rows,** say what happened and what to try in `st.info`
  ("No accounts match these filters; widen the date range"), instead of drawing empty axes.
