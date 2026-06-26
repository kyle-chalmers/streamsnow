---
name: preview-app
description: Run a StreamSnow app locally against live Snowflake so the user can see it in the browser, wiring up secrets.toml first if missing. Use when the user says "preview my app", "preview <slug>", "run my app", "run my app locally", "let me see it in the browser", or after /new-app scaffolds an app.
---

# preview-app

Launch `apps/<slug>` locally against live Snowflake and open it in the browser.

## Steps

1. Confirm the slug (ask if not given); verify `apps/<slug>/` exists.
2. Run `streamsnow doctor` once if prereqs are unverified this session; if it fails, stop and have the user run /onboard.
3. Ensure `apps/<slug>/.streamlit/secrets.toml` exists. If not:
   - copy `secrets.toml.example` to `secrets.toml` if present, else create from the `[connections.snowflake]` shape the example documents;
   - fill the connection from `streamsnow.config.yaml` at the repo root (account, user, role, warehouse, database, authenticator);
   - if config values are missing or stale, run `streamsnow configure` to populate them, then re-copy.
4. Launch: `streamsnow preview <slug>` (add `--port N` if the default port is busy), running in the background.
5. Tail the launch output; surface the local URL and watch for connection or import errors. If a query fails on a grant, note it as a deployed-role gap and continue.
6. Report the URL and ask the user to click through; flag any page that errors or renders empty.

## Done when

The app is serving locally at the reported URL with live Snowflake data and no startup errors — then run /validate-app before /ship-app.
