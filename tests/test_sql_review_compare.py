"""Tests for ``sql-review compare``: the screen held to the reviewed SQL.

No Snowflake and no browser: a fake ``snow`` runner produces a real run
directory, each test writes the ``run`` results and the ``review_value``
captures it needs, and ``compare`` is judged on its JSON. What is pinned here:

- the tolerance: displayed rounding or 0.5%, integers exact, ``FLOAT`` relative,
  ``%`` as a ratio (and as points only above 1);
- frames pair totals by hashed name, then by value, and say which rule matched;
- a scalar compares only against a one-row single total or a row count, or
  (``aggregated``) the sum of a multi-row result's only numeric column;
- a frame grouped from the SQL's rows is ``aggregated`` only when every shown
  total agrees and its keys are non-numeric SQL columns; a slice never is;
- captures are found by page path, stem or (unique) key, never by guesswork;
- ``compare:`` ids become evidence; the agent-written ``screen.json`` never does.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
import test_sql_review_live as lt

from streamsnow.scaffolder import _env
from streamsnow.tools import sql_review as sr
from streamsnow.tools import sql_review_compare as cmp
from streamsnow.tools import sql_review_live as live

SLUG = lt.SLUG


@pytest.fixture()
def run_dir(tmp_path: Path, capsys: pytest.CaptureFixture) -> Path:
    root = lt._make_repo(tmp_path)
    (root / "streamsnow.config.yaml").write_text(lt.CONFIG, encoding="utf-8")
    assert sr.main(["generate", SLUG, "--dir", str(root)]) == 0
    assert lt._live(root, lt.FakeSnow(), "run") == 0
    capsys.readouterr()
    return lt._run_dir(root)


def _repo(run_dir: Path) -> Path:
    return run_dir.parents[3]


def _run(run_dir: Path, page: str, *results: dict) -> None:
    """Replace a page's run results with these (pass, keyed by n)."""
    path = run_dir / f"run-{page}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    by_n = {r["n"]: r for r in data["results"]}
    for r in results:
        by_n[r["n"]] = {**by_n[r["n"]], "status": "pass", "float_columns": [], **r}
    data["results"] = list(by_n.values())
    path.write_text(json.dumps(data), encoding="utf-8")


def _capture(run_dir: Path, page: str | None, key: str, kind: str, **fields) -> None:
    folder = run_dir / "capture"
    folder.mkdir(exist_ok=True)
    page_path = {"overview": "pages/overview.py", "regions": "pages/regions.py"}.get(page or "")
    data = {
        "schema": 1,
        "key": key,
        "page": page,
        "page_path": page_path,
        "kind": kind,
        "row_count": None,
        "columns": [],
        **fields,
    }
    (folder / f"{page or ''}__{key}.json").write_text(json.dumps(data), encoding="utf-8")


def _frame(
    rows: int,
    totals: dict[str, str | None],
    floats: tuple[str, ...] = (),
    keys: tuple[str, ...] = (),
) -> dict:
    """A frame capture; ``keys`` are its non-numeric columns (no total)."""
    d = cmp.name_digest
    return {
        "row_count": rows,
        "columns": [d(k) for k in (*keys, *totals)],
        "headline": {
            "totals": {d(k): v for k, v in totals.items()},
            "float_columns": [d(k) for k in floats],
        },
    }


def _compare(run_dir: Path, capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, dict]:
    args = sr._build_parser().parse_args(
        ["compare", SLUG, "--dir", str(_repo(run_dir)), "--run", run_dir.name, *argv]
    )
    rc = live.dispatch(args)
    return rc, json.loads(capsys.readouterr().out)


def _by_id(out: dict) -> dict[str, dict]:
    return {r["id"]: r for r in out["results"]}


# --------------------------------------------------------------------------- #
# Displayed numbers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "value", "half"),
    [
        ("$12.3K", "12300", "50"),
        ("12.3k", "12300", "50"),
        ("-$1.2M", "-1200000", "50000"),
        ("$-1.2M", "-1200000", "50000"),
        ("(1,234)", "-1234", "0.5"),
        ("1,234,567", "1234567", "0.5"),
        ("45%", "0.45", "0.005"),
        ("34.5%", "0.345", "0.0005"),
        ("−5", "-5", "0.5"),
        ("2.5B", "2500000000", "50000000"),
        (" 7 ", "7", "0.5"),
        (".5", "0.5", "0.05"),
    ],
)
def test_parse_display(text: str, value: str, half: str) -> None:
    d = cmp.parse_display(text)
    assert d is not None
    assert (d.value, d.half_unit) == (Decimal(value), Decimal(half))
    assert d.percent == text.strip().endswith("%")


@pytest.mark.parametrize(
    "text", ["—", "n/a", "", "West", "1998-08-02", "123-45-6789", "(12", "1.2.3", None, 5]
)
def test_parse_display_rejects_what_is_not_a_number(text: object) -> None:
    assert cmp.parse_display(text) is None


def test_display_pattern_is_the_one_review_py_records_with() -> None:
    """review.py only records text this pattern accepts; compare parses the same."""
    import re

    src = _env().get_template("app/review.py.j2").render()
    scope: dict = {"re": re}
    block = src[src.index("_DISPLAY = (") : src.index("# The index's metric key shape")]
    exec(block, scope)
    assert scope["_DISPLAY"] == cmp._DISPLAY_RE.pattern


# --------------------------------------------------------------------------- #
# The four cases the phase-3 plan names
# --------------------------------------------------------------------------- #
def test_rounded_thousands_match_within_the_displayed_rounding(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    _run(run_dir, "01", {"n": 1, "rows": 1, "totals": {"REVENUE": "12345.67"}})
    _capture(run_dir, "overview", "total_revenue", "text", headline={"value": "$12.3K"})
    rc, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:01#1"]
    assert (r["status"], r["rule"]) == ("match", "total")
    assert rc == 0


def test_percent_is_a_ratio(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _run(run_dir, "01", {"n": 1, "rows": 1, "totals": {"REVENUE": "0.4512"}})
    _capture(run_dir, "overview", "total_revenue", "text", headline={"value": "45%"})
    _, out = _compare(run_dir, capsys)
    assert _by_id(out)["compare:01#1"]["status"] == "match"


def test_virtualized_dataframe_rows_come_from_aria_rowcount(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """st.dataframe keeps only visible rows in the DOM; aria-rowcount counts
    them all, plus the header row."""
    _run(run_dir, "02", {"n": 1, "rows": 1000, "totals": {"ORDERS": "5000"}})
    _capture(run_dir, "regions", "orders_by_region", "frame", **_frame(1000, {"ORDERS": "5000"}))
    screen = {"visuals": [{"page": "02", "n": 1, "source": "dataframe", "aria_rowcount": 1001}]}
    (run_dir / "screen.json").write_text(json.dumps(screen), encoding="utf-8")
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert r["status"] == "match"
    assert r["screen"] == {
        "source": "dataframe",
        "rows": 1000,
        "agrees_with_run": True,
        "agrees_with_capture": True,
    }


def test_a_visual_never_captured_is_not_captured(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "12345.67"})
    rc, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert r["status"] == "not_captured" and "opened" in r["reason"]
    assert rc == 0  # nothing contradicts the SQL; the log says what was not seen


# --------------------------------------------------------------------------- #
# Tolerance
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("total", "headline", "status"),
    [
        ("1000", {"value": "1001", "type": "int"}, "mismatch"),  # integers are exact
        ("1000", {"value": "1000", "type": "int"}, "match"),
        ("1000", {"value": "1003.0", "type": "float"}, "match"),  # a float gets 0.5%
        ("12345.67", {"value": "12340.0", "type": "float"}, "match"),
        ("12345.67", {"value": "12000", "type": "int"}, "mismatch"),
    ],
)
def test_scalar_tolerance(
    run_dir: Path, capsys: pytest.CaptureFixture, total: str, headline: dict, status: str
) -> None:
    _run(run_dir, "01", {"n": 1, "rows": 1, "totals": {"REVENUE": total}})
    _capture(run_dir, "overview", "total_revenue", "scalar", headline=headline)
    _, out = _compare(run_dir, capsys)
    assert _by_id(out)["compare:01#1"]["status"] == status


@pytest.mark.parametrize(
    ("shown", "total", "status"),
    [
        ("1,234", "1240", "mismatch"),  # an integer: only the displayed rounding
        ("1.2K", "1234", "match"),
        ("0.45%", "0.45", "mismatch"),  # a ratio without its * 100 is not excused
        ("45%", "45.12", "match"),  # points, since the run value is above 1
        ("12K", "12999", "mismatch"),  # a page that truncates is reported
    ],
)
def test_displayed_tolerance(
    run_dir: Path, capsys: pytest.CaptureFixture, shown: str, total: str, status: str
) -> None:
    _run(run_dir, "01", {"n": 1, "rows": 1, "totals": {"REVENUE": total}})
    _capture(run_dir, "overview", "total_revenue", "text", headline={"value": shown})
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:01#1"]
    assert r["status"] == status
    if shown == "45%":
        assert r["rule"] == "total+percent-as-points"


def test_float_column_is_always_relative(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _run(
        run_dir,
        "02",
        {"n": 1, "rows": 4, "totals": {"SHARE": "100"}, "float_columns": ["SHARE"]},
    )
    _capture(run_dir, "regions", "orders_by_region", "frame", **_frame(4, {"SHARE": "100.4"}))
    _, out = _compare(run_dir, capsys)
    assert _by_id(out)["compare:02#1"]["status"] == "match"


def test_null_total_equals_a_zero(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _run(run_dir, "02", {"n": 1, "rows": 0, "totals": {"ORDERS": None}})
    _capture(run_dir, "regions", "orders_by_region", "frame", **_frame(0, {"ORDERS": "0"}))
    _, out = _compare(run_dir, capsys)
    assert _by_id(out)["compare:02#1"]["status"] == "match"


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #
def test_frame_pairs_renamed_columns_by_name(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _run(run_dir, "02", {"n": 1, "rows": 25, "totals": {"ORDERS": "500", "REVENUE": "9000.50"}})
    frame = _frame(25, {"orders": "500", "Revenue ($)": "9000.5"}, floats=("Revenue ($)",))
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert (r["status"], r["rule"]) == ("match", "name")


def test_frame_row_count_must_be_exact(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _run(run_dir, "02", {"n": 1, "rows": 25, "totals": {"ORDERS": "500"}})
    _capture(run_dir, "regions", "orders_by_region", "frame", **_frame(10, {"ORDERS": "500"}))
    rc, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert r["status"] == "mismatch" and r["diffs"] == ["rows: screen 10 vs run 25"]
    assert rc == 1


def test_frame_falls_back_to_values_and_says_so(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    _run(run_dir, "02", {"n": 1, "rows": 3, "totals": {"ORDERS": "500", "REVENUE": "9000"}})
    frame = _frame(3, {"orders": "500", "Sales in USD": "9000"})
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert (r["status"], r["rule"]) == ("match", "name+values")


def test_frame_value_pairing_that_could_go_two_ways_is_flagged(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    _run(run_dir, "02", {"n": 1, "rows": 3, "totals": {"GROSS": "1000.50"}})
    frame = _frame(3, {"a": "1000.00", "b": "1001.00"})
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert (r["status"], r["rule"]) == ("match", "values-ambiguous")


def test_frame_totals_left_on_both_sides_are_a_mismatch(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    _run(run_dir, "02", {"n": 1, "rows": 3, "totals": {"REVENUE": "9000"}})
    frame = _frame(3, {"Sales": "8100"})
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert r["status"] == "mismatch" and "REVENUE" in r["diffs"][0]


def test_frame_showing_fewer_columns_is_a_note(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    _run(run_dir, "02", {"n": 1, "rows": 3, "totals": {"ORDERS": "5", "REVENUE": "9000"}})
    _capture(run_dir, "regions", "orders_by_region", "frame", **_frame(3, {"ORDERS": "5"}))
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert r["status"] == "match" and r["notes"] == ["not on screen: REVENUE"]


def test_overflowed_run_compares_rows_only(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _run(
        run_dir,
        "02",
        {"n": 1, "rows": 3, "totals": None, "totals_detail": "a column total overflowed"},
    )
    _capture(run_dir, "regions", "orders_by_region", "frame", **_frame(3, {"X": "1"}))
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert (r["status"], r["rule"]) == ("match", "row_count")


def test_frame_names_that_collide_pair_by_value(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    _run(run_dir, "02", {"n": 1, "rows": 2, "totals": {"ORDERS": "7"}})
    d = cmp.name_digest
    frame = {
        "row_count": 2,
        "columns": [d("orders"), d("orders#2")],
        "headline": {
            "totals": {d("orders"): "7", d("orders#2"): "1.0"},
            "float_columns": [d("orders#2")],
            "collided": [d("orders"), d("orders#2")],
        },
    }
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert (r["status"], r["rule"]) == ("match", "values")


def test_run_names_that_collide_are_not_overwritten() -> None:
    totals, collided = cmp._digests({"Revenue": "1", "REVENUE": "2", "ORDERS": "3"})
    assert len(totals) == 3 and len(collided) == 2


# --------------------------------------------------------------------------- #
# Scalars
# --------------------------------------------------------------------------- #
def test_scalar_against_a_row_count(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _run(run_dir, "01", {"n": 1, "rows": 42, "totals": {}})
    _capture(run_dir, "overview", "total_revenue", "text", headline={"value": "42"})
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:01#1"]
    assert (r["status"], r["rule"]) == ("match", "row_count")


@pytest.mark.parametrize(
    ("run", "status", "rule"),
    [
        # The screen equals the multi-row source's single total: the page summed it.
        ({"rows": 5, "totals": {"REVENUE": "12345"}}, "match", "aggregated"),
        # A sum the screen does not equal: the page derived something else.
        ({"rows": 5, "totals": {"REVENUE": "99999"}}, "unsupported", None),
        ({"rows": 1, "totals": {"GROSS": "12345", "NET": "12000"}}, "unsupported", None),
    ],
)
def test_scalar_from_a_derived_result_is_unsupported(
    run_dir: Path, capsys: pytest.CaptureFixture, run: dict, status: str, rule: str | None
) -> None:
    _run(run_dir, "01", {"n": 1, **run})
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "12345"})
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:01#1"]
    assert (r["status"], r["rule"]) == (status, rule)
    if status == "unsupported":
        assert "reviewers judge" in r["reason"]


def test_nan_on_screen_never_matches_a_zero(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _run(run_dir, "01", {"n": 1, "rows": 1, "totals": {"REVENUE": "0"}})
    _capture(
        run_dir, "overview", "total_revenue", "scalar", headline={"value": None, "type": "float"}
    )
    _, out = _compare(run_dir, capsys)
    assert _by_id(out)["compare:01#1"]["status"] == "mismatch"


def test_parse_display_is_fast_on_hostile_input() -> None:
    import time

    start = time.perf_counter()
    for text in (" " * 80 + "1 x", "1" * 5000 + "x", "(" + " " * 200 + "1", "1" * 41):
        assert cmp.parse_display(text) is None
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize(
    "headline",
    [None, [], {"totals": ["1"]}, {"totals": {"a": 1}}, {"totals": {}, "collided": "x"}],
)
def test_a_malformed_capture_is_a_warning_not_a_crash(
    run_dir: Path, capsys: pytest.CaptureFixture, headline: object
) -> None:
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "12345.67"})
    _capture(run_dir, "regions", "orders_by_region", "frame", row_count=4, headline=headline)
    rc, out = _compare(run_dir, capsys)
    assert rc == 0
    assert _by_id(out)["compare:02#1"]["status"] == "not_captured"
    assert any("regions__orders_by_region.json is ignored" in w for w in out["warnings"])


def test_plotly_figure_is_unsupported(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _capture(run_dir, "overview", "revenue_trend", "unsupported", headline={"type": "Figure"})
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:01#2"]
    assert r["status"] == "unsupported" and "Figure" in r["reason"]


def test_failed_run_is_unsupported(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _run(run_dir, "01", {"n": 1, "rows": None, "totals": None})
    path = run_dir / "run-01.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["results"][0]["status"] = "fail"
    path.write_text(json.dumps(data), encoding="utf-8")
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "1"})
    _, out = _compare(run_dir, capsys)
    assert _by_id(out)["compare:01#1"]["status"] == "unsupported"


# --------------------------------------------------------------------------- #
# Aggregated: the page groups or sums the reviewed result before showing it
# --------------------------------------------------------------------------- #
#: One coarse loader, as the /build-app brief has pages share: 72 rows of
#: region x month, sliced and grouped per page in pandas.
COARSE = {
    "n": 1,
    "rows": 72,
    "totals": {"REVENUE": "9000.50", "ORDERS": "500"},
    "float_columns": [],
    "columns": [
        {"name": "REGION", "type": "VARCHAR(16777216)"},
        {"name": "ORDER_MONTH", "type": "DATE"},
        {"name": "YEAR", "type": "NUMBER(4,0)"},
        {"name": "REVENUE", "type": "NUMBER(38,2)"},
        {"name": "ORDERS", "type": "NUMBER(18,0)"},
    ],
}


def _coarse(run_dir: Path, **extra: object) -> None:
    _run(run_dir, "02", {**COARSE, **extra})


def test_grouped_frame_is_aggregated(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    """df.groupby("REGION")[["REVENUE", "ORDERS"]].sum().reset_index(): 6 rows of 72."""
    _coarse(run_dir)
    frame = _frame(6, {"Revenue": "9000.5", "orders": "500"}, keys=("Region",))
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    rc, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert (r["status"], r["rule"]) == ("match", "aggregated")
    assert r["diffs"] == []
    assert rc == 0


def test_a_single_total_row_is_aggregated(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    """df[["REVENUE"]].sum().to_frame().T: one row, no keys."""
    _coarse(run_dir)
    _capture(run_dir, "regions", "orders_by_region", "frame", **_frame(1, {"REVENUE": "9000.5"}))
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert (r["status"], r["rule"]) == ("match", "aggregated")
    assert "not on screen: ORDERS" in r["notes"]


def test_head_slice_stays_a_mismatch(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    """df.head(10): fewer rows, and the totals of only those rows."""
    _coarse(run_dir)
    frame = _frame(10, {"REVENUE": "1250.25", "ORDERS": "70"}, keys=("REGION",))
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    rc, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert r["status"] == "mismatch" and r["rule"] != "aggregated"
    assert "rows: screen 10 vs run 72" in r["diffs"]
    assert rc == 1


@pytest.mark.parametrize("float_column", [False, True])
def test_head_slice_with_a_small_tail_stays_a_mismatch(
    run_dir: Path, capsys: pytest.CaptureFixture, float_column: bool
) -> None:
    """df.head(60) of 72 rows, 0.4% short: inside the display tolerance, but both
    sides of a frame are full-precision sums, so it must not pass as grouped."""
    floats = ["REVENUE"] if float_column else []
    _coarse(run_dir, totals={"REVENUE": "9000.50"}, float_columns=floats)
    frame = _frame(
        60,
        {"REVENUE": "8964.5" if float_column else "8964.50"},
        floats=("REVENUE",) if float_column else (),
        keys=("REGION",),
    )
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    rc, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert r["status"] == "mismatch" and r["rule"] != "aggregated"
    assert rc == 1


def test_grouped_float_column_matches_despite_summation_drift(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """pandas float sums drift around 1e-12; a grouping still matches."""
    _coarse(run_dir, totals={"REVENUE": "9000.50"}, float_columns=["REVENUE"])
    frame = _frame(6, {"REVENUE": "9000.500000000002"}, floats=("REVENUE",), keys=("REGION",))
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert (r["status"], r["rule"]) == ("match", "aggregated")


def test_filtered_slice_stays_a_mismatch(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    """df[df.REGION == "West"]: one total can agree by chance; every one must."""
    _coarse(run_dir)
    frame = _frame(12, {"REVENUE": "1500.25", "ORDERS": "500"}, keys=("REGION",))
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    rc, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:02#1"]
    assert r["status"] == "mismatch" and r["rule"] != "aggregated"
    assert rc == 1


def test_a_derived_key_is_not_aggregated(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    """A key the SQL never returned (a month name built in pandas) proves no grouping."""
    _coarse(run_dir)
    frame = _frame(12, {"REVENUE": "9000.5", "ORDERS": "500"}, keys=("Month name",))
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    _, out = _compare(run_dir, capsys)
    assert _by_id(out)["compare:02#1"]["status"] == "mismatch"


@pytest.mark.parametrize(
    "frame",
    [
        # YEAR shown as a number: its screen total is a sum of distinct years.
        _frame(6, {"YEAR": "12147", "REVENUE": "9000.5", "ORDERS": "500"}),
        # YEAR shown as text: a key, but a numeric source column.
        _frame(6, {"REVENUE": "9000.5", "ORDERS": "500"}, keys=("YEAR",)),
    ],
)
def test_a_numeric_key_is_not_aggregated(
    run_dir: Path, capsys: pytest.CaptureFixture, frame: dict
) -> None:
    _coarse(run_dir, totals={"YEAR": "145728", "REVENUE": "9000.50", "ORDERS": "500"})
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    _, out = _compare(run_dir, capsys)
    assert _by_id(out)["compare:02#1"]["status"] == "mismatch"


def test_all_zero_totals_are_not_aggregated(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _coarse(run_dir, totals={"REVENUE": "0", "ORDERS": "0"})
    frame = _frame(6, {"REVENUE": "0", "ORDERS": "0"}, keys=("REGION",))
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    _, out = _compare(run_dir, capsys)
    assert _by_id(out)["compare:02#1"]["status"] == "mismatch"


def test_scalar_sum_of_a_multi_row_source_is_aggregated(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """st.metric("Revenue", f"${df.REVENUE.sum() / 1000:.1f}K") over 30 daily rows."""
    _run(run_dir, "01", {"n": 1, "rows": 30, "totals": {"REVENUE": "12345.67"}})
    _capture(run_dir, "overview", "total_revenue", "text", headline={"value": "$12.3K"})
    rc, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:01#1"]
    assert (r["status"], r["rule"]) == ("match", "aggregated")
    assert rc == 0


def test_log_cell_names_aggregated(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _coarse(run_dir)
    frame = _frame(6, {"REVENUE": "9000.5", "ORDERS": "500"}, keys=("REGION",))
    _capture(run_dir, "regions", "orders_by_region", "frame", **frame)
    _run(run_dir, "01", {"n": 1, "rows": 1, "totals": {"REVENUE": "5"}})
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "5"})
    _compare(run_dir, capsys)
    cells = live._screen_cells(run_dir)
    assert cells["compare:02#1"] == "match (aggregated)"
    assert cells["compare:01#1"] == "match"


# --------------------------------------------------------------------------- #
# Finding the capture
# --------------------------------------------------------------------------- #
def test_capture_found_by_page_path_whatever_its_stem(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    _run(run_dir, "01", {"n": 1, "rows": 1, "totals": {"REVENUE": "5"}})
    _capture(run_dir, "something_else", "total_revenue", "scalar", headline={"value": "5"})
    data_path = run_dir / "capture" / "something_else__total_revenue.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    data["page_path"] = "pages/overview.py"
    data_path.write_text(json.dumps(data), encoding="utf-8")
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:01#1"]
    assert r["status"] == "match" and r["capture"] == "something_else__total_revenue.json"


def test_key_only_capture_is_used_when_the_key_is_unique(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    _run(run_dir, "01", {"n": 1, "rows": 1, "totals": {"REVENUE": "5"}})
    _capture(run_dir, None, "total_revenue", "scalar", headline={"value": "5"})
    _, out = _compare(run_dir, capsys)
    assert _by_id(out)["compare:01#1"]["status"] == "match"


def test_key_only_capture_is_refused_when_the_key_repeats(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    index_path = _repo(run_dir) / "apps" / SLUG / "sql_review" / "index.yaml"
    import yaml

    index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    for page in index["pages"]:
        for m in page.get("metrics") or []:
            if m["key"] == "orders_by_region":
                m["key"] = "total_revenue"
    index_path.write_text(yaml.safe_dump(index), encoding="utf-8")
    from streamsnow.tools import sql_review_index as sri

    idx = sri.load_index(index_path.parents[1])
    pages = sr._expected_pages(idx)
    capture = cmp.Capture("__total_revenue.json", {"key": "total_revenue", "page": None})
    found, why = cmp._find_capture(pages[0], "total_revenue", [capture], idx)
    assert found is None and "several pages" in why


def test_a_stray_capture_is_a_warning(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "1"})
    _capture(run_dir, "overview", "old_metric", "scalar", headline={"value": "1"})
    _, out = _compare(run_dir, capsys)
    assert any("overview__old_metric.json" in w for w in out["warnings"])


# --------------------------------------------------------------------------- #
# Evidence and the walk
# --------------------------------------------------------------------------- #
def test_compare_ids_are_evidence_and_screen_json_never_is(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "12345.67"})
    screen = {
        "results": [{"id": "screen:01#1"}],
        "visuals": [
            {
                "page": "01",
                "n": 1,
                "id": "made:up",
                "source": "metric",
                "observed": "$12.3K",
                "note": "Wile E. Coyote ordered most",
            },
            {"page": "01", "n": 2, "source": "metric", "observed": "Wile E. Coyote"},
        ],
    }
    (run_dir / "screen.json").write_text(json.dumps(screen), encoding="utf-8")
    _compare(run_dir, capsys)
    ids = live.evidence_ids(run_dir)
    assert {"compare:01#1", "compare:01#2", "compare:02#1"} <= ids
    assert not {i for i in ids if i.startswith(("screen:", "made:"))}
    text = (run_dir / cmp.COMPARE_FILE).read_text(encoding="utf-8")
    assert "Coyote" not in text
    by_id = _by_id(json.loads(text))
    assert by_id["compare:01#1"]["screen"]["observed"] == "$12.3K"
    assert by_id["compare:01#1"]["screen"]["agrees_with_run"] is True


def test_the_walk_never_changes_a_status(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _run(run_dir, "01", {"n": 1, "rows": 1, "totals": {"REVENUE": "12345.67"}})
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "12345.67"})
    screen = {"visuals": [{"page": "01", "n": 1, "source": "metric", "observed": "$99K"}]}
    (run_dir / "screen.json").write_text(json.dumps(screen), encoding="utf-8")
    _, out = _compare(run_dir, capsys)
    r = _by_id(out)["compare:01#1"]
    assert r["status"] == "match"
    assert r["screen"]["agrees_with_run"] is False


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_screen_json_written_by_windows_tools_is_read(
    run_dir: Path, capsys: pytest.CaptureFixture, encoding: str
) -> None:
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "1"})
    screen = {"visuals": [{"page": "02", "n": 1, "source": "table", "rows": 3}]}
    (run_dir / "screen.json").write_text(json.dumps(screen), encoding=encoding)
    _, out = _compare(run_dir, capsys)
    assert _by_id(out)["compare:02#1"]["screen"]["rows"] == 3


def test_unreadable_screen_json_is_a_warning(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "1"})
    (run_dir / "screen.json").write_text("{not json", encoding="utf-8")
    rc, out = _compare(run_dir, capsys)
    assert rc in (0, 1) and any("screen.json" in w for w in out["warnings"])


def test_plotly_on_screen_is_not_read(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _capture(run_dir, "overview", "revenue_trend", "unsupported", headline={"type": "Figure"})
    screen = {"visuals": [{"page": "01", "n": 2, "source": "plotly"}]}
    (run_dir / "screen.json").write_text(json.dumps(screen), encoding="utf-8")
    _, out = _compare(run_dir, capsys)
    block = _by_id(out)["compare:01#2"]["screen"]
    assert block["agrees_with_run"] is None and "Plotly" in block["note"]


# --------------------------------------------------------------------------- #
# The verb
# --------------------------------------------------------------------------- #
def test_nothing_captured_is_a_tool_error(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    args = sr._build_parser().parse_args(
        ["compare", SLUG, "--dir", str(_repo(run_dir)), "--run", run_dir.name]
    )
    with pytest.raises(live.ToolError, match="--review-capture"):
        live.dispatch(args)
    assert sr.main(["compare", SLUG, "--dir", str(_repo(run_dir))]) == 2


def test_compare_defaults_to_the_latest_run_and_writes_compare_json(
    run_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "12345.67"})
    assert sr.main(["compare", SLUG, "--dir", str(_repo(run_dir))]) in (0, 1)
    out = json.loads(capsys.readouterr().out)
    assert out["run_id"] == run_dir.name
    assert out == json.loads((run_dir / cmp.COMPARE_FILE).read_text(encoding="utf-8"))
    assert set(out["counts"]) == set(cmp.STATUSES)
    assert set(out["run_digests"]) == {"01", "02"}
    # Only the original run directory: compare never starts a new run.
    assert len(list(run_dir.parent.iterdir())) == 1


def test_capture_dir_can_be_given(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "12345.67"})
    moved = _repo(run_dir) / "elsewhere"
    (run_dir / "capture").rename(moved)
    _, out = _compare(run_dir, capsys, "--capture", "elsewhere")
    assert out["capture"] == "elsewhere"
    assert _by_id(out)["compare:01#1"]["capture"] == "overview__total_revenue.json"


def test_helper_state(run_dir: Path, capsys: pytest.CaptureFixture) -> None:
    app = _repo(run_dir) / "apps" / SLUG
    assert cmp.helper_state(app) == "missing"
    shipped = _env().get_template("app/review.py.j2").render()
    (app / "review.py").write_bytes(shipped.replace("\n", "\r\n").encode("utf-8"))
    assert cmp.helper_state(app) == "current"
    (app / "review.py").write_text(shipped + "\nX = 1\n", encoding="utf-8")
    assert cmp.helper_state(app) == "modified"
    _capture(run_dir, "overview", "total_revenue", "scalar", headline={"value": "12345.67"})
    _, out = _compare(run_dir, capsys)
    assert out["helper"] == "modified"
    assert any("review.py is modified" in w for w in out["warnings"])
