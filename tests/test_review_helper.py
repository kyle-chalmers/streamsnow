"""The scaffolded ``review.py`` helper must cost nothing when review mode is off.

``review_value`` wraps the value of every visual on every page, in production,
including Streamlit in Snowflake. Outside review preview mode it has to be a
no-op in every sense: no measurable time, no file written, no module imported
beyond ``os``. These tests render the real template and hold it to that.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from streamsnow.scaffolder import _env

#: Mean cost of one disabled call. A bare Python function call with one branch
#: is tens of nanoseconds; the bound is generous so a loaded CI runner passes.
MAX_MEAN_SECONDS = 1e-6
CALLS = 100_000


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str | None):
    if flag is None:
        monkeypatch.delenv("STREAMSNOW_REVIEW_CAPTURE", raising=False)
    else:
        monkeypatch.setenv("STREAMSNOW_REVIEW_CAPTURE", flag)
    # Python's own bytecode cache is not the helper writing a file.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    path = tmp_path / "review.py"
    path.write_text(_env().get_template("app/review.py.j2").render(), encoding="utf-8")
    name = f"_review_under_test_{len(sys.modules)}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    before = set(sys.modules)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    imported = set(sys.modules) - before - {name}
    return module, imported


def test_disabled_review_value_is_a_cheap_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_property
) -> None:
    monkeypatch.chdir(tmp_path)
    module, imported = _load(tmp_path, monkeypatch, None)
    assert imported == set(), f"review.py imported {sorted(imported)} at import time"
    assert module._CAPTURE is None

    value = object()
    assert module.review_value("total_revenue", value) is value
    files_before = sorted(p.name for p in tmp_path.rglob("*"))
    review_value = module.review_value
    start = time.perf_counter()
    for _ in range(CALLS):
        review_value("total_revenue", value)
    mean = (time.perf_counter() - start) / CALLS
    record_property("review_value_mean_ns", round(mean * 1e9, 1))
    print(f"review_value disabled: {mean * 1e9:.1f} ns per call")
    assert mean < MAX_MEAN_SECONDS, f"{mean * 1e9:.0f} ns per call"
    assert sorted(p.name for p in tmp_path.rglob("*")) == files_before
    assert os.listdir(tmp_path) == ["review.py"]


def test_review_flag_on_imports_nothing_until_a_value_is_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture mode loads its stdlib helpers lazily, on the first capture."""
    capture = tmp_path / "capture"
    module, imported = _load(tmp_path, monkeypatch, str(capture))
    assert imported == set()
    assert str(capture) == module._CAPTURE
    assert not capture.exists()


def test_empty_review_flag_means_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module, _ = _load(tmp_path, monkeypatch, "")
    assert module._CAPTURE is None


def test_helper_imports_nothing_snowflake_or_streamlit() -> None:
    """Static belt over the runtime check: the template names no heavy import."""
    src = _env().get_template("app/review.py.j2").render()
    imports = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert imports == ["import os"]


def test_template_is_plain_python_and_python_39_safe() -> None:
    """Copied into apps as is, and imported by Streamlit in Snowflake's warehouse
    runtime (Python 3.9+): no Jinja, no annotations (``X | None`` fails at def
    time on 3.9, and ``from __future__`` would break the import rule above)."""
    path = Path(__file__).parents[1] / "streamsnow" / "_templates" / "app" / "review.py.j2"
    src = path.read_text(encoding="utf-8")
    assert _env().get_template("app/review.py.j2").render() == src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            assert node.returns is None, node.name
            assert all(a is None or a.annotation is None for a in every), node.name
        assert not isinstance(node, ast.AnnAssign)
        assert not isinstance(node, ast.Match)


# --------------------------------------------------------------------------- #
# Capture (review preview mode)
# --------------------------------------------------------------------------- #
class _Kind:
    def __init__(self, kind: str) -> None:
        self.kind = kind


class _Col:
    """Just enough of a pandas Series for the capture: dtype, sum, dropna, len."""

    def __init__(self, values: list, kind: str) -> None:
        self.values = values
        self.dtype = _Kind(kind)

    def sum(self, min_count: int = 0):
        present = [v for v in self.values if v is not None and v == v]
        if len(present) < min_count:
            return float("nan")
        return sum(present)

    def dropna(self) -> list:
        return [v for v in self.values if v is not None]

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self):
        return iter(self.values)


class _ILoc:
    def __init__(self, frame: DataFrame) -> None:
        self.frame = frame

    def __getitem__(self, key: tuple) -> _Col:
        return self.frame._cols[key[1]]


class DataFrame:
    """A stand-in classified as pandas by its module and class name, as capture
    classifies the real one (pandas itself is not a StreamSnow dependency)."""

    def __init__(self, columns: dict[str, tuple[str, list]]) -> None:
        self.columns = list(columns)
        self._cols = [_Col(values, kind) for kind, values in columns.values()]
        self.iloc = _ILoc(self)

    def __len__(self) -> int:
        return len(self._cols[0]) if self._cols else 0


DataFrame.__module__ = "pandas.core.frame"


class Series:
    def __init__(self, name: str, kind: str, values: list) -> None:
        self.name, self.kind, self.values = name, kind, values

    def to_frame(self) -> DataFrame:
        return DataFrame({self.name: (self.kind, self.values)})


Series.__module__ = "pandas.core.series"


class Styler:
    def __init__(self, data: DataFrame) -> None:
        self.data = data


Styler.__module__ = "pandas.io.formats.style"


class _Snowpark:
    """Any attribute read on a Snowpark DataFrame may run a query: never touch one."""

    def __getattr__(self, name: str):
        raise AssertionError(f"capture read .{name} on a lazy remote frame")


_Snowpark.__module__ = "snowflake.snowpark.dataframe"
_Snowpark.__name__ = "DataFrame"


class Figure:
    pass


Figure.__module__ = "plotly.graph_objs._figure"


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str | None = None):
    """An app folder with review.py, imported fresh with capture on."""
    app = tmp_path / "apps" / "acme-sales"
    app.mkdir(parents=True)
    capture = tmp_path / "cap ture"  # a space, as Windows user folders often have
    module, _ = _load(app, monkeypatch, str(capture) if flag is None else flag)
    return app, capture, module


def _call_from(path: Path, module, key: str, value) -> object:
    """Call review_value from code compiled as ``path``, the way Streamlit runs a page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("RESULT = review_value(KEY, VALUE)\n", encoding="utf-8")
    scope = {"review_value": module.review_value, "KEY": key, "VALUE": value}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), scope)
    return scope["RESULT"]


def _read(capture: Path, name: str) -> dict:
    return json.loads((capture / name).read_text(encoding="utf-8"))


def test_capture_names_the_page_that_called_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, capture, module = _app(tmp_path, monkeypatch)
    frame = DataFrame({"REGION": ("O", ["West", "East"]), "REVENUE": ("f", [1.5, 2.25])})
    assert (
        _call_from(app / "pages" / "01_Sales-Overview.py", module, "orders_by_region", frame)
        is frame
    )
    data = _read(capture, "01_sales_overview__orders_by_region.json")
    assert data["page"] == "01_sales_overview"
    assert data["page_path"] == "pages/01_Sales-Overview.py"
    # Any st.Page path works, not only pages/: the page is the innermost app file.
    _call_from(app / "views" / "sales.py", module, "total_revenue", 5)
    assert _read(capture, "sales__total_revenue.json")["page_path"] == "views/sales.py"
    _call_from(app / "streamlit_app.py", module, "total_orders", 7)
    assert _read(capture, "streamlit_app__total_orders.json")["page"] == "streamlit_app"


def test_capture_stem_follows_the_index_page_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from streamsnow.tools.sql_review_index import Page

    app, capture, module = _app(tmp_path, monkeypatch)
    for path in ("pages/01_Overview.py", "pages/sales-by-region.py", "pages/__.py"):
        _call_from(app / path, module, "total_revenue", 1)
        stem = Page(path=path, metrics=[]).stem
        assert _read(capture, f"{stem}__total_revenue.json")["page"] == stem


def test_capture_outside_the_app_is_key_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, capture, module = _app(tmp_path, monkeypatch)
    _call_from(tmp_path / "elsewhere" / "script.py", module, "total_revenue", 3)
    data = _read(capture, "__total_revenue.json")
    assert data["page"] is None and data["page_path"] is None


def test_capture_of_a_frame_holds_counts_and_hashed_totals_never_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, capture, module = _app(tmp_path, monkeypatch)
    frame = DataFrame(
        {
            "CUSTOMER_NAME": ("O", ["Wile E. Coyote", "Road Runner", None]),
            "ORDERS": ("i", [3, 4, 5]),
            "REVENUE": ("f", [1.5, None, 2.5]),
            "DISCOUNT": ("f", [None, None, None]),
            "AMOUNT": ("O", [Decimal("1.10"), None, Decimal("2.20")]),
            "WAIT": ("m", [1, 2, 3]),
            "ACTIVE": ("b", [True, False, True]),
        }
    )
    _call_from(app / "pages" / "overview.py", module, "orders_by_customer", frame)
    raw = (capture / "overview__orders_by_customer.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert set(data) == {
        "schema",
        "key",
        "page",
        "page_path",
        "kind",
        "row_count",
        "columns",
        "headline",
    }
    assert data["kind"] == "frame" and data["row_count"] == 3
    for leak in ("Coyote", "Runner", "CUSTOMER_NAME", "REVENUE"):
        assert leak not in raw
    digest = module._digest
    assert data["columns"] == [digest(n) for n in frame.columns]
    totals = data["headline"]["totals"]
    assert totals == {
        digest("ORDERS"): "12",
        digest("REVENUE"): "4.0",
        digest("DISCOUNT"): None,  # no values: null, as Snowflake's SUM
        digest("AMOUNT"): "3.30",
    }
    assert data["headline"]["float_columns"] == [digest("REVENUE"), digest("DISCOUNT")]


def test_capture_counts_key_columns_never_their_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Columns without a total are a frame's group keys. compare needs to know
    whether they repeat (a slice) and how many distinct values each holds (a
    grouping keeps every one), so capture records counts, never a value."""
    app, capture, module = _app(tmp_path, monkeypatch)
    d = module._digest
    grouped = DataFrame(
        {
            "REGION": ("O", ["Wile E. Coyote", "Road Runner", "Acme"]),
            "MONTH": ("O", ["Jan", "Jan", None]),
            "REVENUE": ("i", [100, 7, -7]),
        }
    )
    _call_from(app / "pages" / "overview.py", module, "orders_by_region", grouped)
    raw = (capture / "overview__orders_by_region.json").read_text(encoding="utf-8")
    headline = json.loads(raw)["headline"]
    assert headline["key_distinct"] == {d("REGION"): 3, d("MONTH"): 1}  # nulls not counted
    assert headline["key_rows_unique"] is True
    assert "Coyote" not in raw and "Jan" not in raw
    # df.head(2) of REGION=[A, A, B, C]: the key repeats and two regions are gone.
    head = DataFrame({"REGION": ("O", ["A", "A"]), "REVENUE": ("i", [60, 40])})
    _call_from(app / "pages" / "overview.py", module, "orders_by_region", head)
    headline = _read(capture, "overview__orders_by_region.json")["headline"]
    assert headline["key_distinct"] == {d("REGION"): 1}
    assert headline["key_rows_unique"] is False
    # No key columns: nothing to say about keys.
    plain = DataFrame({"REVENUE": ("i", [60, 40])})
    _call_from(app / "pages" / "overview.py", module, "orders_by_region", plain)
    headline = _read(capture, "overview__orders_by_region.json")["headline"]
    assert headline["key_distinct"] == {} and headline["key_rows_unique"] is True


def test_capture_skips_key_counts_it_cannot_take(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unhashable cells (a VARIANT as a dict) leave the key counts out, never the capture."""
    app, capture, module = _app(tmp_path, monkeypatch)
    frame = DataFrame({"DATA": ("O", [{"a": 1}, {"a": 2}]), "REVENUE": ("i", [1, 2])})
    _call_from(app / "pages" / "overview.py", module, "orders_by_region", frame)
    headline = _read(capture, "overview__orders_by_region.json")["headline"]
    assert headline["totals"] == {module._digest("REVENUE"): "3"}
    assert "key_distinct" not in headline and "key_rows_unique" not in headline


def test_capture_hashes_names_like_the_compare_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from streamsnow.tools import sql_review_compare as cmp

    _, _, module = _app(tmp_path, monkeypatch)
    for name in ("REVENUE", "Revenue ($)", "revenue", "NAME#2", "Région"):
        assert module._digest(name) == cmp.name_digest(name)
    assert module._digest("REVENUE") == module._digest("Revenue ($)")


def test_capture_numbers_repeated_column_names_like_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, capture, module = _app(tmp_path, monkeypatch)
    frame = DataFrame({"N": ("i", [1, 2])})
    frame.columns = ["N", "N"]
    frame._cols.append(frame._cols[0].__class__([10, 20], "i"))
    _call_from(app / "pages" / "overview.py", module, "orders_by_region", frame)
    headline = _read(capture, "overview__orders_by_region.json")["headline"]
    d = module._digest
    assert headline["totals"] == {d("N"): "3", d("N#2"): "30"}
    assert headline["collided"] == [d("N"), d("N#2")]


def test_capture_keeps_every_total_when_names_collide_once_normalised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Orders" and "Orders %" normalise alike: both totals stay, numbered, and
    are marked so compare pairs them by value, never by a name both share."""
    app, capture, module = _app(tmp_path, monkeypatch)
    frame = DataFrame({"Orders": ("i", [3, 4]), "Orders %": ("f", [0.25, 0.75])})
    _call_from(app / "pages" / "overview.py", module, "orders_by_region", frame)
    headline = _read(capture, "overview__orders_by_region.json")["headline"]
    d = module._digest
    assert headline["totals"] == {d("orders"): "7", d("orders#2"): "1.0"}
    assert headline["collided"] == [d("orders"), d("orders#2")]


def test_capture_text_check_is_fast_on_hostile_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long run of spaces must not backtrack: the page waits on this call."""
    app, capture, module = _app(tmp_path, monkeypatch)
    start = time.perf_counter()
    for text in (" " * 80 + "1 x", "1" * 5000 + "x", "(" + " " * 200 + "1"):
        _call_from(app / "pages" / "overview.py", module, "total_revenue", text)
    assert time.perf_counter() - start < 1.0
    assert _read(capture, "overview__total_revenue.json")["headline"] == {"value": None}


def test_capture_unwraps_series_and_styler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, capture, module = _app(tmp_path, monkeypatch)
    page = app / "pages" / "overview.py"
    _call_from(page, module, "orders_by_region", Series("ORDERS", "i", [1, 2, 3]))
    assert _read(capture, "overview__orders_by_region.json")["row_count"] == 3
    _call_from(page, module, "orders_by_month", Styler(DataFrame({"X": ("i", [4, 5])})))
    data = _read(capture, "overview__orders_by_month.json")
    assert data["kind"] == "frame" and data["headline"]["totals"] == {module._digest("X"): "9"}


@pytest.mark.parametrize(
    ("value", "headline"),
    [
        (12, {"value": "12", "type": "int"}),
        (12.5, {"value": "12.5", "type": "float"}),
        (float("nan"), {"value": None, "type": "float"}),
        (Decimal("12.50"), {"value": "12.50", "type": "decimal"}),
    ],
)
def test_capture_of_a_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object, headline: dict
) -> None:
    app, capture, module = _app(tmp_path, monkeypatch)
    _call_from(app / "pages" / "overview.py", module, "total_revenue", value)
    data = _read(capture, "overview__total_revenue.json")
    assert data["kind"] == "scalar" and data["headline"] == headline


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("$12.3K", "$12.3K"),
        (" 45% ", "45%"),
        ("(1,234)", "(1,234)"),
        ("-$1.2M", "-$1.2M"),
        ("123-45-6789", None),
        ("555-123-4567", None),
        ("4111 1111 1111 1111", None),
        ("4111111111111111", None),
        ("Wile E. Coyote", None),
        ("1998-08-02", None),
        ("", None),
    ],
)
def test_capture_keeps_text_only_when_it_is_a_displayed_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, kept: str | None
) -> None:
    app, capture, module = _app(tmp_path, monkeypatch)
    _call_from(app / "pages" / "overview.py", module, "total_revenue", text)
    data = _read(capture, "overview__total_revenue.json")
    assert data["kind"] == "text" and data["headline"] == {"value": kept}


@pytest.mark.parametrize(
    "value",
    [_Snowpark(), Figure(), [1, 2], None, True],
    ids=["snowpark", "plotly", "list", "none", "bool"],
)
def test_capture_of_anything_else_is_unsupported_and_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    app, capture, module = _app(tmp_path, monkeypatch)
    assert _call_from(app / "pages" / "overview.py", module, "revenue_chart", value) is value
    data = _read(capture, "overview__revenue_chart.json")
    assert data["kind"] == "unsupported"
    assert data["headline"] == {"type": type(value).__name__}


def test_capture_failure_never_breaks_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken(DataFrame):
        def __len__(self) -> int:
            raise RuntimeError("boom with Wile E. Coyote in it")

    app, capture, module = _app(tmp_path, monkeypatch)
    value = Broken({"X": ("i", [1])})
    with caplog.at_level("WARNING", logger="streamsnow.review"):
        assert _call_from(app / "pages" / "overview.py", module, "total_revenue", value) is value
    assert len(caplog.records) == 1
    assert "RuntimeError" in caplog.text and "Coyote" not in caplog.text
    assert not capture.exists()


def test_capture_skips_a_key_that_is_not_a_metric_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app, capture, module = _app(tmp_path, monkeypatch)
    with caplog.at_level("WARNING", logger="streamsnow.review"):
        for key in ("../escape", "a__b", "Revenue", 7):
            assert _call_from(app / "pages" / "overview.py", module, key, 1) == 1
    assert len(caplog.records) == 4
    assert not capture.exists()


def test_capture_into_an_unwritable_place_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    app, _, module = _app(tmp_path, monkeypatch, str(blocker / "capture"))
    assert _call_from(app / "pages" / "overview.py", module, "total_revenue", 1) == 1


def test_capture_overwrites_on_rerun_and_leaves_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, capture, module = _app(tmp_path, monkeypatch)
    page = app / "pages" / "overview.py"
    _call_from(page, module, "total_revenue", 1)
    _call_from(page, module, "total_revenue", 2)
    assert sorted(p.name for p in capture.iterdir()) == ["overview__total_revenue.json"]
    assert _read(capture, "overview__total_revenue.json")["headline"]["value"] == "2"
    assert "\r\n" not in (capture / "overview__total_revenue.json").read_text(encoding="utf-8")


def test_capture_with_real_pandas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stand-ins above mirror pandas; this holds them to the real thing
    (run locally with `uv run --with pandas pytest tests/test_review_helper.py`)."""
    pd = pytest.importorskip("pandas")
    app, capture, module = _app(tmp_path, monkeypatch)
    frame = pd.DataFrame(
        {
            "region": ["West", "East", "North"],
            "orders": [3, 4, 5],
            "revenue": [1.5, None, 2.5],
            "empty": [None, None, None],
            "data": [1, 1, 1],
            "flag": [True, False, True],
            "wait": pd.to_timedelta([1, 2, 3], unit="D"),
        }
    ).astype({"empty": "float64"})
    page = app / "pages" / "overview.py"
    _call_from(page, module, "orders_by_region", frame)
    d = module._digest
    data = _read(capture, "overview__orders_by_region.json")
    assert data["row_count"] == 3
    assert data["headline"]["totals"] == {
        d("orders"): "12",
        d("revenue"): "4.0",
        d("empty"): None,
        d("data"): "3",
    }
    assert data["headline"]["key_distinct"] == {d("region"): 3, d("flag"): 2, d("wait"): 3}
    assert data["headline"]["key_rows_unique"] is True
    head = frame[["region", "orders"]].assign(region=["West", "West", "East"]).head(2)
    _call_from(page, module, "orders_by_day", head)
    headline = _read(capture, "overview__orders_by_day.json")["headline"]
    assert (headline["key_distinct"], headline["key_rows_unique"]) == ({d("region"): 1}, False)
    _call_from(page, module, "orders_by_month", frame["orders"])
    assert _read(capture, "overview__orders_by_month.json")["headline"]["totals"] == {
        d("orders"): "12"
    }
    _call_from(page, module, "orders_by_week", frame.style)
    assert _read(capture, "overview__orders_by_week.json")["row_count"] == 3
    _call_from(page, module, "total_revenue", frame["orders"].sum())
    assert _read(capture, "overview__total_revenue.json")["headline"] == {
        "value": "12",
        "type": "int",
    }
    nullable = pd.DataFrame({"n": pd.array([None, None], dtype="Int64")})
    _call_from(page, module, "total_orders", nullable)
    assert _read(capture, "overview__total_orders.json")["headline"]["totals"] == {d("n"): None}


def test_capture_flag_is_only_set_by_review_preview() -> None:
    """Streamlit in Snowflake never sets STREAMSNOW_REVIEW_CAPTURE: an app there
    has no way to set environment variables, and nothing StreamSnow deploys
    names it. The only writer is `streamsnow preview start --review-capture`
    (a local process), the only reader the scaffolded review.py."""
    root = Path(__file__).parents[1] / "streamsnow"
    flag = "STREAMSNOW_REVIEW_CAPTURE"
    named = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".j2", ".yml", ".yaml", ".toml", ".sql", ".md"}
        and flag in p.read_text(encoding="utf-8")
    )
    assert named == ["_templates/app/review.py.j2", "tools/preview_app.py"]
    src = (root / "tools" / "preview_app.py").read_text(encoding="utf-8")
    assert src.count(f'"{flag}"') == 1  # one definition, used to set and to strip
