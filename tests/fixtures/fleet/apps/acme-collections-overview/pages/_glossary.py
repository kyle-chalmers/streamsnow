"""Metric definitions for Collections Overview — one place, four consumers.

IMPORT CONTRACT: ``from pages._glossary import metric_help`` (package-qualified).
A bare ``from _glossary import ...`` resolves under ``streamlit run`` (which puts
the executing page's directory on sys.path) and raises ModuleNotFoundError in
the deployed runtime, where only the app root is importable.
"""

from __future__ import annotations

from typing import NamedTuple

import streamlit as st  # imported, never called at module scope (container thread safety)


class Metric(NamedTuple):
    key: str
    label: str
    definition: str
    formula: str


_DEFINITIONS: tuple[Metric, ...] = (
    Metric(
        "promise_kept_rate",
        "Promise-kept rate",
        "Share of payment promises honoured by their due date.",
        "SUM(kept_promises) ÷ SUM(promises_due)",
    ),
)
_BY_KEY = {m.key: m for m in _DEFINITIONS}


def metric_help(key: str) -> str:
    m = _BY_KEY[key]
    return f"{m.definition} Formula: {m.formula}"


def column_help(*keys: str) -> dict[str, str]:
    return {k: metric_help(k) for k in keys}


def render_glossary(*keys: str) -> None:
    with st.expander("Metric definitions"):
        for k in keys or tuple(_BY_KEY):
            m = _BY_KEY[k]
            st.markdown(f"**{m.label}** — {m.definition} `{m.formula}`")
