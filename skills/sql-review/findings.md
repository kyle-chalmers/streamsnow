# Findings: the one shape every reviewer returns

Every reviewer writes a JSON object `{"findings": [...]}`. `streamsnow sql-review log` validates
it and refuses the whole file when one finding is wrong, naming the finding and the problem.

```json
{
  "findings": [
    {
      "id": "P02-1",
      "severity": "major",
      "page": "02",
      "metric": "orders_by_region",
      "object": null,
      "claim": "Orders are counted once per region name, so a renamed region is counted twice.",
      "evidence": ["run:02#1", "probe:02#1"],
      "suggested_fix": "Group by region_id in queries/by_region.sql."
    }
  ]
}
```

| Field | Rule |
|---|---|
| `id` | Short and unique in the file: `P<page>-<n>` (page), `O<n>` (object), `X<n>` (optimizer). |
| `severity` | `blocker`, `major` or `minor` (below). |
| `page` | Two-digit page number (`"02"`), or `null` for an object finding. |
| `metric` | The metric key on that page, or `null`. |
| `object` | `DATABASE.SCHEMA.OBJECT` for an object finding, else `null`. |
| `claim` | One or two sentences a person can check. No row-level values. |
| `evidence` | At least one result `id` from this run's JSON (`probe:01#1`, `probe:DB.SCHEMA.OBJ`, `run:01#1`, `bench:01#1:after`). |
| `suggested_fix` | One line, or a short diff summary. Optional. |

## Severity

- **blocker**: the screen shows a wrong number, or the app will fail for its users: a fan-out
  join that inflates a total, a missing grant for the app's role, an object that does not exist,
  a denied schema, DDL drift that changes results.
- **major**: real risk that is not wrong today: an ignored soft-delete or test flag, a null that
  silently drops rows, a view chain three deep, a section that returns no rows in the window.
- **minor**: clarity and hygiene: a comment that restates the SQL, a misleading metric key, an
  optimization with a measured gain.

`/review-app` uses critical / should-fix / nice-to-have for the same three levels.

## What never goes in a finding

Row-level values, small-group breakdowns (a total over two to nine rows), people's names or
emails, absolute file paths, credentials. Cite the result `id` instead of quoting data.
