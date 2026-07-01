---
name: preview-app
description: Run a StreamSnow app locally against live Snowflake so the user can see it in the browser, wiring up secrets.toml first if missing. Use when the user says "preview my app", "preview <slug>", "run my app", "run my app locally", "let me see it in the browser", or after /new-app scaffolds an app.
---

# preview-app

Launch `apps/<slug>` locally against live Snowflake and open it in the browser. This is the "see it before you ship it" step: deploys are CI-driven (`streamsnow deploy-sql` / `deploy-setup` are for the deploy workflow, not local runs), so running the app locally and clicking through it is how the user verifies behavior before /validate-app and /ship-app.

`streamsnow preview <slug>` owns the deterministic parts — resolving the entrypoint, reading `.streamlit/secrets.toml`, and building the launch command. This skill owns the interactive UX around it: confirming the slug, provisioning secrets safely, launching in the background, surfacing the URL, and triaging startup errors.

## Steps

1. **Resolve the slug.** If the user named one, use it. If not, list `apps/*/` and ask which app — or use the app just scaffolded if you arrived here from /new-app. Confirm `apps/<slug>/` exists before going further.
2. **Verify prereqs once per session.** If you haven't already this session, run `streamsnow doctor`. If it reports anything missing (Python 3.11+, uv, snow CLI, streamlit), stop and hand off to /onboard rather than launching into a broken environment.
3. **Ensure `apps/<slug>/.streamlit/secrets.toml` exists and is real.** This file holds the local Snowflake connection under a `[connections.snowflake]` table. If it's missing or still contains placeholders:
   - Copy `secrets.toml.example` to `secrets.toml` if the example is present; otherwise create it from the `[connections.snowflake]` shape the example documents.
   - Fill the connection from `streamsnow.config.yaml` at the repo root — pull `account`, `user`, `role`, `warehouse`, `database`, and `authenticator`. Read a single value with `streamsnow config-get <dotted.path>` if you need to confirm one (e.g. the warehouse or database the app should run against).
   - If config values are missing or stale, run `streamsnow configure` to (re)populate `streamsnow.config.yaml`, then re-copy.
   - **Show the user the values you're about to write and get explicit confirmation before writing `secrets.toml`.** Credentials are theirs to own. Never invent an account, user, or password — fill only from config or what the user provides. `secrets.toml` is gitignored; never commit it.
4. **Pin the query role to the deployed viewer role.** Set `role` to the role the app actually runs as in Snowflake (from `snowflake.roles` / governance config), not a broad personal role. This is the single most valuable preview gotcha: a wide personal role hides missing grants, so the app looks fine locally and then ships with empty KPIs or blank dropdowns. Matching the deployed role makes grant gaps surface here.
5. **Launch in the background.** Run `streamsnow preview <slug>` with the Bash tool's background flag so Streamlit stays alive and you don't block. Add `--port N` if the default 8501 is busy. Use `streamsnow preview <slug> --dry-run` first if you want to show the exact command without launching.
6. **Tail the launch output and classify it.** Wait a few seconds, then read the background log:
   - Look for Streamlit's "Local URL" line — that's the URL to open. Open it **verbatim, at the root** (`http://localhost:<port>/`, no page path) — see Gotchas for why a deep link breaks multipage nav.
   - Watch for connection errors (auth/account/role), import errors (stale venv), or a query that fails on a grant. A grant failure is a deployed-role gap, not a local bug — note it and keep going.
7. **Report and invite click-through.** Give the user the URL and ask them to poke the app: click every page, confirm queries return real data, check charts render and tables are formatted. Flag any page that errors or renders empty. Tell them how to stop it (Ctrl+C in the Streamlit terminal, or "stop the preview").

## Runtime mode: container vs warehouse

How an app connects to Snowflake locally depends on its runtime, declared in `apps/<slug>/snowflake.yml`:

- **Container runtime** (the default for newly scaffolded apps) — uses `st.connection("snowflake")`, which reads `secrets.toml`. Runs locally under plain Streamlit as-is. This is the smooth path.
- **Warehouse runtime** (older / legacy apps) — uses `get_active_session()`, which only exists *inside* Snowflake and raises outside it. Such an app cannot run locally untouched. Options: apply the app's local-parity fallback (a `try get_active_session() / except st.connection("snowflake").query(...)` swap, often pre-written as a commented block in the scaffold), or verify it in the Snowsight editor instead.

If preview fails with a `get_active_session` / "no active session" traceback, that's the warehouse-runtime signature — point the user at the fallback swap rather than treating it as a code bug. **Do not auto-edit the swap**: it's a conscious local-dev toggle the developer owns, and it must be reverted before PR so the deployed (owner's-rights) path stays on `get_active_session()`.

## Gotchas

- **Always open the root URL, never a `/<page>` deep link.** Multipage apps declare navigation via `st.navigation` in `streamlit_app.py`. Entering at root runs the entrypoint so navigation engages; deep-linking a page makes Streamlit serve that file standalone (legacy auto-pages mode), skipping `st.set_page_config` and branding. Symptom: narrow/centered layout, missing sidebar logo, a flat list of raw file-stem names instead of the grouped menu. Open exactly the "Local URL" the launch printed.
- **Personal role masks grant gaps** (Step 4) — the bug ships silently if you preview as an admin/personal role. Always match the deployed viewer role.
- **Don't run deploys locally.** `streamsnow deploy-setup` and `streamsnow deploy-sql` emit DDL/SQL for the CI deploy workflow — they are not how you run an app on your machine. Preview is the only local-run path.
- **Account suffix.** If auth 404s, check that `account` in `secrets.toml` isn't double-suffixed with `.snowflakecomputing.com` — the connector appends it.
- **SSO auth.** Local runs typically need `authenticator = "externalbrowser"` so the browser SSO flow can complete; a non-interactive authenticator copied from CI won't prompt.

## Troubleshooting

- **Port already in use.** A previous preview probably didn't stop. Stop it (see below) and relaunch, or relaunch on another port with `streamsnow preview <slug> --port 8502` and update the URL you report.
- **Import / `connection` attribute errors.** The venv is stale or Streamlit is too old. Refresh deps with `uv sync` (or the project's documented sync) and relaunch.
- **Connection / auth failures.** Re-check `secrets.toml` against `streamsnow.config.yaml` (account, user, role, authenticator). Re-run `streamsnow configure` if the config itself is wrong, then re-copy secrets.
- **A query errors on a missing grant.** Treat as a deployed-role gap, not a local bug — the role needs the grant in Snowflake. Note it for the user and continue the walkthrough.
- **A Snowflake error you can't place** (cryptic SQL/role/warehouse message). `skills/_shared/deploy-error-translator.md` maps common Snowflake error signatures to plain-language causes and fixes; it's deploy-oriented, but the role/warehouse/grant diagnoses there often apply to a local connection or query failure too.
- **Any other traceback.** Print it verbatim and let the user drive the fix — their app code is theirs to debug. Don't guess at app-logic fixes.

## Optional: smoke walkthrough

If the user wants a hands-off "first 30 seconds of the app" instead of clicking through themselves, drive a Playwright browser across every page once the app is serving. Follow `skills/_shared/playwright-walkthrough.md` — it captures a screenshot and console errors per page and writes advisory artifacts under `apps/<slug>/.review/` (gitignored). If no Playwright MCP is loaded, it degrades to a one-line skip; never block on it. The walkthrough is advisory only — it never overrides /validate-app's deterministic PASS/FAIL.

## Hand-offs

- After /new-app scaffolds an app, this is the natural next step to see it live.
- Once the app runs cleanly and the user is happy with it, run /validate-app — the deterministic ship gate.
- For deeper qualitative concerns (SQL efficiency, UI patterns, spec drift), run /review-app or /auto-review-app.
- When validate passes, hand off to /ship-app to open the PR.

## Stopping the preview

When the user says "stop", "stop the preview", or "kill streamlit", terminate the background Streamlit process you launched and confirm it's stopped. If nothing is running, say so and move on.

## Done when

The app is serving locally at the reported root URL with live Snowflake data, queried as the deployed viewer role, with no startup errors — and the user has been pointed at /validate-app as the next step before /ship-app.
