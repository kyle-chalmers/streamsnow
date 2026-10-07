---
name: release
description: Maintainer-only release of StreamSnow, driven by scripts/release.py. Subcommands are prepare <X.Y.Z> (bump, close the changelog, run the gates, open the release PR), tag <X.Y.Z> (after the PR merges, tag main so PyPI publishes, then create the GitHub Release) and verify <X.Y.Z> (check the publish landed). suggest recommends a version.
argument-hint: "prepare|tag|verify|suggest <X.Y.Z>"
model: haiku
disable-model-invocation: true
allowed-tools: ["Bash(uv run python scripts/release.py *)", "Bash(git status *)", "Bash(git fetch origin)", "Bash(git switch -c claude/release-*)", "Bash(git add -u)", "Bash(git commit *)", "Bash(git push -u origin claude/release-*)", "Bash(gh pr create *)"]
---

# /release

You run exact commands and report their output. The script makes every decision.
RELEASING.md is the source of truth for the procedure; this skill only drives the script.
The maintainer typing `/release tag X.Y.Z` is the confirmation for the one step that
cannot be undone (the tag push that publishes to PyPI).

## Rules

1. Run the commands below exactly, in order, from the repo root.
2. If any command exits non-zero, stop. Show its output verbatim and do nothing else.
3. Never edit files by hand. Never skip, retry around or work around a failing gate.
4. Never run `git tag`, `git push --tags`, `gh release` or `uv publish` yourself. Only the script tags and releases.
5. Never pick the version. If the user gave none, run `suggest`, show its output, and ask.
6. Never wait for or poll CI. Report and stop.

## suggest

1. `uv run python scripts/release.py suggest`
2. Show the output verbatim and ask the user which version to release.

## prepare X.Y.Z

1. `git status --porcelain`. If it prints anything, stop and ask the user to commit or stash.
2. `git fetch origin && git switch -c claude/release-X.Y.Z origin/main`
3. Ask the user: "Do the generated workflows now call a command added since the last
   release? If yes I will pass --pin-floor." Use their answer.
4. `uv run python scripts/release.py prepare X.Y.Z` (add `--pin-floor` if they said yes).
5. `uv run python scripts/release.py gates X.Y.Z --online`. Keep its output for the PR body.
   WARN lines are allowed; report them. Any FAIL means stop.
6. `git add -u`
7. `git commit -m "chore(X.Y.Z): release X.Y.Z" -m "<trailer>"`, where `<trailer>` is the
   `Co-Authored-By: Claude Haiku 4.5 <...>` line with Anthropic's noreply address, the
   same trailer Claude Code adds to its own commits. (The address is not spelled out here
   because the repo's privacy scan rejects any non-example email in the tree.)
8. `git push -u origin claude/release-X.Y.Z`
9. `gh pr create --base main --title "chore(X.Y.Z): release X.Y.Z" --body "<body>"`, where the
   body lists every gate line from step 5 verbatim.
10. Tell the user: "Once this PR has merged, type `/release tag X.Y.Z`." Then stop.

## tag X.Y.Z

1. `uv run python scripts/release.py tag X.Y.Z`
2. Show the output verbatim. On exit 0, tell the user to run `/release verify X.Y.Z` in a
   few minutes. If it says to rerun with `--release-only`, tell the user that and stop; run
   `uv run python scripts/release.py tag X.Y.Z --release-only` only when they ask.

## verify X.Y.Z

1. `uv run python scripts/release.py verify X.Y.Z`
2. Show the output verbatim. Exit 3 means still publishing: tell the user to run
   `/release verify X.Y.Z` again later. Do not loop.
