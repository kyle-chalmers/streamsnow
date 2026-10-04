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
