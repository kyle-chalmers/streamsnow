"""Example SQL must never drop a warehouse it did not create.

``CREATE WAREHOUSE IF NOT EXISTS X`` is a silent no-op when ``X`` already
exists, so a later ``DROP WAREHOUSE X`` in the same script would remove
someone else's warehouse. A script that drops its warehouse at the end has to
create it with a plain ``CREATE WAREHOUSE`` so a taken name fails loudly.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_SQL = sorted((REPO_ROOT / "examples").rglob("*.sql"))

_DROP_RE = re.compile(r"^\s*DROP\s+WAREHOUSE\s+(?:IF\s+EXISTS\s+)?(\w+)", re.I | re.M)
_CREATE_RE = re.compile(
    r"^\s*CREATE\s+(OR\s+REPLACE\s+)?WAREHOUSE\s+(IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I | re.M
)


def test_examples_ship_sql():
    assert EXAMPLE_SQL, "expected at least one example .sql file"


def test_dropped_warehouses_are_created_without_if_not_exists():
    for path in EXAMPLE_SQL:
        sql = path.read_text(encoding="utf-8")
        dropped = {m.group(1).upper() for m in _DROP_RE.finditer(sql)}
        for m in _CREATE_RE.finditer(sql):
            name = m.group(3).upper()
            if name not in dropped:
                continue
            assert not m.group(2), f"{path.name}: {name} is dropped but created IF NOT EXISTS"
            assert not m.group(1), f"{path.name}: {name} is dropped but created OR REPLACE"
        created = {m.group(3).upper() for m in _CREATE_RE.finditer(sql)}
        assert dropped <= created, f"{path.name}: drops a warehouse it never creates"
