---
name: preview-app
description: Run an app locally against live Snowflake so the user can see it in the browser, wiring up secrets.toml first if missing. Use when the user says "preview my app", "run my app", "let me see it in the browser", or after /start-app scaffolds an app.
argument-hint: "<slug>"
allowed-tools: [Bash, Read, Write, Edit]
---

# /preview-app

> **Repo overlay:** if `.streamsnow/overlays/preview-app.md` exists in this repo, read it first — committed, repo-specific additions/overrides ([_shared/overlays.md](../_shared/overlays.md)).

Launch `apps/<slug>` locally against live Snowflake and open it in the browser — the "see it
before you ship it" step, ahead of /validate-app and /ship-app. `streamsnow preview` owns the whole
lifecycle — detached launch, health polling, log capture, classified failures, teardown — so never
hand-roll `streamlit run` + `nohup` + PID bookkeeping. This skill owns the interactive UX: slug
confirmation, safe secrets provisioning, URL surfacing, triage.

## Lifecycle verbs

- `streamsnow preview start <slug>` (bare `streamsnow preview <slug>` is shorthand for `start`) —
  verifies the entrypoint and port, launches `streamlit run` detached with output to a log, then
  polls the health endpoint until it answers or the timeout expires. `--port N` if 8501 is busy;
  `--json` for structured output. Exit 0 = serving; on exit 1 the log tail was **classified**
  (missing `secrets.toml`, bad account locator, missing package, port collision,
  session-outside-Snowflake) into an actionable hint — read it before improvising.
- `streamsnow preview status <slug>` — running/not-running + live health probe; stale state from a
  crashed preview is cleaned up silently, so a dead run never wedges the next `start`.
- `streamsnow preview logs <slug> [--lines N]` — tail the launch log (kept after `stop`).
- `streamsnow preview stop <slug>` — graceful kill, state removed; idempotent.

"Healthy" means **serving**, not "queries succeeded" — the health endpoint answers before the
first script run finishes; data errors surface in the browser and in `logs`, not in `start`.

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
   a wide role hides missing grants, so the app looks fine locally and ships with empty KPIs.
5. **Launch:** `streamsnow preview start <slug>`. Success prints the URL — open it **verbatim, at
   the root** (see Gotchas). Failure → relay the classified hint; only fall back to
   `streamsnow preview logs <slug>` triage when the classifier had no match.
6. **Report and invite click-through:** give the URL, ask the user to open every page, confirm
   queries return real data and charts render. Tell them `streamsnow preview stop <slug>` ends it.

## Gotchas

- **Always open the root URL.** Deep-linking a page serves that file standalone (legacy auto-pages
  mode), skipping `st.set_page_config` and branding — symptom: narrow layout, missing sidebar logo,
  raw file-stem names in a flat menu.
- **Warehouse-runtime apps:** a `get_active_session` traceback locally is the runtime's signature,
  not a code bug — see [_shared/runtime-decision.md](../_shared/runtime-decision.md); point at the
  app's commented local-parity fallback (revert before the PR) or verify in Snowsight.
- **A SQL edit that "has no effect":** hot-reload picks up `.py` changes but can serve cached
  results of the old SQL text — `streamsnow preview stop <slug>` then `start` before concluding a
  query change failed.
- **Don't run deploys locally** — `deploy-setup` / `deploy-sql` feed the CI workflow; preview is the
  only local-run path. `.streamsnow/` (preview state + logs) belongs in `.gitignore`.

## Troubleshooting

- **Auth failures** → recheck `secrets.toml` against config (locator not hostname; SSO usually needs
  `authenticator = "externalbrowser"`); re-run `streamsnow configure` if config itself is stale.
- **Import errors in the log** → stale venv; `uv sync` and relaunch.
- **A cryptic Snowflake error** → [_shared/deploy-error-translator.md](../_shared/deploy-error-translator.md)
  maps common signatures to causes; its role/warehouse/grant diagnoses apply locally too.

## Optional smoke walkthrough

For a hands-off pass, enumerate the app's pages with `streamsnow nav <slug>` (one JSON object per
page — title, path, group; `--json-array` for one payload) and drive a Playwright browser across
each per [_shared/playwright-walkthrough.md](../_shared/playwright-walkthrough.md) — advisory only,
silent skip without the MCP. The nav list is the loop input; never guess page URLs from filenames.

## Done when

The app serves at the reported root URL with live data, queried as the deployed viewer role, with
no startup errors — next step /validate-app, then /ship-app. On "stop the preview", run
`streamsnow preview stop <slug>` and confirm.
