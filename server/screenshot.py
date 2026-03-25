"""X11 screenshot capture via ImageMagick import."""

import io
import logging
import os
import subprocess

from .errors import DisplayError
from .window import WindowTracker

logger = logging.getLogger("gui-user.screenshot")


class ScreenshotCapture:
    """Capture screenshots from an Xvfb display."""

    def __init__(self, display: str, pid: int | None = None):
        self._display = display
        self._window_tracker = WindowTracker(display, pid) if pid is not None else None

    def capture(self, region: tuple[int, int, int, int] | None = None) -> bytes:
        """Return PNG bytes of the screen (active window, or full screen fallback).

        Args:
            region: Optional (x, y, width, height) to crop the screenshot.
        """
        env = {**os.environ, "DISPLAY": self._display}

        png = None

        if self._window_tracker is not None:
            window_id = self._window_tracker.get_preferred_window_id()
            if window_id:
                png = self._import_window(window_id, env)
                if png:
                    logger.debug(f"Captured target window {window_id} ({len(png)} bytes)")

        if png is None:
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
            except Exception as e:
                logger.debug(f"Active window capture failed: {e}")

        if png is None:
            # Fallback: full screen
            png = self._import_window("root", env)
            if not png:
                raise DisplayError("Screenshot capture failed: no output from import")
            logger.debug(f"Captured root window ({len(png)} bytes)")

        if region is not None:
            png = self._crop(png, region)

        return png

    def capture_to_file(self, path: str) -> str:
        """Save PNG to file, return the path."""
        png = self.capture()
        with open(path, "wb") as f:
            f.write(png)
        return path

    @staticmethod
    def _crop(png_bytes: bytes, region: tuple[int, int, int, int]) -> bytes:
        """Crop PNG bytes to (x, y, width, height) using Pillow."""
        from PIL import Image
        x, y, w, h = region
        img = Image.open(io.BytesIO(png_bytes))
        cropped = img.crop((x, y, x + w, y + h))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()

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
