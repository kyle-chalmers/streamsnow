"""Overview page — headline KPIs, a weekly-active-users trend, and channel mix."""

import plotly.express as px
import streamlit as st
from branding import BRAND_CHART_COLORS, branded_metric
from data import load_kpis, load_signups_by_channel, load_weekly_active_users

st.title("StreamSnow Example Dashboard")
st.caption(
    "A runnable demo with sample data — no Snowflake connection required. "
    "Generate a real, governed app with `streamsnow init`."
)

kpis = load_kpis()
c1, c2, c3, c4 = st.columns(4)
with c1:
    branded_metric("Weekly active users", kpis["weekly_active_users"], delta=kpis["wau_delta"])
with c2:
    branded_metric("Signups (12 wk)", kpis["signups"], delta=kpis["signups_delta"])
with c3:
    branded_metric("Activation rate", kpis["activation_rate"], delta=kpis["activation_delta"])
with c4:
    branded_metric("Net revenue", kpis["net_revenue"], delta=kpis["revenue_delta"])

st.divider()

left, right = st.columns((3, 2))
with left:
    st.subheader("Weekly active users")
    wau = load_weekly_active_users()
    fig = px.line(
        wau,
        x="week",
        y="active_users",
        markers=True,
        labels={"week": "Week", "active_users": "Active users"},
        color_discrete_sequence=BRAND_CHART_COLORS,
    )
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, use_container_width=True)

with right:
    st.subheader("Signups by channel")
    channels = load_signups_by_channel()
    fig = px.bar(
        channels,
        x="signups",
        y="channel",
        orientation="h",
        labels={"signups": "Signups", "channel": ""},
        color_discrete_sequence=BRAND_CHART_COLORS,
    )
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), yaxis=dict(categoryorder="total ascending"))
    st.plotly_chart(fig, use_container_width=True)

st.caption("Source: sample data in `data.py` (swap for `conn.query(load_sql(...))` in a real app).")
