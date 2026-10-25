# StreamSnow privacy policy

Last updated: 2026-10-24. Applies to the StreamSnow Claude Code plugin and the `streamsnow` CLI.

StreamSnow is open-source software that runs on your own machine, inside your own coding agent,
under your own accounts. Its maintainer runs no StreamSnow service: **no data about you, your
apps or your Snowflake account is sent to the maintainer**, and StreamSnow has no telemetry or
analytics.

## What StreamSnow reads

To build and review apps, the skills read data from your Snowflake account through your own
connection: schema metadata, row counts and date ranges, up to 20 of the most frequent values for
each filter you specify, and screenshots of your running app. Your coding agent sends what the
skills read to its model provider (Anthropic, for Claude Code) under your agreement with that
provider. If your tables hold personal data, such as names or emails, that data can be part of
what is read.

## What StreamSnow stores, and where

Everything stays on your machine or in your own repository. Nothing is stored anywhere else.

| What | Where | How long |
|---|---|---|
| Filter values from data profiling | the app's `REQUIREMENTS.md`, which you commit | Until you edit or delete it |
| Walkthrough screenshots | `apps/<slug>/.review/` (gitignored) | Until you delete them |
| CI private key | `~/.streamsnow-ci/` | Until you delete it |
| Snowflake connection (when you let setup add one) | `~/.snowflake/connections.toml` | Until you edit it |
| Review nudge state | `$TMPDIR/streamsnow-review-gate/` | Swept after 7 days |
| Anaconda package index cache | `$TMPDIR/streamsnow-<user>/` | Refreshed after 24 hours |

If a filter column holds personal data, so will `REQUIREMENTS.md`. Point filters at non-personal
columns, or edit the values out before you commit.

## Services StreamSnow contacts

StreamSnow contacts these services only when you run the step that needs them, under your own
account where one is involved. Each handles data under its own privacy policy.

| Service | What it receives | Privacy policy |
|---|---|---|
| Your coding agent's model provider (Anthropic for Claude Code) | What the skills read, as part of your session | [Anthropic](https://www.anthropic.com/legal/privacy) |
| Snowflake | Queries you can read in your session; app code on deploy; a CI sign-in check | [Snowflake](https://www.snowflake.com/en/legal/privacy/privacy-policy/) |
| GitHub | Your app code, CI secrets through `gh secret set`, and one repo setting after you say yes; also Streamlit's source when checking an `st.*` call against an app's pinned version | [GitHub](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement) |
| OSV.dev (run by Google) | Package names and exact versions from each app's manifest | [OSV.dev FAQ](https://google.github.io/osv.dev/faq/), [Google](https://policies.google.com/privacy) |
| Anaconda | A download request for Snowflake's package index; nothing of yours | [Anaconda](https://legal.anaconda.com/policies/en/?name=privacy-policy) |
| npm and Microsoft's Playwright browser download | Download requests for the pinned Playwright CLI and its browser | [npm](https://docs.npmjs.com/policies/privacy), [Microsoft](https://www.microsoft.com/en-us/privacy/privacystatement) |
| Homebrew, PyPI, astral.sh, nvm | Download requests during setup, after one approval, for tools you are missing | [Homebrew](https://docs.brew.sh/Analytics), [PyPI](https://policies.python.org/pypi.org/Privacy-Notice/), [astral.sh](https://astral.sh/legal/privacy), [GitHub](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement) (nvm) |
| Google Antigravity (`agy`) and OpenAI Codex (`codex`) | The review prompt and read access to your repo, **only** with `review.cross_agent: true` (off by default) | [Google](https://policies.google.com/privacy), [OpenAI](https://openai.com/policies/privacy-policy/) |

The README's [Data and network](README.md#data-and-network) section lists the same behavior with
the commands that trigger it.

## Children

StreamSnow is a developer tool for adults working with their organization's data. It is not
intended for anyone under 18.

## Changes and contact

Changes to this policy ship in a StreamSnow release and are noted in the
[CHANGELOG](CHANGELOG.md). Questions go to
[GitHub issues](https://github.com/kyle-chalmers/streamsnow/issues/new/choose); security problems
go through private vulnerability reporting, as [SECURITY.md](SECURITY.md) describes.
