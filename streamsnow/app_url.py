"""The Snowsight link to a deployed app, so a person can click through it.

A green deploy and a passing ``verify-deploy`` prove the object exists and has a
live version; they cannot prove the pages render, the filters work or the numbers
match what preview showed. CI cannot load the page either: the CI user signs in by
key pair, and Snowsight needs a person. So the last step of a ship is a human
opening the app, and this module gives them the exact URL instead of a hunt
through Projects and Streamlit.

The URL is Snowflake's documented app-builder form,
``https://app.snowflake.com/<organization_name>/<account_name>/#/streamlit-apps/<db>.<schema>.<app>``
(see ``docs/snowflake-docs.md``). The app part reuses ``deploy.streamlit_fqn``,
the same name the deploy SQL creates, so the link cannot drift from the object.
The organization and account come from ``CURRENT_ORGANIZATION_NAME()`` and
``CURRENT_ACCOUNT_NAME()`` on the user's own connection, never from config, so a
config that names no organization still yields a working link.

The URL names the organization and account. That is fine in the user's own
terminal and is why this is a local command, never a CI log line.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .config import Config
from .deploy import streamlit_fqn

RunQuery = Callable[[str], list[dict]]

SNOWSIGHT = "https://app.snowflake.com"
ORG_ACCOUNT_SQL = (
    "SELECT CURRENT_ORGANIZATION_NAME() AS ORGANIZATION, CURRENT_ACCOUNT_NAME() AS ACCOUNT"
)
# Organization and account names are letters, digits and underscores; anything
# else would not be a path segment Snowsight serves, so it is refused, not escaped.
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


class AppUrlError(RuntimeError):
    """The URL could not be built. User-facing."""


def snowsight_url(organization: str, account: str, fqn: str) -> str:
    """The app-builder URL. Snowsight's own address bar shows the organization and
    account in lower case, so they are lower-cased here; the FQN keeps its case."""
    return f"{SNOWSIGHT}/{organization.lower()}/{account.lower()}/#/streamlit-apps/{fqn}"


def _value(row: dict, key: str) -> str:
    for k, v in row.items():
        if str(k).upper() == key:
            return "" if v is None else str(v).strip()
    return ""


def app_url(cfg: Config, slug: str, run_query: RunQuery) -> dict:
    """``{"app", "fqn", "url"}`` for one app. Raises ValueError on an invalid slug
    and AppUrlError when the query fails or returns no usable names."""
    # The deploy SQL names the database and schema unquoted, so Snowflake stores
    # them upper case (`analytics` becomes `ANALYTICS`). Config only accepts
    # unquoted identifiers, so upper-casing gives the stored name.
    fqn = streamlit_fqn(cfg, slug).upper()
    try:
        rows = run_query(ORG_ACCOUNT_SQL)
    except Exception as exc:
        raise AppUrlError(f"could not read the organization and account names: {exc}") from exc
    row = rows[0] if rows else {}
    organization, account = _value(row, "ORGANIZATION"), _value(row, "ACCOUNT")
    for label, value in (("organization", organization), ("account", account)):
        if not _NAME_RE.match(value):
            raise AppUrlError(
                f"the connection returned no usable {label} name, so the URL cannot be built"
            )
    return {"app": slug, "fqn": fqn, "url": snowsight_url(organization, account, fqn)}


def snow_run_query(connection: str) -> RunQuery:
    """A read-only ``run_query`` over the named ``snow`` connection, through the
    guarded executor ``sql-review`` uses. Raises ``sf_exec.SnowError`` on a bad
    connection name."""
    from . import sf_exec as sx

    session = sx.Session(connection=connection, query_tag="streamsnow:app-url", timeout_s=60)
    executor = sx.SnowExec(session, None)
    return lambda sql: sx.upper_rows(executor.run([sql])[0])
