"""Helpers for resolving and activating X11 windows for a target process."""

import logging
import os
import subprocess

logger = logging.getLogger("gui-user.window")


class WindowTracker:
    """Find and operate on windows owned by a target PID."""

    def __init__(self, display: str, pid: int):
        self._env = {**os.environ, "DISPLAY": display}
        self._pid = pid

    def list_window_ids(self, visible_only: bool = True) -> list[str]:
        """Find top-level windows owned by our PID.

        Filters to windows that have a non-empty window name (title),
        which excludes internal/helper windows that xdotool can match.
        """
        args = ["search"]
        if visible_only:
            args.append("--onlyvisible")
        args.extend(["--pid", str(self._pid)])

        try:
            result = subprocess.run(
                ["xdotool"] + args,
                env=self._env,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            logger.debug("Timed out resolving windows for pid=%s", self._pid)
            return []

        if result.returncode != 0:
            return []

        # Filter to windows that have a non-empty title (top-level app windows)
        candidates = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
        window_ids = []
        for wid in candidates:
            name = self._get_window_name(wid)
            if name:
                window_ids.append(wid)
        return window_ids

    def _get_window_name(self, window_id: str) -> str:
        """Get the window title, or empty string if it has none."""
        try:
            result = subprocess.run(
                ["xdotool", "getwindowname", window_id],
                env=self._env,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    def get_preferred_window_id(self) -> str | None:
        visible = self.list_window_ids(visible_only=True)
        if visible:
            return visible[-1]

        all_windows = self.list_window_ids(visible_only=False)
        if all_windows:
            return all_windows[-1]
        return None

    def activate_window(self) -> bool:
        window_id = self.get_preferred_window_id()
        if not window_id:
            return False

        try:
            result = subprocess.run(
                ["xdotool", "windowactivate", "--sync", window_id],
                env=self._env,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            logger.debug("Timed out activating window %s", window_id)
            return False

        if result.returncode != 0:
            logger.debug(
                "Failed to activate window %s for pid=%s: %s",
                window_id,
                self._pid,
                result.stderr.strip(),
            )
            return False
        return True
