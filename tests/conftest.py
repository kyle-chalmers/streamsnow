"""Suite-wide fixtures.

The config wizard and ``init``/``configure`` output read the machine's
``snow connection list`` to default ``snowflake.connection_name`` to an
existing default connection. Stub that lookup for every test so results never
depend on the developer's own ``snow`` setup (CONTRIBUTING: no reliance on the
developer's machine). Tests that exercise the detection patch it explicitly.
The same goes for the git origin lookup and the source probe configure runs,
which would log in to Snowflake.
"""

from __future__ import annotations

import pytest

from streamsnow import cli, probe


@pytest.fixture(autouse=True)
def _no_real_snow_connections(monkeypatch):
    monkeypatch.setattr(cli, "_snow_connections", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_git_origin(monkeypatch):
    # `deploy-setup --source git-repository` falls back to the checkout's origin
    # remote; the suite runs inside StreamSnow's own checkout, so stub it.
    monkeypatch.setattr(cli, "_checkout_github_origin", lambda: None)


@pytest.fixture(autouse=True)
def _no_live_source_probe(monkeypatch):
    # configure probes the sources with a real `snow sql` login when the connection
    # exists; never from the suite (no network, no SSO window).
    monkeypatch.setattr(
        cli,
        "_live_probe",
        lambda connection, targets: probe.unverified(targets, "not probed in tests"),
    )
