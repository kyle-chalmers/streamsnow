# Changelog

All notable changes to StreamSnow are recorded here. This project follows
[semantic versioning](https://semver.org/) once it reaches its first release.

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
