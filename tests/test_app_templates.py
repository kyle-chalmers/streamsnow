"""The scaffold's shared page modules, About page and branding helpers.

A fresh app ships the pieces the build guides call for (skills/_shared/
explainability.md, visualization-guide.md): one glossary every number reads, shared
layout helpers, an About page that documents the app from its own files, and a
default palette that keeps status hues for status. These tests pin that the
scaffold carries them and that the pure helpers behave.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import yaml

from streamsnow.config import Config
from streamsnow.scaffolder import _DEFAULT_CHART_SEQUENCE, scaffold

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "streamsnow.config.example.yaml"
SLUG = "acme-sales-dashboard"

# Status hues the categorical default must not reuse (visualization-guide.md, Color).
_STATUS_HUES = {"#10B981", "#F59E0B", "#EF4444", "#008300", "#C98500", "#E34948"}


def _app(tmp_path: Path) -> Path:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    scaffold(Config.from_dict(data), tmp_path, SLUG)
    return tmp_path / "apps" / SLUG


def _pure_functions(path: Path, names: set[str]) -> dict:
    """Load only the named top-level functions and constants, without the module's
    imports (branding.py imports plotly and streamlit, which the test env lacks)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def wanted(node: ast.stmt) -> bool:
        if isinstance(node, ast.FunctionDef):
            return node.name in names
        return isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in names for t in node.targets
        )

    defs = [n for n in tree.body if wanted(n)]
    namespace: dict = {}
    exec(compile(ast.Module(body=defs, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def test_fresh_app_ships_the_shared_page_modules_and_about_page(tmp_path):
    a = _app(tmp_path)
    for rel in (
        "pages/_glossary.py",
        "pages/_layout.py",
        "pages/_time_controls.py",
        "pages/_data.py",
        "pages/about.py",
    ):
        assert (a / rel).is_file(), rel
    nav = (a / "streamlit_app.py").read_text(encoding="utf-8")
    assert nav.index('"pages/overview.py"') < nav.index('"pages/about.py"')  # About last
    index = yaml.safe_load((a / "sql_review/index.yaml").read_text(encoding="utf-8"))
    about = [p for p in index["pages"] if p["path"] == "pages/about.py"]
    assert about == [{"path": "pages/about.py", "metrics": []}]


def test_shared_modules_are_imported_package_qualified(tmp_path):
    # Bare `from _glossary import` resolves under `streamlit run` and fails deployed.
    a = _app(tmp_path)
    layout = (a / "pages/_layout.py").read_text(encoding="utf-8")
    about = (a / "pages/about.py").read_text(encoding="utf-8")
    assert "from pages._glossary import glossary_lines" in layout
    assert "from pages._glossary import all_metrics" in about
    assert "from _glossary" not in layout + about


def test_query_headers_reads_sources_from_the_query_files(tmp_path):
    a = _app(tmp_path)
    (a / "queries/daily_sales.sql").write_text(
        "-- Query: daily_sales\n"
        "-- Feeds: Sales trend (net paid by day)\n"
        "-- Schemas: ANALYTICS_DB.ANALYTICS\n"
        "-- Params: :1 start_date, :2 end_date\n"
        "SELECT 1\n"
        "-- Feeds: a comment after the header is not part of it\n",
        encoding="utf-8",
    )
    (a / "queries/no_header.sql").write_text("SELECT 1\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("acme_sql_loader", a / "sql_loader.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    rows = {r["Query"]: r for r in module.query_headers()}
    assert rows["daily_sales"] == {
        "Query": "daily_sales",
        "Feeds": "Sales trend (net paid by day)",
        "Reads": "ANALYTICS_DB.ANALYTICS",
    }
    assert rows["no_header"] == {"Query": "no_header", "Feeds": "", "Reads": ""}
    assert "example_metric" in rows  # the scaffold's own query is listed too


def test_branding_formatters(tmp_path):
    fns = _pure_functions(
        _app(tmp_path) / "branding.py", {"_UNITS", "fmt_number", "fmt_currency", "fmt_pct"}
    )
    fmt_number, fmt_currency, fmt_pct = fns["fmt_number"], fns["fmt_currency"], fns["fmt_pct"]
    assert fmt_number(23) == "23"  # counts stay integers, never "23.0"
    assert fmt_number(950.5) == "950.5"
    assert fmt_number(12_340) == "12.3k"
    assert fmt_number(4_500_000) == "4.5M"
    assert fmt_number(-1_200_000_000) == "-1.2B"
    # The unit is chosen after rounding: no "1000.0k" or "1,000.0".
    assert fmt_number(999_999) == "1.0M"
    assert fmt_number(999.96) == "1.0k"
    assert fmt_number(999.4) == "999.4"
    assert fmt_currency(48_600) == "$48.6k"
    assert fmt_currency(-1_200_000, symbol="€") == "-€1.2M"
    assert fmt_pct(0.345) == "34.5%"


def test_branding_stamp_and_metric_card_api(tmp_path):
    branding = (_app(tmp_path) / "branding.py").read_text(encoding="utf-8")
    assert '_BRANDING_VERSION = "1.1.0"' in branding
    assert "BRAND_STATUS_COLORS" in branding
    card = next(
        n
        for n in ast.parse(branding).body
        if isinstance(n, ast.FunctionDef) and n.name == "branded_metric"
    )
    params = [a.arg for a in card.args.args]
    # Existing callers pass (label, value, delta, border_color) positionally; new ones are last.
    assert params == ["label", "value", "delta", "border_color", "help", "delta_color"]


def test_default_palette_keeps_status_hues_out():
    assert not {c.upper() for c in _DEFAULT_CHART_SEQUENCE} & _STATUS_HUES
    example = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    assert [c.upper() for c in example["brand"]["chart_sequence"]] == [
        c.upper() for c in _DEFAULT_CHART_SEQUENCE
    ]


def test_metric_delta_is_colored_by_meaning(tmp_path):
    names = {"BRAND_STATUS_COLORS", "_DELTA_COLORS", "_NEUTRAL_INK", "_delta_ink"}
    ns = _pure_functions(_app(tmp_path) / "branding.py", names)
    ink, good, bad = (
        ns["_delta_ink"],
        ns["BRAND_STATUS_COLORS"]["good"],
        ns["BRAND_STATUS_COLORS"]["bad"],
    )
    grey = ns["_NEUTRAL_INK"]
    assert ink("+5.3%") == good and ink("-5.3%") == bad and ink("\u22121.2k") == bad
    assert ink("+5.3%", "inverse") == bad and ink("-5.3%", "inverse") == good  # e.g. cost
    assert ink("+5.3%", "off") == grey
    for unchanged in ("0%", "+0.0 pts", "0"):  # no change is neither good nor bad
        assert ink(unchanged) == grey and ink(unchanged, "inverse") == grey
