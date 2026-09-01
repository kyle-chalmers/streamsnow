# Migrating a repo with its own skills to the StreamSnow plugin

This guide is for a repo that grew its own `.claude/skills/` for building Streamlit-in-Snowflake
apps and now wants the maintained plugin instead. It maps each custom skill to its plugin
equivalent, names what should stay local, and lays out an incremental path — the same one
`/start-app adopt` automates ([skills/start-app/adopt.md](../skills/start-app/adopt.md) writes a
per-repo `MIGRATION.md` from a live inventory).

The worked example throughout is a production repo with 16 custom skills that predate the plugin —
the shape of the private repo you're migrating from. If that's you, the table below is your
starting MIGRATION.md.

## The skill map (16 custom → 8 plugin)

| Custom skill | Plugin equivalent | Notes |
|---|---|---|
| `start-app` | **`/start-app`** | Same front-door idea; the plugin version also owns spec + scaffold + pages directly |
| `refine-requirements` | `/start-app --spec` | Ticket ingestion is generic; a tracker-specific ingest (e.g. a Jira project key) stays a local note |
| `backfill-requirements` | `/start-app --spec <slug>` (automatic backfill) | Upstreamed in v0.3 — the plugin detects existing source and backfills, `(inferred)` markers included |
| `new-app` | `/start-app` (scaffold phase) | `streamsnow new` replaces the local Copier flow |
| `add-page` | `/start-app` (build phase) | Resumes into the build phase for an existing app |
| `onboard` | `/start-app --setup` | Same doctor-driven, confirm-each-fix walkthrough |
| `review-app` | **`/review-app`** | Same five dimensions |
| `apply-review` | `/review-app --fix` | Same A/B/C bucketing, atomic commits |
| `auto-review-app` | `/review-app --auto` | Same convergence loop |
| `sql-review` | `/review-app --sql` | Same paste-and-runnable companions + lineage README |
| `deep-dive-data` | **`/audit-lineage`** | Renamed; same bounded read-only tracing. Warehouse-specific rules (e.g. an environment-specific schema helper, an intermediate-layer deploy note) stay local |
| `feedback-app` | **`/feedback-app`** | Upstreamed in v0.3 — classification buckets, per-item commits, follow-up review |
| `preview-app` | **`/preview-app`** | Unchanged surface |
| `validate-app` | **`/validate-app`** | The plugin's gate is `streamsnow validate-app`; a local `--pr` checklist variant is an *extends*, keep it if you use it |
| `ship-app` | **`/ship-app`** | Same gate-then-PR flow incl. the squash-merge branch-reuse guard |
| `migrate-app` | **`/migrate-app`** | Same two-commit lift + conform |

Shared recipes (`_shared/playwright-walkthrough.md`, `cross-agent-review.md`,
`deploy-error-translator.md`, `sync-with-main.md`) ship with the plugin; a local
`sql-review-bootstrap.md` is covered by `/review-app --sql`. The container-vs-warehouse guidance
that used to live in several skills is now one recipe:
[`skills/_shared/runtime-decision.md`](../skills/_shared/runtime-decision.md).

## The tool map (home-grown `tools/*.py` → CLI verbs)

Skills are only half of a grown repo — the other half is a `tools/` directory of
checker scripts wired into pre-commit and CI. As of v0.6 the CLI covers the
common ones, so a consumer repo can delete the local script and point its hooks
at the verb:

| Home-grown tool (typical name) | CLI verb | Notes |
|---|---|---|
| `check_schema_refs.py` | `streamsnow check schema-refs` | Denylist from `governance.*` in config, not hardcoded |
| `check_app_security.py` | `streamsnow check security` | |
| `check_caching.py` | `streamsnow check caching` | |
| `check_bind_predicates.py` | `streamsnow check bind-predicates` | |
| `check_page_imports.py` | `streamsnow check page-imports` | |
| `check_path_leaks.py` | `streamsnow check path-leaks` | |
| `check_dependency_vulns.py` + `osv_allowlist.json` | `streamsnow check dependency-vulns` | Same allowlist filename, discovered beside the config; `--best-effort` for pre-commit, fail-closed in CI |
| `check_tombstones.py` + a tombstone registry | `streamsnow check tombstones` (+ `--drop-sql` in deploy) | Registry standardized at `deploy/tombstones.yml` |
| `check_branding_parity.py` | `streamsnow check branding-parity` | Keyed on the `_BRANDING_VERSION` stamp, not file diffing |
| a `REQUIREMENTS.md` §11 validator | `streamsnow check requirements` | Validates exactly what `/start-app` resumes from |
| manifest/artifacts populater | `streamsnow check artifacts --fix` | Repairs `snowflake.yml` `artifacts:` from disk as a minimal edit |
| `review_gate.py` | `streamsnow review-gate` | classify / baseline / stamp / stop-hook; the plugin's Stop hook runs the same file |
| `review_loop.py` | `streamsnow review-loop` | parse / dedup / resolutions / exit-condition / merge |
| a sql-review generator + manifests dir | `streamsnow sql-review` | discover / generate / check / index; manifests move into `apps/<slug>/sql_review/manifests/` |
| an entrypoint/nav extractor | `streamsnow nav <slug>` | AST-based; JSONL or `--json-array` |
| a background preview launcher | `streamsnow preview start\|status\|stop\|logs` | State under `.streamsnow/` (gitignored) |
| migrate-app detection scripts | `streamsnow migrate <verb>` | preflight / scan-hardfails / translate-deps / graft-plan / scan-imports / scan-conformance / scan-inline-sql |
| a machine-setup checker | `streamsnow doctor --json` | Per-check `{name, ok, level, detail, hint}` results |

## What stays local (and should)

The plugin is deliberately generic. Keep — as small local skills or AGENTS.md notes — anything that
encodes *your* organization:

- **Branding parity** — company palettes, Plotly templates, `apply_branding()` conventions, and any
  skill that checks pages against them. The plugin's review checks that branding is *wired*, not
  that it matches your brand book.
- **Tracker integration** — a specific Jira/Asana project, cloud ID, or parent-epic convention for
  spec ingestion and issue filing.
- **Warehouse-specific data rules** — named schemas' semantics (a source-system schema helper, an
  intermediate layer's deploy ownership), soft-delete flag conventions beyond the generic patterns
  `/audit-lineage` already flags.
- **Company governance values** — your real database/schema/role names live in
  `streamsnow.config.yaml`, never in skills.
- **Repo-housekeeping checks** — a `check_root_files.py` (allowlist of files
  permitted at the repo root), a `check_agents_todos.py` (no stale TODO
  markers in agent-instruction files), and similar hygiene scripts encode one
  repo's tidiness rules, not the platform's. StreamSnow deliberately doesn't
  ship them; keep them local and wired into your own pre-commit.
- **Notification CI steps** — a Slack-webhook "deploy finished" step, a
  tracker-comment bot, or any CI job that talks to your chat/ticketing stack.
  The generated workflows stay side-effect-free beyond Snowflake itself; graft
  your notify steps onto the generated `deploy.yml` after `streamsnow update
  --apply` re-renders (they'll need re-adding when the workflow is
  regenerated, so keep them in a small composite action or a separate
  workflow triggered on `workflow_run`).

Classify each local skill the way adopt mode does: **shadows** (the plugin covers it → delete after
a trial run), **extends** (domain-specific variant → keep, note it in AGENTS.md), **unrelated**
(keep as-is).

## The incremental path

1. **Install the plugin** alongside the local skills (nothing breaks — same-named local skills
   shadow the plugin's until you delete them).
2. **`streamsnow configure`** (≤5 questions) if the repo doesn't have `streamsnow.config.yaml` yet —
   or run `/start-app adopt` and let it inventory + configure + write `MIGRATION.md` for you.
3. **Trial run:** take ONE real change through `/start-app → /preview-app → /validate-app →
   /review-app → /ship-app` with the plugin versions.
4. **Delete the shadows** the trial proved covered; keep the extends with a one-line AGENTS.md note
   each; leave unrelated skills alone.
5. **Old names keep working** during the transition — v0.3 ships deprecated aliases
   (`/new-app`, `/refine-requirements`, `/add-page`, `/onboard`, `/auto-review-app`, `/sql-review`,
   `/apply-review`, `/deep-dive-data`) that point at their replacements. They are removed in the
   next major release, so update muscle memory (and any docs/scripts) before then.

## Behavioral deltas to expect

- **`REQUIREMENTS.md` §11 is simpler:** a `Current phase` line plus an append-only Sessions log —
  no per-page status table. Existing specs keep working; the table just stops being maintained
  (page state is visible in the tree and git).
- **`streamsnow configure` asks 5 questions**, writing everything else as commented defaults —
  if your old flow asked more, the answers now live as editable lines in the config file.
- **Missing config degrades instead of blocking:** review runs static-only without governance
  context, lineage marks rows unverified without a connection — each names the enabler instead of
  refusing.
