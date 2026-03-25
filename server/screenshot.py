"""X11 screenshot capture via ImageMagick import."""

import base64
import logging
import os
import subprocess

from .errors import DisplayError

logger = logging.getLogger("gui-user.screenshot")


class ScreenshotCapture:
    """Capture screenshots from an Xvfb display."""

    def __init__(self, display: str):
        self._display = display

    def capture(self) -> bytes:
        """Return PNG bytes of the screen (active window, or full screen fallback)."""
        env = {**os.environ, "DISPLAY": self._display}

        # Try active window first
        try:
            wid_result = subprocess.run(
                ["xdotool", "getactivewindow"],
                env=env, capture_output=True, text=True, timeout=5,
            )
            if wid_result.returncode == 0 and wid_result.stdout.strip():
                window_id = wid_result.stdout.strip()
                png = self._import_window(window_id, env)
                if png:
                    logger.debug(f"Captured active window {window_id} ({len(png)} bytes)")
                    return png
        except Exception as e:
            logger.debug(f"Active window capture failed: {e}")

        # Fallback: full screen
        png = self._import_window("root", env)
        if not png:
            raise DisplayError("Screenshot capture failed: no output from import")
        logger.debug(f"Captured root window ({len(png)} bytes)")
        return png

    def capture_to_file(self, path: str) -> str:
        """Save PNG to file, return the path."""
        png = self.capture()
        with open(path, "wb") as f:
            f.write(png)
        return path

    def capture_base64(self) -> str:
        """Return base64-encoded PNG for MCP image responses."""
        return base64.b64encode(self.capture()).decode("ascii")

    @staticmethod
    def _import_window(window: str, env: dict) -> bytes | None:
        """Use ImageMagick import to capture a window, return PNG bytes or None."""
        try:
            result = subprocess.run(
                ["import", "-window", window, "png:-"],
                env=env, capture_output=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
            if result.stderr:
                logger.debug(f"import stderr: {result.stderr.decode(errors='replace')[:200]}")
            return None
        except subprocess.TimeoutExpired:
            logger.warning("import command timed out")
            return None
