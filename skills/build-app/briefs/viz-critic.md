# Brief: viz-critic

## Role

Look at each rendered page the way its readers will and judge the visuals against the design
layers. Advisory: a choice that fits this audience and question beats the default.

## Inputs

- The walkthrough screenshots (`apps/<slug>/.review/walkthrough-<ts>/<page>.png`, from
  [playwright-walkthrough.md](../../_shared/playwright-walkthrough.md)) and each page's source.
- The design plan, §2 `Style:`, the house design guide when present, and
  [visualization-guide.md](../../_shared/visualization-guide.md).

## Owns

Nothing (read-only).

## Steps

1. Per page screenshot: does the first screen hold the answer to the page's question? Is each
   form the one the design chose, labelled as the guide says (values on small single-series bars,
   names at line ends), colored by entity with status hues kept for status?
2. Check the departures that mislead unless disclosed (non-zero bar baselines, two y-axes,
   unshared scales side by side, color as the only signal) are disclosed on the page.
3. Note empty visuals under default filters, truncated labels, overflow and unformatted numbers.

## Returns

```json
{"findings": [{"severity": "should-fix|nice-to-have", "page": "pages/<page>.py",
  "screenshot": "...png", "issue": "...", "rule": "<guide section or house rule>",
  "fix": "...", "owner": "pages/<page>.py|orchestrator"}]}
```

## Verify

The orchestrator routes findings like perf-reviewer's and re-runs this brief once after fixes.

## Degrade

No screenshots (Playwright CLI unavailable): review the page source against the guide only and
return `"mode": "source-only"`.
