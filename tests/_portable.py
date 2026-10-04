"""Stripped subprocess environments that work on every OS.

Several tests prove a hook or tool runs with no venv and no pip-installed
streamsnow by handing the child a minimal environment. On POSIX that has always
been ``PATH=/usr/bin:/bin``, and it stays exactly that. Windows needs more for a
child process to start at all (``SYSTEMROOT`` for socket and crypto init,
``PATHEXT``, a temp dir), and ``/usr/bin`` does not exist there, so the child
could not find git and the tests passed only because git never ran.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

_WINDOWS_ESSENTIALS = ("SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT", "TEMP", "TMP", "COMSPEC")


def bare_env(**extra: str) -> dict[str, str]:
    """A minimal environment with git reachable; ``extra`` overrides (PATH too)."""
    if sys.platform != "win32":
        env = {"PATH": "/usr/bin:/bin"}
    else:
        git = shutil.which("git")
        env = {"PATH": str(Path(git).parent) if git else ""}
        env.update({k: os.environ[k] for k in _WINDOWS_ESSENTIALS if k in os.environ})
    env.update(extra)
    return env


#: Tests that drive a real preview process through POSIX process control
#: (signals, process groups, ``ps``). Windows has its own path in preview_app;
#: until it lands, running these there would call ``os.kill(pid, 0)``, which on
#: Windows terminates the process instead of probing it.
posix_process_control = pytest.mark.skipif(
    sys.platform == "win32", reason="preview process control is POSIX-only for now"
)
