"""Load SQL from ``queries/``. Each app ships its own copy.

- ``load_sql(name)`` returns the raw file contents.
- ``render_sql(name, **tokens)`` substitutes ``{UPPERCASE_TOKEN}`` placeholders
  via str.replace (NOT str.format — SQL patterns like ``{2}`` would collide).
"""

from pathlib import Path

_QUERIES = Path(__file__).parent / "queries"


def load_sql(name: str) -> str:
    return (_QUERIES / f"{name}.sql").read_text()


def render_sql(name: str, **tokens: str) -> str:
    sql = load_sql(name)
    for key, value in tokens.items():
        sql = sql.replace("{" + key + "}", value)
    return sql
