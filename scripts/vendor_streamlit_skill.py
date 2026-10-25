#!/usr/bin/env python3
"""Refresh the vendored copy of the Streamlit team's `developing-with-streamlit` skill.

The page-builders and reviewers used to read that skill from GitHub at Streamlit's latest
release tag while they worked. The Claude plugin directory's policy forbids a plugin that
"direct[s] Claude to dynamically pull behavioral instructions from external sources", so
StreamSnow ships a pinned copy instead, and this script is the only way it changes. A
maintainer runs it, reviews the diff, and ships it in a release like any other change.

    uv run python scripts/vendor_streamlit_skill.py 1.66.1

Copies `SKILL.md` and `references/` verbatim into `vendor/upstream-streamlit-skill/`, outside
`skills/` so Claude Code never loads it as a skill of its own, at that release tag, plus Streamlit's Apache-2.0
`LICENSE`, and rewrites `VENDORED.md`. It skips `assets/templates/`, which StreamSnow's recipe
never uses (`skills/_shared/streamlit-upstream-skill.md`). Online by design; not part of the
offline test suite.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.request
from pathlib import Path

REPO = "streamlit/streamlit"
SKILL_PATH = "lib/streamlit/.agents/skills/developing-with-streamlit"
DEST = Path(__file__).resolve().parent.parent / "vendor" / "upstream-streamlit-skill"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "streamsnow-vendor-script"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https hosts)
        return resp.read()


def _wanted(rel: str) -> bool:
    return rel == "SKILL.md" or rel.startswith("references/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="a Streamlit release tag, e.g. 1.66.1")
    tag = parser.parse_args(argv).tag

    commit = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{tag}"))["sha"]
    tree = json.loads(_get(f"https://api.github.com/repos/{REPO}/git/trees/{commit}?recursive=1"))
    prefix = SKILL_PATH + "/"
    files = sorted(
        e["path"][len(prefix) :]
        for e in tree["tree"]
        if e["type"] == "blob"
        and e["path"].startswith(prefix)
        and _wanted(e["path"][len(prefix) :])
    )
    if "SKILL.md" not in files:
        print(f"no {SKILL_PATH}/SKILL.md at {tag}", file=sys.stderr)
        return 1

    if DEST.exists():
        shutil.rmtree(DEST)
    raw = f"https://raw.githubusercontent.com/{REPO}/{commit}"
    for rel in files:
        out = DEST / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_get(f"{raw}/{SKILL_PATH}/{rel}"))
    (DEST / "LICENSE").write_bytes(_get(f"{raw}/LICENSE"))
    (DEST / "VENDORED.md").write_text(
        f"""# Vendored: Streamlit's `developing-with-streamlit` skill

A pinned, unmodified copy of part of the Streamlit team's agent skill, read by StreamSnow's
page-builders and reviewers through
[streamlit-upstream-skill.md](../../skills/_shared/streamlit-upstream-skill.md). StreamSnow's conventions and
checks win every conflict with it.

- Source: [{REPO}](https://github.com/{REPO}), `{SKILL_PATH}`
- Release tag: `{tag}` (commit `{commit}`)
- Copied verbatim: `SKILL.md` and `references/` ({len(files)} files)
- Left out: `assets/templates/`, which StreamSnow never uses. Links in `SKILL.md` that point
  there do not resolve in this copy.
- License: Apache License 2.0 ([LICENSE](LICENSE)). Streamlit's source files carry
  "Copyright (c) Streamlit Inc. (2018-2022) Snowflake Inc. (2022-...)". Streamlit's
  [NOTICES]({raw}/NOTICES) file lists third-party software bundled in the Streamlit product,
  none of which is part of this copy.

Refresh only with `uv run python scripts/vendor_streamlit_skill.py <tag>`, then review the diff
and ship it in a release. Never edit these files by hand.
""",
        encoding="utf-8",
    )
    print(f"vendored {len(files)} files from {REPO}@{tag} ({commit[:12]}) into {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
