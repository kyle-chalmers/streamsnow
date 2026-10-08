# Changelog

All notable changes to StreamSnow are recorded here. This project follows
[semantic versioning](https://semver.org/): from 1.0.0 the stable surface in
[docs/versioning.md](docs/versioning.md) breaks only in a major release, after a deprecation
period. Before 1.0, a breaking change can land in any minor release and is called out in its
entry.

## [Unreleased]

## [0.11.0] - 2026-10-08

### Breaking

- **Config `schema_version: 2`: `governance.sources` and `governance.app_data` replace
  `governance.database` and `governance.schema_allow`** (#78). Sources are `DATABASE.SCHEMA`
  entries in any databases; app data is the one schema per repo for views and dynamic tables
  built for the apps (default `<app_database>.STREAMSNOW_REPORTING`). A schema_version 1 file
  now fails with a message naming the new keys; there is no automatic migration. To upgrade:
  run `streamsnow configure` (it proposes sources from your old database and schemas and keeps
  every other value), review the file, run `streamsnow update --apply`, and have your admin
  re-run `streamsnow deploy-setup --admin`, which now also creates the app-data schema.
- **`configure` and `init` take `--sources` and `--app-data`**; `--database` and `--schemas`
  are gone, so scripts that pass them fail with "No such option".
- **App SQL names objects in full.** `check schema-refs` now reads which names sit in relation
  position (after `FROM`, `JOIN`, `INTO`, `UPDATE`, `USE`, a FROM list's commas,
  `IDENTIFIER('...')`, `TABLE('...')`) and reports names
  outside `governance.sources` and app data, and two-part `SCHEMA.OBJECT` names. Quoted names
  keep their case, as in Snowflake. Under `governance.boundary: warn` (the default) these are
  warnings in schema-refs, validate-app, the live SQL review and `migrate`; `enforce` makes
  them failures. The deny list always fails, and now accepts `FINANCE.RAW` as well as `RAW`;
  `USE ROLE` lines are no longer read as schema references. `ci-key verify --object` accepts
  objects in any source or the app data.

### Added

- **`streamsnow doctor --live`** adds an optional `source-access` check: each governance source
  and the app-data schema are visible to your `snow` connection's role (read-only SHOW
  statements; it logs in, so it is opt-in). `streamsnow configure` runs the same check on the
  sources when the connection exists and records shared databases that hold a source in
  `governance.imported_databases`. In `configure` the wait is capped at 20 seconds per statement
  and 90 seconds in all, and a sign-in that never happens reads as unverified instead of
  blocking the config.
- **`deploy-setup --admin` reads sources across databases** (`USAGE` once per database),
  grants `IMPORTED PRIVILEGES` once per shared database, and creates the app-data schema with
  `CREATE VIEW` and `CREATE DYNAMIC TABLE` for the CI role (never `CREATE TABLE`).
  `--teardown` keeps every source database and prints app data outside the app database as a
  commented `DROP SCHEMA`.
- **`governance.boundary: warn|enforce`** (default `warn`) decides what happens to a name outside
  the sources and app data, or a two-part name. The deny list fails either way.
- **`check schema-refs --format json` gains `warnings` and `boundary`**, and its findings gain
  `database` and `reason`. `validate-app` shows boundary warnings alongside its checks.
- **`streamsnow sql-review helper <slug> [--apply] [--force]`** refreshes an app's `review.py`
  from the current scaffold, which `streamsnow update` never does. The default is a dry run that
  prints the helper's state (`current`, `modified` or `missing`) and a diff; `--apply` writes the
  scaffold, and a `modified` file is refused (exit 1) unless `--force` is also passed. An unknown
  slug, a missing config or a non-UTF-8 helper is exit 2, and a symlinked `review.py` is
  refused (exit 1) even with `--force`. `sql-review compare` points at it when the helper is stale.

### Fixed

- **Docs no longer say `streamsnow update --apply` refreshes `review.py`.** `update` re-renders
  repo-level governance files only; `.sqlfluff` and app files are never rewritten. The docs now
  say so, name `sql-review helper` for `review.py`, and give the one-line manual change an
  existing repo needs for the glossary `%` fix and the `.sqlfluff` comment.
- **`stage-bundle` leaves out `*.egg-info` directories, `*.pyc` and `.DS_Store`.** A local
  editable install's ignored build metadata reached the stage in a live test; it is never read
  at runtime.

## [0.10.1] - 2026-10-07

- Generated CI and deploy workflows pin `streamsnow>=0.10.1,<0.11`, because the stage-copy
  deploy now calls `streamsnow stage-bundle`; run `streamsnow update --apply` to pick it up.

Most entries below come from a first end-to-end run of the plugin (onboard through ship).

### Added

- **`streamsnow app-url <slug>`** prints the Snowsight URL of a deployed app (Snowflake's
  app-builder form), built from the name the deploy SQL creates and the organization and
  account your connection reports. `/ship-app` runs it after a green deploy and hands you
  the link with a short click-through checklist, since CI cannot load the page. The URL
  names your organization and account, so it is meant for your own terminal.
- **`/onboard` offers to turn on GitHub's automatic deletion of merged branches.** In a repo
  with a GitHub remote it reads `delete_branch_on_merge` first, and when it is off asks yes or no
  before running `gh repo edit --delete-branch-on-merge`, so a squash-merged branch is not left
  behind to be reused. Without admin rights on the repo it says so and moves on.
- **`streamsnow stage-bundle --out DIR [SLUG...]`** copies each app without the files a running
  app never reads: root-level docs (AGENTS.md, REQUIREMENTS.md, CLAUDE.md, README), `sql_review/`
  and its review logs, tooling folders, `.env` files and any `secrets.toml` (in any letter case),
  and `.streamlit/` files other than `config.toml`. A symlink that resolves inside the repo ships,
  so a shared helper or `.streamlit/config.toml` still deploys, and is judged by its target too,
  so a link to `.env` stays out. A symlink that resolves outside the repo fails the bundle with
  exit 2, naming the link, before anything is written. It lists every file it left out and why.
- **`verify-deploy` gains a warn-only `stage-files` check** for stage-copy deploys. It compares
  the commit's staged files with what `stage-bundle` would ship and warns about a missing file or
  an excluded one that reached the stage. A warn-level check prints `!` and never fails the run.
- **`streamsnow ci-key verify`** signs in as the CI service user with the CI key, the way the
  deploy job does, and checks read-only that the role, warehouse, app schema, the admin script's
  grants and (with `--object`) one allowed read work, with secondary roles off so the CI role is
  proven alone. The key, account and user never reach argv or output. It uses the production CI
  key from your machine, so the sign-in shows in the CI user's login history, and a runner-only
  network policy refuses it. `/onboard` offers it once after admin setup and asks first.
- **`ci-key push --config`** (with `--account`) refuses, by file name, when a user, warehouse,
  role or account secret file no longer matches the config, before anything reaches GitHub.
- **`review-gate stamp --expect-baseline <digest>`** refuses to stamp (exit 2) when the app
  changed after the review was dispatched, so an older report can't mark newer code reviewed.
  Stamps also record `Reviewed-head:`, and `classify` JSON adds `reviewed_head`,
  `reviewed_head_status` and `commits_since_review`.
- **`review-loop open-findings <dir>`** counts the findings the newest review left open, against
  that report's own Applied block. A report it can't fully read returns `parsed: false`, never 0.
- **`validate-app` gains a warn-only `starter-text` check** for an app `AGENTS.md` that still has
  the scaffold's starter lines and a README Apps table with no row for the app. It never changes
  the exit code.
- **`sql-review compare` matches aggregated pages.** A page that groups a shared loader in pandas
  reads `match` with rule `aggregated` when every total it shows equals the SQL's exactly and its
  group keys are unique and complete. Head and filtered slices stay `mismatch`. `sql-review run`
  records distinct counts for text, date, time and boolean columns, and the scaffolded
  `review.py` records key counts (never values) to support this; refresh an existing app's
  `review.py` with `streamsnow sql-review helper <slug> --apply` (`streamsnow update --apply`
  does not touch app files) or grouped visuals keep reading `mismatch`.
- **`sql-review run` output lists the `run-NN.json` files it wrote** in a new `files` key.

### Changed

- **`/migrate-app` finishes the app's documents the way `/build-app` does:** it rewrites
  the app `AGENTS.md` starter Pages and Queries lines and adds the repo README Apps row,
  so `validate-app`'s `starter-text` check no longer warns on a migrated app.
- **`/ship-app` after-merge cleanup wording:** the remote tip is read from the exact
  `refs/heads/<branch>` line of `git ls-remote` (a suffix match could hit another
  branch), an approval means one given in the conversation (not a GitHub review), and the
  step-4 path says `<branch>` is the spent branch, not the current one.
- **The generated stage-copy deploy uploads the `stage-bundle` output** instead of the whole
  `apps/` tree, so internal docs and review logs no longer land on the stage. Run
  `streamsnow update --apply` to pick it up. The workflow calls a command added in this release,
  so it needs the release that ships `stage-bundle`. Git-repository deploys read the committed
  folder and can't exclude docs; see `docs/git-repository.md`.
- **`/review-app` stamps the review gate after every default pass** (with `--expect-baseline`),
  even when findings stay open; `--fix` never stamps. Before, only `--auto` stamped, so a full
  review still read `needs_review: true` at ship time.
- **`/ship-app` puts "Open critical: N" and the commits made since the review in the PR body**,
  hands the user `gh pr checks --watch` when the host forbids polling CI, and gains an After merge
  cleanup that deletes the spent branch only after checking the PR is `MERGED` and both branch
  tips equal its merged head. When the user approved the merge in the same run, the cleanup runs
  without a second question and is reported afterward; otherwise it asks once. A branch found
  spent at step 4 is cleaned up after the work is committed on the fresh branch. `/ship-app`
  never merges and stays user-typed; `/build-app` now says so.
- **CLI tracebacks no longer print local variables** (`pretty_exceptions_show_locals=False`), so
  a crash can't echo a secret held in a variable.
- **`ci-key create`** says when every kept secret file matches the config, and its next steps
  run `mkdir -p .internal` as its own command so the CI key guard allows the `deploy-setup` line.
- **`ci-key push`** refuses a secret file that is not UTF-8 text or contains a NUL byte, by name.
- **Skill text:** query headers name tokens without braces, the page-builder runs
  `check sql-tokens`, sample tokens mirror the page's default filters, an unused `review_window`
  is omitted, preview uses a role whose reads match the CI role, the `st.form` rule applies only
  when a rerun is expensive, `/build-app` rewrites the app `AGENTS.md` and README Apps row, and
  `/build-app` and `/review-app` explain how their reviewer sets relate. `/sql-review` documents
  `--warehouse`, and `review-gate` docs say `verdict` is review depth while `needs_review` is the
  decision.

### Fixed

- **`stage-bundle` names a link to the repo root for what it is.** Its target resolved to
  `.` and was reported as a "tooling dot-directory"; it now reads "links to the repo
  root".
- **`streamsnow <group> <verb> --help`** shows the verb's own flags for `sql-review`,
  `review-gate`, `review-loop`, `migrate` and `preview` (it printed the group's text before).
- **A failing sql-review session setup** (`USE ROLE` / `USE WAREHOUSE`) names the statements and
  points at `--role` / `--warehouse`, instead of a bare 002043 error.
- **Generated pre-commit hooks run with `require_serial: true`**, so a BLOCK line is no longer
  followed by a parallel batch's "clean" line.
- **The generated glossary's `hover_definition`** no longer doubles `%` (plotly.js doesn't
  unescape `%%`), the generated `.sqlfluff` comment points at `streamsnow sql-review check`, and
  the generated entrypoint warns against data calls before `st.navigation`.
- **Generated deploy workflows print "Deploy secrets found: deploying"** on the deploy path, so the
  skip text GitHub echoes in the step header no longer reads as a skipped deploy.
- **`deploy-setup --admin` now grants read on dynamic tables** and every other object type an
  app can read (materialized views, semantic views, Iceberg and external tables), current and
  future, on each allowed schema (#80). Before, it granted tables and views only, so a dynamic
  table was invisible to the CI role and the deployed app failed on it while local preview
  worked. Re-run `streamsnow deploy-setup --admin` and apply it to pick up the new grants. If
  another role relies on database-level future grants in those databases, check
  `SHOW FUTURE GRANTS IN DATABASE <db>` first: schema-level future grants replace them for that
  object type.
- **`migrate-app` grant guidance** now lists every object type the CI role needs (#80), instead of
  tables only, in each `required_grants` reason.

## [0.10.0] - 2026-10-06

- Generated CI and deploy workflows pin `streamsnow>=0.10,<0.11`; run
  `streamsnow update --apply` to move an existing repo's pin.

### Added

- **`streamsnow deploy-setup --admin --viewer-role ROLE`** (repeatable) grants the viewer role to a
  role you already use, so an analyst or agent role can open the apps and see the app database and
  warehouse when an admin login ran the script. `PUBLIC`, system roles and StreamSnow's own roles
  are refused. `/onboard` lists your connection's roles and asks which ones get it.

### Changed

- **Install docs: `/reload-plugins` is a fallback.** A project-scope install normally shows the
  skills straight away, so the README, getting-started and `init`'s `Next:` block now say to run
  `/onboard` and only reload plugins if it isn't listed.
- `/onboard` now explains and asks each setup answer individually, explains the defaults and the
  Snowflake admin script in bullets before asking, and no longer treats a detected value as
  confirmation or adds its own commentary about the admin file. Other agents ask in chat when they
  have no question tool.

### Fixed

- **A local `uv build` no longer packs per-machine files into the sdist.** Hatchling sweeps in
  untracked files, so `.claude/settings.local.json` (an absolute home path) could ship from a
  developer checkout. It, `.claude/*.local.*`, `.streamsnow/export-denylist.txt` and `.internal/`
  are now excluded from the sdist, and the settings file is gitignored. PyPI releases were
  unaffected: they build from a fresh checkout.

## [0.9.1] - 2026-10-05

### Added

- **Screen comparison for `/sql-review`** (SQL review redesign, phase 3 of 3). The scaffolded
  `review.py` now records, in review preview mode only, what each `review_value` visual
  received: a row count, hashed column names and column totals, or the number a metric shows.
  Never a row or a column name in clear; a capture problem is logged and never breaks the page,
  and Streamlit in Snowflake never captures. `streamsnow sql-review compare` holds each capture
  to its `run` result (within 0.5% or the displayed rounding, integers exactly) and writes
  `compare:NN#n` results the reviewers can cite; a browser walk with the Playwright CLI adds a
  cross-check that is never evidence. `/sql-review` gains a screen step between `run` and the
  reviewers (`--no-screen` skips it), and the page reviewer turns each mismatch into a
  candidate finding.
- **`streamsnow preview start --review-capture DIR`** (review preview mode) and **`--port 0`**
  (any free port). A preview already running with another capture setting reports
  `capture_mismatch` instead of being reused, and a normal preview never inherits the flag from
  the shell.

### Changed

- `/build-app` accepts a `coverage` warning for a helper query that shows nothing on screen
  (a date-bounds or filter-options loader in `pages/_data.py`) instead of inventing a metric.
- **The review log's Screen match column is filled** from `compare`: `match`, `mismatch`,
  `not captured`, `unsupported`, or `stale` when a page was re-run after the comparison (it was
  always `n/a`). `log` also lists screen mismatches no kept finding cites; it never logs them
  itself.
- **Until 1.0.0, StreamSnow assumes it has no users** ([CONTRIBUTING](CONTRIBUTING.md),
  [versioning](docs/versioning.md)): a change to generated files needs no migration code or
  refresh step for apps generated by an older release. Apps scaffolded before this release keep
  the no-op `review.py`; replace it with the one a new scaffold writes to compare the screen.
- **[docs/versioning.md](docs/versioning.md) no longer overstates what exists.** Config
  migration is described as a promise (a breaking config change will bump `schema_version` and
  ship a migration in `streamsnow update`; none exists yet), and the plugin skills row says what
  `tests/test_plugin_surface.py` enforces: the skill names and that each declares an
  `argument-hint`, not the arguments themselves.

## [0.9.0] - 2026-10-05

### Breaking

- **`/audit-lineage` is replaced by `/sql-review`**, with no deprecation release (none is
  needed before 1.0.0, per [docs/versioning.md](docs/versioning.md)). `/sql-review <slug>`
  proves an app's numbers against live Snowflake and commits a review log a person signs
  (SQL review redesign, phase 2 of 3). `/review-app --sql` now runs it. Rename a repo overlay
  `.streamsnow/overlays/audit-lineage.md` to `sql-review.md`. Codex users re-run
  `streamsnow agent-skills install`, which removes an unedited `audit-lineage` copy. The live
  review covers the SQL listed in `sql_review/index.yaml`; SQL written inline in page code is
  no longer traced live (the static `/review-app` pass still reads it).

### Added

- **Live SQL review commands**: `streamsnow sql-review probe` (objects exist, direct grants,
  DDL drift against `GET_DDL`, every section compiles), `run` (every section as aggregates:
  row count, column totals, an order-insensitive hash, timing; never rows), `bench` (a section
  with the result cache off, before and after a candidate rewrite, with a result-equivalence
  check) and `log` (writes `sql_review/review_log/YYYY-MM-DD_<sha>.md` from verified findings,
  refusing any finding whose evidence is not in the run). They run through `snow sql` with the
  configured connection, the app's CI role with secondary roles off, a query tag and a
  statement timeout, and refuse non-read-only SQL and denied schemas before sending.
  Evidence stays local in `.streamsnow/sql-review/`, which new repos' `.gitignore` also lists.
- **Reviewer agents** for `/sql-review` in the plugin's `agents/` (page, object, optimizer,
  verifier); the same briefs ship in `skills/sql-review/reviewers/` for other agents.
- `sql_review/AGENTS.md` (refreshed by `generate`) gains the review-log rules.
- **In-app documentation in every new app.** `streamsnow new` adds an **About** page (last in the
  navigation): purpose, audience, owner, each page and its question, the full metric glossary,
  and every query with the schemas it reads, read at runtime from the `queries/*.sql` header
  blocks (new `query_headers()` in `sql_loader.py`). It is listed in `sql_review/index.yaml`
  with `metrics: []`.
- **Shared page modules** scaffolded under `pages/`, imported package-qualified:
  `_glossary.py` (one definitions table), `_layout.py` (`definitions_expander`, `empty_state`,
  `sources_footer`, and `show_sql`, which shows the exact query and bound values behind a visual),
  `_time_controls.py` (a period picker bounded by the data) and `_data.py` (loaders several pages
  share).
- **`branding.py` 1.1.0:** `fmt_number`, `fmt_currency`, `fmt_pct`, `BRAND_STATUS_COLORS`, and
  `branded_metric(help=..., delta_color=...)` (tooltip definition; delta colored by meaning). Additive:
  existing calls keep working; a zero delta ("0%") is grey, not green. Older apps on 1.0.0 next
  to a new 1.1.0 app get a `check branding-parity` **note**, not a failure: a lag behind the
  installed template within one major version is an upgrade, not a hand edit. Copy the new
  `branding.py` into them when convenient.
- **House design guide.** An optional, committed `.streamsnow/design.md` holds a repo's house
  style for pages; `/build-app` and `/review-app` read it before the shipped defaults.
- `/build-app` specs record each page's **Question** (§4) and a **Style** line (§2).
- **[docs/cli-reference.md](docs/cli-reference.md):** every command, flag and check, with exit
  codes, JSON keys, and the steps `validate-app` runs. Several flags were documented nowhere
  before (`check dependency-vulns --allowlist`, `check session-fallback --all`,
  `check tombstones --registry/--apps-dir`, `verify-deploy --attempts/--delay`,
  `agent-skills --dry-run`).
- **`scripts/readme_media/`:** reproducible generators for the README's demo GIF, terminal
  recording, social preview image and diagram PNGs, plus a runbook for recording the full
  Claude Code to Snowflake demo.
- **`docs/superpowers/README.md`** indexes the internal design specs and plans.

- **`/build-app` orchestrates subagents.** New phases between spec and scaffold: **discover**
  (a data-scout profiles grain, date range, size and filter values into §3) and **design** (an
  app-designer plans every page's forms, copy, glossary and shared data, shown as a text
  wireframe at the new CHECKPOINT 1b). The scaffold builds the shared layer once; one
  page-builder per page then builds in parallel, owning only its page and its queries, while the
  orchestrator merges navigation, `sql_review/index.yaml` and the glossary and runs
  `sql-review generate` once. A verify phase runs perf, visual and cold-reader reviewers with at
  most two fix rounds before the click-through. Briefs live in `skills/build-app/briefs/`; hosts
  without subagents follow them one at a time. `check requirements` accepts the `discover` and
  `design` phases.

### Changed

- Generated CI and deploy workflows pin `streamsnow>=0.9,<0.10`; run
  `streamsnow update --apply` to move an existing repo's pin.
- **A redesigned README.** It now has a logo with light and dark variants, a nav row, a demo
  GIF, a quick-start callout near the top, a "Without / with StreamSnow" table that pairs real
  production incidents with the check that prevents each, a new "How the repos fit together"
  diagram, an updated skills diagram, and a star-history chart. Long reference sections fold
  into `<details>`. "This is for you if" gains three lines, and a one-line Prerequisites
  statement replaces "Who can use this" and the "Where it fits next to a BI tool" paragraph.
  Images use absolute URLs, so they also render on PyPI.
- **The principles moved to [docs/principles.md](docs/principles.md)**, with the same nine
  rules and numbers, plus where each one shows up. The README links to them.
- **`streamsnow init` prints the project-scope plugin install** (`claude plugin marketplace add
  --scope project …` and `claude plugin install --scope project …`) in its `Next:` block,
  matching the README and Getting started. It printed the user-scope `/plugin` commands before.
- **Default chart palette** no longer uses green, amber or red, which the visualization guide
  reserves for status. New default: `#2A78D6, #EB6834, #1BAF7A, #4A3AA7, #E87BA4`. It is **five
  colors, not six**: a chart with a sixth series repeats a color, so fold the tail into "Other" or
  use small multiples. Adjacent pairs were checked for colorblind separation on the light theme
  with an external palette validator (the check isn't part of this repo's tests); slots 3 and 5
  sit below 3:1 contrast on white, so label those series directly. Only apps scaffolded without
  a `brand.chart_sequence` change.
- **`/migrate-app` conforms through `/build-app`.** Step 2 now backfills the spec, builds the
  shared layer and conforms the pages with build-app's parallel build (page-builders in
  `conform` mode, worklist from `scan-conformance` and `scan-inline-sql`), then verifies. Done
  is the gates: `validate-app` passes, `sql-review check` is clean and the conform scans are
  empty. A new test lifts and conforms a fixture app and fails if the scanners and the gates
  ever disagree.
- **The starter page's chart uses `width="stretch"`** in place of the deprecated
  `use_container_width=True`, so a new app no longer logs a deprecation warning on every
  render (both the 1.52.2 warehouse pin and the 1.59.2 container pin warned). Only newly
  scaffolded apps change; existing apps keep working as they are.

### Removed

- **`/feedback-app` is now `/build-app <slug> --feedback "<feedback>"`.** Same classification
  (BUG / POLISH / UX / NEW-FEATURE / CROSS-CUTTING), locked with you before any edit; small
  fixes keep the one-commit-per-item fast path, while UX, new features and cross-cutting items
  go through the design, build and verify phases for the affected pages. The old skill is
  removed; `streamsnow agent-skills` and the plugin no longer install it.

### Fixed

- **`sql-review probe` credits grants to `PUBLIC`.** Every role inherits `PUBLIC`, so an object
  granted to it (shared data such as `SNOWFLAKE_SAMPLE_DATA` usually is) no longer reports a
  missing grant for the review role. Found in the first live run of `/sql-review`.

- **Docs follow the new `/build-app` phases.** The skills diagram shows spec, discover and
  design (with the new CP1b wireframe checkpoint), the foundation scaffold, the parallel page
  build, preview and verify, then validate, review and ship. The Getting started and examples
  app trees list the new `pages/` modules and the About page, and the CLI reference lists the
  `REQUIREMENTS.md` §11 phases `check requirements` accepts.
- **`validate-app` and `check security` no longer fail an app on its own maintained DDL.**
  Files in `sql_review/app_specific_reporting_objects/` hold `CREATE` statements by design (a
  human applies them; StreamSnow never executes them), but `app-security` flagged them as write
  SQL, so any app that declared a reporting object could not pass the gate or the pre-commit hook.
  A file directly in that folder of an app may now use `CREATE`, `ALTER` and `GRANT`; any other
  write there, and the same statements anywhere else in an app, still fail.
- **`/validate-app` matches the gate it runs.** It lists all 15 steps under their printed names
  (adding `manifest`, `naming`, `path-leaks`, `requirements`, `sql-review` and `placeholders`),
  reports sql-review warnings on a PASS instead of calling the app clean, names the real focused
  commands (`streamsnow sql-review check <slug>`, `check session-fallback --all`), and drops false
  claims (schema-refs requiring an allowed-schema reference, both dependency manifests failing).
  Its fixing guide adds `manifest`, `naming` and `path-leaks`, the full `app-security` kinds and
  waivers, the new phases `discover` and `design`, and covers the About page and `pages/_data.py`
  loaders from the new scaffold. `docs/cli-reference.md`'s `validate-app` rows follow.
- **Docs that disagreed with the code:**
  - `verify-deploy` does not flag a renamed or removed app's old object; only
    `check tombstones` catches it, at PR time (deploying, troubleshooting #14, production
    lessons).
  - A fresh scaffold fails `validate-app` on its placeholders until you replace them; the
    README said it passed.
  - Only `schema_deny` is enforced; `schema_allow` is a convention (data discovery).
  - `/onboard` is not a removed alias (migrating a consumer repo).
  - `streamsnow update` re-renders files; it does not vendor tools or bump the plugin.
  - The README hooks table now says the deploy guard watches PowerShell too.
  - Getting started on Windows: preview and the guards work natively; the bash SessionStart
    hook is what keeps WSL the supported route.
- **Docs that were missing things:**
  - The config reference documents `cache_ttl`, `deploy.git_origin`,
    `snowflake.objects.runtime_name`/`container_python`, the `brand` keys, `review_gate`
    and `github_auth_mode: public`; the same keys are in the example config.
  - Scaffold file lists now include `CLAUDE.md`, `.sqlfluff` and `osv_allowlist.json`.
  - `validate-app` step names are listed correctly.
  - The examples app tree includes `review.py` and `sql_review/`.
  - The deploy-secret push order is spelled out.

## [0.8.0] - 2026-10-05

### Breaking

- **`/start-app` is renamed `/build-app`**, with no deprecation release
  (none is needed before 1.0.0, per [docs/versioning.md](docs/versioning.md)). Same
  front door, same `--spec` mode. Rename a repo overlay
  `.streamsnow/overlays/start-app.md` to `build-app.md`. Codex users re-run
  `streamsnow agent-skills install`, which removes an unedited `start-app`
  copy; if you edited it, the install stops and writes nothing, so move your
  edits to `.streamsnow/overlays/build-app.md`, delete
  `.agents/skills/start-app`, and run it again.
- **SQL review is page-based** (`sql_review/` redesign, phase 1 of 3; design in
  `docs/superpowers/specs/2026-10-04-sql-review-redesign.md`). Each app gets
  one generated SQL file per page, `sql_review/NN_<page>.sql` (`NN` = the page's
  position in the app's navigation), with one runnable section per metric in
  on-screen order. Each section starts with a `--N_key` tag (the first is
  always line 9), carries its own `params` CTE for the review window, and runs
  on its own with the cursor in it (DataGrip, Snowsight). A single
  `sql_review/index.yaml` per app is the editing surface. **The 0.6/0.7 format
  is removed with no automatic migration**: `sql_review/manifests/*.json`,
  `*.review.sql`, combos, metrics mode, token strategies, and the `discover`
  and `index` verbs are gone. `check` reports an app still on the old format
  as an `index` finding; `docs/auditing-a-visual.md` has the upgrade steps, and
  `generate` deletes the old `*.review.sql` files.
  Before 1.0.0 a breaking change needs no deprecation release
  ([docs/versioning.md](docs/versioning.md)); the 0.7 format had no known users.
- **`sql-review check` lints app queries with sqlfluff** (Snowflake dialect,
  the repo's new `.sqlfluff`, created by `init` and by `update` when missing)
  and requires a one-line comment directly above every CTE, with comment lines
  of 100 characters or fewer. Both fail the gate. `sqlfluff` is now a runtime
  dependency. Apps without an `index.yaml` are only reported as uncovered.
- Generated CI and deploy workflows pin `streamsnow>=0.8,<0.9`.
- **`/start-app --setup` and `/start-app adopt` are removed**, with no
  deprecation release (none is needed before 1.0.0, per
  [docs/versioning.md](docs/versioning.md)). Use `/onboard`, which does both;
  `/build-app` hands off to it by itself when the machine or repo isn't ready.
- **The plugin no longer bundles the Playwright MCP** (`.mcp.json`). UI
  walkthroughs use the Playwright CLI instead; see Changed below.

### Added

- **Three shared design guides for app-touching skills:**
  `skills/_shared/streamlit-performance.md`, `visualization-guide.md` and `explainability.md`.
  They are defaults with their reasons, linked from `page-conventions.md`. `/review-app`'s UI
  dimension judges pages against them, as advisory findings.
- **`review_value("<key>", value)` markers.** A new scaffolded `review.py`
  (listed in `snowflake.yml` artifacts) ties each visual in page code to its
  metric in `index.yaml`; `check` reads the calls by AST, never by import, and
  fails on a metric with no visual or a visual with no metric. Outside review
  preview mode the call returns its input and does nothing else (about 40 ns;
  a benchmark test holds it under 1 µs, with no file written and no import).
- **`sql_review/app_specific_reporting_objects/`**: maintained DDL, one file per
  object built to make the app work, headed by Object, Purpose, Used by
  (generated) and Grants. `check` fails on a DDL file nothing reads, an object
  with no DDL file, or a header that disagrees with `index.yaml`. A human
  applies DDL; these files are exempt from the read-only guard and never run.
- **`sql_review/AGENTS.md`, `CLAUDE.md` and `README.md`** are created with each
  app; `generate` refreshes the README tables and the tool-owned part of
  `AGENTS.md`. The app `AGENTS.md` gains a Data notes section.
- **`sql-review check --lint-files F …`** lints only the named query files; the
  generated pre-commit hook passes the staged ones, CI lints everything.
- `generate` applies sqlfluff's layout and capitalisation fixes (never rules
  that change what SQL means) to every section it writes.
- **Versioning and stability policy** ([docs/versioning.md](docs/versioning.md)): what the
  stable surface is, why a stricter check counts as a breaking change (warn-only for one
  minor release first), the deprecation window that applies from 1.0.0, and the path to 1.0.0. A new
  `tests/test_cli_surface.py` pins every command, flag and argument in
  `tests/fixtures/cli_surface.json`, so a rename or removal fails CI as BREAKING. The
  release gates for 1.0.0 are in `RELEASING.md`.
- **Community intake and agent-run maintenance** for this repository: issue forms (blank
  issues off), a PR template with AI-use disclosure, labels as code, CODEOWNERS, Dependabot,
  a Code of Conduct, `SUPPORT.md`, `.claude/CLAUDE.md` for agents, and workflows that triage every
  issue (the model only classifies; scripts apply labels and canned comments), implement
  maintainer-approved `ready-for-agent` issues as PRs, review same-repo PRs, answer
  maintainer `@claude` comments, post a daily maintainer digest, and close issues left in
  `status:needs-info` for 14 days. CONTRIBUTING describes the flow and the AI-assisted
  contribution policy.
- **`/onboard`**, a skill of its own again. It gets a machine, a repo and a
  Snowflake account ready in four stages: check and prepare (one install
  approval), one round of clickable questions, build after you confirm, then
  the steps that wait on others. It explains every step as it goes, ends at
  "ready to build and preview" or "ready to deploy", and is safe to re-run.
- **The Snowflake admin step.** `/onboard` checks read-only whether the admin
  objects exist ("not confirmed", never "missing"), runs `ci-key create` and
  `deploy-setup --admin --public-key-file` into `.internal/admin-setup.sql`
  (gitignored), then copies it for Snowsight or writes a note for your admin.
  It never runs the admin SQL. Once confirmed it runs `ci-key push`.
- **Wider Snowflake access detection:** MCP servers that are configured but not
  connected, `SNOWFLAKE_*` variable names, legacy SnowSQL config, and
  `SNOWFLAKE_DEFAULT_CONNECTION_NAME`, all by name only. `/onboard` can also
  set up your `snow` connection for you, or guide you through it.
- **Session start** points to `/onboard` when the plugin is enabled but the
  repo isn't set up, and when this clone has no pre-commit hook.
- `streamsnow new` prints the runtime-matched install command, and `/build-app`
  runs it into the repo `.venv` (which `/onboard` creates) right after
  scaffolding, so the first preview works.

### Changed

- **UI walkthroughs use the Playwright CLI** (`@playwright/cli`, pinned in
  `skills/_shared/playwright-walkthrough.md`) instead of the bundled Playwright
  MCP: nothing to start at session launch (the MCP's first download could
  outlast the startup timeout), fewer tokens, and Codex can run it too when its
  sandbox allows network. `/onboard` downloads the CLI and its browser ahead of
  time. The repo `.gitignore` template ignores `.playwright-cli/`.

### Removed

- **`/start-app --setup` and `/start-app adopt`.** Use `/onboard`.
  `/build-app` now hands off to `/onboard` by itself when the machine or repo
  isn't ready. docs/migrating-a-consumer-repo.md maps the old names.
- **`.mcp.json`** (the bundled Playwright MCP). Skills no longer use an MCP;
  `/reload-plugins` drops the old server, and old `browser_*` tool approvals can
  be deleted. Codex users get the new recipe on their next
  `streamsnow agent-skills install`.

## [0.7.7] - 2026-10-05

### Added

- **`streamsnow ci-key push`** sets the five deploy secrets on GitHub from the
  files `ci-key create` wrote. Each value goes to `gh secret set` on stdin,
  never on the command line, and only names are printed. `SNOWFLAKE_ACCOUNT`
  goes last, and a failure stops before it, so deploys never switch on with a
  partial set. It checks `gh`, its sign-in, the target repo and every file
  before setting anything.
- **Key guard hook** (`hooks/secret_guard.py`, launched through the cross-shell
  `uv run` launcher). A `PreToolUse` guard that denies Claude's shell, file and
  search tools any access to `~/.streamsnow-ci`, apart from `streamsnow ci-key
  ...` and `streamsnow deploy-setup ...`. File and search tools are checked on
  the location they touch, not the text they write or search for, so docs that
  mention the directory stay editable. Covers PowerShell and Windows paths.
  Not repo-gated, and it denies rather than asks.
- **README: "What Claude can and can't see"**, before the install section, with
  a secrets-flow diagram, and a matching "How StreamSnow handles secrets"
  section in SECURITY.md. A test checks the README still names `ci-key push` and the guard.

### Changed

- **The plugin's Python hooks launch through `uv`, not `python3`.** The deploy
  guard and the review gate now run as `uv run --no-project --offline
  --no-python-downloads ... ; exit 0`. That one command works in every shell
  Claude Code runs hooks in (sh, Git Bash, and PowerShell on Windows without
  Git Bash), needs no `python3` on PATH (Windows often has none, or only the
  Microsoft Store stub), never touches your project's environment or the
  network, and exits 0 whatever goes wrong, so a broken launcher can never
  block your commands. uv is already a required prerequisite; if it is
  missing from PATH the hooks are silently off, and `streamsnow doctor` flags
  the missing uv. The guard costs about 0.1s more per shell command.
- **The deploy guard watches PowerShell too.** On Windows the PowerShell tool
  is Claude's default shell, so the guard now inspects it as well as Bash, and
  catches the Windows shapes of the same commands: `snow.exe`, backslash
  paths, the `&` call operator, PowerShell's backtick escape, and SQL piped in
  with `Get-Content deploy.sql | snow sql --stdin`.
- **`streamsnow preview` manages the process on native Windows.** Start,
  status and stop now work there (psutil, installed on Windows only), and stop
  takes the whole process tree, since `streamlit.exe` is a launcher whose
  child process holds the port.
- `ci-key create`'s closing steps point at `ci-key push` instead of a
  `gh secret set` loop, and remind you to save a copy of the private key
  somewhere safe, such as a password manager. docs/deploy-setup.md keeps the manual loop for anyone
  who prefers it.
- **`/start-app --setup` sets the deploy secrets with `ci-key push`.** Claude
  runs `streamsnow ci-key create` and, once your admin has run the admin script,
  `streamsnow ci-key push`, instead of setting values with `gh secret set
  --body` and handing you the private key command.

### Fixed

- **Text encoding and line endings on Windows.** Python on Windows reads and
  writes text as cp1252 by default, and Git for Windows checks files out with
  CRLF. `streamsnow init` and `new` crashed writing the scaffold's emoji, and
  `validate-app`, `doctor` and `--help` crashed when their output went to a
  pipe. Every file read and write now names UTF-8, a test fails any that does
  not, and the CLI switches a non-UTF-8 stdout to UTF-8. `sql-review` writes LF
  and hashes CRLF pairs as LF, so a Windows checkout no longer reads every
  committed review file as hand-edited or its inputs as drifted (a lone CR, or
  any other byte change, still counts as an edit). The cached Anaconda package
  list no longer crashes on Windows, the review gate keeps its state in the
  system temp dir instead of `/tmp`, and findings print paths with forward
  slashes. CI now runs the tests on Windows and macOS as well as Linux. The
  `/start-app --setup` routing to WSL stays until local preview and the hooks
  work natively.
- **`streamsnow preview start` behind a proxy.** The health probe of
  `127.0.0.1` honored `HTTP_PROXY` and the macOS/Windows system proxy, so on a
  machine whose proxy does not exempt localhost a serving app read as "not
  healthy" after the full timeout. The probe now always connects directly.
  `ci-key create` no longer warns on Windows that the key directory is
  readable by other users (Windows reports every directory as 0o777 and
  protects it with ACLs instead).

## [0.7.6] - 2026-10-04

### Added

- **The plugin bundles the Playwright MCP** (`.mcp.json`, an exact
  `@playwright/mcp` pin). Five skills already walked running apps in a browser,
  but nothing set the browser tool up, so the walkthrough was silently skipped
  for anyone who had not configured one by hand.
- **Onboarding rows in `streamsnow doctor`:** `repo-files`, `git-identity`
  (reports only whether name and email are set) and `pre-commit-hook` (pre-commit's
  own hook, honoring `core.hooksPath`), all required once a config exists;
  `node` (warning) for the browser tool; `ci-secrets`, which reads secret names
  through `gh` and says "not checked", never "missing", when it cannot list them.
  On native Windows a `platform` row points to WSL.
- **`/start-app --setup` runs the setup and explains each step.** Before every
  install it says in a line or two what it is and why the user needs it, then
  waits for a yes. It routes a teammate cloning a configured repo straight to
  machine setup, adds a browser check, saves the plugin at project scope, and
  sets the deploy workflow's non-secret GitHub secrets (the user sets the key).
- **`streamsnow deploy-setup --teardown` prints the start-fresh reverse of
  `--admin`.** Reviewable DROP statements, never run: the app database (and
  every deployed app in it), warehouse, CI service user, viewer and CI roles,
  then the external access integration, the configured compute pool (never
  `SYSTEM_COMPUTE_POOL_CPU`), and the git API integration. Every DROP uses
  `IF EXISTS`. It keeps the governance database, refuses when the app or stage database is the governance
  database or a Snowflake-shared one or a role is a system role, and marks each
  object that could predate StreamSnow.
- **`streamsnow ci-key create` makes the CI key pair and the five secret
  files.** It writes them to `~/.streamsnow-ci` with `openssl`, reuses an
  existing key, never overwrites a secret file, and prints only file names,
  the `SHA256:` fingerprint `DESC USER` shows, and the `gh secret set` loop.
- **`deploy-setup --admin --public-key-file` fills in the CI user's key**, so
  the stage-copy admin script runs unedited, and re-applies it with
  `ALTER USER` on a re-run. `--viewer-user NAME` (repeatable) grants the
  viewer role to more users.

### Changed

- **The admin script is safe to re-run end to end.** The PyPI external access
  integration, which Snowflake cannot create with `IF NOT EXISTS`, now uses
  `CREATE OR REPLACE` followed by the CI role's `USAGE` grant. A deployed app
  kept serving through a replace in a live test.
- **The admin script grants the viewer role to whoever runs it**, through
  `CURRENT_USER()`, instead of leaving a commented `GRANT` to fill in.

### Fixed

- `docs/getting-started.md` promised a git-identity check that doctor did not
  have.

## [0.7.5] - 2026-10-03

### Added

- **`init` and `configure` take the five wizard answers as flags.**
  `--runtime`, `--account` (or `--connection <name>`), `--database`,
  `--schemas` and `--deploy-source` each replace their question, and
  `--deny-schemas` sets `governance.schema_deny` (default `RAW,STAGING`; `''`
  denies none). With all five passed no prompt fires. The flags go through the
  same defaults and prefill logic as the wizard, so the same answers write the
  same file. `--connection` reads the account from that `snow` connection
  without printing it and uses the connection as `snowflake.connection_name`.
  Bad values, `--account` with `--connection`, a schema both allowed and
  denied, or flags combined with `--config` exit 2 before anything is written;
  on an existing config the flags need `--reconfigure`, so they are never
  silently ignored. The interactive wizard is unchanged.

### Changed

- **`/start-app --setup` proposes the wizard's answers.** Instead of leaving
  the user to answer the wizard cold, the setup skill first works out what
  Snowflake access the user has (a default `snow` connection, other
  connections, a Snowflake MCP server, a dbt profile, or nothing) and offers
  to help create a `snow` connection when there is none, in the user's own
  terminal so the account is never typed into chat. It then probes the
  account read-only (`SHOW` with `INFORMATION_SCHEMA` fallbacks for tools that
  refuse it), checks the role before trusting an empty result, and weighs the
  evidence as a method rather than name rules: what the user said, then
  comments, then what objects hold, then dbt, with names last. It asks only
  what the evidence could not settle, writes the config with `configure` and
  the confirmed answers, lets the user change the unasked defaults
  (warehouse, roles, app database, compute pool), then runs
  `streamsnow init --no-starter-app` so the governed files render from the
  final values. Everything is a proposal the user can skip or override,
  including `container` or `git-repository` against the evidence and keeping
  a non-default connection (step 3 then sets up preview for it). It never
  runs DDL or grants and never handles credentials in chat.

## [0.7.4] - 2026-10-03

The git-repository deploy source, hardened from a live test against Snowflake
on 2026-10-03. Stage-copy stays the default.

### Fixed

- **A git-repository deploy could report success while serving old code.**
  The workflow refreshed existing apps with ABORT / PULL / COMMIT behind
  `|| true`, and verify-deploy skipped the version check for this source.
  Deploys now run `CREATE OR REPLACE STREAMLIT ... FROM
  '@<repo>/branches/<branch>/apps/<slug>/'` after `snow git fetch`, the same
  idempotent statement stage-copy uses; a failed fetch fails the deploy.
  Snowflake rejects a `/commits/<sha>/` path as a Git source ("Invalid git
  branch path"), so `verify-deploy --sha` now compares the merge commit with
  the `last_version_git_commit_hash` that `DESCRIBE STREAMLIT` reports. The
  generated workflow passes `--sha "$GITHUB_SHA"` to it. Re-render with
  `streamsnow update --apply` to pick this up; an older workflow's
  `deploy-sql --refresh` call still works (it now prints a comment).
- **The git setup SQL needed hand edits.** `CREATE GIT REPOSITORY` carried a
  literal `<https://github.com/your-org/your-repo.git>` placeholder, and the
  API integration allowed every GitHub repository. The new
  `deploy.git_origin` config field fills `ORIGIN`, the integration allows only
  `https://github.com/<owner>`, the non-admin script grants the CI role the
  WRITE it needs to fetch, and the generated workflow is no longer labeled
  experimental.

### Added

- **`github_auth_mode: public`.** A public repo needs no token: no secret, no
  `GIT_CREDENTIALS`, no `CREATE SECRET` grant.
- **`streamsnow deploy-setup --source git-repository`** prints the other deploy
  source's setup for review without changing your config (with `--git-origin`
  and `--github-auth`), under a `PREVIEW` banner. It never runs SQL.
- **[Switching to the Git repository deploy source](docs/git-repository.md)**:
  when to choose it, a least-privilege GitHub token, switching the config,
  checking the first deploy, and dropping the old stage.

## [0.7.3] - 2026-10-03

### Removed

- **The 8 pre-0.3 alias commands.** `/new-app`, `/refine-requirements`,
  `/add-page`, `/onboard`, `/auto-review-app`, `/sql-review`, `/apply-review`
  and `/deep-dive-data` only forwarded to the 8 real skills, and they doubled
  the `/streamsnow:` menu to 16 entries. The `commands/` folder is gone, so the
  menu now lists the 8 skills. Use `/start-app` (with `--spec` or `--setup`),
  `/review-app` (with `--fix`, `--auto` or `--sql`) and `/audit-lineage`
  instead; [docs/migrating-a-consumer-repo.md](docs/migrating-a-consumer-repo.md)
  maps each old name. This was promised for the next major release; it ships
  in a patch because the plugin is still beta and the aliases added nothing
  the skills do not. The CLI is unchanged.

### Changed

- **Install docs use project scope and `/reload-plugins`.** The README's agent
  install prompt, Quickstart and upgrade steps install the plugin with
  `--scope project` and load it with `/reload-plugins` instead of a restart.
  The README also gains a diagram of how the skills fit together.

## [0.7.2] - 2026-09-27

Fixes from the first real GitHub Actions deploy and from a `doctor` run on a
machine that had been idle.

### Fixed

- **`verify-deploy` reported checks it never ran as passed.** Current
  Snowflake's `SHOW STREAMLITS` returns no version URIs, so on the first real
  CI deploy the live-version and version-source checks each printed a check
  mark above "not in SHOW output, skipped". `verify-deploy` still uses
  `SHOW STREAMLITS` to confirm the app exists, then reads `DESCRIBE STREAMLIT`
  for the other two: live-version fails when `live_version_location_uri` is
  empty, and version-source (stage-copy) fails when neither
  `default_version_source_location_uri` nor `last_version_source_location_uri`
  contains `/commits/<sha>/`. A check that cannot run now prints
  `○ <check> (skipped)` with the reason, reports `"status": "skipped"` in
  `--format json`, and is counted apart from the passes, as in
  `PASS: store-sales (3 passed; 1 skipped: service-logs)`. Skips still never
  fail the run, and the container service-log scan stays best-effort. Existing
  deploy workflows get this on their next run without a re-render: their
  `streamsnow>=0.7.1,<0.8` pin resolves to the newest 0.7 release.
- **A repo with deploy secrets but no app failed its deploy.**
  `streamsnow init --no-starter-app` writes no `apps/` directory, so
  `snow stage copy "apps/"` stopped the deploy with "No data" (exit 2), and
  `for d in apps/*/` ran once over the literal pattern. Both deploy workflows
  now collect app directories with `nullglob`; when there are none, the deploy
  and verify steps print "No app directories under apps/ yet: nothing to
  deploy" (or "verify") and exit 0 before any `snow` call. Existing repos
  re-render `deploy.yml` with `streamsnow update --apply` and commit it.
- **`doctor` called a slow `snow` broken.** The first `snow --version` after a
  Mac sat idle took 24.8 s on about 1 s of CPU, past the 15 s probe timeout, so
  doctor printed `[BROKEN ] snow` with reinstall advice and then skipped the
  connection checks. The `snow` probes now wait 45 s. A timeout is a warning to
  re-run doctor, never a failure, and the connection listing still runs once
  afterwards. If that listing also fails or times out, `snow-connection` and
  `snow-key-file` say "not checked" with the reason instead of suggesting
  `snow connection add`. A `snow` that exits non-zero, such as an import crash,
  is still `BROKEN` with the `uv tool install snowflake-cli` hint.

### Changed

- **The deploy workflows install `streamsnow>=0.7.2,<0.8`** (was `>=0.7.1`),
  the first release whose `verify-deploy` actually evaluates the live-version
  and version-source checks. `streamsnow update --apply` writes the new pin
  along with the no-apps guard.

## [0.7.1] - 2026-09-27

The launch-fix release. An end-to-end run of the install path from an empty
folder (PyPI package plus the marketplace plugin) found that the build half
worked and the ship half did not: the plugin setup path never wrote the
governed repo files, a fresh scaffold failed its own CI, and nothing created
the Snowflake objects a first deploy needs. A second pass, building a
dashboard over a historical sample dataset with a key-pair connection, found
the rest: empty review SQL and empty default dashboards on data that ends in
the past, a local preview crash, and a starter app that could still ship.

### Added

- **`streamsnow agent-skills install --agent codex`** (and `list`): copies the
  skills and their shared recipes into `.agents/skills` (repo scope, committed)
  or `~/.agents/skills` (user scope), where OpenAI Codex CLI finds them
  (tested with 0.157.1). A manifest keeps a re-run from overwriting edited
  skills; `ship-app` and `migrate-app` stay explicit-only in Codex, as in
  Claude Code. The wheel now ships the skills (`streamsnow/_skills`), `init`'s
  `Next:` block names the Codex install, and `skills/_shared/other-agents.md`
  covers what reads differently outside Claude Code (skill syntax, subagents,
  checkpoints, and the plugin hooks, which have no Codex equivalent). README:
  "Use with other agents".
- **`streamsnow init --no-starter-app`**: the config wizard (or an existing
  config) plus the governed repo files (`AGENTS.md`, `CLAUDE.md`, `.gitignore`,
  `.pre-commit-config.yaml`, CI and deploy workflows, `README.md`,
  `deploy/tombstones.yml`) with no example app. `/start-app --setup` now runs
  it instead of `configure` alone.
- **`streamsnow deploy-setup --admin`**: the full, reviewable one-time admin
  bootstrap derived from `streamsnow.config.yaml`, in `USE ROLE` sections
  (SYSADMIN, USERADMIN, SECURITYADMIN, ACCOUNTADMIN, then the CI role): app
  database, schema and `XSMALL` warehouse, CI and viewer roles, a
  `TYPE = SERVICE` CI user with an `RSA_PUBLIC_KEY` placeholder, grants,
  governance reads (`USAGE` + `SELECT` per allowed schema, or
  `IMPORTED PRIVILEGES` for a shared database), and for the container runtime
  the PyPI external access integration on Snowflake's managed
  `snowflake.external_access.pypi_rule` plus compute pool `USAGE`.
  `CREATE COMPUTE POOL` is emitted only for a pool other than the
  pre-provisioned `SYSTEM_COMPUTE_POOL_CPU`.
- **`validate-app` `placeholders` check**: fails while any authored app file
  (query, page or `sql_review` manifest) still carries the scaffold's
  `YOUR_TABLE`, or while the starter page still shows its hard-coded sample
  metric and chart (marked `STREAMSNOW_STARTER_PLACEHOLDER`; the sample values
  themselves match too, so pages scaffolded by 0.7.0 are caught). CI deploys
  every app under `apps/`, so the placeholder app used to ship beside the real
  one. The sample values never read the query, so replacing `YOUR_TABLE`
  everywhere does not clear the page on its own.
- **`doctor` `snow-key-file`** (optional, warns): the default `snow`
  connection uses key-pair auth with no `private_key_file`, the signature of a
  key named `private_key_path` (see Fixed). Reads parameter names only, never
  values, and never edits the connection.
- **`sql-review check` `window` finding** (advisory, never gates under either
  coverage policy): a manifest with no `set_block` renders the implicit review
  window, the year ending `CURRENT_DATE`.
- **`preview logs` ends with a `cause:` line** for a known failure, including
  ones raised on the first page load after `start` already reported ready.
- **`examples/tpcds-demo/`**: a small store-sales extract from
  `SNOWFLAKE_SAMPLE_DATA.TPCDS_SF10TCL` into `STREAMSNOW_DEMO.TPCDS`, about two
  years with real seasonality, kept small to limit the scan. Its README covers
  the cost and how to point `/start-app` at any small table instead.
- **`SECURITY.md`**: report vulnerabilities through GitHub private
  vulnerability reporting; everything else through issues.
- **Generated `.gitignore` ignores `.internal/`** (local-only working notes).
  `.gitignore` is user-owned, so existing repos add the line by hand.
- **`doctor`**: `gh` (optional; `/ship-app` needs it) and, in a
  container-runtime repo, `container-python` (warns when no Python 3.11 is
  findable; fix: `uv python install 3.11`).
- **README "Who can use this"** and a getting-started block on what to ask a
  Snowflake admin for.

### Fixed

- **Every first CI deploy failed with `Connection default is not
  configured`.** Snowflake CLI 3.27.0 run with only `SNOWFLAKE_*` environment
  variables and no `config.toml` (a GitHub Actions runner) needs
  `--temporary-connection`, and the generated `deploy.yml` passed no connection
  flag. Every `snow sql`, `snow stage copy` and `snow git fetch` call in both
  deploy workflows now passes it; `verify-deploy` gains a
  `--temporary-connection` option the workflows pass (leave it off locally to
  use your default connection); and the passphrase secret reaches `snow` as
  `PRIVATE_KEY_PASSPHRASE`, the only name it reads for an encrypted key. The
  deploy workflows install `streamsnow>=0.7.1,<0.8`. Existing repos pick this
  up with `streamsnow update --apply`, which re-renders
  `.github/workflows/deploy.yml` (troubleshooting #19).
- **`streamsnow new` warns when repo files are missing**, naming them and the
  fix (`streamsnow init --no-starter-app`). Without them there were no hooks or
  CI, and nothing gitignored `.streamlit/secrets.toml`.
- **A fresh scaffold failed its own CI.** The generated `checks.yml` installed
  ruff unpinned while pre-commit pinned v0.15.9; newer ruff defaults flagged
  `I001`, `RUF100`, `C408` and `BLE001` in the templates. One `RUFF_VERSION`
  now renders into both files, and the templates are clean under 0.15.9 and
  current ruff defaults.
- **`doctor` passed a `snow` that crashes.** It now runs `snow --version`; on
  PATH but failing is a required `BROKEN` result with the
  `uv tool install snowflake-cli` hint. The `snow` probe timeout rose from
  5 s to 15 s, because a cold start reported "no connections".
- **Local preview install for warehouse apps.** `uv pip install -e apps/<slug>`
  fails without a `pyproject.toml`; docs, the scaffolded README, `init`'s
  `Next:` block and `streamsnow preview` now give the runtime-specific line.
- **`deploy-setup` suggested creating `SYSTEM_COMPUTE_POOL_CPU`**, which
  Snowflake pre-provisions. The default output keeps its statements and
  corrects the comment.
- **`init`'s `Next:` block** lists the plugin install first, matching docs
  Path B, and names the `YOUR_TABLE` step.
- **Warehouse apps pin Streamlit 1.52.2** (was 1.50.0), the newest version
  Snowflake supports in warehouse runtimes. Every supported version carries
  two Streamlit advisories fixed only in 1.53.1 and 1.54.0, so warehouse repos
  also get `osv_allowlist.json`: one dated entry per advisory ID, each with its
  reason, expiring 2026-12-31 for a re-check. `streamsnow update` creates the
  file in warehouse repos scaffolded before it existed.
- **Review SQL and default dashboards were empty on historical data.** With no
  `set_block`, a manifest's review window was the year ending `CURRENT_DATE`,
  so every per-visual `.review.sql` over data that ends in the past (TPC-DS
  ends in 2003) returned zero rows while `check` reported clean. The skills now
  require a window anchored to the source's `MAX(date)`
  (`review-app/sql-companions.md` step 3, `start-app/pages.md` 8.2), the page
  conventions say a page's default date range comes from the data's max date,
  not today, `check` names the implicit default, and the starter manifest
  shows the anchored form. An explicit `set_block`, including an explicit
  `CURRENT_DATE`, renders exactly as before.
- **Local preview crashed with key-pair connections**: `TypeError: Expected
  bytes, RSAPrivateKey, or EllipticCurvePrivateKey, got <class 'NoneType'>`.
  `st.connection("snowflake")` opens the default connection through
  snowflake-connector-python, which reads `private_key_file` but silently drops
  `private_key_path`, a legacy alias only the `snow` CLI understands, so `snow
  sql` worked on the same connection. The fix is to rename the key to
  `private_key_file` (passphrase: `private_key_file_pwd`), which both tools
  read; `doctor` and `preview logs` now say so (troubleshooting #17).
- **The wizard's default compute pool is `SYSTEM_COMPUTE_POOL_CPU`** (was
  `STREAMLIT_POOL`), so `deploy-setup --admin` for a default container config
  emits no `CREATE COMPUTE POOL`. A pool already named in a config is kept.
- **`connection_name` defaults to your default `snow` connection** when one
  exists and its `account` matches the account you answer (case-insensitive;
  was the folder slug), falling back to the slug when `snow` is missing or
  broken, or the default connection is for another account or names none (a
  one-line note says so, without printing either account). Users with a
  working connection from a prior tutorial no longer fail `doctor`'s
  connection check or get told to add a second `--default` connection; `init`
  and `configure` only print `snow connection add` when no connection by that
  name exists.
- **The starter trio from `streamsnow new`** (`queries/example_metric.sql`, its
  `sql_review` manifest, `pages/overview.py` with sample numbers) is replaced
  explicitly by `/start-app`'s build phase, and `new` says they are
  placeholders.
- **A fresh `init --no-starter-app` repo failed its own `checks.yml`**: with
  no `apps/` directory yet, `ruff check apps/` errored and `check tombstones`
  refused to run. Lint now runs only when `apps/` exists, and tombstones is
  skipped only while `apps/` is absent both locally and on `origin/main`.
- **`uv pip install -e apps/<slug>` failed on every fresh container app**
  (setuptools found `pages/`, `queries/` and `sql_review/` and refused to
  guess a package). The generated `pyproject.toml` declares `packages = []`;
  existing apps add the two lines from troubleshooting #18.

### Changed

- **Privacy gate.** `check_export_clean` holds generic checks only (personal
  paths, private keys, tokens, email addresses outside reserved example
  domains) and reads organization-specific terms from a gitignored
  `.streamsnow/export-denylist.txt`. A committed deny list shipped the names it
  guarded.
- **README positioning**: for internal analytics StreamSnow can replace a BI
  tool; external or customer-facing analytics needs additional customization.
- **Default Snowflake object names are StreamSnow-branded.** The wizard,
  `streamsnow.config.example.yaml` and the loader now default to
  `STREAMSNOW_APPS` (app and stage database), `DASHBOARDS` (schema),
  `STREAMSNOW_WH`, `STREAMSNOW_DEPLOY_ROLE` (whose CI user is
  `STREAMSNOW_DEPLOY_USER`), `STREAMSNOW_VIEWER_ROLE` and
  `STREAMSNOW_CODE_STAGE`, so `LIKE 'STREAMSNOW%'` finds every database,
  warehouse, role, user and stage `deploy-setup --admin` creates.
  `PYPI_ACCESS_INTEGRATION` is unchanged. Existing repos keep whatever their
  `streamsnow.config.yaml` says: a name set there wins over the default, and
  re-running `configure` keeps it. The one default an existing config can
  inherit is `snowflake.objects.stage_name`, which is optional and which the
  wizard never wrote: a stage-copy repo without it deploys through
  `STREAMSNOW_CODE_STAGE` from its next deploy (the deploy's `CREATE STAGE IF
  NOT EXISTS` creates it, using the CI role's `CREATE STAGE` grant). To keep
  the stage you have, add `stage_name:` with its name under
  `snowflake.objects`.

## [0.7.0] - 2026-09-11

The onboarding release. Everything here came from three sources read together:
a walk through the install path as a first-time user, the production fleet's
`.streamsnow/MIGRATION.md` (its honest reasons for pausing adoption at 0.6),
and a fresh read of the official Streamlit-in-Snowflake docs, which Snowflake
reorganized in early 2026. An adversarial Codex review of the plan removed a
comment-marker artifact waiver and a phase-suffix parser that would each have
weakened a gate; both landed as typed config instead.

### Added

- **Mission, vision and eight principles** at the top of the README (and in
  CONTRIBUTING as the review bar). Two principles are new and come from the
  fleet's pause: *faithful to a real fleet* and *leaving should be cheap*.
- **`sql_review: {coverage: warn | fail}`** in `streamsnow.config.yaml`
  (default `warn`). Every `sql-review check` finding now carries a `kind`
  (`coverage | fragment | collision | orphan | bind | provenance | readonly`);
  correctness kinds always fail, coverage follows the policy — in `validate-app`,
  pre-commit and CI alike. This replaces 0.6's "warn now, FAIL in 0.7" promise
  with a per-repo switch, and it is the fleet's own design note for the tool
  (it had been filtering coverage by matching finding *text*).
- **`deploy.artifact_exclude`**: typed exclusions for non-code files a deploy
  pipeline ships by another step (`.streamlit/config.toml` in the generated
  workflow and in the fleet). Exact relative paths only; `streamlit_app.py`,
  `pages/`, `queries/`, `*.py` and `*.sql` are rejected at config load, so the
  artifacts gate can never become an opt-out for code.
- **`**Phase notes:**`** line in §11: progress narrative lives there, and
  `**Current phase:**` stays the exact value `/start-app` resumes on.
- **Doctor checks:** `pre-commit` (optional without a config, required once one
  exists — the generated hooks are `language: system`) and `snow-connection`
  (does the configured `snow` connection exist; never runs `connection test`).
  The config result now carries `runtime` and `connection_name`.
- **`tests/fixtures/fleet/`**: an anonymized three-app repo mirroring the shapes
  that failed `validate-app` on every real app in MIGRATION.md (pipeline-shipped
  config.toml, narrative phase notes, a waived narrow session fallback,
  brace-less `-- Tokens:` headers, a package-qualified glossary helper, a nested
  page package). All three pass, and a second test keeps the shapes present.
- **`docs/snowflake-docs.md`**: every official Snowflake / Streamlit docs link
  the toolkit relies on, as a compatibility matrix with scope, retrieved date
  and StreamSnow's position where it deliberately differs. Offline registry
  test (`tests/test_docs_links.py`) plus an opt-in online sweep
  (`scripts/check_docs_links.py --online`, now in RELEASING.md).
- **`docs/troubleshooting.md`**: sixteen numbered symptom / cause / fix entries.
- **Skill/CLI parity test**: every `streamsnow <verb>` a skill or doc cites must
  exist in the CLI (argparse passthrough groups included).
- **SessionStart hook test** and a `skills/_shared/page-conventions.md` recipe
  (the glossary module and four-block page contract the fleet converged on).

### Changed

- **Onboarding is two lanes.** README and getting-started open with *With Claude
  Code* (three commands, `/start-app --setup` drives everything) and *CLI only*.
  `--setup` now installs the `streamsnow` CLI itself when missing and requires
  it before continuing; the `uvx` fallback is gone because every later skill
  calls bare `streamsnow`.
- **One connection store.** `snow connection add … --default` is the documented
  path; `st.connection("snowflake")` reads it locally, and the per-app
  `secrets.toml` is an optional override. `configure`, `init` and the preview
  classifier say so.
- **`streamsnow preview`** launches `<repo>/.venv/bin/streamlit` when it exists
  (the documented `uv venv && uv pip install -e apps/<slug>` never activates
  the venv), then PATH.
- **`/start-app` runs preview, validate and the review procedure itself** at
  the build/check phases instead of naming them for the user to type; it still
  stops at the three human checkpoints. Standalone verbs unchanged.
- **SessionStart banner** prints the plugin version, nudges `/start-app --setup`
  in a repo that has Streamlit apps but no config (it used to stay silent), and
  says when the CLI is missing from PATH.
- **`init`'s Next block** lists both plugin commands, the pre-commit install,
  `validate-app`, and the README Apps-table reminder; `new` prints the reminder.
- `snowflake-cli` everywhere (`snowflake-cli-labs` dropped from the doctor hint
  and both deploy workflow templates); generated CI pins `streamsnow>=0.7,<0.8`.
- Pre-commit hook `description:` blocks name the incident behind each check.
- Docs facts refreshed against the reorganized Snowflake docs: container runtime
  GA (2026-03-09), `get_active_session()` is warehouse-only and not thread-safe
  on containers, message limits differ by runtime, compute-pool packing, MFA
  rollout phase 3, `python=3.11` in the official `environment.yml` example vs
  our incident-backed gate, artifact repositories vs EAI.

### Known gaps

- PyPI access via a **Snowflake artifact repository** (now Snowflake's preferred
  path) is not emitted; the Snowflake CLI's `snowflake.yml` schema has no field
  for it yet. The generated EAI keeps working; do not attach both.
- `environment.yml` `python=3.11` (single `=`, as the official example shows)
  is still rejected. Re-test against a live warehouse app before relaxing.
- `check dependency-vulns` scans per-app manifests, not `uv.lock`.
- Deploy every app on every merge; a changed-apps-only option is planned.
- No `--vendor` mode yet; the exit path is documented as it is in
  `docs/distribution.md`.
- The generated `checks.yml` runs the governance gate, OSV, sql-review and
  tombstones but not `detect-secrets` or `ruff format` — those run in
  pre-commit only. `schema_allow` stays a convention; only `schema_deny` is
  enforced (documented in the generated AGENTS.md).
- The starter page renders mock numbers until `YOUR_TABLE` is repointed; a
  fresh scaffold therefore "works" without ever reaching Snowflake. Called
  out in the generated README.
- The deploy workflow exposes the Snowflake secrets to every step and pins
  actions by tag; scoping secrets per step and SHA-pinning are planned.

### Planned (next major)

- Fold `/preview-app` and `/validate-app` into `/start-app` modes and
  `/audit-lineage` into `/review-app --lineage`; drop the 8 pre-0.3 alias stubs
  (16 → 5 in the inventory, ~500 always-on tokens saved).

## [0.6.3] - 2026-09-05

Hotfix. Found by reviewing the 0.6.2 round-8 commit after it shipped — it had
never been reviewed before publishing.

### Fixed

- **`x$$y` was refused as an unterminated dollar-quote.** Snowflake permits `$`
  inside unquoted identifiers, so `x$$y` is a legal column name; 0.6.2's
  fail-closed guard treated every `$$` as a constant opener and refused the
  whole file. A `$$` now opens a constant only when it does not continue an
  identifier. The closing `$$` is unchanged, since a body may end in an
  identifier character (`$$abc$$`).
- **`COMMENT IF EXISTS ON …` and `COMMENT ON TAG|SHARE|MASKING POLICY|…`
  slipped past the SET-expression scan.** The command pattern omitted the
  documented optional `IF EXISTS` and most object types. Not executable after a
  `)` in Snowflake, but it falsified the stated invariant. Pattern widened.
- **`set_vars.name` accepted any non-blank string**, so `"bad name"` passed
  validation and rendered `SET bad name = 1;`. It must now be a session-variable
  identifier. A used entry missing `default` also raised `KeyError` in the
  renderer instead of being skipped.
- **`//` line comments were not masked** — Snowflake accepts them alongside
  `--`. An apostrophe inside one opened a phantom string literal that ran to the
  next `'` and hid real SQL from every guard, and because that literal
  *terminated*, the fail-closed path could not catch it. The sixth masking
  bypass of the same class. Found by a second review of the shipped commit,
  before this hotfix was tagged.
- **`CALL start()` had stopped being a command.** Excluding clause keywords
  after a verb (to stop refusing bare aliases) also excluded procedures named
  after them. `CALL` now also matches any identifier immediately followed by
  `(`, which a bare alias never is.
- Widened `COPY FILES INTO`, `UNDROP ICEBERG|DYNAMIC|EXTERNAL|EVENT TABLE`,
  `TRUNCATE IF EXISTS`; `NATURAL`/`ASOF` joins after a bare alias no longer
  refused; a name declared in both `set_block` and `set_vars` is rejected
  (it rendered two `SET` lines and the second silently won) — including against
  the implicit default `set_block` and case-insensitively, since session-variable
  names are, and the renderer itself now never emits a second `SET` for one
  name; tripwire messages
  now name only the verb; `_var_used` and `_BIND_RE` changes pinned by tests.

## [0.6.2] - 2026-09-03

Everything here came out of adopting 0.6.1 on a real 5-app repo with 231
queries and 28 existing audit manifests — the failures a greenfield scaffold
never surfaces.

### Fixed

- **Read-only guard bypass via double-quoted identifiers** (security). Snowflake
  treats `"…"` as a delimited identifier rather than a string, so the masker
  left it alone — and `WITH x AS (SELECT 1 AS "x) SELECT y") DELETE FROM t`
  passed `_verify_read_only` clean, because the `)` inside the identifier ended
  the CTE scan early and the trailing `SELECT` read as the terminal verb while
  Snowflake would execute the `DELETE`. A second review round found two more quoting
  forms with the same hole: **dollar-quoted constants** (`$$ ) SELECT y $$`),
  which were not recognised as a quoting form at all, and **backslash-escaped
  quotes** (`'\')`) — Snowflake accepts both `''` and `\'`, but only the
  doubling form was handled. All four quoting forms are now masked for
  structural analysis, and each is pinned in both directions.

  Masking identifiers also introduced a false *positive*: a legal delimited CTE
  name (`WITH "cte name" AS (SELECT 1) SELECT …`) made the CTE walker bail and
  the file be refused. Refusing to generate a legitimate audit file is a defect
  too, so the walker now accepts a quoted CTE name.

  It briefly introduced a **regression**, too: an odd `"` inside a `$$…$$`
  constant (`SELECT $$5" pipe$$ AS a; DELETE FROM t;`) masked to end-of-text and
  hid every following statement, turning a write 0.6.1 *caught* into one that
  passed. Dollar quotes are now consumed before `"` is treated as a delimiter.

  Four bypasses in one hand-rolled masker is a pattern, so there is now a
  second, independent layer: a write verb in command position is refused with no
  parsing of its own. Two anchors, deliberately different — at the START of a
  statement ANY of these verbs is a command, while right after a `)` only
  RESERVED words are checked, because that is where a bare column alias lives
  (`SELECT MAX(d) comment FROM t` is legal Snowflake, and `comment` is a real
  `INFORMATION_SCHEMA` column). The non-reserved verbs are still covered there
  in their two-token command form (`MERGE INTO`, `TRUNCATE [TABLE]`,
  `COMMENT ON <object-type>`, `COPY INTO`, `EXECUTE IMMEDIATE|TASK`,
  `UNDROP TABLE|SCHEMA|DATABASE`, `REMOVE|RM @`, `PUT file://`, and
  `CALL`/`UNSET` with an argument). Matching the verb plus "any identifier" is
  NOT sufficient: a bare alias is followed by a CLAUSE keyword (`FROM`,
  `WHERE`, `ON`, `,`) which is itself identifier-shaped, so clause keywords are
  excluded explicitly. `COMMENT ON` must name an object type, because
  `JOIN (SELECT …) comment ON a.id = …` is legal read-only SQL. There is no `;`
  anchor: statements are split on `;` before this runs. The
  allowlist depends on locating statement boundaries correctly and that has
  been defeated four times; the next parser gap should not also be a pass.

  Position, not bare tokens: most of these verbs are **not** Snowflake reserved
  words, so `SELECT 1 AS CALL`, `AS COPY`, `AS PUT`, `AS REMOVE`, `AS UNLOAD`
  and `AS EXECUTE` are all legal read-only SQL that a blanket token match
  rejected. Refusing to generate a legitimate audit file is its own defect.
  `COMMENT` and `UNDROP` are covered too. Zero false positives across the 30
  real audit files of the adopting repo — a floor, not a proof: review later
  found two more false-positive classes (a JOIN alias before `ON`, and a
  `set_block` expression continuing past a subquery), both now fixed and
  pinned.
- **Unsubstituted binds could ship in a rendered file.** A manifest declaring
  only `:1`/`:2` for a query that also uses `:3` emitted seven live
  `AND col <= :3` predicates. Every existing gate passed it: the allowlist
  checks statement roots, provenance checks input hashes, coverage checks
  claims — none asks whether the output *runs*. `generate` now refuses to write
  a file with a surviving `:N`, and `check` additionally audits the committed
  bytes (still import-free), so a hand-edited or pre-guard file is caught
  without regenerating. `Params:` banner comments stay exempt by construction.
- **SET blocks that promised a review window they did not control.** Three
  files declared `start_date`/`end_date`, referenced them zero times (their
  queries self-anchor on `CURRENT_DATE`/`DATE_TRUNC`), and carried a header
  telling the reviewer to edit those lines to change the window. The reviewer
  edits, reruns, gets byte-identical numbers, and signs off believing the
  window applied — a confidently wrong verification, which is worse than no SET
  block. The block is now pruned to the variables the body actually references,
  omitted entirely when none survive, and the header describes what was really
  emitted.

  Two follow-on fixes to that pruning: variable matching is **case-insensitive**
  because Snowflake identifiers are (`$START_DATE` is `$start_date`, and
  matching case-sensitively would prune a SET line that IS used, leaving a
  dangling reference — a worse failure than the unused line the pruning
  removes); and it reads masked text, so a `$name` inside a string literal or a
  quoted identifier no longer counts as a reference.

- **A referenced-but-undeclared session variable could ship.** SET-block pruning
  removed lines for declared variables a body never referenced; nothing checked
  the converse. A manifest whose `param_bindings` named a variable absent from
  `set_block` rendered `WHERE d BETWEEN $window_start AND $window_end` with NO
  SET block, under a header stating no section references a session variable,
  and both `generate` and `check` reported success. Pasted into Snowsight that
  dies on the first block. `generate` and `check` now refuse a `$var` with no
  `SET` line - the symmetric half of the bind check.
- **A `SET`-rooted statement escaped both read-only layers.** The tripwire used
  `search` (first match only) and then skipped the whole statement on the `SET`
  exemption, while the allowlist's `SET` form is prefix-only - so everything
  after the `=` was examined by neither, and `SET x = (SELECT 1) DELETE FROM t`
  passed clean. Not exploitable (Snowflake syntax-errors at the `)`), but it
  falsified the stated invariant. Now `finditer`, the exemption excuses only the
  match at offset 0, and the whole expression is scanned for the same reserved
  verbs and two-token commands the after-paren anchor looks for. To be precise
  about what that does and does not close: it is a denylist over those forms,
  so an unlisted command shape (`USE ROLE`, `GET @s`, `COMMIT`) still parses as
  a valid SET expression. None of those is executable after a `)` in Snowflake,
  and the root allowlist refuses them as statement roots — but the mechanism is
  a denylist, not a proof that "verbs nobody listed" are closed.
- **Path-dependent provenance, and two gates that silently passed.** Three tools
  filtered on the ABSOLUTE path's components, so a checkout under any dotted
  directory - a git worktree at `.claude/worktrees/<name>/`, which this
  project's own guidance recommends - made every file look hidden. Consequences:
  `sql-review` hashed zero app modules, so provenance differed between a
  worktree and a clean clone and a contributor saw false `DRIFT`; and
  `check app-security` and `check schema-refs` scanned NOTHING and reported OK.
  A gate that passes because it examined zero files is worse than no gate. All
  three now filter relative to the scan root, or by directory name where no root
  exists. Each argument carries its own root, so a checkout living under a path
  segment literally named `venv` or `node_modules` is scanned in full rather
  than skipped wholesale — while a `.review/` artifact handed over by explicit
  path is still skipped. Both fixes are mutation-covered: reverting either now
  fails tests, where before it left the suite green.
- **A `SET` statement's expression was unchecked past its prefix.** The
  allowlist's `SET` form was prefix-anchored, so `SET x = (SELECT 1) DELETE
  FROM t` matched it and every token after the `=` was examined by neither
  layer. Chasing that with more tripwire verbs fixed one verb at a time; a
  `SET` statement must now END where its expression ends, which closes the
  class including verbs nobody listed (`CALL`, `UNLOAD`, `UNSET`). An empty
  `set_block` value (`SET x = ;`) is rejected at validation, since it was
  invalid SQL that still read as a definition to the session-variable check.
- **Snowflake's multi-variable `SET (a, b) = (…)` was falsely refused** — the
  session-variable check recorded neither name and then reported both as
  undefined.
- **`COMMENT ON` matched a JOIN alias.** `JOIN (SELECT …) comment ON a.id =
  comment.id` is legal read-only SQL; it now requires an object type after
  `ON`, which a JOIN never has.
- **Duplicate `fragments` declarations across manifests** were silently
  accepted with the first reason winning; now reported.
- **A symlinked app module made provenance environment-dependent** again by
  hashing whatever the target held in that checkout; the link text is hashed
  instead.

### Added

- **`fragments` manifest field** — declares a `queries/*.sql` that is a shared
  CTE inlined via a token and therefore not independently runnable, exempting
  it from coverage. Without this the gate is unsatisfiable for any repo that
  factors CTEs into their own files. Exemption is explicit and never inferred
  from a filename, so it cannot be used to silence the gate by renaming; a
  declaration whose file no longer exists is itself a finding, and the `reason`
  renders into the README index.

  Because a fragment declaration *suppresses* a gate, its shape is validated
  strictly rather than best-effort: a bare list of strings (the form that looks
  like it works) is an error instead of a silent no-op, `file` must name exactly
  one file in `queries/` with no path separators or traversal (`../../x.sql`
  would otherwise reduce to a stem and exempt a different file), `reason` is
  mandatory, duplicates are rejected, and a query that is both page-claimed and
  declared a fragment is a contradiction rather than a silent last-wins. That
  last check works both WITHIN a manifest (rejected at validation) and ACROSS
  manifests (coverage resolves toward the stricter reading, ignores the
  exemption, and reports it) - the cross-manifest half was missing in review and
  produced two contradictory README rows for the same query, one of them reading
  `Verified: n/a` for a query that feeds a page.
- **`set_block_note` manifest field** — renders the rationale for the defaults
  (which source the bounds derive from, why that source and not the calendar,
  mechanics that bite when editing them) inline above the SET lines, where the
  auditor actually reads it.

## [0.6.1] - 2026-09-01

### Added

- **sql-review metrics mode** (`"mode": "metrics"` manifests): one AUTHORED
  runnable block per dashboard visual, in on-screen order, from files under
  `sql_review/_metrics/*.sql` (or a `queries/*.sql` when a visual is 1:1 with
  an app query — that reference also claims the query for coverage). A
  dashboard-map index heads the single rendered file. Sources are allowlisted
  to the two roots (traversal-safe), digest-pinned like every other input,
  and never imported — metrics blocks are static by nature. For dashboards
  whose visuals aggregate differently than any single app query.
- **Repo overlays** — project-level augmentation of the plugin's skills
  without forking them: every skill now reads `.streamsnow/overlays/<skill>.md`
  (committed, repo-owned) first, applying repo-specific additions/overrides;
  `overlays/all.md` applies to every skill. Overlay prose cannot disable the
  coded safety gates (hooks, validate, the sql-review read-only guard, CI).
  Contract: `skills/_shared/overlays.md`; a plugin-surface test pins the
  overlay point in every skill.

### Changed

- The generated `.gitignore` now excludes only `.streamsnow/preview/` (was all
  of `.streamsnow/`) so committed overlays fit under `.streamsnow/overlays/`.
  **Existing repos:** `.gitignore` is user-owned and `streamsnow update`
  never rewrites it — hand-edit the `.streamsnow/` line to `.streamsnow/preview/`
  before adding overlays, or git will ignore them silently.
- The migrate engine's Anaconda repodata cache moved into a private
  per-user directory (created 0o700, ownership-verified; unique-temp atomic
  writes). A predictable shared temp file could collide across users and be
  pre-seeded; any doubt about the directory now means fetch-fresh, never a
  trusted read.

## [0.6.0] - 2026-09-01

The enforcement release: the review-escalation loop becomes executable (one
gate, one Stop hook, loop primitives instead of prose), every app grows a
human-runnable SQL audit trail, and the generated pipeline gains its missing
delete path — detection automated, destruction only by committed consent.

> **Upgrading the plugin — reinstall required:** this release adds a `Stop`
> hook to `hooks/hooks.json`, and hook additions do **not** reach installed
> copies via autoUpdate (Claude Code pins the install path — see claude-code
> issue #52218, same dance as v0.4). Reinstall: `/plugin uninstall streamsnow`
> then `/plugin install streamsnow@streamsnow`, and relaunch.

> **Upgrading a consumer repo:** run `streamsnow update --apply` to receive the
> new pre-commit hooks (`page-imports`, `path-leaks`, `sql-review`,
> `dependency-vulns --best-effort`), the regenerated CI gates, and the deploy workflows'
> tombstone-reconcile step. The `validate-app` gate tightens — a
> previously-passing app can newly fail `page-imports`, `path-leaks`, or
> `requirements` (one-line fixes each; see **Changed** below). The new
> `sql-review` section only **warns** in 0.6, so missing audit trails don't
> block this upgrade. `deploy/tombstones.yml` is scaffolded on `init` for new
> repos only — `streamsnow update` never creates or rewrites it (it's a
> user-appended registry). In an existing repo, create the file by hand at the
> first rename or removal: a missing registry reads as empty, so
> `check tombstones` fails the PR until the file and its entry exist.

### Added

**Review gate + Stop hook**

- **`streamsnow review-gate`** (`classify` | `baseline` | `stamp` |
  `stop-hook`) — the single decision function for "does this change need
  review, and how deep?". In the source monorepo each caller hand-rolled its
  own substantive-vs-trivial bash and they drifted until the full review loop
  had no executable caller at all; now `/ship-app`'s preflight,
  `/feedback-app`'s follow-up, and the Stop hook all call the same `classify`.
  Coverage is **per-change, not per-app**: review artifacts record a
  `Reviewed-baseline:` digest plus per-file coverage keys computed from the
  AST shape (comments/docstrings stripped), so a comment reword stays
  reviewed while a logic change reopens exactly the files it touched.
- **Warn-only `Stop` hook** (`hooks/review_gate_stop.py`) — when a turn ends
  with a substantive app change no review artifact covers, it emits a one-line
  `systemMessage` pointing at `/review-app <slug> --auto`. Never blocks a
  turn; fail-open (any internal error exits 0); repo-gated on
  `streamsnow.config.yaml`; dedupes per (repo, slug, baseline) so the same
  unreviewed state nudges once. The payload is `systemMessage`-only on
  purpose: emitting `additionalContext` from a Stop hook was measured
  (2026-08-04) to start a fresh assistant turn with no user input — an
  unrequested turn per change. Off-switches: `REVIEW_GATE_OFF=1`, an
  `apps/<slug>/.review/SKIP` marker, or `review_gate: {enabled: false}` in
  `streamsnow.config.yaml` (block added to the example config). The hook
  executes the gate **by path** from the plugin root, so it works on
  plugin-only installs with no `streamsnow` pip package.
- **`streamsnow review-loop`** (`parse-findings` | `dedup-findings` |
  `write-resolutions` | `exit-condition` | `merge-findings`) — the
  deterministic primitives `/review-app --auto` runs each cycle, extracted so
  the loop doesn't re-derive its dedup from prose (which re-reports findings
  it already resolved and never converges). Dedup is against everything
  previously *resolved* — including judge-rejected findings — within a 7-day
  artifact window; `exit-condition` names its reason (`max-iterations`,
  `plateau`, `walk-degraded`, `walk-reentry`, `clean`, `continue`) so a
  transcript shows *why* the loop stopped.

**SQL-review audit trail**

- **`streamsnow sql-review`** (`discover` | `generate` | `check` | `index`) —
  every query under `apps/<slug>/queries/` (the convention UI-feeding SQL
  lands in; SQL inlined in Python is outside its reach) gets a fully-rendered,
  paste-runnable `.review.sql` under `apps/<slug>/sql_review/`, driven by
  per-feature JSON
  manifests in `sql_review/manifests/` (co-located with the app so a rename
  moves the audit trail with it). Rationale: apps store `{TOKEN}` + `:N`
  templates a reviewer can't run, and a dashboard whose numbers nobody can
  independently re-run is a dashboard nobody can sign off.
- **Import-free `check`** — each generated file carries a provenance line
  (`-- Provenance: schema=1 inputs=<sha256/16> output=<sha256/16>`) digesting
  the manifest, the referenced query templates, and (manifest strategy) the
  app modules the token dispatchers call into. `check` recomputes both hashes
  statically — it **never imports consumer app code** (a shared hook importing
  arbitrary modules would execute code on every commit) — so an edited
  template, manifest, module, or hand-edited rendered file all read as DRIFT,
  and every `queries/*.sql` must be claimed by some manifest (uncovered is a
  named failure, never a silent skip). Only `generate` may import app code,
  and only under the opt-in `manifest` token strategy on a developer machine.
- **Read-only guard** — rendered output is verified against a statement-root
  **allowlist** (`SELECT` / `WITH`→`SELECT` / `SHOW` / `DESCRIBE` / `EXPLAIN`
  plus `SET <ident> =` session variables), an allowlist rather than a
  write-verb denylist because the failure mode of a denylist is the statement
  type nobody thought of. Structural analysis runs with string literals and
  comments masked. Scope honesty (documented in the tool): this catches
  accidents and drift, not a deliberate committer — repo review remains the
  trust boundary.
- `streamsnow init` / `streamsnow new` render a starter manifest for the
  example query and run `sql-review generate`, so the audit-trail pattern is
  live in the tree from commit 1. Wired into pre-commit
  (`streamsnow-sql-review`), generated CI, and the validate gate (warn-only
  this release — see **Changed**).

**New tools and verbs**

- **`page-imports` check** — blocks bare imports of modules that live in an app
  subdirectory (`from _header import ...` for `pages/_header.py`). Deployed,
  only the app root is on `sys.path`; `streamlit run` *additionally* puts the
  executing page's own directory there, so this class boots clean locally,
  survives a full UI walkthrough, and then `ModuleNotFoundError`s on every
  affected page in production. Also flags the quieter variant: a name present
  both in a page's own directory and at the app root (or in stdlib, or in a
  dependency) resolves to a *different file* in each environment. Local
  verification is structurally incapable of catching either — the check is the
  only thing that can. (From a real four-page outage that shipped with green
  CI, a live local boot, and a clean click-through of every page.)
- **`dependency-vulns` check** — queries OSV.dev for every **exact** pin in
  `pyproject.toml` / `environment.yml` (one batched POST, stdlib urllib);
  a new CVE against an existing pin fails the next run, so the gate ages with
  the ecosystem, not the repo. Range pins are reported "unscanned" but never
  fail — they have no single version to query. Findings can be
  expiry-dated-allowlisted in `osv_allowlist.json`; `--best-effort` (the
  pre-commit default) warns instead of failing when OSV is unreachable, while
  generated CI runs it fail-closed as the authority.
- **`path-leaks` check** — blocks personal absolute paths
  (`C:/Users/<name>/…`, `/Users/<name>/Development/…`, `/home/<name>/…`) in
  committed `.py`/`.md`: broken for every other developer, and a real
  username leaked into a repo that may become public. Placeholder forms like
  `<user>` never trip it; the GitHub Actions `runner` home is exempt.
- **`tombstones` check + `deploy/tombstones.yml`** — the generated pipeline
  only ever runs `CREATE OR REPLACE STREAMLIT`; it has **no delete path**, so
  a renamed/removed app directory abandons its deployed object, frozen at the
  last merge and flagged unhealthy by `verify-deploy` forever after. The check
  diffs declared identifiers against `--base-ref` (via `git merge-base`, read
  with `git ls-tree` so it works across branches) and blocks a PR that stops
  declaring an identifier without tombstoning it in the same PR. Identity is
  `streamsnow.deploy.streamlit_fqn` of the slug — the *same derivation the
  deploy uses*, deliberately not a second parse of `snowflake.yml`.
  `--drop-sql` emits `DROP STREAMLIT IF EXISTS …` for the deploy reconcile
  step, and **refuses** (exit 2) when a tombstone matches a currently-declared
  app — dropping it would kill the app the same deploy just created. An
  unresolvable base ref exits 2: "could not compare" must never pass as clean.
- **`requirements` check** — validates the §11 Build Progress block
  (`**Current phase:**` with a recognized lifecycle phase, an append-only
  `Sessions` log whose last line is ISO-timestamped and names `Next:` for
  non-terminal phases) — exactly what `/start-app` resumes from, so a mangled
  hand edit becomes a named finding instead of silent amnesia. Apps without a
  `REQUIREMENTS.md` are not findings (spec presence is a build-phase concern).
- **`branding-parity` check** — every app ships its own `branding.py` copy, so
  a branding change never propagates by itself. The scaffold template now
  carries a `_BRANDING_VERSION` stamp; the check flags apps whose stamp lags
  the newest across apps, and only *notes* (never fails) unstamped pre-0.6
  copies and lag behind the installed template.
- **`streamsnow migrate`** (`preflight` | `scan-hardfails` | `translate-deps`
  | `graft-plan` | `scan-imports` | `scan-conformance` | `scan-inline-sql`) —
  the deterministic detection engine behind `/migrate-app`: JSON out,
  AST-only (no exec/eval/import of the source tree, so a hostile source can't
  run code on the migrating machine), secrets detected by *presence* only
  (contents never read, so nothing can leak into output or a transcript).
- **`streamsnow nav <slug>`** — AST enumeration of an app's pages
  (`st.navigation` dict/list forms, bare `st.Page` assignments, legacy
  `pages/`-dir, single-page), JSONL or `--json-array`, with a
  partial-enumeration warning when navigation is built dynamically — for
  walkthrough tooling that needs the ordered page list without regex.
- **`streamsnow doctor` per-check JSON** — `doctor` is rebuilt on a module
  returning one `{name, ok, level, detail, hint}` dict per prerequisite
  (`--json` / `--format json`), so `/start-app` preflight and fix-then-recheck
  loops stop scraping console text. `level` is per-result: a missing config is
  *optional* (a machine can be healthy outside any repo) but a present,
  invalid config is *required* — malformed must never read as "not configured
  yet".
- **`check artifacts --fix`** — repairs the `snowflake.yml` `artifacts:` block
  from files on disk as a minimal edit (only the block's own lines rewritten),
  also shipping referenced image/data assets that would 404 deployed; refuses
  (with a finding) manifests it can't rewrite safely.
- **`check session-fallback --base-ref`** — git-aware baseline mode: flag only
  files whose violation count *grew* versus the base ref, so adopting repos
  gate new debt without first paying down legacy debt (`--all` restores the
  tree-wide scan).

**Enforcement templates**

- Generated CI (`checks.yml`) now runs the deterministic gates
  **fail-closed**: `streamsnow check dependency-vulns` (no `--best-effort` —
  CI is the authority), `streamsnow sql-review check`, and `streamsnow check
  tombstones --base-ref origin/main` (checkout gains `fetch-depth: 0` for the
  diff). Verified by template regression tests in `tests/test_init.py`.
- Both deploy workflows (stage-copy and git-repository) gain a **Reconcile
  tombstones** step: every registry identifier is dropped on every deploy via
  `check tombstones --drop-sql` (`IF EXISTS`, so re-runs are no-ops; a
  malformed registry exits 2 *before* any DROP; the live-app guard re-checks
  on the deploy side because a direct push to main never saw the PR check).
- `streamsnow init` scaffolds `deploy/tombstones.yml` as an empty,
  self-documenting registry, excluded from `streamsnow update` re-renders —
  it's user-appended consent, not a governance file to regenerate.
- The 8 skills are rebuilt on the new tools: `/ship-app`'s preflight and
  `/feedback-app`'s follow-up step call `review-gate classify` (asks, never
  blocks — shipping unreviewed stays available; validate + CI are the real
  publish gates),
  `/review-app --auto` runs on the `review-loop` primitives, `--sql` drives
  `sql-review`, and `/migrate-app` reasons over `streamsnow migrate` JSON.

### Changed
- **`validate-app` tightens** — four new sections:
  - `page-imports` (fix: package-qualify the import, `from pages._x import …`);
  - `path-leaks` (fix: replace the personal path with a placeholder or docs
    link);
  - `requirements` (fix: restore the §11 `**Current phase:**` line / a
    timestamped last session line with a `Next:` hint);
  - `sql-review` — **warn-only in 0.6, planned to become a FAIL in 0.7**, so
    adopters get one release to backfill audit trails (fix: `streamsnow
    sql-review discover <slug> --write`, edit the manifests, `generate`).
  `dependency-vulns` is deliberately NOT in the aggregate (it needs the
  network; the gate stays no-DB/no-network) — it runs as its own pre-commit
  hook and CI job instead.
- **`streamsnow preview` is now a lifecycle**, not a blocking foreground run:
  `start` (detached launch, log capture, `/_stcore/health` poll, classified
  launch failures — missing secrets, bad account locator, missing package,
  port collision — instead of a raw traceback), `status`, `stop`
  (idempotent, process-group SIGTERM→SIGKILL), `logs` (kept after stop for
  post-mortems). State lives per-repo in `.streamsnow/preview/` (gitignored by
  the scaffold; nothing global). Bare `streamsnow preview <slug>` remains a
  shorthand for `start`; the old `--dry-run` flag is gone.
- Generated CI and deploy workflows install a **pinned range**
  (`uv tool install 'streamsnow>=0.6,<0.7'`) instead of an unpinned latest, so
  a future 0.7 gate-tightening can't fail consumer CI unasked.
- `AGENTS.md` (generated) and the start-app page recipe now state the
  package-qualified import rule, so a scaffolded repo teaches it before an
  agent has a chance to imitate the wrong form from a sibling page; the
  repo-level template also documents the `sql_review/` audit-trail convention.
- The validate-app skill's "What it covers" list was three checks out of date
  (`artifacts`, `sql-tokens`, `session-fallback` were missing).

### Fixed
- **`streamsnow update` outside a configured repo tracebacked** with a raw
  `FileNotFoundError` instead of the "run `streamsnow init`" guidance every
  other path gets: `load_config` with an explicit `--config` path that doesn't
  exist now maps `OSError` to the friendly `ConfigError` (seen live;
  regression-tested in `tests/test_config.py`).

## [0.5.0] - 2026-07-20

Production-lessons release: the guardrails a real Streamlit-in-Snowflake fleet
accumulated — three new governance checks, post-deploy health verification, and
a written catalog of the failure modes behind them.

> **Upgrading a consumer repo:** run `streamsnow update --apply` to receive the
> new pre-commit hooks, the deploy workflow's verify step, and the AGENTS.md
> Production rules section. The `validate-app` gate tightens on upgrade —
> previously-passing apps can newly fail `artifacts` / `sql-tokens` /
> `session-fallback`; fixes are one-liners, see the validate-app skill's
> `fixing-checks.md`. No plugin hook changes, so no reinstall dance this time.

### Added
- **`artifacts` check** — cross-checks `snowflake.yml`'s `artifacts:` list
  against the files on disk. Local dev reads disk while a manifest-driven
  deploy reads the list, so an uncovered new file works locally and silently
  404s deployed; a stale entry breaks the deploy. (A recurring production
  incident — twice in one fleet, months apart.)
- **`sql-tokens` check** — flags `{TOKEN}` placeholders inside SQL comments.
  `render_sql` substitutes tokens with comment-unaware `str.replace`, so a
  documented token in a comment expands into live SQL and parse errors.
- **`session-fallback` check** — requires a broad `try/except Exception`
  around `get_active_session()` (it raises during local `streamlit run`;
  narrow `except ImportError` misses resolver-dependent failure types).
- **`streamsnow verify-deploy <slug> [--sha]`** — post-deploy health
  verification, because "deploy succeeded" is not "app serves": object exists,
  `live_version_location_uri` is set (NULL renders nothing in Snowsight),
  version-source URI contains the merge SHA (stage-copy), and container
  service logs show no crash-loop signature. Retries absorb container cold
  start; the log scan is strictly fail-open. Both generated deploy workflows
  gain a `Verify deploy health` step.
- **`docs/production-lessons.md`** + **`skills/_shared/production-gotchas.md`**
  — the full catalog and the condensed symptom→rule table: owner's-rights
  grants, passthrough-view pushdown, dynamic-table role/layering rules,
  out-of-band DDL audit trail, local SQL-cache restart, verify-runtime-before-
  diagnosing, `None`-in-`params=`, retire-by-moving. Pointers wired into the
  review-app, audit-lineage, preview-app, and validate-app skills.

### Changed
- Pre-commit template gains three hooks (`streamsnow-sql-tokens`,
  `streamsnow-session-fallback`, `streamsnow-artifacts`).
- AGENTS.md template gains a **Production rules** section and a correction:
  deployed apps execute with **owner's rights** (the ci_role's grants), not
  caller's rights as the viewer role.
- Template regression tests now pin the deploy workflows' concurrency
  serialization and the `.streamlit/config.toml` dotfile-copy loop (which
  `snow stage copy --recursive` would otherwise silently skip).

### Fixed
- **Scaffolded apps could crash-loop after a Snowflake base-image rollout.** The
  container app template pinned `streamlit==1.50.0`. The container runtime's base
  image launches Streamlit with CLI flags from its own bundled build (e.g.
  `--server.unsafeMetricsUserAttributes`, added in Streamlit 1.59.0); a pin below
  what the current image expects makes Streamlit reject the flag and crash-loop on
  startup (service READY, app never serves). Bumped the container template pin to
  `streamlit==1.59.2` and documented the base-image floor rule in both dependency
  templates. The warehouse template pin is left as-is with a note that the floor is
  container-only (Snowflake runs its own bundled Streamlit there; the conda pin is
  cosmetic).

## [0.4.0] - 2026-07-15

Safety + adoption release: the deploy-safety guard jobwright pioneered arrives in StreamSnow,
plus a load-blocking manifest fix and the trust/discoverability hardening the whole plugin
family shipped together.

> **Upgrading an existing install:** hook additions do not reach installed copies via
> autoUpdate (Claude Code pins the install path — see claude-code issue #52218). Reinstall:
> `/plugin uninstall streamsnow` then `/plugin install streamsnow@streamsnow`, and relaunch.

### Fixed
- **Plugin failed to load on Claude Code ≥ 2.1.** `plugin.json` declared
  `"hooks": "./hooks/hooks.json"`, but current Claude Code auto-loads that standard path, so
  the manifest key pointed at an already-loaded file and aborted the whole plugin with
  "Duplicate hooks file detected" — skills included. Exactly the bug jobwright fixed in its
  v0.1.1; the key is removed and a regression test now keeps it out
  (`tests/test_plugin_surface.py`).

### Added
- **Deploy-safety guard** (`hooks/deploy_safety.py`, PreToolUse) — ported from jobwright.
  Pauses for confirmation before destructive Streamlit/SQL commands: `snow streamlit
  deploy`/`drop`, `CREATE OR REPLACE / DROP / ALTER STREAMLIT`, stage `REMOVE`, and
  destructive SQL through any warehouse CLI, including SQL hidden in `-f` files and stdin
  redirects. Defends against shell-quote and full-path evasion; repo-gated on
  `streamsnow.config.yaml`; stdlib-only; fail-open (only ever *adds* a confirmation).
  The session banner now announces the guard — an invisible safety net reads as no safety net.
- **Frontmatter parity with jobwright**: every skill now declares `argument-hint` and
  `allowed-tools` (fewer permission prompts), and `/ship-app` + `/migrate-app` carry
  `disable-model-invocation: true` — shipping and migrating are human decisions.
- **System-evolution retro** at the end of `/ship-app` (ported from ticketwright's `/ship`
  Phase C): when something went wrong, fix the layer — config, skill, check, or deploy path —
  not just the instance.
- **Hooks-in-full README section**: every hook, what it does, the stdlib-only/no-network/
  fail-open guarantees, and how to disable — hook transparency is the trust bar for plugins
  that run PreToolUse guards.
- **Explicit hook timeouts** (SessionStart 5s, PreToolUse 10s) so a hung hook can never stall
  a session, and a `plugin-validate` CI job (`claude plugin validate . --strict`).

### Changed
- The 80-line SKILL.md cap is now measured on the **body** (after frontmatter) — frontmatter
  grew for parity and shouldn't force cutting instructions.

### Deferred (noted for a future release)
- Skill trigger evals (skill-creator description-tuning); submission to
  `claude-plugins-community`; multi-harness install docs.

## [0.3.0] - 2026-07-02

The UX release: **14 skills → 8**, one front door, ≤5-question setup, plain language on every
user-facing surface. Engine changes are minimal — the CLI checks, scaffolder, deploy generators,
hooks, and templates carry over (one exception: the `configure` wizard slimmed down); this is a
surface-area consolidation, applying the design system shipped in Ticketwright v2.0.

### Changed — the rename map (v0.2 → v0.3)
| v0.2 | v0.3 |
|---|---|
| `start-app` + `new-app` + `refine-requirements` + `add-page` + `onboard` | **`start-app`** (the front door — owns spec → scaffold → build → ship; `--spec` for the requirements phase incl. **backfill from existing source**, `--setup` for machine + repo setup, `adopt` for existing repos) |
| `review-app` + `apply-review` + `auto-review-app` + `sql-review` | **`review-app`** (`--fix` applies findings as atomic commits, `--auto` loops review→fix to convergence, `--sql` writes the audit companions) |
| `deep-dive-data` | **`audit-lineage`** |
| — | **`feedback-app`** (new — upstreamed from production use: classify user feedback into BUG / POLISH / UX / NEW-FEATURE / CROSS-CUTTING, apply as atomic per-item commits, follow-up review) |
| `preview-app`, `validate-app`, `ship-app`, `migrate-app` | unchanged names, refreshed surfaces |

All 8 retired names still work as deprecated alias stubs (`commands/`); they will be removed in
the next major release.

### Added
- **Adopt mode** (`skills/start-app/adopt.md`) — `/start-app adopt` on a repo that already has
  Streamlit apps maps onto the observed layout instead of scaffolding over it, classifies custom
  commands/skills as shadows / extends / unrelated against the plugin's skills, and writes a
  `MIGRATION.md` checklist. An existing `AGENTS.md` is never overwritten (renders to
  `AGENTS.streamsnow.md` for manual merge).
- **≤5-question `streamsnow configure`** — down from ~14 prompts. Asks only runtime, account
  locator, governed database, allowed schemas, and deploy source; everything else (project
  identity derived from the directory name, roles, warehouse, schema names, container objects,
  git-repository deploy fields) is written as an **inline-commented default** in
  `streamsnow.config.yaml` — the file is the editing surface, and re-running `configure` prefills
  from it so hand edits survive. Guarded by a new `tests/test_wizard.py` contract test.
- **Plugin-surface contract test** (`tests/test_plugin_surface.py`) — CI now asserts the 8-skill
  surface, the ≤80-line SKILL.md cap, the 8 alias stubs pointing at their replacements, no retired
  name referenced as live inside `skills/`, and every relative markdown link resolving.
- **Spec backfill** (`/start-app --spec <slug>` on an app with existing code) — reverse-engineers
  `REQUIREMENTS.md` from `st.Page` declarations, chart/KPI/filter calls, SQL header blocks, cache
  decorators, and `snowflake.yml`, marking anything uncertain `(inferred)` for §10 review.
  Upstreamed from proven production use.
- **`skills/_shared/runtime-decision.md`** — the container-vs-warehouse decision in one place,
  neutrally framed (both runtimes are legitimate; the repo default wins absent a concrete reason),
  with the detection rule, trade-off table, and the manifest/connection checklist. Skills now link
  to it instead of re-explaining the choice (previously restated in 5+ skills).
- `docs/migrating-a-consumer-repo.md` — map a repo's home-grown skills to the plugin surface
  (worked example: a 16-skill production repo), what stays local (branding parity, tracker
  integration, warehouse-specific rules), and the incremental adoption path.

### Changed (language & structure)
- **SKILL.md ≤80 lines, depth in reference files** — every skill's front page is now a scannable
  contract (modes, steps, boundaries), with detail split into per-skill reference files
  (`start-app/{spec,scaffold,pages,setup,adopt}.md`,
  `review-app/{dimensions,fixes,auto-loop,sql-companions}.md`, `audit-lineage/tracing.md`,
  `feedback-app/classification.md`, `validate-app/fixing-checks.md`).
- **Plain language on user surfaces** — skill descriptions lead with the trigger use-case;
  "deterministic PASS/FAIL ship gate" reads "the pass/fail check that must be clean before
  shipping"; report summaries print critical / should-fix / nice-to-have; "legacy" dropped from
  warehouse-runtime framing. Contributor-facing terms (bucket mechanics, check internals) stay in
  reference files.
- **Graceful degradation instead of hard failure** — missing config: review runs static-only and
  says which findings go unverified; missing Snowflake connection: lineage/companion rows are
  marked unverified with the exact enabler named; missing Playwright MCP: walkthroughs skip
  silently. Skills name the enabler instead of refusing.
- **§11 Build Progress simplified** — `Current phase` plus an append-only Sessions log whose last
  line names the next command. The per-page status table is gone (page state is visible in the
  tree and git); existing specs keep working, the table just stops being maintained.
- `hooks/session_start.sh` discovery line, README, and docs updated to the 8-skill surface.

### Carried from the previous Unreleased
- `docs/distribution.md` — how StreamSnow ships (PyPI CLI + Claude Code plugin)
  and the recorded decision **not** to add a separate `cp -r` copyable kit
  (`streamsnow init` is the config-driven "better `cp -r`").
- Thickened the plugin skills toward the source's depth — that content now lives in the v0.3
  reference files rather than monolithic SKILL.mds.

## [0.2.0] - 2026-06-30

### Fixed
- The scaffolded `branded_metric` now HTML-escapes its label/value/delta before
  rendering with `unsafe_allow_html=True`, so a database-derived value cannot
  inject markup into the viewer's page (hardening applied to the template and the
  example). Dependency-name matching is PEP 503-normalized, so a manifest that
  spells a package with underscores/dots (`snowflake_snowpark_python`) is no
  longer reported as missing.
- `validate-app` now validates the **contents** of the sibling dependency
  manifest, not just its presence: container apps must declare a
  `requires-python` that admits the container runtime's Python (PEP 440
  specifier semantics, so `>=3.10` is accepted and `<3.11` / `==3.10.*` are
  correctly rejected) plus `streamlit` + `snowflake-snowpark-python`; warehouse
  apps must declare those deps in `environment.yml` and must not pin `python`.
- `check caching` now flags two patterns it previously missed: a public loader
  that hands a **named query through a local variable**
  (`sql = load_sql("x"); conn.query(sql)`) and one that **delegates** a named
  query to a private fetch helper (including transitive helper chains). Only the
  SQL-bearing argument is inspected, so an unrelated string keyword (e.g.
  `query_tag="adhoc"`) no longer trips the generic-executor guard.

### Added
- Documentation guides: `docs/getting-started.md` (try the example with no
  Snowflake, then set up a governed repo), `docs/data-discovery.md` (find tables
  and wire governed queries), and `docs/deploying.md` (the end-to-end deploy
  story for both deploy sources). Linked from the README.
- Runnable example app at `examples/sample-dashboard/` — a StreamSnow-shaped
  Streamlit dashboard wired to deterministic sample data, so it renders with
  `streamlit run` and **no Snowflake connection**. Mirrors the `streamsnow init`
  structure (st.navigation entrypoint, branding, `@st.cache_data` loaders).
- `packaging` runtime dependency (PEP 440 version-specifier parsing in
  `validate-app`).

## [0.1.0] - 2026-06-27

Initial release.

### CLI (`streamsnow`)
- `configure` / `init` / `new` — set up the Snowflake environment and scaffold a
  governed monorepo + apps (container or warehouse runtime).
- `doctor` — machine + config prerequisite checks.
- `validate-app` — deterministic PASS/FAIL gate; `check schema-refs|security|caching|bind-predicates`.
- `preview` — run an app locally against live Snowflake.
- `deploy-setup` / `deploy-sql` / `stage-path` / `config-get` — deploy SQL + helpers
  for stage-copy and git-repository sources.
- `update` — re-render governance files from the current config (dry-run by default).

### Claude Code plugin
- 14 skills (onboard, refine-requirements, new-app, add-page, preview-app,
  validate-app, ship-app, start-app, review-app, deep-dive-data, apply-review,
  auto-review-app, sql-review, migrate-app) + 4 shared recipes.
- SessionStart hook, guarded to StreamSnow repos.

### Governance & safety
- Typed, validated `streamsnow.config.yaml` with an injection-safe rendering gate.
- Config-driven schema allow/deny, app-security, caching-TTL, and bind-predicate checks.
- Pre-publish privacy/export gate; generated repos ship pre-commit + CI guardrails.

### Packaging
- PyPI Trusted-Publishing release workflow; wheel-smoke + 3.11/3.12 CI matrix.
