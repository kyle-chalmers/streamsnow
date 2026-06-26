# sync-with-main

Recipe to bring a feature branch up to date with `origin/main` by **rebase** (never merge), then push with `--force-with-lease`. A contract /ship-app reads and follows before opening or updating a PR. Not an invocable skill.

## Why rebase, never merge

Merging `origin/main` into a feature branch can silently revert your own fix. After a PR squash-merges, `main` gets a new commit unlinked from your branch; a later `git merge origin/main` can read the squash as "theirs added the line your branch deleted" and drop the deletion. Rebase replays your commits on top of current `main`, so your changes stay yours.

## Preconditions

- **One PR per branch.** Branch fresh from `main` for every new PR.
- **Never reuse a squash-merged branch.** Check first:
  `gh pr list --search "head:$(git branch --show-current) is:merged" --json number`
  Non-empty result → stop; the branch is spent. Start a new branch off `main`.
- Working tree is clean (`git status --short` empty) — commit or stash before syncing.

## Steps

1. `git fetch origin main` — update the remote-tracking ref.
2. If `git rev-list --count HEAD..origin/main` is `0`, the branch is already current — skip the rest.
3. Rebase: `git rebase origin/main`.
4. **On conflict:** rebase pauses per commit. Resolve the files, `git add <files>`, then `git rebase --continue`. Repeat until done. To bail out cleanly: `git rebase --abort` (returns the branch untouched) — then report the conflict to the user rather than guessing a resolution.
5. Push: `git push --force-with-lease`. The lease (not plain `--force`) refuses the push if the remote moved since your last fetch, so a teammate's push can't be clobbered.
6. **On lease rejection** (`stale info` / `rejected`): someone pushed to the branch after your fetch. Re-run from step 1 (`git fetch` → `git rebase origin/main` → push). Do **not** escalate to `--force`.

## Done when

The branch contains every commit from `origin/main` beneath your own, the rebase is clean, and `--force-with-lease` succeeded (or the branch was already current). If conflicts or repeated lease rejections can't be resolved cleanly, the recipe stops and reports to the user — it does not fall back to `git merge` or `--force`.
