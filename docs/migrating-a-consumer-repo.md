# Migrating a repo with its own skills to the StreamSnow plugin

This guide is for a repo that grew its own `.claude/skills/` for building Streamlit-in-Snowflake
apps and now wants the maintained plugin instead. It maps each custom skill to its plugin
equivalent, names what should stay local, and lays out an incremental path — the same one
`/start-app adopt` automates ([skills/start-app/adopt.md](../skills/start-app/adopt.md) writes a
per-repo `MIGRATION.md` from a live inventory).

The worked example throughout is the upstream source repo itself: a production repo with 16 custom
skills that predate the plugin. If that's you, the table below is your starting MIGRATION.md.

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
| `deep-dive-data` | **`/audit-lineage`** | Renamed; same bounded read-only tracing. Warehouse-specific rules (e.g. LMS-schema conventions, a BRIDGE-layer deploy note) stay local |
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

## What stays local (and should)

The plugin is deliberately generic. Keep — as small local skills or AGENTS.md notes — anything that
encodes *your* organization:

- **Branding parity** — company palettes, Plotly templates, `apply_branding()` conventions, and any
  skill that checks pages against them. The plugin's review checks that branding is *wired*, not
  that it matches your brand book.
- **Tracker integration** — a specific Jira/Asana project, cloud ID, or parent-epic convention for
  spec ingestion and issue filing.
- **Warehouse-specific data rules** — named schemas' semantics (an LMS schema helper, a bridge
  layer's deploy ownership), soft-delete flag conventions beyond the generic patterns
  `/audit-lineage` already flags.
- **Company governance values** — your real database/schema/role names live in
  `streamsnow.config.yaml`, never in skills.

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
