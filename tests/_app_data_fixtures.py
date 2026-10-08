"""Acme repos with app-data objects, shared by the app-data tests. No network."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "streamsnow.config.example.yaml"
AD = "STREAMSNOW_APPS.STREAMSNOW_REPORTING"
OBJECTS_DIR = "app_specific_reporting_objects"
ORDERS_BY_DAY = (
    "SELECT order_date, SUM(revenue) AS revenue\n"
    "FROM ANALYTICS_DB.REPORTING.ORDERS\n"
    "GROUP BY order_date"
)


def dynamic_table(name: str, query: str = ORDERS_BY_DAY, ad: str = AD) -> str:
    return (
        f"CREATE OR ALTER DYNAMIC TABLE {ad}.{name}\n"
        "  TARGET_LAG = '1 hour'\n"
        "  WAREHOUSE = STREAMSNOW_WH\n"
        "  INITIALIZE = ON_CREATE\n"
        f"AS\n{query};\n"
    )


def view(name: str, query: str, ad: str = AD) -> str:
    """The default view form every skill and doc proposes."""
    return f"CREATE OR REPLACE VIEW {ad}.{name} COPY GRANTS AS\n{query};\n"


def header(fqn: str, grants: list[str] | None = None) -> str:
    """The comment header sql-review check requires on every DDL file, so a test that
    runs the whole check sees only the rule under test. Used by stays "none" because
    the fixture apps list no metrics."""
    return (
        f"-- Object: {fqn}\n"
        "-- Purpose: Acme fixture object for the app-data tests.\n"
        "-- Used by: none\n"
        f"-- Grants: {', '.join(grants or []) or 'none'}\n"
    )


def write_config(repo: Path, **governance) -> Path:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data["governance"].update(governance)
    path = repo / "streamsnow.config.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def write_app(
    repo: Path,
    slug: str,
    objects: dict[str, str],
    *,
    reasons: dict[str, str] | None = None,
    grants: dict[str, list[str]] | None = None,
    queries: dict[str, str] | None = None,
    read_objects: bool = True,
    ad: str = AD,
) -> Path:
    """apps/<slug> declaring each {NAME: ddl} in app data. By default each object gets
    reason: performance and one query that reads it, so only the rule under test fires.
    A reason of "" leaves the key out. Every DDL file gets the mandatory header."""
    app = repo / "apps" / slug
    odir = app / "sql_review" / OBJECTS_DIR
    odir.mkdir(parents=True, exist_ok=True)
    (app / "queries").mkdir(exist_ok=True)
    (app / "snowflake.yml").write_text("definition_version: 2\n", encoding="utf-8")
    entries = []
    for name, ddl in objects.items():
        fqn = f"{ad}.{name}"
        granted = list((grants or {}).get(name, []))
        (odir / f"{fqn}.sql").write_text(header(fqn, granted) + ddl, encoding="utf-8")
        entry = {"name": fqn, "grants": granted}
        reason = (reasons or {}).get(name, "performance")
        if reason:
            entry["reason"] = reason
        entries.append(entry)
        if read_objects:
            (app / "queries" / f"{name.lower()}.sql").write_text(
                f"SELECT * FROM {fqn}\n", encoding="utf-8"
            )
    for fname, sql in (queries or {}).items():
        (app / "queries" / fname).write_text(sql, encoding="utf-8")
    index = {"schema_version": 2, "app": slug, "pages": [], "objects": entries}
    (app / "sql_review" / "index.yaml").write_text(
        yaml.safe_dump(index, sort_keys=False), encoding="utf-8"
    )
    return app
