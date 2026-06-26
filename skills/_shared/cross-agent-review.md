# cross-agent-review

Purpose: fan one qualitative review prompt to external AI CLIs (`agy`, `codex`) **in parallel** with Claude subagents, then merge all findings into one attributed list. This is a contract that /review-app and /deep-dive-data read and follow — not an invocable skill. It never blocks: `streamsnow validate-app` is the only deterministic PASS/FAIL gate; everything here is judgment that a human decides on.

## When it runs

Always OFF by default in OSS. A calling skill opts in only when **both** hold:

1. Config enables it — `streamsnow.config.yaml` has `review.cross_agent: true` (absent or `false` → skip entirely, Claude-only).
2. At least one external CLI passes the smoke test below.

If either fails, degrade silently to Claude-only review and emit one line: `cross-agent review off (config disabled | no external CLI found); using Claude reviewers only`. Never error out, never prompt to install anything.

Honor a caller `--no-cross-agent` flag: skip detection, go Claude-only.

## Detect + smoke-test each CLI

For each candidate (`agy`, `codex`), confirm it's on PATH and actually answers before trusting it. macOS has no `timeout`(1) — use a perl-alarm wrapper so a hung CLI can't stall the review.

```bash
# perl-alarm wrapper: run-bounded <seconds> <cmd...>
run_bounded() { local s=$1; shift; perl -e 'alarm shift; exec @ARGV' "$s" "$@"; }

have() { command -v "$1" >/dev/null 2>&1; }

# agy smoke test (~20s budget)
have agy && run_bounded 20 agy --version >/dev/null 2>&1 && AGY_OK=1

# codex needs stdin closed or `codex exec` hangs on EOF; closed-stdin also
# drops the banner, which is fine for a smoke test.
have codex && run_bounded 20 codex exec "reply OK" < /dev/null >/dev/null 2>&1 && CODEX_OK=1
```

Only CLIs that set their `*_OK` flag join the fan-out. A CLI that's present but fails the smoke test is treated as absent (log one line, move on).

## Fan out the prompt

Build **one** review prompt string (the caller supplies it — the same prompt its Claude subagents get, including the app slug, the dimensions to cover, and the finding format below). Dispatch all reviewers concurrently:

- **Claude reviewers** — launch via the Task tool (parallel subagents), one per dimension, exactly as the caller already does.
- **`agy`** (if `AGY_OK`) — `run_bounded 180 agy -p "$PROMPT"` (non-interactive print mode).
- **`codex`** (if `CODEX_OK`) — `run_bounded 180 codex exec "$PROMPT" < /dev/null` (the `< /dev/null` is mandatory).

Give each external CLI a generous wall-clock budget (180s shown). On timeout or non-zero exit, drop that reviewer's output, note `<cli> review skipped (timeout/error)`, and continue — partial coverage beats a stalled review.

All reviewers are read-only: they critique source, they do not edit it. Any live-DB lineage step stays inside /deep-dive-data and uses `snow sql` (read-only, bounded) — never delegated to an external CLI.

## Finding format (every reviewer emits this)

```
[SEVERITY] (dimension) finding — file:line — one-line fix
```

`SEVERITY` ∈ `BLOCK | FLAG | NICE-TO-HAVE`. Each line carries an attribution tag the merge step adds: `(Claude)`, `(Agy)`, `(Codex)`.

## Merge + consensus

1. Collect every reviewer's findings; tag each with its source.
2. Group near-duplicates (same file:line + same concern). When ≥2 distinct sources raise one issue, collapse to a single line and append `(also flagged by <sources>)` — consensus is a strong signal, surface it.
3. Order by severity: `BLOCK` → `FLAG` → `NICE-TO-HAVE`. Preserve attribution on every line.
4. Hand the merged list back to the caller **unchanged in shape**, so /apply-review consumes it identically whether it came from one agent or four.

## Contract for callers

- /review-app and /deep-dive-data call this recipe, supply the prompt + dimensions, and receive one merged finding list.
- The merged list is qualitative only — it advises, it never gates a ship. Mechanical auto-fixes flow on to /apply-review; judgment items go to the user.
- Output schema is stable across reviewer count so downstream skills need no special-casing for the cross-agent case.
