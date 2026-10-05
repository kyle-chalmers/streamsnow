"""The scaffolded ``review.py`` helper must cost nothing when review mode is off.

``review_value`` wraps the value of every visual on every page, in production,
including Streamlit in Snowflake. Outside review preview mode it has to be a
no-op in every sense: no measurable time, no file written, no module imported
beyond ``os``. These tests render the real template and hold it to that.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
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


def test_review_flag_changes_nothing_yet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Capture lands in a later release; until then the flag must not write files."""
    capture = tmp_path / "capture"
    module, imported = _load(tmp_path, monkeypatch, str(capture))
    assert imported == set()
    data = [1, 2, 3]
    assert module.review_value("orders_by_region", data) is data
    assert not capture.exists()


def test_helper_imports_nothing_snowflake_or_streamlit() -> None:
    """Static belt over the runtime check: the template names no heavy import."""
    src = _env().get_template("app/review.py.j2").render()
    imports = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert imports == ["import os"]
