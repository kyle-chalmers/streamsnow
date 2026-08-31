"""Tests for the app-navigation enumerator (``streamsnow.tools.app_nav``)."""

from __future__ import annotations

import json
from pathlib import Path

from streamsnow.tools import app_nav

DICT_NAV = """\
import streamlit as st

st.set_page_config(page_title="Acme Sales", layout="wide")

home = st.Page("home.py", title="Home", default=True)
revenue = st.Page(
    "pages/revenue_overview.py",
    title="Revenue Overview",
)

nav = st.navigation(
    {
        "Overview": [home, revenue],
        "Marketing": [
            st.Page("pages/campaign_roi.py", title="Campaign ROI"),
        ],
    }
)
nav.run()
"""

LIST_NAV = """\
import streamlit as st

orders = st.Page("pages/orders.py", title="Orders")
nav = st.navigation([st.Page("home.py", title="Home", default=True), orders])
nav.run()
"""

SINGLE_PAGE = """\
import streamlit as st

st.title("Acme Inventory Snapshot")
st.dataframe({"region": ["west", "east"], "revenue": [10, 20]})
"""

DYNAMIC_NAV = """\
import streamlit as st

pages = [st.Page(p) for p in page_paths]
st.navigation({"All": pages}).run()
"""


def _app(tmp_path: Path, entry_text: str, slug: str = "acme-sales-dashboard") -> Path:
    app_dir = tmp_path / "apps" / slug
    app_dir.mkdir(parents=True)
    (app_dir / "streamlit_app.py").write_text(entry_text)
    return app_dir


def test_dict_navigation_groups_titles_default(tmp_path):
    app_dir = _app(tmp_path, DICT_NAV)
    entries = app_nav.extract_nav(app_dir)
    assert [(e["title"], e["group"], e["default"]) for e in entries] == [
        ("Home", "Overview", True),
        ("Revenue Overview", "Overview", False),
        ("Campaign ROI", "Marketing", False),
    ]
    # Inline st.Page entries have no variable name; assigned ones keep theirs.
    assert entries[0]["var"] == "home"
    assert entries[2]["var"] is None
    assert all(e["source"] == "navigation" for e in entries)


def test_list_navigation_flat_no_groups(tmp_path):
    app_dir = _app(tmp_path, LIST_NAV)
    entries = app_nav.extract_nav(app_dir)
    assert [(e["title"], e["path"], e["default"]) for e in entries] == [
        ("Home", "home.py", True),
        ("Orders", "pages/orders.py", False),
    ]
    assert all(e["group"] is None for e in entries)


def test_single_page_app_reports_entrypoint(tmp_path):
    app_dir = _app(tmp_path, SINGLE_PAGE)
    entries = app_nav.extract_nav(app_dir)
    assert entries == [
        {
            "title": "acme-sales-dashboard",
            "path": "streamlit_app.py",
            "group": None,
            "default": True,
            "var": None,
            "source": "entrypoint",
        }
    ]


def test_legacy_pages_dir_sorted_with_derived_titles(tmp_path):
    app_dir = _app(tmp_path, SINGLE_PAGE)
    pages = app_dir / "pages"
    pages.mkdir()
    (pages / "02_sales_by_region.py").write_text("import streamlit as st\n")
    (pages / "01_overview.py").write_text("import streamlit as st\n")
    (pages / "_shared_header.py").write_text("# helper, not a page\n")
    entries = app_nav.extract_nav(app_dir)
    assert [(e["title"], e["path"]) for e in entries] == [
        ("acme-sales-dashboard", "streamlit_app.py"),
        ("overview", "pages/01_overview.py"),
        ("sales by region", "pages/02_sales_by_region.py"),
    ]
    assert all(e["source"] == "pages-dir" for e in entries)
    assert entries[0]["default"] and not entries[1]["default"]


def test_dynamic_navigation_warns_partial(tmp_path, capsys):
    app_dir = _app(tmp_path, DYNAMIC_NAV)
    rc = app_nav.main([str(app_dir)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "partial enumeration" in captured.err


def test_missing_entrypoint_is_tool_error(tmp_path, capsys):
    app_dir = tmp_path / "apps" / "acme-empty"
    app_dir.mkdir(parents=True)
    assert app_nav.main([str(app_dir)]) == 2
    assert "not found" in capsys.readouterr().err


def test_unparseable_entrypoint_is_tool_error(tmp_path, capsys):
    app_dir = _app(tmp_path, "def broken(:\n")
    assert app_nav.main([str(app_dir)]) == 2
    assert "not valid Python" in capsys.readouterr().err


def test_jsonl_default_and_json_array_flag(tmp_path, capsys):
    app_dir = _app(tmp_path, LIST_NAV)

    assert app_nav.main([str(app_dir)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["title"] == "Home"

    assert app_nav.main([str(app_dir), "--json-array"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list) and len(payload) == 2
