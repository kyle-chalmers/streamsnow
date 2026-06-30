"""Branding for the StreamSnow example — neutral StreamSnow defaults.

This is the same module `streamsnow init` generates from your brand config; the
values below are the out-of-the-box defaults. Each app ships its own local copy
(Snowflake deploy runtimes can't import a shared module).
"""

import html

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

BRAND_COLORS = {
    "primary": "#3B82F6",
    "background": "#FFFFFF",
    "text": "#111827",
}
BRAND_CHART_COLORS = ["#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6"]

# Register the Plotly template once. Guard against re-registration AND the
# default-set side effect: the container runtime shares one Python process
# across all viewers, so module-import mutations must be idempotent.
if "streamsnow_brand" not in pio.templates:
    pio.templates["streamsnow_brand"] = go.layout.Template(
        layout=dict(
            font=dict(family="Inter, system-ui, sans-serif", color=BRAND_COLORS["text"]),
            colorway=BRAND_CHART_COLORS,
            paper_bgcolor=BRAND_COLORS["background"],
            plot_bgcolor=BRAND_COLORS["background"],
        )
    )
    pio.templates.default = "streamsnow_brand"
BRAND_PLOTLY_TEMPLATE = "streamsnow_brand"


def apply_branding() -> None:
    """Call once, right after ``st.set_page_config``. Sets the Plotly default."""
    pio.templates.default = "streamsnow_brand"


def branded_metric(
    label: str, value: str, delta: str | None = None, border_color: str | None = None
) -> None:
    """A metric card with a brand-colored left border.

    ``label`` / ``value`` / ``delta`` are HTML-escaped before interpolation: the
    card is rendered with ``unsafe_allow_html=True``, and in a real app these are
    database-derived strings, so escaping prevents stored values from injecting
    markup into the viewer's page.
    """
    color = border_color or BRAND_COLORS["primary"]
    label, value = html.escape(label), html.escape(value)
    delta_html = (
        f'<div style="font-size:0.8rem;color:#6b7280;">{html.escape(delta)}</div>' if delta else ""
    )
    st.markdown(
        f'<div style="border-left:4px solid {color};padding:0.25rem 0.75rem;margin:0.25rem 0;">'
        f'<div style="font-size:0.8rem;color:#6b7280;">{label}</div>'
        f'<div style="font-size:1.6rem;font-weight:600;">{value}</div>'
        f"{delta_html}</div>",
        unsafe_allow_html=True,
    )
