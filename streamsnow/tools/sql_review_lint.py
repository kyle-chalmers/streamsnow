"""sqlfluff for ``sql_review``: fix page sections, lint app queries.

One config drives both: the repo's ``.sqlfluff`` (rendered by ``streamsnow
init``), or the packaged default when a repo has none. It is loaded from that
text alone, never from ``~/.sqlfluff`` or parent directories, so generated
output does not depend on whose machine ran ``generate``.

Two deliberate limits:

- ``generate`` applies only the ``layout`` and ``capitalisation`` rule groups.
  Other core rules have fixes that change what SQL does (AL05 deletes an alias
  it believes is unused, and on a ``LATERAL FLATTEN`` that can be wrong); a
  review file must run exactly what the app runs.
- ``check`` lints the app's ``queries/*.sql`` with the full configured rule
  set, and only parses page sections: a page section is a pure function of its
  query plus the index, so its style findings are the query's findings, and
  provenance already proves the file is what ``generate`` wrote.

sqlfluff is imported lazily: ``check`` on an app with no index never pays for it.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from ..scaffolder import TEMPLATES_DIR

#: Rule groups whose fixes only re-lay out or re-case SQL.
FIX_RULES = "layout,capitalisation"
#: Repo-level config file name, and the packaged default it is rendered from.
CONFIG_NAME = ".sqlfluff"
DEFAULT_CONFIG = TEMPLATES_DIR / "repo" / "sqlfluff.j2"

_TOKEN_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


def config_text(repo: Path) -> str:
    """The repo's ``.sqlfluff`` text, or the packaged default."""
    path = repo / CONFIG_NAME
    if path.is_file():
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return DEFAULT_CONFIG.read_text(encoding="utf-8")


@lru_cache(maxsize=8)
def _linter(cfg_text: str, mode: str):
    from sqlfluff.core import FluffConfig, Linter  # noqa: PLC0415  (lazy: slow import)
    from sqlfluff.core.config import load_config_string  # noqa: PLC0415

    overrides: dict[str, str] = {"templater": "raw"}
    if mode == "fix":
        overrides["rules"] = FIX_RULES
    elif mode == "query":
        overrides["templater"] = "placeholder"
    cfg = FluffConfig(configs=load_config_string(cfg_text), overrides=overrides)
    if mode == "query":
        # App queries carry driver binds (`:1`, `:start_date`); the placeholder
        # templater stands them in so the SQL around them still parses.
        cfg.set_value(["templater", "placeholder", "param_style"], "colon")
    return Linter(config=cfg)


def fix_section(sql: str, cfg_text: str) -> str:
    """Layout and capitalisation fixes only (see the module docstring)."""
    fixed = _linter(cfg_text, "fix").lint_string(sql, fix=True).fix_string()[0]
    return fixed


def parse_problems(sql: str, cfg_text: str) -> list[tuple[int, str]]:
    """``(line, detail)`` for each part of ``sql`` sqlfluff cannot parse."""
    parsed = _linter(cfg_text, "parse").parse_string(sql)
    return [(v.line_no, v.desc()) for v in parsed.violations]


def lint_query(
    sql: str, cfg_text: str, tokens: dict[str, str]
) -> tuple[list[tuple[int, str, str]], list[tuple[str, int]]]:
    """Lint an app query: ``([(line, rule, detail)], [(cte_name, line)])``.

    Each ``{TOKEN}`` is replaced with its sample value on one line, so line
    numbers stay true to the file. A line-length finding on a line that held a
    token is dropped: the length is the sample's, not the query's. Whitespace an
    empty sample leaves behind is dropped too: the "All" state of
    ``... :2 {REGION_FILTER}`` would otherwise end in a space (LT01), and a token
    on the last line would leave a blank one (LT12), neither of which the query
    has. Trailing whitespace the author wrote is kept, so LT01 still finds it.
    CTEs come from sqlfluff's parse tree (nested ones included), which is what
    the comment rule needs and what no regex over SQL text gets right.
    """
    token_lines: set[int] = set()
    out_lines = []
    for lineno, line in enumerate(sql.split("\n"), start=1):
        if _TOKEN_RE.search(line):
            token_lines.add(lineno)
            authored = line
            for name, value in tokens.items():
                line = line.replace("{" + name + "}", " ".join(value.split()))
            if authored == authored.rstrip():
                line = line.rstrip()
        out_lines.append(line)
    # Blank token lines just before the final newline: dropping them shifts no
    # other line's number.
    while (
        len(out_lines) > 1
        and out_lines[-1] == ""
        and out_lines[-2] == ""
        and len(out_lines) - 1 in token_lines
    ):
        del out_lines[-2]
    linted = _linter(cfg_text, "query").lint_string("\n".join(out_lines))
    found = []
    for v in linted.get_violations():
        code = v.rule_code()
        if code == "LT05" and v.line_no in token_lines:
            continue
        found.append((v.line_no, code, v.desc()))
    ctes: list[tuple[str, int]] = []
    if linted.tree is not None:
        for seg in linted.tree.recursive_crawl("common_table_expression"):
            name = next((r for r in seg.raw_segments if r.is_code), None)
            if name is not None:
                ctes.append((name.raw, name.pos_marker.source_position()[0]))
    return sorted(found), ctes
