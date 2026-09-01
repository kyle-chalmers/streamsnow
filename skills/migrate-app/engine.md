# The migration engine — verb by verb

`streamsnow migrate <verb>` is the deterministic detection layer under /migrate-app: every verb
emits JSON on stdout, human-readable errors on stderr, and a stable exit code (0 = clean/pass,
1 = abort/blocks, 2 = tool/config error). Detection is AST-based — it never executes source code,
so a hostile source tree can't run anything on the migrating machine. The skill's job is the layer
the engine deliberately doesn't do: reading the JSON, making the judgment calls, and talking to the
user. Never re-derive by grep what a verb already reports.

## Step 1 verbs — is it liftable, and what lands where

### `streamsnow migrate preflight <source> --target-slug <slug> [--dir <repo-root>]`

Run **first**, before touching any file. Exit 1 = abort.

- JSON: `is_streamlit_app`, `entrypoints` (list), `deps_manifest` (which manifest file it found),
  `target_exists`, `catastrophic_deps`, `abort`, `abort_reason`.
- Judgment on abort: `target_exists` → pick a different slug with the user (or they remove the old
  dir — never remove it for them). Multiple `entrypoints` → ask which app to migrate; one at a
  time. `catastrophic_deps` (web frameworks / heavy ML stacks no hosted runtime carries) → stop and
  discuss: if the dependency is real, the app needs the container runtime and a PyPI dep set —
  that's a user decision, not a retry.

### `streamsnow migrate graft-plan <source>`

Informational (exit 0 always) — where the source's UI lands in the scaffold.

- JSON: `graft_target` (`pages/*` | `pages/overview.py` | `streamlit_app.py`), `reason`,
  `source_has_pages`, `source_uses_st_tabs`, `source_entrypoint_count`.
- Judgment: follow `graft_target` when copying. The `pages/overview.py` case matters most — a
  single-file app organized around module-scope `st.tabs` must NOT be merged into the scaffold
  entrypoint, where it would fight `st.navigation` for the page structure. Disagree with the plan
  only with a reason you can tell the user.

### `streamsnow migrate scan-imports <source>`

Run before copying so the copy is complete. Exit 0 always.

- JSON: `relative_imports` (`{file, lineno, module}`) and `subpackage_init_files`.
- Judgment: every subpackage named there gets grafted **whole** — a relative import whose package
  didn't come along imports fine at rest and dies at first render. Preserve relative imports as-is
  in Step 1; restructuring is conform-pass work.

### `streamsnow migrate scan-hardfails <source> [--config ...]`

Run on the source before the lift commit; re-run until exit 0. Exit 1 = blocks.

- JSON: `schema_refs` (denied-schema references, same policy as `streamsnow check schema-refs`),
  `secrets_in_py` (hardcoded credentials at module scope), `has_secrets_toml` / `has_env_file`
  (**presence only** — the engine never reads a secrets file, so a secret can't leak into JSON or a
  transcript; neither should you).
- Judgment: re-point each denied schema at its governed equivalent with the user — you can't guess
  the mapping. Move hardcoded credentials out of `.py`; the user copies values by hand into
  gitignored `secrets.toml`. `has_secrets_toml`/`has_env_file` true → make sure those files are
  never copied into the repo.

### `streamsnow migrate translate-deps <source> --out <path> [--offline]`

Warehouse-runtime targets only — it writes a conda-channel `environment.yml`. Container targets
declare PyPI deps in `pyproject.toml` instead; then use this verb's JSON only as a package
inventory, not its output file. Exit 2 = `--out` unwritable.

- JSON: `translated` (spec → conda pin), `dropped` (with per-package reasons: not on the channel,
  or `python` pins — never pinnable there), `unmapped`, `inferred_suggestions` (AST-inferred
  imports when the source has **no** manifest — suggestions only, never auto-added), `error`.
- Judgment: every `dropped`/`unmapped` package is a conversation — swap it, drop the feature, or
  choose the container runtime. Confirm each `inferred_suggestions` entry with the user before
  adding it. `--offline` skips the channel-repodata fetch (conservative allowlist; use when the
  network is unavailable, and expect more conservative drops).

## Step 2 verbs — what the conform pass still owes

### `streamsnow migrate scan-conformance apps/<slug> [--config ...]`

The conform worklist. Exit 0 always — the findings, not the exit code, are the output. Re-run
after each batch of fixes until `uncached_queries`, `select_stars`, and `altair_imports` are
empty and `legacy_pages_only` is false. `required_grants` inventories every detected schema (it may be empty when none are found);
only entries with `granted_by_default == false` need action.

- JSON: `uncached_queries` (`{file, func, lineno, callee}` — data fetches without
  `@st.cache_data`), `select_stars` (`{file, lineno, snippet}`), `altair_imports`,
  `legacy_pages_only` (a `pages/` dir with no `st.navigation` in the entrypoint),
  `required_grants` — the (database, schema) pairs the app queries, split into ones the CI role's
  allowlist already covers vs ones that need a DBA.
- Judgment: wrap each uncached fetch (TTL = repo default unless the app argues otherwise); pin
  explicit columns for each `SELECT *` (defer with a note rather than hallucinate — `/audit-lineage`
  can fetch the real list); swap altair chart layers to the repo standard (its 5,000-row default
  cap breaks real dashboards); `legacy_pages_only` → rebuild navigation with `st.navigation` (the
  two conventions cannot mix). Grants needing a DBA go in the PR description, not silent hope.

### `streamsnow migrate scan-inline-sql apps/<slug>`

The SQL-externalization worklist. Exit 0 always.

- JSON: `candidates` (`{file, line, function, sample}`) — string literals with real SQL shape.
  Already-externalized queries self-filter (a `load_sql("revenue_daily")` arg is a name, not SQL).
- Judgment: each candidate either moves to `queries/<name>.sql` with the required header block
  (`Query / Feeds / Schemas / Params / Tokens`), loaded via `load_sql`/`render_sql` — walk the
  Feeds/Schemas mapping with the user one at a time, it can't be guessed from code — or is plumbing
  (heartbeats, INFORMATION_SCHEMA discovery) and earns a `# noqa: inline-sql` on its line.

## The sql-review bootstrap (end of Step 2)

Once queries live in `queries/*.sql`, the app owes its audit trail — the paste-runnable review SQL
a reviewer opens in Snowsight to re-run the numbers behind each visual:

1. `streamsnow sql-review discover <slug> --write` — persists a skeleton manifest per uncovered
   query (exit 1 just means gaps existed; it never overwrites an existing manifest).
2. Author the manifests: replace each dispatcher's `-- TODO: sample fragment for <TOKEN>`
   placeholder with a real fragment the app renders (e.g. `AND region = 'West'`); fix
   `description`/`pages` from the query headers.
3. `streamsnow sql-review generate <slug>` — renders `sql_review/*.review.sql` with provenance;
   `streamsnow sql-review index <slug>` rebuilds the README coverage table.

Commit the manifests and rendered files inside the conform commit — the conformed app and its
audit trail land together, and `streamsnow sql-review check` stays green from the first PR.
