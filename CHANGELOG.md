# Changelog

All notable changes to StreamSnow are recorded here. This project follows
[semantic versioning](https://semver.org/) once it reaches its first release.

## [Unreleased]

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
