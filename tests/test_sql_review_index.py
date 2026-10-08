"""Tests for streamsnow.tools.sql_review_index: index.yaml loading and validation.

Pinned here: page numbers come from the app's navigation, never the YAML order;
metric numbers from the order under metrics:; every validation problem is a
finding (kind `index`), never an exception; a nav page the index omits is a
`coverage` finding; `review_value` markers are read by AST, never by import.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from streamsnow.tools import sql_review_index as sri

SLUG = "acme-sales"

NAV = """import streamlit as st

nav = st.navigation(
    [
        st.Page("pages/overview.py", title="Overview", default=True),
        st.Page("pages/regions.py", title="Regions"),
    ]
)
nav.run()
"""

REVENUE = """-- Query: revenue
-- Params: :1 start_date, :2 end_date
SELECT SUM(revenue) AS revenue
FROM ANALYTICS_DB.REPORTING.ORDERS
WHERE order_date BETWEEN :1 AND :2 {REGION_FILTER}
"""

BY_REGION = """-- Query: by_region
SELECT region, COUNT(*) AS n
FROM ANALYTICS_DB.REPORTING.ORDERS
WHERE order_date >= :start_date
GROUP BY region
"""


def _index(**overrides) -> dict:
    data = {
        "schema_version": 2,
        "app": SLUG,
        "review_window": {
            "start_date": "DATEADD(DAY, -30, CURRENT_DATE())",
            "end_date": "CURRENT_DATE()",
        },
        "pages": [
            {
                "path": "pages/regions.py",
                "metrics": [
                    {
                        "key": "orders_by_region",
                        "query": "queries/by_region.sql",
                        "binds": {"start_date": "params.start_date"},
                    }
                ],
            },
            {
                "path": "pages/overview.py",
                "metrics": [
                    {
                        "key": "total_revenue",
                        "query": "queries/revenue.sql",
                        "tokens": {"REGION_FILTER": "AND region = 'West'"},
                        "binds": {"1": "params.start_date", "2": "params.end_date"},
                        "reads": ["ANALYTICS_DB.REPORTING.ORDERS"],
                    }
                ],
            },
        ],
    }
    data.update(overrides)
    return data


@pytest.fixture()
def app(tmp_path: Path) -> Path:
    a = tmp_path / "apps" / SLUG
    (a / "queries").mkdir(parents=True)
    (a / "pages").mkdir()
    (a / "sql_review").mkdir()
    (a / "streamlit_app.py").write_text(NAV, encoding="utf-8")
    (a / "queries" / "revenue.sql").write_text(REVENUE, encoding="utf-8")
    (a / "queries" / "by_region.sql").write_text(BY_REGION, encoding="utf-8")
    _write(a, _index())
    return a


def _write(app: Path, data: dict) -> None:
    (app / "sql_review" / "index.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _details(index: sri.Index, kind: str = "index") -> list[str]:
    return [f["detail"] for f in index.findings if f["kind"] == kind]


def test_valid_index_loads_clean_with_numbers_from_nav(app: Path) -> None:
    idx = sri.load_index(app)
    assert idx.exists and idx.findings == []
    # The YAML lists regions first; the nav puts overview first. Nav wins.
    assert [(p.number, p.filename, p.title) for p in idx.numbered_pages] == [
        (1, "01_overview.sql", "Overview"),
        (2, "02_regions.sql", "Regions"),
    ]
    metric = idx.numbered_pages[0].metrics[0]
    assert (metric.number, metric.key, metric.tokens, metric.binds) == (
        1,
        "total_revenue",
        {"REGION_FILTER": "AND region = 'West'"},
        {"1": "params.start_date", "2": "params.end_date"},
    )


def test_reordering_the_nav_renumbers_pages(app: Path) -> None:
    entry = app / "streamlit_app.py"
    lines = entry.read_text(encoding="utf-8").splitlines()
    lines[4], lines[5] = lines[5], lines[4]
    entry.write_text("\n".join(lines) + "\n", encoding="utf-8")
    idx = sri.load_index(app)
    assert [p.filename for p in idx.numbered_pages] == ["01_regions.sql", "02_overview.sql"]


def test_metric_numbers_follow_the_metrics_order(app: Path) -> None:
    data = _index()
    (app / "queries" / "orders.sql").write_text(
        "SELECT COUNT(*) AS n FROM T.S.ORDERS\n", encoding="utf-8"
    )
    data["pages"][1]["metrics"].insert(0, {"key": "order_count", "query": "queries/orders.sql"})
    _write(app, data)
    keys = [(m.number, m.key) for m in sri.load_index(app).numbered_pages[0].metrics]
    assert keys == [(1, "order_count"), (2, "total_revenue")]


def test_nav_page_missing_from_the_index_is_a_coverage_finding(app: Path) -> None:
    data = _index()
    del data["pages"][0]
    _write(app, data)
    idx = sri.load_index(app)
    assert _details(idx) == []
    assert any("pages/regions.py" in d for d in _details(idx, "coverage"))


def test_missing_index_is_not_an_error(app: Path) -> None:
    (app / "sql_review" / "index.yaml").unlink()
    idx = sri.load_index(app)
    assert not idx.exists and idx.findings == []


def _mutate(app: Path, fn) -> list[str]:
    data = _index()
    fn(data)
    _write(app, data)
    return _details(sri.load_index(app))


def _overview_metric(data: dict) -> dict:
    return data["pages"][1]["metrics"][0]


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (lambda d: d.update(extra=1), "unknown key"),
        (lambda d: d.update(schema_version=1), "schema_version must be 2"),
        (lambda d: d.update(app="other-app"), "app must be"),
        (lambda d: d["review_window"].update(BadKey="x"), "lower-case SQL identifier"),
        (lambda d: d["pages"].append({"path": "pages/nope.py"}), "not in the app's navigation"),
        (lambda d: d["pages"].append(dict(d["pages"][0])), "listed more than once"),
        (lambda d: _overview_metric(d).update(colour="red"), "unknown key"),
        (lambda d: _overview_metric(d).update(key="TotalRevenue"), "snake_case"),
        (lambda d: _overview_metric(d).update(key="a_b_c_d_e_f"), "five words or fewer"),
        (lambda d: _overview_metric(d).update(query="queries/missing.sql"), "does not exist"),
        (lambda d: _overview_metric(d).update(query="../secrets.sql"), "app-relative path"),
        (lambda d: _overview_metric(d).update(tokens={}), "no sample value"),
        (
            lambda d: _overview_metric(d)["tokens"].update(UNUSED="x"),
            "token 'UNUSED' is not used",
        ),
        (lambda d: _overview_metric(d)["binds"].pop("2"), "uses :2 but binds"),
        (lambda d: _overview_metric(d)["binds"].update({"3": "1"}), "bind '3' is not used"),
        (
            lambda d: _overview_metric(d)["binds"].update({"1": "params.nowhere"}),
            "review_window does not define",
        ),
        (lambda d: _overview_metric(d).update(reads=["ORDERS"]), "DATABASE.SCHEMA.OBJECT"),
        (
            lambda d: d["pages"][1]["metrics"].append(dict(_overview_metric(d))),
            "appears twice",
        ),
        (lambda d: d.update(objects=[{"name": "not-an-fqn"}]), "DATABASE.SCHEMA.OBJECT"),
        (
            lambda d: d.update(fragments=[{"file": "queries/revenue.sql", "reason": "x"}]),
            "also a metric's query",
        ),
        (
            lambda d: d.update(fragments=[{"file": "queries/gone.sql", "reason": "x"}]),
            "does not exist",
        ),
        (
            lambda d: d.update(fragments=[{"file": "queries/by_region.sql"}]),
            "reason is required",
        ),
    ],
)
def test_each_validation_problem_is_a_finding(app: Path, mutate, expect: str) -> None:
    details = _mutate(app, mutate)
    assert any(expect in d for d in details), details


def test_unparseable_yaml_is_a_finding_not_a_traceback(app: Path) -> None:
    (app / "sql_review" / "index.yaml").write_text("pages: [unclosed\n", encoding="utf-8")
    idx = sri.load_index(app)
    assert idx.exists and _details(idx)[0].startswith("unreadable")


def test_findings_point_at_the_offending_line(app: Path) -> None:
    data = _index()
    _overview_metric(data)["query"] = "queries/missing.sql"
    _write(app, data)
    finding = next(f for f in sri.load_index(app).findings if "does not exist" in f["detail"])
    text = (app / "sql_review" / "index.yaml").read_text(encoding="utf-8").splitlines()
    assert "total_revenue" in text[finding["line"] - 1]


def test_binds_and_tokens_are_read_from_sql_not_comments() -> None:
    sql = "-- Params: :1 start_date\nSELECT x::DATE, v:field, '{NOT}' AS s\nWHERE a = :1 AND b = :name {TOK}\n"
    assert sri.query_binds(sql) == ["1", "name"]
    assert sri.query_tokens(sql) == ["NOT", "TOK"]  # the app's str.replace sees quoted ones too


def test_markers_are_read_by_ast() -> None:
    src = (
        "import review\n"
        "from review import review_value\n"
        'st.metric("A", review_value("total_revenue", x))\n'
        'st.dataframe(review.review_value("orders_by_region", df))\n'
        "review_value(key_var, y)\n"
        'review_value(key="by_keyword", value=z)\n'
        '# review_value("in_a_comment", q)\n'
    )
    assert sri.scan_markers(src) == [
        ("total_revenue", 3),
        ("orders_by_region", 4),
        (None, 5),
        ("by_keyword", 6),
    ]


def test_paths_resolve_only_inside_the_app(app: Path, tmp_path: Path) -> None:
    (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")
    assert sri._contained(app, "queries/revenue.sql") == app / "queries" / "revenue.sql"
    assert sri._contained(app, "../../outside.py") is None
    assert sri._contained(app, "queries/missing.sql") is None


def test_objects_take_a_reason(app: Path) -> None:
    obj = {
        "name": "STREAMSNOW_APPS.STREAMSNOW_REPORTING.DAILY_REVENUE",
        "grants": [],
        "reason": "performance",
    }
    _write(app, _index(objects=[obj]))
    idx = sri.load_index(app)
    assert idx.objects[0].reason == "performance"
    assert not [d for d in _details(idx) if "unknown key" in d]


def test_a_non_string_reason_is_an_index_finding(app: Path) -> None:
    obj = {"name": "STREAMSNOW_APPS.STREAMSNOW_REPORTING.DAILY_REVENUE", "reason": 3}
    _write(app, _index(objects=[obj]))
    assert any("reason must be a string" in d for d in _details(sri.load_index(app)))


@pytest.mark.parametrize(
    "objects",
    [
        "not a list",
        [{"name": "NOT_THREE_PARTS"}],
        ["STREAMSNOW_APPS.STREAMSNOW_REPORTING.DAILY_REVENUE"],
    ],
)
def test_an_objects_list_that_did_not_load_is_marked_incomplete(app: Path, objects) -> None:
    """Tombstones and teardown read the declared inventory: one that lost an entry
    must say so, or a removed object would read as never declared."""
    _write(app, _index(objects=objects))
    assert sri.load_index(app).objects_complete is False


def test_a_clean_or_missing_objects_list_is_complete(app: Path) -> None:
    assert sri.load_index(app).objects_complete is True
    (app / "sql_review" / "index.yaml").write_text("pages: [\n", encoding="utf-8")
    assert sri.load_index(app).objects_complete is False
