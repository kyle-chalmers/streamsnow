"""Trends page — a retention curve, to show multi-page st.navigation."""

import plotly.express as px
import streamlit as st
from branding import BRAND_CHART_COLORS
from data import load_retention_curve

st.title("Trends")
st.caption("Day-N retention for the latest cohort (sample data).")

curve = load_retention_curve()
fig = px.line(
    curve,
    x="day",
    y="retention_pct",
    markers=True,
    labels={"day": "Days since signup", "retention_pct": "Retention (%)"},
    color_discrete_sequence=BRAND_CHART_COLORS,
)
fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), yaxis=dict(range=[0, 100]))
st.plotly_chart(fig, use_container_width=True)

st.info(
    "This page exists to demonstrate `st.navigation` with multiple pages — the "
    "same pattern `streamsnow init` scaffolds. Add a page by dropping a module in "
    "`pages/` and registering it in `streamlit_app.py`."
)
