"""Session helper for the warehouse runtime.

The narrow ``except ImportError`` is deliberate for this app: its local
environment resolves snowpark identically to the deployed one, and the team
wants any *other* failure (auth, network) to surface loudly instead of being
swallowed by a broad handler. The waiver records that decision on the call line.
"""

import streamlit as st


def get_session():
    try:
        from snowflake.snowpark.context import get_active_session

        return get_active_session()  # noqa: session-fallback
    except ImportError:
        return st.connection("snowflake").session()
