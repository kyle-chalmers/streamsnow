# CLI reference

Every `streamsnow` command, its flags, and what the governance checks return. The
skills and CI call these same commands, so this page is also what they do under the
hood. Run any command with `--help` for the same text in your terminal.

Command and flag names are part of the stable surface
([Versioning and stability](versioning.md)); `tests/test_cli_surface.py` pins them.

**Jump to:** [Setup](#setup) · [Apps](#apps) · [validate-app](#validate-app) ·
[Checks](#checks) · [SQL review](#sql-review) · [Review](#review) ·
[Migrate](#migrate) · [Deploy](#deploy) · [CI key](#ci-key) · [Other agents](#other-agents)

## Conventions

- **Exit codes.** Every check and gate exits `0` pass, `1` finding, `2` tool error
  (bad config, unreadable file, a tool that could not run).
- **`--format md|json`.** Checks default to `md` (human-readable). `json` prints one
  object; its keys are public and only ever gain new keys.
- **Paths.** Checks take files or directories and default to `apps/`.
- **Config.** Commands find `streamsnow.config.yaml` by walking up from the current
  directory; `--config <path>` points at another one.

## Setup

| Command | What it does |
|---|---|
| `streamsnow doctor` | Checks the machine and repo for what StreamSnow needs: required `python`, `git`, `uv`; optional `snow`, a `snow` connection, `gh`, `pre-commit` and its hook, `node` (the browser tool), the config, repo files, git identity and CI secrets. Each sub-check reports `{name, ok, level, detail, hint}`; `level` is `required` or `optional`. `--format json` (or `--json`) prints `{"ok", "checks": [...]}`. Exits `1` only when a required check fails. |
| `streamsnow configure` | Writes or edits `streamsnow.config.yaml` with a five-question wizard. Re-running prefills from the current file. |
| `streamsnow init` | `configure`, then the governed repo files, then a starter app. |
| `streamsnow update` | Re-renders the governance files from your config and the installed templates. Dry run unless `--apply`. |

**Wizard answer flags** (shared by `configure` and `init`; giving all five skips the
prompts): `--runtime container|warehouse`, `--account <locator>` or
`--connection <snow connection>`, `--database`, `--schemas` (comma-separated
`schema_allow`), `--deploy-source stage-copy|git-repository`, plus optional
`--deny-schemas` (default `RAW,STAGING`; `''` denies none).

| Flag | Command | Meaning |
|---|---|---|
| `--dir` | `configure`, `init`, `update` | Repo directory (default `.`) |
| `--config` | `configure`, `init` | Import an existing config file |
| `--app <slug>` | `init` | Starter app slug (default `example-dashboard`) |
| `--no-starter-app` | `init` | Repo files only, no example app (what `/onboard` runs) |
| `--force` | `init` | Overwrite existing scaffold files |
| `--reconfigure` | `init` | Re-run the wizard although a config exists (needed with answer flags on an existing config) |
| `--apply` | `update` | Write the changes (default is a dry run) |

**What `init` writes:** `streamsnow.config.yaml`, `AGENTS.md`, `CLAUDE.md`,
`.pre-commit-config.yaml`, the CI and deploy workflows, `.sqlfluff`, `.gitignore`,
`README.md`, `deploy/tombstones.yml`, `osv_allowlist.json` (warehouse runtime only),
and the starter app. **What `update` re-renders:** `AGENTS.md`, `CLAUDE.md`,
pre-commit, CI and the deploy workflow. It leaves `README.md`, `.gitignore`,
`deploy/tombstones.yml`, `.sqlfluff` and `osv_allowlist.json` alone (it creates the
last two if they are missing).

## Apps

| Command | What it does |
|---|---|
| `streamsnow new <domain> <function>` | Scaffolds `apps/<domain>-<function>/`. `--force` overwrites. |
| `streamsnow preview start <slug>` | Runs the app locally against live Snowflake in the background and polls its health endpoint. `--port` (`0` picks any free port), `--timeout` (seconds, default 60), `--review-capture DIR` (review preview mode for `/sql-review`: `review_value` records what each visual received, aggregates only, in DIR). A bare `streamsnow preview <slug>` means `start`. |
| `streamsnow preview status <slug>` | Is it running, and where. |
| `streamsnow preview logs <slug>` | The last lines of its log (`--lines`). |
| `streamsnow preview stop <slug>` | Stops it. |
| `streamsnow nav <slug>` | Lists the app's pages in navigation order, one JSON object per line (`--json-array` for one array). Handles `st.navigation`, single-page and legacy `pages/` apps. |

All `preview` verbs take `--dir` and `--json`.

## validate-app

`streamsnow validate-app <slug>` is the deterministic ship gate: PASS or FAIL for one
app, offline (no Snowflake, no network). Flags: `--dir`, `--config`, `--format md|json`.
JSON is `{"app", "runtime", "ok", "checks": [{"name", "ok", "findings"}]}`; the sql-review
entry also carries `warnings` (coverage under `warn`, and advisories), and the
`required-files`, `manifest` and `naming` findings are plain strings. It runs, in order:

| Step | What fails it |
|---|---|
| `required-files` | A file every app needs is missing |
| `manifest` | `snowflake.yml` is invalid or disagrees with the config; `pyproject.toml` (container) or `environment.yml` (warehouse) lacks required content, or `environment.yml` pins `python` |
| `artifacts` | `snowflake.yml` `artifacts:` disagrees with the files on disk |
| `naming` | The slug does not match `^[a-z][a-z0-9-]*$` |
| `schema-refs` | A query or SQL string names a denied schema |
| `app-security` | Egress, code execution, write SQL or dynamic SQL in app code (the `check security` rules) |
| `bind-predicates` | The `:N IS NULL OR` bind trap |
| `sql-tokens` | A `{TOKEN}` inside a SQL comment |
| `session-fallback` | `get_active_session()` without a broad `try/except` (whole app, not only new calls) |
| `page-imports` | An import that works under `streamlit run` but not deployed |
| `caching` | A data-fetching function without `@st.cache_data(ttl=...)` |
| `path-leaks` | A personal absolute path in committed code or docs |
| `requirements` | `REQUIREMENTS.md` §11 build state is malformed (see the `check requirements` row below) |
| `sql-review (coverage policy: warn\|fail)` | The `sql-review check` finding kinds `index`, `provenance`, `marker`, `objects`, `lint`, `comments`, `readonly` and `bind`; uncovered pages or queries (`coverage`) fail only under `fail` |
| `placeholders` | The starter's `YOUR_TABLE` or sample numbers are still there |
| `starter-text` | Warn-only. The app `AGENTS.md` still has the scaffold's starter lines, or the repo README's Apps table has no row for the app |

A freshly scaffolded app **fails** `placeholders` on purpose until you repoint the
starter query and review window; every other step passing is what proves the scaffold
is whole.

## Checks

`streamsnow check <name> [paths]...` runs one check. Every check takes
`--format md|json` and prints `{"ok": bool, "findings": [...]}` in JSON, plus the
extra keys listed below.

| Check | Blocks | Extra flags | Extra JSON keys |
|---|---|---|---|
| `schema-refs` | References to denied schemas (`governance.schema_deny`, minus exact `read_exceptions`) | `--config` | |
| `security` | Egress, code execution, write SQL, dynamic SQL in app code. A DDL file directly in an app's `sql_review/app_specific_reporting_objects/` may use `CREATE`, `ALTER` and `GRANT` | | |
| `caching` | Data-fetching functions without `@st.cache_data(ttl=...)` | | |
| `bind-predicates` | The `:N IS NULL OR` Go-driver bind trap | | |
| `sql-tokens` | `{TOKEN}` placeholders inside SQL comments (`render_sql` would substitute them) | | |
| `session-fallback` | `get_active_session()` without a broad `try/except` | `--base-ref` (default `origin/main`), `--all` | |
| `page-imports` | Imports that resolve under `streamlit run` but not deployed | | |
| `artifacts` | `snowflake.yml` `artifacts:` out of sync with files on disk | `--fix` repairs it | `fixed` (with `--fix`) |
| `path-leaks` | Personal absolute paths (home directories) | | |
| `requirements` | A malformed `REQUIREMENTS.md` §11 build-state block: `Current phase` must be one of `spec`, `discover`, `design`, `scaffold`, `build`, `preview`, `verify`, `ship`, `done`, `in-production`, `in-production (backfilled)`, and the last Sessions line must start with an ISO timestamp and, for an unfinished build, name the `Next:` step | | |
| `branding-parity` | `_BRANDING_VERSION` skew across apps' `branding.py` copies | | |
| `dependency-vulns` | Exact dependency pins with known vulnerabilities (OSV.dev); range pins are reported unscanned | `--allowlist` (default `osv_allowlist.json` next to the config), `--best-effort` (warn when OSV.dev is unreachable) | `unscanned`, `allowlisted`, `expired`, `checked` |
| `tombstones` | A PR that renames or removes an app without adding it to `deploy/tombstones.yml` | `--base-ref` (default `origin/main`), `--registry`, `--apps-dir`, `--config`, `--drop-sql` (print `DROP STREAMLIT IF EXISTS` for tombstoned apps; the deploy job runs it) | `tombstones`, `notes` |

**`session-fallback` is new-only by default:** it flags only calls added since
`--base-ref`, so an adopting repo is not failed for legacy debt. `--all` scans
everything; `validate-app` always scans the whole app.

## SQL review

| Command | What it does |
|---|---|
| `streamsnow sql-review generate <slug>` | Writes one runnable file per page, `sql_review/NN_<page>.sql`, from `sql_review/index.yaml`. `--dir`. |
| `streamsnow sql-review check [<slug>]` | Fails on an invalid index, drift or hand edits, `review_value` marker mismatches, DDL folder disagreements, lint, missing CTE comments, and sections that would write or don't run; uncovered pages or queries fail or warn per `sql_review.coverage`. No slug checks every app. `--dir`, `--format md\|json`, `--lint-files <file>...` (lint only these; pre-commit passes the staged files). |

The live review (the `/sql-review` skill drives these; each needs a `snow` connection):

| Command | What it does |
|---|---|
| `streamsnow sql-review probe <slug>` | Checks every object the index names exists, has a direct grant to the review role, and (for views in `app_specific_reporting_objects/`) matches the live definition; compiles every section and reports its columns. Starts a run and prints its `run_id`. |
| `streamsnow sql-review run <slug>` | Runs every section wrapped in an aggregate: row count, a total per numeric column, an order-insensitive hash, timing. Never fetches rows. `--page NN`, `--slow-s N` (default 10). |
| `streamsnow sql-review bench <slug> --metric NN#n` | Times one section with the result cache off (median of `--runs`, default 3): elapsed time, bytes and partitions scanned. `--sql-file F` benchmarks a candidate rewrite of the query against it and reports `equivalent`. |
| `streamsnow sql-review compare <slug>` | Holds what each visual received in review preview mode (`preview start --review-capture`) to its `run` result: `match`, `mismatch`, `not_captured` or `unsupported` per metric, within 0.5% or the displayed rounding, integers exactly. Reads the run's `capture/` (`--capture DIR`) and, when present, its `screen.json` from a browser walk (`--screen F`, a cross-check only). Needs no connection. |
| `streamsnow sql-review log <slug> --findings F` | Writes `sql_review/review_log/YYYY-MM-DD_<sha>.md` from verified findings and links it from the README; refuses a finding whose evidence is not a result in the run. `--dry-run` validates and writes nothing. |

`probe`, `run` and `bench` share `--run <id>|latest` (default: a new run), `--connection`
(default `snowflake.connection_name`), `--role` (default `snowflake.roles.ci_role`),
`--warehouse` (default `objects.default_warehouse`) and `--timeout` (statement timeout in
seconds, default 120); `log` takes `--run` (default `latest`). All take `--dir`. They print JSON,
write it to `.streamsnow/sql-review/<slug>/<run_id>/` (ignored by git), and refuse non-read-only
SQL and governance-denied schemas before anything is sent. Exit codes: `0` every check passed,
`1` a check failed (or `log` refused the findings), `2` nothing ran.

See [Auditing a visual](auditing-a-visual.md) for the file format and the live review.

## Review

| Command | What it does |
|---|---|
| `streamsnow review-gate classify [<slug>]` | Does this change need a review before shipping? Classifies each changed app's diff as trivial or needing the review loop (no slug: every changed app). `--base-ref`, `--format md\|json`. `/ship-app` runs it. See "Reading `classify`" below. |
| `streamsnow review-gate baseline <slug>` | Prints the app's current baseline digest. |
| `streamsnow review-gate stamp <artifact> --slug <slug>` | Writes or refreshes the `Reviewed-baseline`, `Reviewed-files` and `Reviewed-head` lines in a review artifact (`--base-ref`). `--expect-baseline <digest>`: the `baseline` captured when the review was dispatched; if the app has changed since, it exits 2 ("app changed since dispatch") and writes nothing. |
| `streamsnow review-gate stop-hook` | The plugin's warn-only Stop-hook nudge (`--payload both\|system-only`). |
| `streamsnow review-loop <verb>` | Deterministic bookkeeping for `/review-app --auto`: `parse-findings`, `dedup-findings`, `merge-findings`, `write-resolutions`, `exit-condition`. Called by the skill, not by hand. |
| `streamsnow review-loop open-findings <dir>` | Counts the findings a review left open in the newest report under `<dir>` (`--report <file>` picks one), after subtracting those the same report's `### Applied` block records as fixed. Applied blocks in other reports do not count, so a finding re-flagged after an older fix stays open. Prints `counts` per severity, the open BLOCK list, `parsed`, and `stamped` (the report carries a `Reviewed-baseline` line); a report it cannot parse gives `parsed: false` and null counts, never 0. Exits 2 when there is no report. `/ship-app` writes the open BLOCK count into the PR body only when the counted report is the stamped one and the change needs no review. |

**Reading `classify`.** `needs_review` is the decision: true means a substantive change has no
review covering its current content and no skip marker. `verdict` is review depth only: `loop`
means the diff is substantive enough for the full review loop, and it stays `loop` after a review
covers it. Gate on `needs_review`, never on `verdict`.

Each app in the JSON also carries `reviewed_head` (the commit the newest stamp recorded),
`reviewed_head_status` (`none` when no stamp recorded one, `ancestor` when it is in the current
history, `not-ancestor` after a rebase or amend) and `commits_since_review` (`[{sha, subject}]`,
oldest first, for commits that touched the app after that head; empty unless the status is
`ancestor`, so check the status before reading an empty list as "none").

The default `/review-app` pass stamps its report with `--expect-baseline`, and `--auto` stamps once
at the end of its loop. A stamp means the code was reviewed, not that it is clean: it is written
even when critical findings are open.

## Migrate

`streamsnow migrate <verb>` is the engine behind `/migrate-app`. Each verb prints
JSON for the skill to act on.

| Verb | Arguments | What it does |
|---|---|---|
| `preflight` | `<source> --target-slug <slug> [--dir]` | Is the source safe to migrate into that slug |
| `scan-hardfails` | `<source> [--config]` | Denied-schema references and hardcoded secrets in the source |
| `translate-deps` | `<source> --out <file> [--offline]` | Translates the source's dependencies into a warehouse-runtime `environment.yml` |
| `graft-plan` | `<source>` | Where to graft the source's entrypoint (`pages/*`, `pages/overview.py` or `streamlit_app.py`) |
| `scan-imports` | `<source>` | Relative imports and nested `__init__.py` files |
| `scan-conformance` | `<app_path> [--config]` | Uncached queries, `SELECT *`, altair imports, legacy `pages/` layouts, grants |
| `scan-inline-sql` | `<app_path>` | Inline SQL literals that belong in `queries/*.sql` |

## Deploy

| Command | What it does |
|---|---|
| `streamsnow deploy-setup` | Prints the one-time Snowflake DDL for your deploy source; review it, then run it. Never runs anything itself. |
| `streamsnow deploy-sql <slug>` | Prints the `CREATE OR REPLACE STREAMLIT` SQL for one app (`--sha`, `--config`). The deploy job runs it. |
| `streamsnow verify-deploy <slug>` | Checks that a deployed app actually serves: the object exists, a live version is set, the version source matches `--sha`, and container logs show no crash loop. A check that cannot run is reported as skipped, never as a pass. With stage-copy and `--sha`, the warn-only `stage-files` check also compares the staged files, file by file, with what `stage-bundle` would ship for the app. |
| `streamsnow config-get <key>` | Prints one config value by dotted path, e.g. `deploy.git_repository_fqn`. |
| `streamsnow stage-path` | Prints the stage-copy base path, `@DB.SCHEMA.STAGE`. |
| `streamsnow stage-bundle --out <dir> [<slug>...]` | Copies each app (default: every app under `apps/`) into `<dir>/<slug>/` without root-level docs an `artifacts:` entry does not declare, `sql_review/`, tooling dot-directories, anything in `.streamlit/` but `config.toml`, `.env` files, symlinks that leave the app, and symlinks to any of those. The stage-copy deploy job uploads this bundle. |

`deploy-setup` flags: `--admin` (the full bootstrap a first deploy needs, in
`USE ROLE` sections, to hand a Snowflake admin), `--public-key-file <pem>` (with
`--admin`: the CI user's public key), `--viewer-user <user>` (with `--admin`,
repeatable), `--viewer-role <role>` (with `--admin`, repeatable: an existing role
that gets the viewer role), `--teardown` (print the reverse of `--admin`; keeps the governance
database), `--source stage-copy|git-repository` (preview the other source),
`--git-origin <url>`, `--github-auth pat|github-app|public`, `--config`. See
[Deploy setup](deploy-setup.md).

`verify-deploy` flags: `--sha`, `--attempts` (default 3) and `--delay` (seconds,
default 20) to absorb a cold start, `--temporary-connection` (connect from
`SNOWFLAKE_*` environment variables, as CI does), `--config`, `--format md|json`.

`verify-deploy` runs `stage-files` only for a stage-copy source, a full commit
`--sha` (40 or more characters), and an `apps/<slug>/` directory next to the config. Each check in the JSON
output carries `level`: a failed `block` check fails the run, a failed `warn` check
(`stage-files`) prints `!` and `(warning)` and the run still exits 0.

`stage-bundle` flags: `--out <dir>` (required; must be empty or missing, and outside
`apps/`), `--dir` (repo root, default `.`), `--format md|json`. It exits 2 on an
invalid slug, an app directory that does not exist, or an unusable `--out`.

`verify-deploy` checks only the app you name. The deploy workflow runs it for every
directory under `apps/`, so an app whose directory was renamed or removed is never
verified again; `check tombstones` is what catches it, at PR time.

## CI key

| Command | What it does |
|---|---|
| `streamsnow ci-key create` | Creates (or reuses) the CI user's key pair and the five deploy secret files under `--dir` (default `~/.streamsnow-ci`, outside any repo). Never overwrites a key and never prints a secret. `--account`, `--config`. |
| `streamsnow ci-key push` | Sets the five GitHub secrets from those files via `gh secret set` on stdin, `SNOWFLAKE_ACCOUNT` last. `--dir`, `--repo owner/name`. With `--config` (and `--account` if you overrode it in `create`), it first checks that the user, warehouse, role and account files match the config and refuses, by name, before any `gh` call. |
| `streamsnow ci-key verify` | Signs in as the CI service user with those files, the way the deploy job does (`SNOWFLAKE_*` variables, key-pair auth, `--temporary-connection`), and runs read-only probes, each named by object: the CI role, `USE WAREHOUSE`, the app schema, every grant the admin script gives the CI role (`SHOW GRANTS TO ROLE`), and with `--object DB.SCHEMA.OBJECT` (an allowed governance schema only) a `LIMIT 0` read. The key, account and user never reach argv, the output or the JSON. It uses the production CI key from your machine: the sign-in shows in the CI user's login history, and a network policy that only admits the CI runners refuses it. Run it once after the admin setup. `--dir`, `--config`, `--format md\|json`. Exit 0 all pass, 1 a probe failed, 2 a tool error with no probe results printed: a missing or unreadable secret file, a file that differs from the config, a bad config or `--object`, no `snow`, or a `snow` call that could not start, timed out or printed unreadable output. |

How the key moves, and what Claude can and cannot see:
[README](../README.md#what-claude-can-and-cant-see) and [Deploy setup](deploy-setup.md).

## Other agents

| Command | What it does |
|---|---|
| `streamsnow agent-skills install` | Copies the skills into `.agents/skills/` for Codex (`--agent codex`). `--scope repo` (default, committed with the repo) or `--scope user` (`~/.agents/skills`), `--dir`, `--dry-run`, `--force` (overwrite edited or foreign folders). |
| `streamsnow agent-skills list` | What is installed where. Same `--agent`, `--scope`, `--dir`. |

`streamsnow --version` (or `-V`) prints the version.
