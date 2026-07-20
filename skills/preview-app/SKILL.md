---
name: preview-app
description: Run an app locally against live Snowflake so the user can see it in the browser, wiring up secrets.toml first if missing. Use when the user says "preview my app", "run my app", "let me see it in the browser", or after /start-app scaffolds an app.
argument-hint: "<slug>"
allowed-tools: [Bash, Read, Write, Edit]
---

# /preview-app

Launch `apps/<slug>` locally against live Snowflake and open it in the browser — the "see it before
you ship it" step. Deploys are CI-driven, so clicking through locally is how the user verifies
behavior before /validate-app and /ship-app.

`streamsnow preview <slug>` owns the deterministic parts (entrypoint, `secrets.toml`, launch
command). This skill owns the interactive UX: confirming the slug, provisioning secrets safely,
launching in the background, surfacing the URL, and triaging startup errors.

## Steps

1. **Resolve the slug.** Named → use it; fresh from a `/start-app` scaffold → that app; otherwise
   list `apps/*/` and ask. Confirm `apps/<slug>/` exists.
2. **Verify prereqs once per session** with `streamsnow doctor`; anything missing → offer
   `/start-app --setup` rather than launching into a broken environment.
3. **Ensure `apps/<slug>/.streamlit/secrets.toml` exists and is real.** Missing or placeholder →
   copy `secrets.toml.example` and fill the `[connections.snowflake]` table from
   `streamsnow.config.yaml` (`streamsnow config-get <dotted.path>` reads one value). **Show the
   values and get explicit confirmation before writing** — credentials are the user's to own; never
   invent one, never commit the file (it's gitignored).
4. **Pin the query role to the deployed viewer role** (from config), not a broad personal role —
   the single most valuable preview gotcha. A wide personal role hides missing grants, so the app
   looks fine locally and ships with empty KPIs; matching the deployed role surfaces grant gaps here.
5. **Launch in the background:** `streamsnow preview <slug>` (background Bash; `--port N` if 8501
   is busy; `--dry-run` to show the command without launching).
6. **Tail the launch output and classify it.** Find Streamlit's "Local URL" line and open it
   **verbatim, at the root** — never a `/<page>` deep link (see Gotchas). Watch for auth errors,
   stale-venv import errors, or a grant failure (a deployed-role gap, not a local bug — note it,
   keep going).
7. **Report and invite click-through:** give the URL, ask the user to open every page, confirm
   queries return real data and charts render. Tell them how to stop the preview.

## Runtime note

How the app connects locally depends on its runtime — see
[_shared/runtime-decision.md](../_shared/runtime-decision.md). Container apps run locally as-is. A
warehouse app raising a `get_active_session` traceback is the runtime's signature, not a code bug:
point at the app's commented local-parity fallback (the developer owns that toggle and must revert
it before the PR), or verify in Snowsight.

## Gotchas

- **Always open the root URL.** Deep-linking a page serves that file standalone (legacy auto-pages
  mode), skipping `st.set_page_config` and branding — symptom: narrow layout, missing sidebar logo,
  raw file-stem names in a flat menu.
- **Account locator, not hostname:** auth 404s when `account` in `secrets.toml` carries a doubled
  `.snowflakecomputing.com` suffix — the connector appends it.
- **SSO:** local runs usually need `authenticator = "externalbrowser"`; a non-interactive
  authenticator copied from CI won't prompt.
- **A SQL edit that "has no effect":** hot-reload picks up `.py` changes but can serve cached
  results of the old SQL text — fully restart the preview before concluding a query change failed.
- **Don't run deploys locally** — `deploy-setup` / `deploy-sql` feed the CI workflow; preview is the
  only local-run path.

## Troubleshooting

- **Port in use** → stop the previous preview or relaunch with `--port 8502`.
- **Import / connection-attribute errors** → stale venv; `uv sync` and relaunch.
- **Auth failures** → recheck `secrets.toml` against config; re-run `streamsnow configure` if config
  itself is stale.
- **A cryptic Snowflake error** → [_shared/deploy-error-translator.md](../_shared/deploy-error-translator.md)
  maps common signatures to plain-language causes; its role/warehouse/grant diagnoses apply locally too.
- **Any other traceback** → print it verbatim and let the user drive the fix.

## Optional smoke walkthrough

For a hands-off pass, drive a Playwright browser across every page per
[_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md) — screenshot + console
errors per page, advisory only, silent skip without the MCP.

## Done when

The app serves at the reported root URL with live data, queried as the deployed viewer role, with no
startup errors — next step /validate-app, then /ship-app. On "stop the preview", terminate the
background process and confirm.
