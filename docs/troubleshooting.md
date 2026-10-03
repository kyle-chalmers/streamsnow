# Troubleshooting

Numbered issues in a fixed **Symptom / Cause / Fix** shape, so you can scan the
symptoms for yours. Each entry names the check or command that would have
caught it. Runtime-specific facts link to the official page in
[Official Snowflake docs, by topic](snowflake-docs.md).

## First run

### 1. `streamsnow: command not found`

- **Symptom:** the plugin's `/start-app --setup` or a doc says to run
  `streamsnow …` and the shell cannot find it.
- **Cause:** the CLI was never installed (`uvx streamsnow …` runs are one-shot),
  or `uv tool install` put it in a bin dir that is not on PATH yet.
- **Fix:** `uv tool install streamsnow`, then re-open the shell. `/start-app
  --setup` does this for you and refuses to continue until the command works.

### 2. `pre-commit` fails on the first commit with "executable not found"

- **Symptom:** `git commit` in a fresh scaffold errors on the first hook.
- **Cause:** the generated hooks are `language: system` and call
  `streamsnow check …`; `pre-commit` or `streamsnow` is not installed.
- **Fix:** `uv tool install pre-commit streamsnow && pre-commit install`.
  `streamsnow doctor` reports `pre-commit` as **required** once a config exists.

### 3. Preview says "Local runs need a Snowflake connection"

- **Symptom:** `streamsnow preview start <slug>` exits 1 with the connection hint.
- **Cause:** no default `snow` connection and no per-app `secrets.toml`.
  `st.connection("snowflake")` reads `[connections.snowflake]` from
  `secrets.toml` first, then the `snow` CLI's default connection in
  `connections.toml` ([configure connections](https://docs.snowflake.com/en/developer-guide/snowflake-cli/connecting/configure-connections)).
- **Fix:** `snow connection add --connection-name <name> --account <locator>
  --user <you> --authenticator externalbrowser --default` (the exact command is
  in `streamsnow configure`'s output). `streamsnow doctor` shows whether the
  configured connection name exists.

### 4. Auth fails with a doubled `.snowflakecomputing.com`

- **Symptom:** the log shows `<acct>.snowflakecomputing.com.snowflakecomputing.com`
  or error 250001.
- **Cause:** the full hostname was used where the connector expects the account
  **locator**.
- **Fix:** `account = "ab12345.us-east-1"`, no `https://`, no suffix.

### 5. Preview runs the wrong Python / `ModuleNotFoundError: streamlit`

- **Symptom:** `uv venv && uv pip install -e apps/<slug>` succeeded, but preview
  cannot import the app's dependencies.
- **Cause:** `uv venv` does not activate the environment, so a bare `streamlit`
  resolves elsewhere.
- **Fix:** none needed since 0.7 — `streamsnow preview` prefers `<repo>/.venv/bin/streamlit`
  when it exists. On older versions, activate the venv first.

### 6. `get_active_session() is not supported outside of Snowflake`

- **Symptom:** a warehouse-runtime app crashes on local preview.
- **Cause:** that is the warehouse runtime's signature, not a code bug —
  `get_active_session()` only exists inside Snowflake, and it is not thread-safe
  on the container runtime either ([secrets and configuration](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/secrets-and-configuration)).
- **Fix:** wrap the call in a broad `try/except` with an `st.connection("snowflake")`
  fallback (`streamsnow check session-fallback`), or switch the app to the
  container runtime.

### 7. A SQL edit "has no effect" in preview

- **Symptom:** you change a `queries/*.sql` file, the page reloads, the data is
  unchanged.
- **Cause:** hot-reload picks up `.py` edits; cached query results keyed on the
  loader's arguments do not see the new SQL text.
- **Fix:** `streamsnow preview stop <slug>` then `start`.

### 17. Preview crashes with `TypeError: Expected bytes, RSAPrivateKey, ... got NoneType`

- **Symptom:** `streamsnow preview start` reports the app serving, and the first
  page load fails with that `TypeError` from
  `snowflake/connector/auth/keypair.py`, while `snow sql` works on the same
  connection.
- **Cause:** the default `snow` connection uses key-pair auth and names its key
  `private_key_path`. The `snow` CLI accepts that name as a legacy alias; the
  Python connector behind `st.connection("snowflake")` does not, drops it
  without a warning, and reaches key-pair auth with no key. The connector reads
  `private_key_file` and `private_key_file_pwd`
  ([Python connector: connecting](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-connect)),
  and `snow` reads `private_key_file` first
  ([configure connections](https://docs.snowflake.com/en/developer-guide/snowflake-cli/connecting/configure-connections)).
- **Fix:** rename `private_key_path` to `private_key_file` in that connection's
  entry (a passphrase goes in `private_key_file_pwd`), then restart the preview.
  `streamsnow doctor` warns about it as `snow-key-file`, and `streamsnow
  preview logs <slug>` prints the same fix under the traceback.

### 18. `uv pip install -e apps/<slug>` fails: "Multiple top-level packages discovered"

- **Symptom:** the local install for a container app stops with
  `error: Multiple top-level packages discovered in a flat-layout: ['pages',
  'queries', 'sql_review']`.
- **Cause:** the app's `pyproject.toml` lists dependencies but no packages, so
  setuptools' automatic discovery finds several top-level directories and
  refuses to guess. Apps scaffolded before this was fixed carry that file.
- **Fix:** add an empty package list to `apps/<slug>/pyproject.toml` (an app is
  never built as a package; the install only needs its dependencies), then
  re-run the install:
  ```toml
  [tool.setuptools]
  packages = []
  ```

## Validate and review

### 8. `artifacts` fails on `.streamlit/config.toml` although your pipeline uploads it

- **Symptom:** `validate-app` says the file "exists on disk but no artifacts
  entry covers it", and your deploy step ships it separately.
- **Cause:** the check demands every deployable file be listed unless config
  says otherwise.
- **Fix:** `deploy.artifact_exclude: [".streamlit/config.toml"]` in
  `streamsnow.config.yaml`. Only non-code files are excludable; `streamlit_app.py`,
  `pages/`, `queries/`, `*.py` and `*.sql` are always artifacts.

### 9. `requirements` rejects `**Current phase:** build (pages 3/5)`

- **Symptom:** the §11 phase line carries progress narrative and fails.
- **Cause:** `/start-app` resumes on the exact phase value; narrative makes it
  ambiguous.
- **Fix:** `**Current phase:** build` plus a `**Phase notes:** pages 3/5` line.

### 10. `sql-review check` fails on queries with no companion

- **Symptom:** every `queries/*.sql` without a manifest is a FAIL.
- **Cause:** `sql_review.coverage: fail` in config, or the repo is on 0.6, where
  coverage always failed the standalone check.
- **Fix:** set `sql_review: {coverage: warn}` while backfilling; drift, hand
  edits, unbound binds and write statements still fail regardless.

### 11. `environment.yml` rejected for pinning `python`

- **Symptom:** `validate-app` fails a warehouse app's manifest on a `python=…`
  line the official example shows.
- **Cause:** a pinned interpreter failed `CREATE STREAMLIT` in production
  (`Packages not found: python==3.11`); StreamSnow keeps the stricter rule
  ([dependency management](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/dependency-management)).
- **Fix:** remove the line; the runtime supplies Python.

## Deploy

### 12. Works locally, `does not exist or not authorized` deployed

- **Symptom:** a query renders in preview and 403s in the deployed app.
- **Cause:** deployed apps run with **owner's rights** — the CI role's grants,
  not yours ([owner's rights](https://docs.snowflake.com/en/developer-guide/streamlit/object-management/owners-rights)).
- **Fix:** grant the CI role `SELECT` (prefer `FUTURE` grants per schema), or
  expose the fact through a narrow passthrough view. See
  [production lessons](production-lessons.md).

### 13. Container app healthy but never serves; `No such option` in the log

- **Symptom:** `verify-deploy` reports a restart loop.
- **Cause:** the app pins a `streamlit` older than the base image expects.
- **Fix:** raise the pin to at least the image's version and redeploy.

### 14. Renamed an app, the old one is still live

- **Symptom:** `verify-deploy` flags an orphan after a `git mv`.
- **Cause:** a `CREATE OR REPLACE` pipeline has no delete path; the slug is the
  object identity.
- **Fix:** add the old identifier to `deploy/tombstones.yml` in the same PR.

### 15. Git-repository deploy shows stale or no content

- **Symptom:** the app opens blank after a fetch.
- **Cause:** a Streamlit created `FROM @repo/…` needs `ADD LIVE VERSION FROM
  LAST`; repositories over 2 GB are unsupported ([Git overview](https://docs.snowflake.com/en/developer-guide/git/git-overview)).
- **Fix:** `streamsnow deploy-sql <slug> --refresh` emits the refresh statements.

### 19. Every CI deploy fails with `Connection default is not configured`

- **Symptom:** the deploy workflow's first `snow` step fails although all the
  `SNOWFLAKE_*` secrets are set.
- **Cause:** the runner has no Snowflake CLI `config.toml`, and a `snow` call
  without `--temporary-connection` looks for a connection named `default`
  instead of reading the `SNOWFLAKE_*` variables
  ([temporary connections](https://docs.snowflake.com/en/developer-guide/snowflake-cli/connecting/configure-connections#use-a-temporary-connection)).
  `deploy.yml` files generated before 0.7.1 omit the flag.
- **Fix:** `uv tool upgrade streamsnow`, then `streamsnow update --apply` and
  commit the re-rendered `.github/workflows/deploy.yml`.

## Plugin

### 16. Overlays or the Stop hook seem inert

- **Symptom:** a `.streamsnow/overlays/<skill>.md` is ignored, or no review
  nudge ever fires.
- **Cause:** the installed plugin predates the feature; installed copies do not
  pick up hook or skill changes on their own. The SessionStart line prints the
  installed version.
- **Fix:** `claude plugin uninstall --scope project streamsnow@streamsnow`,
  `claude plugin install --scope project streamsnow@streamsnow`, then run
  `/reload-plugins` in Claude Code (no restart needed).

## Adding new issues

Append a numbered entry in the same Symptom / Cause / Fix shape under the right
heading, name the check or command that catches it, and link the official
Snowflake page via [snowflake-docs.md](snowflake-docs.md) if the cause is a
platform behavior. If the cause is a StreamSnow defect, fix the layer that was
insufficient (check, skill, template) in the same PR and cite the entry from
`docs/production-lessons.md`.
