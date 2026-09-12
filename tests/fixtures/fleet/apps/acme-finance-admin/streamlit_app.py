"""Finance Admin — entrypoint (fleet fixture, container runtime)."""

import streamlit as st

st.set_page_config(page_title="Finance Admin", page_icon="📊", layout="wide")

from branding import apply_branding  # noqa: E402  (must follow set_page_config)

apply_branding()

nav = st.navigation(
    {
        "Admin": [
            st.Page(
                "pages/admin/report.py", title="Fee report", icon="📈", default=True
            )
        ]
    }
)
nav.run()
