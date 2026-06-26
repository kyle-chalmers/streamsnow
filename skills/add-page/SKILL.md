---
name: add-page
description: Add a page to an existing StreamSnow app from its REQUIREMENTS.md — generates pages/<page>.py with branded metric/chart stubs, scaffolds queries/*.sql with the required header block, and registers the page in streamlit_app.py's st.navigation. Use when the user says "add a page", "scaffold a page", "add page to <app>", or after /refine-requirements adds an entry to §4 Pages & Sections.
---

# add-page

Scaffold one new page into an existing app so its charts, KPIs, filters, and queries match the spec.

## Steps

1. Resolve `<slug>` and the page name from the user's request. Confirm `apps/<slug>/` and `apps/<slug>/REQUIREMENTS.md` exist; if the spec is missing, hand off to /refine-requirements first, then resume.
2. Read `apps/<slug>/REQUIREMENTS.md` §4 (Pages & Sections) for the target page: its sections, metric cards, charts, filters, and source schemas. If the page isn't in §4, hand off to /refine-requirements to add it, then resume.
3. Create `apps/<slug>/pages/<page>.py`: title + caption, one branded metric/chart stub per §4 section, and `@st.cache_data`-wrapped loader functions that call the per-app `sql_loader` — match the patterns in a sibling page under `apps/<slug>/pages/`.
4. For each query the page needs, create `apps/<slug>/queries/<name>.sql` with the required header block (Query / Feeds / Schemas / Params / Tokens) and a named-column SELECT against the §4 source schemas. Copy header shape from an existing file in `apps/<slug>/queries/`.
5. Register the page in `apps/<slug>/streamlit_app.py` — add a `st.Page(...)` entry to the existing `st.navigation` structure; do not introduce a `pages/` auto-discovery convention.
6. Run `streamsnow check schema-refs apps/<slug>` and `streamsnow check caching apps/<slug>` on the new files to catch denied schema refs and missing TTLs early.
7. Update §11 (Build Progress) in REQUIREMENTS.md to mark the new page scaffolded, then hand off: run /validate-app on `<slug>`, and /preview-app to eyeball the page locally.

## Done when

`pages/<page>.py` + its `queries/*.sql` exist with valid headers, the page is registered in `st.navigation`, the schema-refs and caching checks pass, and §11 reflects the new page.
