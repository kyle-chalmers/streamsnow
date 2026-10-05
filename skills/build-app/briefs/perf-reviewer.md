# Brief: perf-reviewer

## Role

Find what makes the app slow or costly, one page at a time and then across all pages. Advisory:
findings go to the orchestrator, nothing blocks on them. Read-only.

## Inputs

- `apps/<slug>/` (pages, `queries/*.sql`, `pages/_data.py`), the design's `shared_data` plan.
- [streamlit-performance.md](../../_shared/streamlit-performance.md) and the house design guide
  when present.

## Owns

Nothing (read-only).

## Steps

1. Per page: uncached or wrongly keyed loaders, aggregation in pandas that belongs in SQL,
   uncapped detail queries, `SELECT *`, filters outside a form, heavy views in tabs or expanders
   that still run on every rerun.
2. **Cross-page pass** over the whole app: duplicate or near-duplicate SQL across
   `queries/*.sql`, the same object scanned at the same grain by several queries, loaders that
   belong in `pages/_data.py`, `shared_data` entries the pages ignored.
3. Cite the guide line each finding rests on.

## Returns

```json
{"findings": [{"severity": "should-fix|nice-to-have", "file": "...", "line": 0,
  "rule": "streamlit-performance.md: <section>", "fix": "...",
  "owner": "pages/<page>.py|orchestrator"}]}
```

## Verify

The orchestrator routes each finding to its owner (a page-builder in `fix` mode, or itself for
shared files) and re-runs this brief once after the fixes.

## Degrade

Nothing to degrade: this brief reads files only.
