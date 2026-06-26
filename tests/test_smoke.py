"""Phase 0 smoke tests — prove the package imports and the core seams exist.

These run with no Snowflake account, no network, no Claude Code. Phase 1+ adds
offline contract tests (generated-repo Jinja snapshots, mocked `snow` CLI,
plugin-manifest validation) per the cross-agent review.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamsnow
from streamsnow.policy import SchemaPolicy

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_version_is_string():
    assert isinstance(streamsnow.__version__, str)
    assert streamsnow.__version__


def test_cli_app_imports():
    from streamsnow.cli import app

    assert app is not None


def test_schema_policy_denies_case_insensitively():
    policy = SchemaPolicy(database="DEMO_DB", schema_allow=("ANALYTICS",), schema_deny=("BRIDGE",))
    assert policy.is_denied("bridge") is True
    assert policy.is_denied("BRIDGE") is True
    assert policy.is_denied("analytics") is False


def test_plugin_manifest_is_valid_json_with_name():
    manifest = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "streamsnow"


def test_marketplace_lists_the_plugin():
    market = json.loads((REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text())
    names = {p["name"] for p in market["plugins"]}
    assert "streamsnow" in names
