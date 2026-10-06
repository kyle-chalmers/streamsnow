"""``sql-review compare``: hold what the screen shows to the reviewed SQL.

Why this exists
---------------
``run`` proves each section returns sane aggregates. It cannot prove the page
shows them: an app can slice a frame to its first ten rows, rename and drop a
column, divide by a thousand and forget the ``K``, or render with default
filters that no longer match the index's sample tokens. Every one of those
passes ``run`` and still puts a wrong number in front of a person.

In review preview mode (``streamsnow preview start <slug> --review-capture
DIR``) the app's ``review_value`` calls write what each visual received: a
row count, hashed column names and column totals, or the scalar or formatted
number a metric shows. Never a row. ``compare`` reads those captures and holds
each one to the ``run`` result for its metric, deterministically, and writes
``compare.json`` with one ``compare:NN#n`` id per metric, which reviewers may
cite as evidence.

Matching
--------
- **Frames:** the row count must equal ``rows``. Totals pair by column name
  (normalised and hashed on both sides, so ``REVENUE``, ``Revenue ($)`` and
  ``revenue`` pair), then whatever is left pairs one-to-one by value. Totals
  left over on both sides are a mismatch; on one side only, a note (the page
  may show fewer or extra columns).
- **Aggregated frames:** the /build-app brief has pages share one coarse
  loader and group it per page in pandas, so a correct frame can show fewer
  rows than ``rows``. That is a ``match`` with the rule ``aggregated`` only
  when the screen has fewer rows (at least one), every screen total pairs by
  name with its run total exactly (``FLOAT`` within 1e-9, summation drift
  only, never the display tolerance), the screen's group keys are all
  non-numeric columns of the SQL result (or the screen is one total row), and
  at least one total is non-zero. A head or filtered slice changes the totals,
  and a derived or numeric key cannot tell a grouping from a slice, so those
  stay a ``mismatch``.
- **Scalars and displayed numbers:** against the single numeric total of a
  one-row result, or against the row count when the result has no numeric
  column. A multi-row result with exactly one numeric column whose non-zero
  sum the number equals is a ``match`` with the rule ``aggregated`` (the page
  shows ``df.REVENUE.sum()``). Anything else is ``unsupported``: the app
  derives the value, and the reviewers judge it.
- **Tolerance:** the looser of 0.5% of the run value or the displayed rounding
  (``$12.3K`` is anything from 12,250 to 12,350). Integers match exactly, give
  or take only the displayed rounding. ``FLOAT`` columns always get the relative
  tolerance. A page that truncates instead of rounding shows up as a mismatch.
- **Percent:** ``45%`` is read as 0.45. It is read as 45 only when the run value
  is above 1, so a ratio shown without its ``* 100`` is never excused.

The browser walk
----------------
``screen.json``, written by the agent that walks the pages, is a cross-check.
``compare`` parses it (counts and displayed numbers only) and records whether
it agrees, but it never changes a status, never reaches the committed log, and
never mints an id: an agent's reading of a page is not evidence.

Exit codes: 0 = no mismatch, 1 = at least one mismatch, 2 = tool error (no
``run`` results, or nothing captured).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import sql_review as sr
from . import sql_review_index as sri
from . import sql_review_live as live

COMPARE_FILE = "compare.json"
SCREEN_FILE = "screen.json"
CAPTURE_DIR = "capture"
#: Streamlit's virtualized dataframe counts its header row in ``aria-rowcount``
#: (observed on Streamlit 1.59: a five-row frame reads 6).
ARIA_HEADER_ROWS = 1
RELATIVE_TOLERANCE = Decimal("0.005")
#: An aggregated frame's FLOAT totals: summation-order drift only, never rounding.
AGGREGATED_FLOAT_TOLERANCE = Decimal("1e-9")
STATUSES = ("match", "mismatch", "not_captured", "unsupported")
SCREEN_SOURCES = frozenset({"metric", "table", "dataframe", "vega", "plotly"})

#: A number as a page shows it. The scaffolded review.py holds the same pattern
#: (tests/test_sql_review_compare.py keeps the two equal).
_DISPLAY_RE = re.compile(
    r"^(?=.*\d)\(?[+\-\u2212]?[$\u20ac\u00a3\u00a5]?[+\-\u2212]?"
    r"(?:\d{1,3}(?:,\d{3})+|\d{1,15})?(?:\.\d+)?\s?[kKmMbBtT%]?\)?$"
)
_DISPLAY_MAX = 40
_UNITS = {"k": 3, "m": 6, "b": 9, "t": 12}


# --------------------------------------------------------------------------- #
# Numbers as displayed
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Display:
    """A displayed number: its value and half its last shown digit (the rounding)."""

    value: Decimal
    half_unit: Decimal
    percent: bool


def parse_display(text: object) -> Display | None:
    """``"$12.3K"`` -> 12300 ± 50; ``"45%"`` -> 0.45 ± 0.005; not a number -> None."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if len(s) > _DISPLAY_MAX or not _DISPLAY_RE.match(s):
        return None
    negative = (s.startswith("(") and s.endswith(")")) or "-" in s or "−" in s
    if s.startswith("(") != s.endswith(")"):
        return None
    unit = s.rstrip(")").rstrip()[-1:]
    body = re.sub(r"[^0-9.]", "", s.rstrip(")").rstrip().rstrip("kKmMbBtT%"))
    try:
        value = Decimal(body)
    except InvalidOperation:
        return None
    decimals = len(body.split(".", 1)[1]) if "." in body else 0
    half = Decimal(5).scaleb(-(decimals + 1))
    percent = unit == "%"
    if percent:
        value, half = value.scaleb(-2), half.scaleb(-2)
    elif unit.lower() in _UNITS:
        value, half = value.scaleb(_UNITS[unit.lower()]), half.scaleb(_UNITS[unit.lower()])
    return Display(-value if negative else value, half, percent)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def _integral(value: Decimal | None) -> bool:
    return value is not None and value == value.to_integral_value()


def _close(
    expected: Decimal | None,
    observed: Decimal | None,
    *,
    display: Display | None = None,
    integer: bool = False,
) -> tuple[bool, Decimal]:
    """Within tolerance? A null equals a zero (an empty SUM vs a page's 0)."""
    half = display.half_unit if display else Decimal(0)
    if expected is None:  # SUM over no values: the page may well show 0
        return (observed is None or observed == 0), half
    if observed is None:  # the page got nothing (or NaN) where the SQL has a number
        return False, half
    tol = half if integer else max(abs(expected) * RELATIVE_TOLERANCE, half)
    return abs(observed - expected) <= tol, tol


def _normal(name: str) -> str:
    return re.sub(r"[^a-z0-9#]", "", name.casefold())


def name_digest(name: str) -> str:
    """Hashed column name, the same rule as the scaffolded review.py: a pivoted
    frame's column names can be data values, so captures never hold them."""
    return hashlib.sha256(_normal(name).encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
@dataclass
class Capture:
    file: str
    data: dict


def load_captures(capture_dir: Path) -> tuple[list[Capture], list[str]]:
    """Every capture file; unreadable ones become warnings, never a crash."""
    out: list[Capture] = []
    warnings: list[str] = []
    if not capture_dir.is_dir():
        return out, warnings
    for path in sorted(capture_dir.glob("*__*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            warnings.append(f"capture {path.name} is unreadable: {exc}")
            continue
        problem = _capture_problem(data)
        if problem:
            warnings.append(f"capture {path.name} is ignored: {problem}")
            continue
        out.append(Capture(path.name, data))
    return out, warnings


def _capture_problem(data: object) -> str | None:
    """Why a capture file cannot be used, or None. A modified review.py can
    write anything; a bad file is a warning, never a crash or a mismatch."""
    if not isinstance(data, dict) or data.get("schema") != 1:
        return "not a review_value capture (schema 1)"
    if not isinstance(data.get("key"), str):
        return "no metric key"
    headline = data.get("headline")
    if not isinstance(headline, dict):
        return "no headline"
    if data.get("kind") == "frame":
        totals = headline.get("totals")
        if type(data.get("row_count")) is not int or not isinstance(totals, dict):
            return "a frame needs a row count and totals"
        if not all(isinstance(v, str | None) for v in totals.values()):
            return "frame totals must be text or null"
        for k in ("float_columns", "collided"):
            if not isinstance(headline.get(k, []), list):
                return f"{k} must be a list"
    return None


def _read_text_any(path: Path) -> str:
    """UTF-8 with or without a BOM, or UTF-16 (PowerShell's default redirect)."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def load_screen(path: Path, index: sri.Index) -> tuple[dict[tuple[str, int], dict], list[str]]:
    """The walk's readings by ``(page, n)``: counts and displayed numbers only.

    Anything else the agent wrote (notes, ids, labels) is dropped here, so it
    can neither leak into ``compare.json`` nor pose as evidence.
    """
    try:
        raw = json.loads(_read_text_any(path))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {}, [f"{path.name} is unreadable, so the walk was not cross-checked: {exc}"]
    visuals = raw.get("visuals") if isinstance(raw, dict) else None
    if not isinstance(visuals, list):
        return {}, [f"{path.name} has no visuals list; the walk was not cross-checked"]
    known = {(f"{p.number:02d}", m.number) for p in sr._expected_pages(index) for m in p.metrics}
    out: dict[tuple[str, int], dict] = {}
    warnings: list[str] = []
    for v in visuals:
        if not isinstance(v, dict):
            continue
        page, n = v.get("page"), v.get("n")
        if not isinstance(page, str) or type(n) is not int or (page, n) not in known:
            warnings.append(f"{path.name}: a visual names no metric in index.yaml ({page}#{n})")
            continue
        entry: dict = {"source": v.get("source") if v.get("source") in SCREEN_SOURCES else None}
        if parse_display(v.get("observed")) is not None:
            entry["observed"] = v["observed"].strip()
        for k in ("rows", "aria_rowcount", "marks"):
            if type(v.get(k)) is int and v[k] >= 0:
                entry[k] = v[k]
        out[(page, n)] = entry
    return out, warnings


def helper_state(app: Path) -> str:
    """Is the app's review.py the scaffolded one? A modified helper can capture
    anything, so the verifier weighs its ``compare:`` ids accordingly."""
    from ..scaffolder import _env  # noqa: PLC0415

    path = app / "review.py"
    if not path.is_file():
        return "missing"
    shipped = _env().get_template("app/review.py.j2").render()
    current = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return "current" if current == shipped else "modified"


def _find_capture(
    page: sri.Page, key: str, captures: list[Capture], index: sri.Index
) -> tuple[Capture | None, str | None]:
    """By the page's path, then its stem (when unique), then a key-only file
    (when the key is unique across the app): keys repeat across pages."""
    pages = sr._expected_pages(index)
    want = os.path.normcase(page.path.replace("\\", "/"))
    for c in captures:
        path = c.data.get("page_path")
        if c.data.get("key") == key and isinstance(path, str) and os.path.normcase(path) == want:
            return c, None
    if sum(1 for p in pages if p.stem == page.stem) == 1:
        for c in captures:
            if c.data.get("key") == key and c.data.get("page") == page.stem:
                return c, None
    key_only = [c for c in captures if c.data.get("key") == key and c.data.get("page") is None]
    if key_only:
        if sum(1 for p in pages for m in p.metrics if m.key == key) == 1:
            return key_only[0], None
        return None, f"only a capture without its page exists, and {key} is on several pages"
    return None, "nothing was captured for this visual (was its page opened in the preview?)"


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _scalar_expected(run: dict) -> tuple[Decimal | None, bool, str, str | None]:
    """(expected, integer, rule, unsupported-reason) for a single shown number."""
    totals = run.get("totals")
    if totals is None:
        return None, False, "", run.get("totals_detail") or "run has no totals"
    if not totals:
        return Decimal(int(run.get("rows") or 0)), True, "row_count", None
    if len(totals) == 1 and run.get("rows") == 1:
        name, value = next(iter(totals.items()))
        expected = _decimal(value)
        floats = set(run.get("float_columns") or [])
        return expected, _integral(expected) and name not in floats, "total", None
    return (
        None,
        False,
        "",
        "the page derives this number from a multi-row or multi-column result; "
        "the reviewers judge it",
    )


def _match_number(
    expected: Decimal | None,
    integer: bool,
    observed: Decimal | None,
    display: Display | None,
) -> tuple[bool, str, Decimal]:
    ok, tol = _close(expected, observed, display=display, integer=integer)
    if ok or display is None or not display.percent:
        return ok, "", tol
    # "45%" for a run value of 45: points, not a ratio. Only above 1, so a
    # ratio shown without its * 100 ("0.45%" for 0.45) is never excused.
    if expected is not None and abs(expected) > 1:
        points = Display(display.value.scaleb(2), display.half_unit.scaleb(2), True)
        ok, tol = _close(expected, points.value, display=points, integer=integer)
        if ok:
            return True, "percent-as-points", tol
    return False, "", tol


def _compare_number(run: dict, cap: dict) -> dict:
    headline = cap.get("headline") or {}
    display: Display | None = None
    if cap.get("kind") == "text":
        display = parse_display(headline.get("value"))
        if display is None:
            return _result("unsupported", reason="the visual shows text that is not a number")
        observed: Decimal | None = display.value
        observed_float = False
    else:
        observed = _decimal(headline.get("value"))
        observed_float = headline.get("type") == "float"
        if observed is None and headline.get("value") is not None:
            return _result("unsupported", reason="the visual received a value that is not finite")
    expected, integer, rule, why = _scalar_expected(run)
    if why:
        summed = _aggregated_number(run, observed, observed_float, display)
        return summed or _result("unsupported", reason=why)
    integer = integer and not observed_float and (display is not None or _integral(observed))
    ok, extra, tol = _match_number(expected, integer, observed, display)
    rule = "+".join(x for x in (rule, extra) if x)
    if ok:
        return _result("match", rule=rule)
    shown = headline.get("value")
    return _result(
        "mismatch",
        rule=rule,
        diffs=[
            f"screen {shown} vs run {expected if expected is not None else 'null'} "
            f"(tolerance {tol})"
        ],
    )


def _aggregated_number(
    run: dict, observed: Decimal | None, observed_float: bool, display: Display | None
) -> dict | None:
    """A metric that sums a multi-row result's only numeric column, or None.

    A page that shares one coarse loader shows ``df.REVENUE.sum()`` from it. A
    sum that does not agree stays ``unsupported`` (the page may compute
    something else entirely), so nothing that passed before starts failing.
    """
    totals = run.get("totals")
    rows = run.get("rows")
    if type(rows) is not int or rows < 2 or not isinstance(totals, dict) or len(totals) != 1:
        return None
    name, value = next(iter(totals.items()))
    expected = _decimal(value)
    if not expected:  # a null or zero sum agrees with too much to prove a grouping
        return None
    floats = set(run.get("float_columns") or [])
    integer = (
        _integral(expected)
        and name not in floats
        and not observed_float
        and (display is not None or _integral(observed))
    )
    ok, extra, _ = _match_number(expected, integer, observed, display)
    if not ok:
        return None
    return _result(
        "match",
        rule="+".join(x for x in ("aggregated", extra) if x),
        notes=[f"the sum of {name} over {rows} rows"],
    )


def _aggregated_frame(run: dict, cap: dict) -> dict | None:
    """A frame that groups the reviewed result before showing it, or None.

    The /build-app brief has pages share one coarse loader and slice it per
    page in pandas, so a correct visual can show 6 rows of a 72-row query.
    Compare exists to catch a frame cut to its first rows or filtered, so this
    is deliberately narrow; every condition must hold:

    - the screen has fewer rows than the run, and at least one;
    - every screen total pairs by name with a run total, exactly (``FLOAT``
      within 1e-9; see ``_same_sum``);
    - the screen's columns without a total (its group keys) are all
      non-numeric columns of the SQL result, or the screen is one total row;
    - at least one total is non-zero.

    A head or filtered slice changes the totals. A derived key (a month name
    built in pandas) or a numeric key (a year) is not trusted, since its
    totals cannot tell a grouping from a slice. Anything else falls through
    to the ordinary frame rules.
    """
    rows, shown_rows = run.get("rows"), cap.get("row_count")
    if type(rows) is not int or type(shown_rows) is not int or not 1 <= shown_rows < rows:
        return None
    headline = cap.get("headline") or {}
    cap_totals: dict = dict(headline.get("totals") or {})
    if not run.get("totals") or not cap_totals:
        return None
    run_totals, run_collided = _digests(run["totals"])
    unpairable = set(headline.get("collided") or []) | run_collided
    run_floats = {name_digest(n) for n in run.get("float_columns") or []}
    cap_floats = set(headline.get("float_columns") or [])
    non_zero = False
    for digest, shown in cap_totals.items():
        if digest not in run_totals or digest in unpairable:
            return None
        expected, observed = _decimal(run_totals[digest][1]), _decimal(shown)
        is_float = digest in run_floats or digest in cap_floats
        if not _same_sum(expected, observed, is_float=is_float):
            return None
        non_zero = non_zero or bool(observed)
    if not non_zero:
        return None
    columns = cap.get("columns")
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        return None
    keys = [c for c in columns if c not in cap_totals]
    if shown_rows > 1 and (not keys or not set(keys) <= _text_columns(run)):
        return None
    notes = [f"grouped: {shown_rows} screen rows from {rows} run rows"]
    hidden = [name for d, (name, _) in run_totals.items() if d not in cap_totals]
    if hidden:
        notes.append("not on screen: " + ", ".join(hidden))
    return _result("match", rule="aggregated", notes=notes)


def _same_sum(expected: Decimal | None, observed: Decimal | None, *, is_float: bool) -> bool:
    """Two full-precision sums of the same rows: exact, or within float drift.

    ``_close`` allows 0.5% because a displayed number is rounded. A frame total
    is not: ``NUMBER`` reaches pandas as ``Decimal`` and sums exactly, and a
    float sum drifts only around 1e-12. The display tolerance would let a head
    slice whose dropped rows hold less than 0.5% of the total pass as grouped.
    """
    if expected is None:  # SUM over no values: the page may well show 0
        return observed is None or observed == 0
    if observed is None:
        return False
    if not is_float:
        return observed == expected
    return abs(observed - expected) <= abs(expected) * AGGREGATED_FLOAT_TOLERANCE


def _text_columns(run: dict) -> set[str]:
    """Hashed names of the run's non-numeric columns: the only trusted group keys.

    A name that is numeric anywhere in the result is left out, so a key that
    collides with a numeric column once normalised is never trusted.
    """
    numeric: set[str] = set()
    other: set[str] = set()
    for c in run.get("columns") or []:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            continue
        kind = c.get("type")
        if not isinstance(kind, str) or not kind:
            continue
        column = live.Column(0, c["name"], kind)
        (numeric if column.numeric else other).add(name_digest(c["name"]))
    return other - numeric


def _compare_frame(run: dict, cap: dict) -> dict:
    diffs: list[str] = []
    notes: list[str] = []
    if cap.get("row_count") != run.get("rows") and run.get("totals") is not None:
        grouped = _aggregated_frame(run, cap)
        if grouped:
            return grouped
    if cap.get("row_count") != run.get("rows"):
        diffs.append(f"rows: screen {cap.get('row_count')} vs run {run.get('rows')}")
    if run.get("totals") is None:
        reason = run.get("totals_detail") or "run has no totals"
        return _result(
            "mismatch" if diffs else "match", rule="row_count", reason=reason, diffs=diffs
        )
    headline = cap.get("headline") or {}
    cap_totals: dict = dict(headline.get("totals") or {})
    cap_floats = set(headline.get("float_columns") or [])
    run_floats = {name_digest(n) for n in run.get("float_columns") or []}
    run_totals, run_collided = _digests(run.get("totals") or {})
    # Names that collide once normalised cannot pair by name on either side.
    unpairable = set(headline.get("collided") or []) | run_collided

    def check(digest: str, cap_digest: str) -> tuple[bool, Decimal, Decimal | None, Decimal | None]:
        expected = _decimal(run_totals[digest][1])
        observed = _decimal(cap_totals[cap_digest])
        integer = (
            _integral(expected)
            and _integral(observed)
            and digest not in run_floats
            and cap_digest not in cap_floats
        )
        ok, tol = _close(expected, observed, integer=integer)
        return ok, tol, expected, observed

    by_name = [d for d in run_totals if d in cap_totals and d not in unpairable]
    for d in by_name:
        ok, tol, expected, observed = check(d, d)
        if not ok:
            diffs.append(
                f"{run_totals[d][0]}: screen {observed} vs run {expected} (tolerance {tol})"
            )
    left_run = [d for d in run_totals if d not in by_name]
    left_cap = [d for d in cap_totals if d not in by_name]
    by_value: list[str] = []
    ambiguous = False
    for d in list(left_run):
        fits = [c for c in left_cap if check(d, c)[0]]
        if fits:
            ambiguous = ambiguous or len(fits) > 1
            left_cap.remove(fits[0])
            left_run.remove(d)
            by_value.append(d)
    if left_run and left_cap:
        names = ", ".join(run_totals[d][0] for d in left_run)
        diffs.append(
            f"run totals {names} pair with no screen total by name or value "
            f"({len(left_cap)} screen total(s) left over)"
        )
    elif left_run:
        notes.append("not on screen: " + ", ".join(run_totals[d][0] for d in left_run))
    elif left_cap:
        notes.append(f"{len(left_cap)} screen total(s) the SQL does not have")
    if ambiguous:
        rule = "values-ambiguous"
        notes.append("a total paired by value could pair with more than one column: check it")
    else:
        rule = "+".join(x for x, used in (("name", by_name), ("values", by_value)) if used)
    return _result(
        "mismatch" if diffs else "match", rule=rule or "row_count", diffs=diffs, notes=notes
    )


def _digests(totals: dict) -> tuple[dict[str, tuple[str, object]], set[str]]:
    """Run totals by hashed name, numbered like review.py numbers its repeats."""
    normals = [_normal(k) for k in totals]
    out: dict[str, tuple[str, object]] = {}
    collided: set[str] = set()
    seen: dict[str, int] = {}
    for (name, value), normal in zip(totals.items(), normals, strict=True):
        seen[normal] = seen.get(normal, 0) + 1
        digest = name_digest(normal if seen[normal] == 1 else f"{normal}#{seen[normal]}")
        out[digest] = (name, value)
        if normals.count(normal) > 1:
            collided.add(digest)
    return out, collided


def _result(
    status: str,
    *,
    rule: str = "",
    reason: str | None = None,
    diffs: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict:
    return {
        "status": status,
        "rule": rule or None,
        "reason": reason,
        "diffs": diffs or [],
        "notes": notes or [],
    }


def compare_metric(run: dict, capture: dict | None) -> dict:
    """One metric's verdict from its ``run`` result and its capture."""
    if run.get("status") != "pass":
        return _result("unsupported", reason=f"run did not pass (status {run.get('status')})")
    if capture is None:
        return _result("not_captured")
    kind = capture.get("kind")
    if kind == "frame":
        return _compare_frame(run, capture)
    if kind in ("scalar", "text"):
        return _compare_number(run, capture)
    kind_name = (capture.get("headline") or {}).get("type") or kind
    return _result(
        "unsupported",
        reason=f"the visual received a {kind_name}; only frames and numbers are compared",
    )


def screen_check(run: dict, capture: dict | None, seen: dict) -> dict:
    """The walk's reading against run and capture. Informational only."""
    block: dict = {
        "source": seen.get("source"),
        "agrees_with_run": None,
        "agrees_with_capture": None,
    }
    if seen.get("source") == "plotly":
        block["note"] = "Plotly charts are not read"
        return block
    rows = seen.get("rows")
    if rows is None and "aria_rowcount" in seen:
        rows = max(seen["aria_rowcount"] - ARIA_HEADER_ROWS, 0)
    if rows is None and "marks" in seen:
        rows = seen["marks"]
    if rows is not None:
        block["rows"] = rows
        block["agrees_with_run"] = rows == run.get("rows")
        if capture and capture.get("kind") == "frame":
            block["agrees_with_capture"] = rows == capture.get("row_count")
        return block
    display = parse_display(seen.get("observed"))
    if display is None:
        return block
    block["observed"] = seen["observed"]
    expected, integer, _, why = _scalar_expected(run)
    if not why and run.get("status") == "pass":
        block["agrees_with_run"] = _match_number(expected, integer, display.value, display)[0]
    if capture and capture.get("kind") in ("scalar", "text"):
        cap = capture["headline"].get("value")
        cap_display = parse_display(cap)
        value = cap_display.value if cap_display else _decimal(cap)
        block["agrees_with_capture"] = _close(value, display.value, display=display)[0]
    return block


# --------------------------------------------------------------------------- #
# The verb
# --------------------------------------------------------------------------- #
def _shown(repo: Path, path: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return str(path)


def cmd_compare(args: argparse.Namespace, runner: object = None) -> int:
    del runner  # compare never touches Snowflake
    repo = Path(args.dir).resolve()
    app, index = live.load_app(repo, args.slug)
    run_dir = live.resolve_run(repo, app, args.run or "latest", None)
    run_files = sorted(run_dir.glob("run-*.json"))
    if not run_files:
        raise live.ToolError(f"run {run_dir.name} has no `run` results; run the sections first")
    capture_dir = run_dir / CAPTURE_DIR
    if args.capture:
        capture_dir = Path(args.capture).expanduser()
        if not capture_dir.is_absolute():
            capture_dir = repo / capture_dir
    captures, warnings = load_captures(capture_dir)
    if not captures:
        where = _shown(repo, capture_dir)
        raise live.ToolError(
            f"nothing was captured in {where}: start the preview with `streamsnow preview start "
            f"{app.name} --port 0 --review-capture {where}`, open every page once without "
            "touching a filter, then compare (an app whose review.py predates capture records "
            "nothing)"
        )
    screen_path = Path(args.screen) if args.screen else run_dir / SCREEN_FILE
    screen: dict[tuple[str, int], dict] = {}
    if args.screen or screen_path.is_file():
        screen, more = load_screen(screen_path, index)
        warnings += more

    pages = {f"{p.number:02d}": p for p in sr._expected_pages(index)}
    used: set[str] = set()
    results: list[dict] = []
    digests: dict[str, str] = {}
    for path in run_files:
        data = live.read_json(path)
        digests[str(data.get("page") or path.stem[4:])] = live.run_digest(path)
        for run in data.get("results", []):
            page = pages.get(str(run.get("page")))
            metric = (
                next((m for m in page.metrics if m.number == run.get("n")), None) if page else None
            )
            if page is None or metric is None:
                warnings.append(f"{run.get('id')} matches no metric in index.yaml; skipped")
                continue
            cap, why = _find_capture(page, metric.key, captures, index)
            if cap is not None:
                used.add(cap.file)
            verdict = compare_metric(run, cap.data if cap else None)
            if verdict["status"] == "not_captured":
                verdict["reason"] = why
            seen = screen.get((run["page"], metric.number))
            results.append(
                {
                    "id": f"compare:{run['page']}#{metric.number}",
                    "page": run["page"],
                    "stem": page.stem,
                    "n": metric.number,
                    "key": metric.key,
                    **verdict,
                    "capture": cap.file if cap else None,
                    "screen": screen_check(run, cap.data if cap else None, seen) if seen else None,
                }
            )
    for c in captures:
        if c.file not in used:
            warnings.append(f"capture {c.file} matches no metric in this run")
    counts = {s: sum(1 for r in results if r["status"] == s) for s in STATUSES}
    data = {
        "verb": "compare",
        "run_id": run_dir.name,
        "app": app.name,
        "capture": _shown(repo, capture_dir),
        "screen": _shown(repo, screen_path) if screen else None,
        "helper": helper_state(app),
        "run_digests": digests,
        "results": results,
        "counts": counts,
        "warnings": warnings,
    }
    if data["helper"] != "current":
        warnings.append(
            f"apps/{app.name}/review.py is {data['helper']}: it is not the scaffolded helper, "
            "so weigh its captures accordingly"
        )
    live.write_json(run_dir / COMPARE_FILE, data)
    print(json.dumps(data, indent=2))
    return 1 if counts["mismatch"] else 0
