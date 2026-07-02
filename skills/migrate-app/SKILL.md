---
name: migrate-app
description: Port an external Streamlit app into the repo in two reviewable steps — lift-and-shift into apps/<slug>/, then conform to repo conventions until validation passes. Use when the user says "migrate a streamlit app", "port this dashboard into the repo", "bring an external app in", or "modernize this dashboard".
---

# /migrate-app

Bring an external Streamlit app into the repo, then conform it to StreamSnow conventions until the
validation check passes. **Two commits** — the lift (files relocated, only ship blockers scrubbed),
then the conform diff — because mixing the move and the rewrite makes review impossible and kills
your ability to bisect a misbehaving conform against the known-good lift.

## Step 1 — lift-and-shift (get it in the tree)

1. `streamsnow doctor` first; failures → `/start-app --setup` before continuing.
2. Settle `<slug>` (`<domain>-<function>`, kebab-case); confirm the source path and that
   `apps/<slug>/` doesn't already exist.
3. Copy the source in. The one structural change: rename the entrypoint to `streamlit_app.py` (not
   configurable). Preserve relative imports; no refactoring yet.
4. Scrub only ship blockers: hardcoded **secrets** out of `.py` (never read or commit a source
   secrets file — the user copies values by hand into gitignored `secrets.toml`), and
   **denied-schema references** re-pointed to governed equivalents
   (`streamsnow check schema-refs apps/<slug>` flags them).
5. Commit the lift as one changeset.

## Step 2 — conform (make it a StreamSnow app)

6. Read `streamsnow.config.yaml` (allowlist, governance database, default runtime). **Decide the
   runtime with the user** per [_shared/runtime-decision.md](../_shared/runtime-decision.md) —
   it drives the manifest, connection pattern, and which packages even exist; check Anaconda-channel
   availability before assuming warehouse deps are present.
7. Source the canonical local helpers (`branding.py`, `sql_loader.py`) from a scratch
   `streamsnow new` or a sibling app. Always `from branding import ...` — never `from shared...`;
   the deployed runtime can't reach a repo-level `shared/`, so a shared import runs locally and
   fails at deploy.
8. Externalize every UI-feeding query into `queries/<name>.sql` with the required header block
   (`Query / Feeds / Schemas / Params / Tokens`), loaded via `load_sql`/`render_sql`. Only
   plumbing/discovery SQL stays inline. **Walk header candidates with the user one at a time** —
   the Feeds/Schemas mapping can't be guessed from code alone.
9. Swap connections to the chosen runtime pattern; wrap every data fetch in
   `@st.cache_data(ttl=...)`; pass filters as function arguments, not closures.
10. Replace optional `(:N IS NULL OR col = :N)` predicates with `{TOKEN}` fragments via
    `render_sql` — deployed, the driver NULL-binds every parameter when any one is `None`, so an
    "All" filter silently zeroes results (`streamsnow check bind-predicates` catches it).
11. Write `snowflake.yml` for the chosen runtime; add an app `AGENTS.md` noting non-default TTLs
    and the runtime decision. Dependencies: container → `pyproject.toml` (PyPI); warehouse →
    `environment.yml` (channel names, never pin `python`). No source manifest → infer candidates
    from imports but confirm each with the user; never auto-add a guess.
12. Iterate with the focused checks (`streamsnow check schema-refs|security|caching|bind-predicates
    apps/<slug>`); scrub personal absolute paths and machine-specific config the copy brought along.
13. Preview via /preview-app so the user confirms each page still renders.
14. Gate: `streamsnow validate-app <slug>` until PASS, then commit the conform pass as its own
    changeset.

## Gotchas

- **`SELECT *` in externalized SQL:** pin explicit columns; if you can't enumerate them without a
  live session, defer with a note — never hallucinate column names (`/audit-lineage` can fetch the
  real list).
- **Local run fails but deployed works (warehouse):** expected — `get_active_session()` raises
  outside Snowflake; use the local-parity fallback or verify in Snowsight.
- **Manifest validation FAILs:** the message names the offending field — re-check the
  manifest-vs-runtime pairing.
- **A deploy error you can't place:**
  [_shared/deploy-error-translator.md](../_shared/deploy-error-translator.md); a first-time account
  may need one-time `streamsnow deploy-setup` DDL.

## Hand-offs

PASS → /ship-app opens the PR. Quality depth beyond the gate → /review-app (and /audit-lineage for
live-number fidelity). Refactoring an app already in the repo (not an external port) → skip Step 1
and run the conform work + gate directly.

## Done when

`apps/<slug>/` holds the conformed app — local helpers, headered `queries/*.sql`, a
runtime-matching `snowflake.yml` and connection pattern, every fetch cached, no denied-schema or
bind-predicate findings — `streamsnow validate-app <slug>` passes, and the lift and conform are two
separate commits.
