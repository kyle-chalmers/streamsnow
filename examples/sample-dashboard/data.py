"""Sample data for the example dashboard — deterministic, no Snowflake.

In a real StreamSnow app these loaders live alongside ``sql_loader.py`` and call
``conn.query(load_sql("name"), ...)`` against an allowed schema, each decorated
with ``@st.cache_data(ttl=...)`` so warehouse spend stays bounded. Here they
return fixed sample frames so the demo runs anywhere — but the caching shape is
exactly what the governance checks (``streamsnow check caching``) expect.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

# A fixed anchor date keeps the demo identical on every run (no randomness).
_WEEK_ZERO = dt.date(2026, 1, 5)


@st.cache_data(ttl=3600)
def load_kpis() -> dict[str, str]:
    """Headline KPIs for the metric cards."""
    wau = load_weekly_active_users()
    signups = load_signups_by_channel()
    return {
        "weekly_active_users": f"{int(wau['active_users'].iloc[-1]):,}",
        "wau_delta": "+8.1% WoW",
        "signups": f"{int(signups['signups'].sum()):,}",
        "signups_delta": "+12.4% MoM",
        "activation_rate": "63.2%",
        "activation_delta": "+1.8 pts",
        "net_revenue": "$248.6k",
        "revenue_delta": "+5.3% MoM",
    }


@st.cache_data(ttl=3600)
def load_weekly_active_users() -> pd.DataFrame:
    """12 weeks of weekly active users (sample). Real app: a cached conn.query()."""
    rows = []
    base = 4200
    for week in range(12):
        day = _WEEK_ZERO + dt.timedelta(weeks=week)
        # A smooth upward trend with a mild oscillation — deterministic.
        active = base + week * 175 + (140 if week % 3 == 0 else -55)
        rows.append({"week": day, "active_users": active})
    return pd.DataFrame(rows)


@st.cache_data(ttl=3600)
def load_signups_by_channel() -> pd.DataFrame:
    """Signups grouped by acquisition channel (sample)."""
    data = {
        "channel": ["Organic", "Referral", "Paid search", "Social", "Email"],
        "signups": [1820, 1240, 980, 760, 540],
    }
    return pd.DataFrame(data)


@st.cache_data(ttl=3600)
def load_retention_curve() -> pd.DataFrame:
    """Day-N retention for the latest cohort (sample)."""
    days = [0, 1, 3, 7, 14, 30]
    retention = [100.0, 58.0, 41.0, 33.0, 27.5, 22.0]
    return pd.DataFrame({"day": days, "retention_pct": retention})
