# visualization-guide

**Design for the use case and the people who read the page** (REQUIREMENTS.md §2). These are
starting points with reasons; the house style wins
([page-conventions.md § Related defaults](page-conventions.md#related-defaults)).

## Pick the form from the reader's question

| The reader needs to… | Start from | Usually not |
|---|---|---|
| Know one current number and its direction | KPI card: value, delta, optional sparkline | A one-bar chart |
| Scan a few headline numbers | A KPI row (3 to 5 cards) | A bar chart of unrelated measures |
| See change over time | Line | Bars for dozens of time points |
| Compare categories | Bar, sorted by value unless the categories have a natural order; horizontal for long labels | Pie, alphabetical order |
| Follow one series among many | Emphasis: that series in color, the rest gray | Many equal hues |
| See parts of a whole | Stacked bar, or 100% stacked for shares | Pie with more than a few slices |
| See distance from a target | Bar or line against a labelled target | A second y-axis |
| See a distribution | Histogram or box plot | An average alone |
| Look up exact values | A table formatted with `st.column_config` | A chart crowded with labels |

## Departures to tell the reader about

Breaking these silently misleads. Break one when the use case calls for it, and say so on the
page:

| Default | When you depart, say so |
|---|---|
| Bar and area values start at zero | Name the axis range in its title, or use a dot or line instead |
| One y-axis per chart | Label each series with its own axis and unit |
| Charts shown side by side share a scale | Note "scales differ" in the caption |
| Color isn't the only signal ([WCAG 1.4.1](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html)) | Add labels, icons or patterns. A heatmap needs a legend |

## Label the data

- **On a single-series bar chart with a handful of bars,** show each bar's value at its end and
  lighten or drop the value axis. Readers want the number, not an estimate from gridlines.
- **On a line chart,** name each series at the end of its line instead of in a legend.
- **When labels would collide** (many bars, dense lines, small multiples), label only the mark that
  matters (latest, largest, target) and leave the rest to tooltips or a table.
- **Format labels exactly like the KPI cards:** the same units, rounding and locale.

## Layout

- **Default reading order:** KPIs, main trend, breakdown, then detail, inside the four-block
  contract ([page-conventions.md](page-conventions.md)), so the first screen holds the answer.
- **Filters go in one place** (a row above the content, or the sidebar), the same on every page.
- **At most two charts side by side** on a wide layout. Narrower charts lose their labels.

## KPI cards

- **A delta names its baseline** ("vs. prior 30 days") and is colored by meaning, not sign: a
  rise in cost is bad (`st.metric(delta_color="inverse")`). Partial periods say so ("Oct, to date").
- **Format for reading** in the house currency and locale: `1.2M`, `34.5%`, and integer counts
  (`23`, not `23.0`). Percentage-point changes say "pts".
- **Put the definition on the card** with `st.metric(help=…)`. The scaffold's `branded_metric`
  has no `help=`, so with it the definition goes in the caption or glossary.

## Color

- **Color follows the entity, never its rank:** one color per region or product on every page,
  so a filter never repaints the survivors.
- **Use the fewest hues that do the job:**
  - magnitude: one hue, light to dark
  - above or below a midpoint: two opposite hues with a neutral middle
  - more series than the palette holds: fold into "Other" or use small multiples
- **Status hues (green, amber, red) mean good, warning and bad.** If a page shows status and the
  brand sequence contains those hues, skip them for ordinary series.
- **Check contrast and colorblind separation** against the app's theme. Marks need 3:1
  ([WCAG 1.4.11](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html)); a palette
  that falls short gets direct labels.

## Chrome

- **Axis titles name the measure and unit** ("Revenue, $k"). Gridlines are faint or absent.
  Tooltips repeat the name, unit and format.
- **Use one chart library per app,** themed once in the entrypoint. The scaffold's
  `apply_branding()` registers a Plotly template; Altair and the native `st.*_chart` functions
  theme their own way.

## Further reading

- [FT Visual Vocabulary](https://github.com/Financial-Times/chart-doctor/tree/main/visual-vocabulary):
  which chart answers which question
- UK Analysis Function: [Data visualisation: charts](https://analysisfunction.civilservice.gov.uk/policy-store/data-visualisation-charts)
  (bars from zero, data labels, dual axes, pies) and
  [colours](https://analysisfunction.civilservice.gov.uk/policy-store/data-visualisation-colours-in-charts)
- [ColorBrewer](https://colorbrewer2.org): sequential, diverging and colorblind-safe palettes
- Stephen Few: [Dual-scaled axes](https://www.perceptualedge.com/articles/visual_business_intelligence/dual-scaled_axes.pdf),
  [Save the pies for dessert](https://www.perceptualedge.com/articles/visual_business_intelligence/save_the_pies_for_dessert.pdf)
- Cleveland & McGill, [Graphical Perception](https://doi.org/10.1080/01621459.1984.10478080) (1984):
  why position beats angle and area
- Tufte, *The Visual Display of Quantitative Information* (2nd ed., 2001)
