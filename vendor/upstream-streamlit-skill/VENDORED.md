# Vendored: Streamlit's `developing-with-streamlit` skill

A pinned, unmodified copy of part of the Streamlit team's agent skill, read by StreamSnow's
page-builders and reviewers through
[streamlit-upstream-skill.md](../../skills/_shared/streamlit-upstream-skill.md). StreamSnow's conventions and
checks win every conflict with it.

- Source: [streamlit/streamlit](https://github.com/streamlit/streamlit), `lib/streamlit/.agents/skills/developing-with-streamlit`
- Release tag: `1.66.1` (commit `ca6718b7d3d788c31c97d1bc9144fd8388168f57`)
- Copied verbatim: `SKILL.md` and `references/` (28 files)
- Left out: `assets/templates/`, which StreamSnow never uses, and `references/server-asgi.md`,
  which does not apply to a Streamlit-in-Snowflake app. Links in `SKILL.md` that point to them do
  not resolve in this copy.
- License: Apache License 2.0 ([LICENSE](LICENSE)). Streamlit's source files carry
  "Copyright (c) Streamlit Inc. (2018-2022) Snowflake Inc. (2022-...)". Streamlit's
  [NOTICES](https://raw.githubusercontent.com/streamlit/streamlit/ca6718b7d3d788c31c97d1bc9144fd8388168f57/NOTICES) file lists third-party software bundled in the Streamlit product,
  none of which is part of this copy.

Refresh only with `uv run python scripts/vendor_streamlit_skill.py <tag>`, then review the diff
and ship it in a release. Never edit these files by hand.
