# `--sql` — build the audit-ready `sql_review/` companions

Give every UI-feeding query in `apps/<slug>/queries/` a paste-and-runnable companion plus a lineage
README, driven by `streamsnow sql-review`. **Read-only** — never mutates Snowflake, never deploys.
The tool owns everything deterministic: rendering, the read-only guard, provenance digests,
coverage, the README table skeleton. Your judgment work is the **manifests** and the lineage
narrative. Data-correctness judgment lives in `/audit-lineage`, code judgment in the review pass,
the ship gate in `streamsnow validate-app`. Review and lineage passes run this automatically when
`streamsnow sql-review check <slug>` reports gaps; invoke `--sql` directly for the scaffolding alone.

**The manifest is the editing surface — the rendered file never is.** Each
`sql_review/<feature>[.<combo>].review.sql` carries a provenance digest, and `sql-review check`
flags a hand-edited body (or a manifest/template/module that changed since generation) as DRIFT
until regenerated. To change what a companion renders, edit
`apps/<slug>/sql_review/manifests/<feature>.json` and re-run `generate`.

## Steps

1. Resolve the slug; confirm `apps/<slug>/queries/` exists. No `queries/*.sql` (a legacy app that
   inlines its SQL) → nothing to generate; suggest externalizing queries first and stop.
2. **Compute the gap:** `streamsnow sql-review discover <slug> --write` — prints coverage
   (claimed vs. uncovered) and persists one static skeleton manifest per uncovered query under
   `sql_review/manifests/`. Exit 1 means gaps existed; existing manifests are never overwritten.
   Leave already-covered queries' manifests untouched unless the user asks for a refresh.
3. **Author the manifests — this is the judgment work.** A skeleton renders, but renders badly.
   Per manifest, before generating:
   - Replace every `-- TODO` dispatcher literal with a **real sample fragment whose literals
     satisfy the predicates** (pick values the data actually contains, inside the review window) —
     otherwise a correct query and an empty one look the same on paste. A leftover TODO literal is
     worse than a placeholder: it renders as a comment mid-clause and silently comments out the
     rest of that line.
   - Make `combos` mirror the dashboard's **default filter state** first (`all-default`), then add
     one combo per meaningfully different filter shape — not one per possible value.
   - Set each query spec's `metric_name` to the **on-screen visual title**, so a pasted section's
     result labels itself to match the dashboard.
   - Group related queries into one `feature` manifest whose `pages` mirror the app's navigation —
     `discover` proposes one manifest per query; consolidating them is your call. Two manifests may
     never share a feature name (`generate` refuses the filename collision).
   - When the app exposes its own token producers (e.g. a `region_filter_sql()` helper in a data
     module), switch `token_strategy` to `"manifest"` and point `call`/`const_attr` dispatchers at
     them via `modules` — the render is then exactly what the app emits, not your transcription of
     it. Only `generate` imports app code, and only on a developer machine; `check` stays
     import-free by design.
   - Defaults cover `:1 start_date, :2 end_date` via the `SET` block; override `param_bindings` /
     `set_block` when the query binds something else. **Every bind a query uses must be declared**
     — an undeclared `:3` is a hard error, and `param_bindings` must point at a variable that
     `set_block` actually declares (a `$var` with no `SET` line is also a hard error). Unused
     `set_block` entries are pruned from the output, so declare what the queries use.
   - `set_block_note`: why these defaults are what they are — which source the bounds derive from,
     and any mechanics that bite when editing them. It renders above the `SET` lines, where the
     auditor reads it. Put the rationale here, not in a manifest comment.
   - `fragments`: `[{"file": "_shared_ctes.sql", "reason": "..."}]`. A `queries/*.sql` that is a
     shared CTE inlined via a token is **not independently runnable**, so it can never be claimed
     by a page — and without declaring it, coverage demands a companion it can never have, which
     makes the gate unsatisfiable for any app that factors CTEs into their own files. `reason` is
     required and renders into the index. Never rename a query to dodge coverage; declare it.
4. **Render:** `streamsnow sql-review generate <slug>` (scope one manifest with `--feature
   <name>`). The tool substitutes tokens and binds, verifies the output is read-only, stamps
   provenance, and deletes stale files for removed combos. Read-only is enforced in two
   independent layers: a statement-root allowlist (`SELECT` / `WITH…SELECT` / `SHOW` / `DESCRIBE`
   / `EXPLAIN` / session-variable `SET`), plus a tripwire that refuses a write verb in command
   position even if the parser is fooled. Four hard errors — an unresolved `{TOKEN}`, a surviving
   `:N` bind, a `$var` with no `SET` line, or a write-shaped statement. Fix the manifest, never
   the output.
5. **Check for a connection** (`streamsnow doctor` / `snow connection list`). It's a branch, not a
   gate: with one, lineage rows get live-verified; without one, everything is written from static
   analysis and marked **unverified** — still useful, still honest. Never fabricate column lists.
6. **Live-verify lineage when connected:** per upstream object, a zero-row resolve probe
   (`SELECT COUNT(*) FROM <fqn> WHERE 1=0`) and type/columns from `INFORMATION_SCHEMA`. `LIMIT`
   any row-returning probe; no DDL, no writes, nothing outside `governance.schema_allow`.
7. **Index:** `streamsnow sql-review index <slug>` rebuilds the README coverage table between its
   `<!-- sql-review-index:start/end -->` markers (creating README.md if absent). The tool owns the
   table skeleton and carries the two human columns per row, keyed by query name; everything
   outside the markers is preserved byte-for-byte. After it runs, fill in what only you know:
   - **Upstream object(s)** — replace the `_(fill via /review-app --sql)_` placeholder with the
     fully-qualified object(s) the query reads.
   - **Verified** — a date (e.g. `2026-08-31`) only for rows whose objects step 6 live-confirmed
     this pass; leave `no` otherwise.
   - The **narrative around the markers** — lineage notes, known caveats, how to read the files.
8. **Report the coverage delta** — total queries, covered, still uncovered (`check` names them),
   declared CTE fragments, and live-verified vs. static rows. Two `check` findings need action
   rather than a number: a declared fragment whose file no longer exists (a stale exemption), and
   a query claimed by a page in one manifest while another declares it a fragment (contradictory;
   the exemption is ignored until resolved). Report those as findings, not as coverage.

## Judgment calls

- **One rendered section per query, whatever its join width** — a three-table join is one section;
  its README row lists all three upstream objects.
- **No `{TOKEN}`s in a query** → no dispatchers needed; the rendered body is the template with
  binds substituted. That's fine.
- **Zero rows in the predicate window** is a finding for the README (the UI may render empty), not
  an error to fix here — route the judgment to `/audit-lineage`.

## Edge cases

- **Auth expires mid-run:** finish the remaining rows static-only, mark them unverified, and say
  how to upgrade them (`snow connection test`, re-run steps 6–7).
- **"Does not exist or not authorized" on a probe:** either genuinely missing or the role can't see
  it — run `streamsnow check schema-refs apps/<slug>` to confirm the reference is allowed, check
  grants, and leave the row unverified rather than guessing columns.
- **Rendered section errors or returns nothing on paste:** a sample literal doesn't match real
  data — fix the dispatcher in the manifest and regenerate; never patch the `.review.sql`.
- **`generate` fails importing app modules** (`token_strategy: "manifest"`): run from an
  environment with the app's dependencies installed, or fall back to `"static"` with literal
  fragments transcribed from the app's helpers.
- `sql_review/` is review scaffolding, not app code — it isn't deployed and isn't loaded by the
  app's `sql_loader`; editing it never changes what ships. The generator refuses to emit any write
  statement into a review file, and `check` re-verifies committed files stay read-only.
