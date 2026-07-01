---
name: apply-review
description: Apply the latest /review-app or /deep-dive-data findings to a StreamSnow app — auto-fix mechanical BLOCK/FLAG findings as atomic per-finding commits and walk judgment-required + NICE-TO-HAVE items interactively. Use when the user says "apply the review", "fix the findings", "auto-fix review", or after /review-app returns BLOCK/FLAG findings.
---

# apply-review

Turn the latest qualitative review findings for an app into atomic, per-finding commits, then re-run the deterministic gate. This is the fix-side counterpart to /review-app (which only *writes* findings) — it closes the loop so findings don't rot in the gitignored `.review/` report.

> Reviews are **qualitative judgment** — they never block. The hard PASS/FAIL gate is `streamsnow validate-app`, run at the end (step 7) and via /validate-app. This skill applies findings; it does not re-judge them. A review finding never flips validate to FAIL on its own — but an unfixed BLOCK that maps to a governance rule usually *will* show up as a validate FAIL, which is why re-gating is the last step.

## Buckets

Every BLOCK / FLAG / NICE-TO-HAVE finding sorts into exactly one bucket. The bucket decides whether you touch code without asking.

- **A — mechanical:** unambiguous, auto-applied without asking. The fix is invariant across apps and a regex/AST-level edit suffices. Examples: missing `@st.cache_data(ttl=…)` on a data-fetch function; `SELECT *` → named columns **when the column list is inlined in the finding**; a denied-schema reference swapped to the allowed `REPORTING`/`ANALYTICS` equivalent named in the finding; missing `use_container_width=True`; Altair *import* → Plotly import; the `:N IS NULL OR` bind-predicate trap rewritten to the parameterized form.
- **B — judgment:** needs a human call. Examples: which TTL value (300 vs 1800 vs 86400); which view/table to read from; a wide-view → narrow-view join rewrite; an Altair `alt.Chart(...)` spec translated to Plotly; wrapping a multi-widget page in `st.form`; container thread-safety guards. Walked interactively, one at a time.
- **C — informational:** no code change — context only ("consider materializing in REPORTING", view-chain-depth notes). Collated into a punch list at the end.

When in doubt between A and B, treat it as B. A wrong silent auto-fix costs more trust than one extra question.

## Steps

1. **Resolve the `<slug>`** (ask if absent; if cwd is inside `apps/<slug>/`, use that). Confirm `apps/<slug>/streamlit_app.py` exists — if not, stop and ask whether it's a valid app.
2. **Start clean.** `git status --short apps/<slug>` — atomic commits only work on a clean tree. If there are unrelated edits, ask the user to stash/commit them first, or stash on their say-so. Do not entangle their work-in-progress with review fixes.
3. **Load the latest report.** `ls -t apps/<slug>/.review/review-*.md | head -1`. This is the artifact /review-app and /deep-dive-data both write (same Markdown schema: `## <Dimension>` sections with `### BLOCK` / `### FLAG` / `### NICE-TO-HAVE` lists, each line `[file:line] <finding> — <why>`; `- _none_` for empty buckets). If no report exists, tell the user to run /review-app or /deep-dive-data first and stop.
4. **Bucket every finding** into A / B / C per the table above, skipping `- _none_` lines. Cite each finding's `file:line`. Print a short plain-English plan first ("I'll fix N automatically, ask you about N, and N are heads-up only") so the user knows the shape before any commit lands.
5. **Bucket A — auto-apply, one commit per finding.** In report order: read the cited line, apply the edit, **re-run the matching focused gate** for that finding's dimension (see mapping below) and a lint pass, then commit just that finding:
   ```
   git add apps/<slug>/<changed-files>
   git commit -m "fix(<slug>): <finding summary>"
   ```
   Keep commits atomic — one finding, one commit — so the eventual PR is auditable line-by-line. If a single fix fails its gate or lint, revert just that edit (`git checkout -- <file>`), mark the finding **deferred**, and keep going. Do not abort the whole chain on one failure.
6. **Bucket B — walk interactively.** Present each finding with its cited line and a proposed diff. Apply only what the user approves; show the diff before writing, then commit atomically exactly as in step 5. "Skip" and "mark resolved" are valid answers — record them as deferred / resolved, don't force an edit.
7. **Re-gate.** Run `streamsnow validate-app <slug>` (or hand off to /validate-app). Any FAIL → fix and re-run until PASS, or until the only remaining failures are documented judgment calls the user deferred.
8. **Report.** List the Bucket A commits applied, the Bucket B decisions (applied / deferred / resolved), and the Bucket C punch list. Note the final validate result and what to do next (`git log --oneline`, /review-app to re-scan, /ship-app to open the PR).

### Focused-gate mapping (step 5)

Match the finding's dimension to the check that proves it cleared, and run that check scoped to the app before committing:

| Finding type | Re-run after fixing |
|---|---|
| Denied/allowed schema swap | `streamsnow check schema-refs apps/<slug>` |
| Egress / code-exec / write-SQL / dynamic-SQL | `streamsnow check security apps/<slug>` |
| Missing `@st.cache_data(ttl=…)` | `streamsnow check caching apps/<slug>` |
| `:N IS NULL OR` bind-predicate trap | `streamsnow check bind-predicates apps/<slug>` |
| UI / chart / docs | lint only (no dedicated `check` subcommand) — verify visually if a Playwright MCP is loaded |

Only these four `check` subcommands exist. There is no schema/security/caching/bind-predicates fix that isn't provable by one of them — if a "fix" can't be confirmed green by the matching check, it's a Bucket B judgment call, not a Bucket A auto-fix.

## Runtime-aware fixes (container vs warehouse)

The same finding can need a different fix depending on the app's runtime, declared in `snowflake.yml` / config. Read it before touching deps or connection code:

- **Dependency manifest syntax.** Warehouse runtime uses conda-style pins (`pkg=1.2.3`, single `=`) in its environment manifest; container runtime uses PEP 440 (`pkg==1.2.3`, double `==`) in `pyproject.toml`. A review finding that flags a "pin syntax mismatch" is only mechanical when it cites the exact line — apply the right form for that runtime, don't sweep the whole file.
- **Never pin `python` in the warehouse manifest.** The warehouse runtime supplies Python itself; a `python==`/`python =` line is a known landmine. Deleting it is a safe Bucket A fix. Container apps pin Python normally.
- **Cache disable on the container connection.** Container-mode data calls may need the inner driver cache disabled so `@st.cache_data(ttl=…)` is the single source of truth — apply only in container apps, and only when the finding cites it.

If a finding's correct fix depends on a runtime you can't determine from config, reclassify it as Bucket B and ask.

## Gotchas & edge cases

- **`SELECT *` is only Bucket A when columns are inlined.** /review-app is static and can't reach the warehouse, so it surfaces `SELECT *` *without* a column list — that degrades to a deferred `-- TODO: enumerate columns` note, **not** a silent rewrite. /deep-dive-data runs live and inlines the explicit columns; only then is the substitution mechanical.
- **Altair → Plotly is two fixes.** Swapping the *import* is Bucket A; translating each `alt.Chart(...)` into a Plotly spec is Bucket B — never auto-rewrite the chart construction.
- **Stale line numbers.** If a file changed between /review-app and now, the cited `file:line` may point at the wrong place. If the recipe doesn't match what's there, don't force it — reclassify as Bucket B and walk it, or suggest re-running /review-app to refresh.
- **Don't re-run the review here.** That's /review-app's job. To loop review → fix → review automatically, use /auto-review-app instead.
- **Cross-agent and ticketing findings are optional, off by default in OSS.** Apply them only if present in the report and the tools/config are available (see `_shared/cross-agent-review.md`); otherwise ignore silently. Never create or update tickets unless the user asks.
- **UI verify is best-effort.** If a Playwright MCP is loaded and a fix touched a page (or a query feeding one), drive a single-page smoke walk per `_shared/playwright-walkthrough.md` to confirm the fix actually renders. If no MCP is loaded, skip silently — never block on it.

## Guardrails

- **One commit per finding. No batching.** Atomic commits are how a PR reviewer sees what changed and why.
- **Abort on git ambiguity, not on a single gate/lint failure.** If a file is already staged with unrelated changes, stop and ask. If one fix fails its check, revert that edit and continue the chain.
- **Never bypass a pre-commit hook with `--no-verify`.** If a commit is blocked by a hook (e.g. a `check` hook), fix the underlying issue — the hook is the governance lever, not an obstacle. Translate cryptic failures via `_shared/deploy-error-translator.md` if needed.
- **Stay within governance.** Never introduce a reference to a denied schema (`governance.schema_deny`); swap only to an allowed `governance.schema_allow` equivalent the finding names. If a proposed fix would add a denied schema, refuse it and downgrade to Bucket C with the policy reason.
- **Branch hygiene.** Don't switch branches, don't push. The user runs /ship-app when ready — the atomic commits here become that PR's body.

## Troubleshooting

- **"No review report found":** /review-app or /deep-dive-data hasn't run, or `.review/` was cleaned. Run /review-app `<slug>` first.
- **Everything classified as Bucket B:** common for older apps — the report is mostly judgment items. Walk them, or focus on BLOCK-only by triaging the report's `### BLOCK` lists first.
- **Lint fails after every fix:** suspect config drift in the repo's lint/format settings, not the fixes. Stop and surface it — that's a higher-priority fix than the review findings.
- **A focused `check` still fails after the fix:** the edit didn't actually clear the rule. Revert it, mark the finding deferred with the reason, and move on; don't commit a fix that doesn't pass its own gate.
- **`validate-app` FAILs on something not in the report:** the gate caught a deterministic issue the qualitative review didn't. Fix that first (run the failing `check` subcommand for detail), then resume.

## Done when

Every Bucket A finding is an atomic commit (or an explicit deferral with a reason), Bucket B is resolved or explicitly deferred, Bucket C is reported as a punch list, and `streamsnow validate-app <slug>` passes — or the only remaining failures are documented human calls the user chose to defer.

## See also

- /review-app, /deep-dive-data — produce the report this skill consumes
- /validate-app — the deterministic PASS/FAIL gate run in step 7
- /auto-review-app — loops review → fix → review automatically
- /ship-app — opens the PR these atomic commits become the body of
- /sql-review — focused SQL pass when most findings are in the SQL dimension
- `_shared/playwright-walkthrough.md`, `_shared/cross-agent-review.md`, `_shared/deploy-error-translator.md`
