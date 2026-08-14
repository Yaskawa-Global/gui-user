"""Persisted session descriptor.

A launched session (display + app process) normally lives only inside the process that
started it.  Writing a small descriptor to disk lets a *separate*, short-lived process
attach to the same running app instead of launching its own — which is what makes a
one-shot CLI usable against a session started elsewhere (e.g. by the MCP server).

The descriptor records only what is needed to re-derive the handles: the display, the
D-Bus address that carries AT-SPI, and the application PID.
"""

import json
import logging
import os

from .errors import DisplayError

logger = logging.getLogger("gui-user.session")

SESSION_DIR = ".gui-user"
SESSION_FILE = "session.json"


def default_session_path(base_dir: str | None = None) -> str:
    """Path of the session descriptor, under `base_dir` (default: current directory)."""
    return os.path.join(base_dir or os.getcwd(), SESSION_DIR, SESSION_FILE)


def write_session(
    path: str | None = None,
    *,
    display: str,
    display_mode: str,
    dbus_address: str | None,
    app_pid: int,
    binary: str | None = None,
    working_dir: str | None = None,
    vnc_display: str | None = None,
) -> str:
    """Record a running session so another process can attach to it."""
    path = path or default_session_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "display": display,
        "display_mode": display_mode,
        "dbus_address": dbus_address,
        "app_pid": app_pid,
        "binary": binary,
        "working_dir": working_dir,
        "vnc_display": vnc_display,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    logger.debug(f"Session descriptor written to {path}")
    return path


def read_session(path: str | None = None) -> dict:
    """Read a session descriptor. Raises DisplayError if absent or malformed."""
    path = path or default_session_path()
    if not os.path.exists(path):
        raise DisplayError(
            f"No session descriptor at {path} — nothing to attach to. "
            "Launch an app first (the launching process writes this file)."
        )
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise DisplayError(f"Unreadable session descriptor {path}: {e}") from e

    for key in ("display", "app_pid"):
        if not data.get(key):
            raise DisplayError(f"Session descriptor {path} is missing '{key}'")
    return data


def clear_session(path: str | None = None) -> None:
    """Remove the session descriptor, if present."""
    path = path or default_session_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"Could not remove session descriptor {path}: {e}")


def pid_alive(pid: int) -> bool:
    """True if the PID exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    # A terminated-but-unreaped child still answers signal 0; check its state.
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return True
