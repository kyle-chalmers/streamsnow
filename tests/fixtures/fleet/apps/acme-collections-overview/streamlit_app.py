"""Acme Collections Overview — entrypoint. Scaffolded by StreamSnow (container runtime)."""

import streamlit as st

st.set_page_config(
    page_title="Acme Collections Overview", page_icon="📊", layout="wide"
)

from branding import apply_branding  # noqa: E402  (must follow set_page_config)

apply_branding()

nav = st.navigation(
    [
        st.Page("pages/overview.py", title="Overview", icon="📈", default=True),
    ]
)
nav.run()
