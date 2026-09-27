"""Suite-wide fixtures.

The config wizard and ``init``/``configure`` output read the machine's
``snow connection list`` to default ``snowflake.connection_name`` to an
existing default connection. Stub that lookup for every test so results never
depend on the developer's own ``snow`` setup (CONTRIBUTING: no reliance on the
developer's machine). Tests that exercise the detection patch it explicitly.
"""

from __future__ import annotations

import pytest

from streamsnow import cli


@pytest.fixture(autouse=True)
def _no_real_snow_connections(monkeypatch):
    monkeypatch.setattr(cli, "_snow_connections", lambda: None)
