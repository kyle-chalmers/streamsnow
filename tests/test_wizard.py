"""The configure wizard's UX contract: ≤5 questions, commented-YAML output."""

from __future__ import annotations

from pathlib import Path

import typer
import yaml

from streamsnow.cli import _prompt_config, _render_config_yaml
from streamsnow.config import Config


def _run_wizard(monkeypatch, prefill=None, directory=Path("acme-analytics")):
    """Drive the wizard accepting every default; return (config dict, prompts asked)."""
    asked: list[str] = []

    def fake_prompt(text, default=None, **kwargs):
        asked.append(str(text))
        # The account locator is the one question with no default.
        return default if default is not None else "ab12345.us-east-1"

    monkeypatch.setattr(typer, "prompt", fake_prompt)
    return _prompt_config(prefill, directory), asked


def test_wizard_asks_at_most_five_questions(monkeypatch):
    cfg_dict, asked = _run_wizard(monkeypatch)
    assert len(asked) <= 5, f"configure asked {len(asked)} questions: {asked}"
    # The result is a complete, valid config despite only 5 answers.
    Config.from_dict(cfg_dict)


def test_wizard_derives_project_identity_from_directory(monkeypatch):
    cfg_dict, _ = _run_wizard(monkeypatch, directory=Path("Acme Analytics"))
    assert cfg_dict["project"]["slug"] == "acme-analytics"
    assert cfg_dict["project"]["name"] == "Acme Analytics"
    assert cfg_dict["snowflake"]["connection_name"] == "acme-analytics"


def test_wizard_prefill_survives_for_unasked_values(monkeypatch):
    prefill = {
        "project": {"name": "Custom Name", "slug": "custom-slug"},
        "snowflake": {"roles": {"viewer_role": "MY_VIEWER"}},
        "governance": {"schema_deny": ["SECRET_SCHEMA"]},
    }
    cfg_dict, asked = _run_wizard(monkeypatch, prefill=prefill)
    assert len(asked) <= 5
    # Hand-edited values the wizard no longer asks about are preserved.
    assert cfg_dict["project"]["slug"] == "custom-slug"
    assert cfg_dict["snowflake"]["roles"]["viewer_role"] == "MY_VIEWER"
    assert cfg_dict["governance"]["schema_deny"] == ["SECRET_SCHEMA"]


def test_wizard_preserves_hand_edited_keys_it_never_asks_about(monkeypatch):
    # Keys Config supports but the wizard doesn't build — a rewrite must not drop them.
    prefill = {
        "project": {"agents_md_char_limit": 20000},
        "snowflake": {
            "objects": {"stage_name": "MY_STAGE", "container_python": "3.11"},
        },
        "governance": {"read_exceptions": ["ANALYTICS_DB.RAW.SANCTIONED_VIEW"]},
    }
    cfg_dict, _ = _run_wizard(monkeypatch, prefill=prefill)
    assert cfg_dict["project"]["agents_md_char_limit"] == 20000
    assert cfg_dict["snowflake"]["objects"]["stage_name"] == "MY_STAGE"
    assert cfg_dict["snowflake"]["objects"]["container_python"] == "3.11"
    assert cfg_dict["governance"]["read_exceptions"] == ["ANALYTICS_DB.RAW.SANCTIONED_VIEW"]
    text = _render_config_yaml(cfg_dict)
    assert yaml.safe_load(text) == cfg_dict
    Config.from_dict(cfg_dict)


def test_rendered_yaml_survives_values_longer_than_yaml_wrap_width(monkeypatch):
    # PyYAML wraps flow lists at ~80 cols by default; the renderer must not truncate.
    prefill = {
        "snowflake": {
            "objects": {"allowed_warehouses": [f"REPORTING_WAREHOUSE_{i:02d}" for i in range(8)]}
        }
    }
    cfg_dict, _ = _run_wizard(monkeypatch, prefill=prefill)
    text = _render_config_yaml(cfg_dict)
    assert yaml.safe_load(text) == cfg_dict


def test_slugify_directory_starting_with_digits(monkeypatch):
    cfg_dict, _ = _run_wizard(monkeypatch, directory=Path("2024-reports"))
    assert cfg_dict["project"]["slug"] == "reports"


def test_rendered_yaml_round_trips_and_carries_comments(monkeypatch):
    cfg_dict, _ = _run_wizard(monkeypatch)
    text = _render_config_yaml(cfg_dict)
    assert yaml.safe_load(text) == cfg_dict
    Config.from_dict(yaml.safe_load(text))
    # The defaulted values are self-documenting in the file.
    assert "#" in text
    assert "viewer" in text.lower()
