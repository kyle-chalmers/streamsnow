"""Official-docs link registry: every Snowflake/Streamlit docs URL the repo cites
must be listed in docs/snowflake-docs.md, in canonical form.

This is a *registry* test, not a rot detector: it guarantees one place holds
every external docs link (with scope and retrieved date), and that nobody links
a path known to 404 or a pre-2026 flat path that only survives via redirect.
Rot (a listed URL going dark) is a network question — `scripts/check_docs_links.py
--online` is the opt-in pre-release sweep for that (see RELEASING.md).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY = REPO_ROOT / "docs" / "snowflake-docs.md"

_URL_RE = re.compile(r"https://docs\.(?:snowflake\.com|streamlit\.io)/[^\s<>()\[\]`'\"|]*")
_SKIP_PARTS = {".git", ".venv", ".ai-friend-review", ".claude", "__pycache__", "node_modules"}
_SKIP_NAMES = {"uv.lock", "CHANGELOG.md"}
_SUFFIXES = {".md", ".j2", ".py", ".sh", ".yaml", ".yml", ".toml"}
_FORBIDDEN = (
    "/snowflake-cli-v2/",
    "/project-definitions/entity-types",
    "/app-development/connect-to-snowflake",
)
# Pre-reorg flat pages that now only resolve through a 308 redirect.
_LEGACY_FLAT = re.compile(
    r"https://docs\.snowflake\.com/en/developer-guide/streamlit/"
    r"(create-streamlit-ui|create-streamlit-sql|create-streamlit-snowflake-cli|"
    r"additional-features|owners-rights|example-multi-page)\b"
)


def _clean(url: str) -> str:
    url = url.rstrip(".,;:!?*_")
    url = url.split("#", 1)[0]
    return url.rstrip("/")


def _scan() -> dict[str, set[Path]]:
    seen: dict[str, set[Path]] = {}
    for p in REPO_ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in _SUFFIXES or p.name in _SKIP_NAMES:
            continue
        if _SKIP_PARTS & set(p.relative_to(REPO_ROOT).parts):
            continue
        if p == REGISTRY or p.parts[-2:] == ("tests", "test_docs_links.py"):
            continue
        for m in _URL_RE.finditer(p.read_text(errors="ignore")):
            seen.setdefault(_clean(m.group(0)), set()).add(p.relative_to(REPO_ROOT))
    return seen


def _registry() -> set[str]:
    return {_clean(m.group(0)) for m in _URL_RE.finditer(REGISTRY.read_text())}


def test_registry_exists_and_is_non_trivial():
    assert REGISTRY.is_file()
    assert len(_registry()) >= 20


def test_every_cited_docs_url_is_in_the_registry():
    missing = {u: sorted(map(str, f)) for u, f in _scan().items() if u not in _registry()}
    assert not missing, f"link these from docs/snowflake-docs.md too: {missing}"


def test_no_known_404_or_legacy_paths_anywhere():
    urls = set(_scan()) | _registry()
    bad = [u for u in urls if any(f in u for f in _FORBIDDEN) or _LEGACY_FLAT.search(u)]
    assert not bad, bad


def test_registry_urls_are_canonical():
    raw = [m.group(0) for m in _URL_RE.finditer(REGISTRY.read_text())]
    assert all(not u.endswith("/") for u in raw), [u for u in raw if u.endswith("/")]
    assert all("#" not in u for u in raw), [u for u in raw if "#" in u]
