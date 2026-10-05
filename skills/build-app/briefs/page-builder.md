# Brief: page-builder

## Role

Build one page of the app from its design, inside the files it owns, and hand back everything
the shared files need. Several page-builders run at once, so a page-builder never edits a file
another agent or the orchestrator owns.

## Inputs

- `slug`, the runtime (`runtime_name:` in `snowflake.yml`), and `mode`: `build` (new page),
  `conform` (an existing page: keep its visuals and behavior, apply the conventions) or `fix`
  (apply the listed findings or feedback items only).
- This page's entry from the design plan, its `page_queries`, and the shared loaders already in
  `pages/_data.py`.
- [pages.md](../pages.md) steps 2–4, 6 and 8 (marks, comments, lint), and the gotchas there.

## Owns

- `apps/<slug>/pages/<page>.py`
- `apps/<slug>/queries/<name>.sql` for the queries listed under this page in `page_queries`

Never edit: `streamlit_app.py`, `sql_review/`, `pages/_*.py`, `pages/about.py`, `REQUIREMENTS.md`,
another page, or a shared query. Need something there? Return it as a request.

## Steps

1. Write the queries with their header blocks; real SQL against the profiled objects.
2. Write the page: the design's caption and sections, `help=` from the glossary keys,
   `definitions_expander(<keys>)` and `sources_footer(...)` at the end, `date_range` for the
   period, shared data from `pages/_data.py`, `show_sql` where the design says so. Mark each
   visual with `review_value("<key>", value)` in this file.
3. Run `streamsnow check page-imports apps/<slug>`, `streamsnow check caching apps/<slug>`,
   `streamsnow check schema-refs apps/<slug>` and `streamsnow check bind-predicates apps/<slug>`;
   fix findings in your own files.

## Returns

```json
{"page": "pages/<page>.py", "files_written": ["..."],
 "nav_entry": "st.Page(\"pages/<page>.py\", title=\"...\", icon=\"...\")",
 "index_entry": "  - path: pages/<page>.py\n    metrics:\n      - key: ...",
 "glossary_entries": [{"key": "...", "label": "...", "definition": "...", "formula": "..."}],
 "data_requests": ["a loader or query another page also needs"],
 "checks": {"page-imports": 0, "caching": 0, "schema-refs": 0, "bind-predicates": 0}}
```

## Verify

The orchestrator re-runs the four checks, rejects any write outside `files_written` ∩ Owns, merges
the entries, runs `streamsnow sql-review generate <slug>` once for all pages, then
`streamsnow sql-review check <slug>`.

## Degrade

A check that can't run (missing tool) is reported in `checks` as `"skipped"`, never as 0.
