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
        args = ["search"]
        if visible_only:
            args.append("--onlyvisible")
        args.extend(["--pid", str(self._pid), "--name", ".*"])

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
            stderr = result.stderr.strip()
            if stderr and "No such window" not in stderr:
                logger.debug("xdotool search failed for pid=%s: %s", self._pid, stderr)
            return []

        window_ids = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                window_ids.append(line)
        return window_ids

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
