"""Every text read and write names its encoding.

Python 3.11/3.12 on Windows default to the ANSI code page (cp1252) for
``read_text``, ``write_text``, ``open`` and ``subprocess(text=True)``. The
scaffold templates carry emoji and arrows that cp1252 cannot encode, so
``streamsnow init`` crashed on native Windows, and UTF-8 files read back as
cp1252 broke YAML parsing and strict UTF-8 readers further down the line.
macOS and Linux default to UTF-8, which is why none of that showed up there.

This AST scan fails on any such call without ``encoding=`` on every OS, so the
regression is caught locally instead of only on the Windows CI row. Ruff's
PLW1514 is preview-only and misses calls on untyped receivers
(``out.write_text(...)``), which is exactly where the scaffolder crash was.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNED = ["streamsnow", "hooks", "scripts", "tests"]
_TEXT_IO = {"read_text", "write_text"}
_SUBPROCESS = {"run", "check_output", "Popen"}
#: ``X.open(...)`` receivers that are not files: modules, plus any name ending in
#: "opener" (urllib openers, by convention).
_NON_FILE_OPENERS = {"os", "tarfile", "zipfile", "webbrowser"}


def _mode(call: ast.Call, positional_index: int) -> object:
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    if len(call.args) > positional_index and isinstance(call.args[positional_index], ast.Constant):
        return call.args[positional_index].value
    return None


def _missing_encoding(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        return None
    keywords = {kw.arg for kw in call.keywords}
    if "encoding" in keywords:
        return None
    if name in _TEXT_IO:
        return name
    if name == "open":
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and (func.value.id in _NON_FILE_OPENERS or func.value.id.lower().endswith("opener"))
        ):
            return None
        # open(path, mode) vs Path.open(mode)
        mode = _mode(call, 0 if isinstance(func, ast.Attribute) else 1)
        return None if isinstance(mode, str) and "b" in mode else "open"
    if name in _SUBPROCESS and keywords & {"text", "universal_newlines"}:
        return f"subprocess {name}(text=True)"
    return None


def _python_files() -> list[Path]:
    files = []
    for top in SCANNED:
        for p in sorted((REPO_ROOT / top).rglob("*.py")):
            rel = p.relative_to(REPO_ROOT)
            if "fixtures" in rel.parts or ".venv" in rel.parts:
                continue
            files.append(p)
    return files


def test_every_text_io_call_names_its_encoding() -> None:
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (kind := _missing_encoding(node)):
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno} {kind}")
    assert not offenders, "add encoding='utf-8' (cp1252 is the Windows default):\n" + "\n".join(
        offenders
    )


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("p.read_text()", True),
        ("out.write_text(s)", True),
        ("open(path)", True),
        ("open(path, 'w')", True),
        ("subprocess.run(cmd, text=True)", True),
        ("p.read_text(encoding='utf-8')", False),
        ("open(path, 'rb')", False),
        ("p.open('rb')", False),
        ("p.read_bytes()", False),
        ("subprocess.run(cmd, capture_output=True)", False),
        ("opener.open(req, timeout=5)", False),
        ("_DIRECT_OPENER.open(url, timeout=1)", False),
    ],
)
def test_scanner_flags_exactly_the_risky_calls(source: str, flagged: bool) -> None:
    call = ast.parse(source).body[0].value
    assert (_missing_encoding(call) is not None) is flagged
