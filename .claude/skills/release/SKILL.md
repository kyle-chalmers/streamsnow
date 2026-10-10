---
name: release
description: Maintainer-only release of StreamSnow, driven by scripts/release.py. One prompt ("cut the release", /release, /release X.Y.Z) runs it end to end, and publish.yml tags and publishes when the release PR merges. Also suggest, prepare <X.Y.Z>, verify <X.Y.Z>, and the by-hand fallback tag <X.Y.Z>.
argument-hint: "[X.Y.Z] | suggest | prepare|verify|tag <X.Y.Z>"
model: haiku
disable-model-invocation: true
allowed-tools: ["Bash(uv run python scripts/release.py suggest)", "Bash(uv run python scripts/release.py prepare *)", "Bash(uv run python scripts/release.py open-pr *)", "Bash(uv run python scripts/release.py gates *)", "Bash(uv run python scripts/release.py verify *)", "Bash(git status *)", "Bash(git fetch origin)", "Bash(git switch -c claude/release-* origin/main)", "Bash(git log *)", "Bash(git diff *)"]
---

# /release

You run exact commands and report their output. The script makes every decision.
RELEASING.md is the source of truth for the procedure; this skill only drives the script.
It is the maintainer-sanctioned exception to the rule in `.claude/CLAUDE.md` that puts
tags and releases off-limits for agents.

**The protocol: one prompt.** When the maintainer asks for a release, run cut below to the
end and hand nothing back. open-pr turns on the release PR's auto-merge, and when it merges
`publish.yml` tags the commit (`release.py tag --from-ci`), publishes to PyPI and creates the
GitHub Release. Merging is the publish decision; the maintainer made it by asking.

## Rules

1. Run the commands below exactly, in order, from the repo root.
2. If a command exits non-zero, stop. Show its output verbatim, say what failed and why, and
   propose the fix. Never work around, skip or retry around a failing gate.
3. Never edit files by hand during a release.
4. Never run git push, git commit, git tag, gh pr, gh release or uv publish yourself; the
   script and publish.yml do those.
5. Never wait for or poll CI. Report and stop.
6. Never run the by-hand `tag` unless the maintainer says "tag X.Y.Z" in the chat session.

## cut ("cut the release", `/release`, `/release X.Y.Z`)

1. `git status --porcelain`. If it prints anything, stop and ask the user to commit or stash.
2. Version: the one the maintainer named, else run `uv run python scripts/release.py suggest`
   and take its recommendation.
3. `git fetch origin`, then `git switch -c claude/release-X.Y.Z origin/main`
4. `uv run python scripts/release.py prepare X.Y.Z`. It raises the generated workflows' pin
   by itself when the old pin cannot install X.Y.Z. Add `--pin-floor` only for a patch whose
   generated workflows call a command added since the last release.
5. Do its checklist yourself and report what you checked: read `git log` and `git diff`
   from the last tag to HEAD for real company, people or customer names, internal URLs,
   email addresses, ticket IDs and account locators; look at every changed image; note
   changes to LICENSE, README and CONTRIBUTING. If the Playwright CLI pin moved, or you find
   anything, stop and show it.
6. `uv run python scripts/release.py open-pr X.Y.Z --trailer "<line>"`, where `<line>` is the
   Co-Authored-By line from your own commit attribution instructions, copied verbatim (leave
   it out if you have none). With a pin floor from step 4, add `--pin-floor-note "<why>"`.
7. Show the output and stop: the PR merges itself when CI is green, and publish.yml takes it
   from there. Say `/release verify X.Y.Z` can confirm it later.

## suggest

`uv run python scripts/release.py suggest`, and show the output verbatim.

## prepare X.Y.Z

Steps 1, 3, 4 and 5 of cut, then stop and report.

## verify X.Y.Z

`uv run python scripts/release.py verify X.Y.Z`; show the output verbatim. Exit 3 means
still publishing (or still tagging): say so. Do not loop.

## tag X.Y.Z (fallback, only after the maintainer says "tag X.Y.Z")

For when publish.yml refused to tag (verify names its run). Say: "Next you will see a
permission prompt for the tag command. Approving it pushes the tag, which publishes to
PyPI and cannot be undone." Then run `uv run python scripts/release.py tag X.Y.Z` and show
the output verbatim. If it says to rerun with `--release-only`, say so and stop.
