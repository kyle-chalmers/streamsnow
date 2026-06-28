"""StreamSnow — open-source toolkit for Streamlit-in-Snowflake apps with Claude Code.

The ``streamsnow`` package is the single source of truth for tool logic. The CLI,
the Claude Code plugin, pre-commit, and CI all call into this package — one
implementation, many consumers.
"""

from __future__ import annotations

__all__ = ["__version__"]

# Keep in sync with [project.version] in pyproject.toml. Read dynamically when
# installed so `streamsnow --version` reflects the resolved distribution.
try:  # pragma: no cover - trivial
    from importlib.metadata import version as _version

    __version__ = _version("streamsnow")
except Exception:  # pragma: no cover - source checkout without install
    __version__ = "0.1.0"
