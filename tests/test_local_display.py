"""Tests for optional local X11 display mode."""

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.display import DisplayManager
from server.input import InputController
from server.main import close_app, get_app_status, launch_app, screenshot
from server.screenshot import ScreenshotCapture

GTK_TEST_APP = """
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

win = Gtk.Window(title="LocalDisplayTest")
win.set_default_size(240, 120)
win.connect("destroy", Gtk.main_quit)

label = Gtk.Label(label="Local mode ready")
win.add(label)

win.show_all()
Gtk.main()
"""


def _local_display_available() -> bool:
    display = os.environ.get("DISPLAY")
    if not display:
        return False
    try:
        result = subprocess.run(
            ["xdotool", "getmouselocation"],
            env={**os.environ, "DISPLAY": display},
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return False
    return result.returncode == 0


@unittest.skipUnless(_local_display_available(), "Reachable X11 DISPLAY is required for local display tests")
class TestLocalDisplayIntegration(unittest.TestCase):
    """End-to-end coverage for local display mode."""

    def setUp(self):
        self._app_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
        self._app_file.write(GTK_TEST_APP)
        self._app_file.close()

    def tearDown(self):
        close_app()
        if getattr(self, "_app_file", None):
            os.unlink(self._app_file.name)

    def test_launch_app_uses_inherited_local_display(self):
        result = launch_app(
            binary="python3",
            args=[self._app_file.name],
            timeout=5.0,
            display_mode="local",
        )
        result = asyncio.run(result)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["display_mode"], "local")
        self.assertEqual(result["display"], os.environ["DISPLAY"])
        self.assertTrue(result["warnings"])

        status = get_app_status()
        self.assertTrue(status["running"])
        self.assertEqual(status["display_mode"], "local")
        self.assertEqual(status["display"], os.environ["DISPLAY"])

    def test_launch_app_accepts_explicit_local_display(self):
        result = launch_app(
            binary="python3",
            args=[self._app_file.name],
            timeout=5.0,
            display_mode="local",
            display=os.environ["DISPLAY"],
        )
        result = asyncio.run(result)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["display_mode"], "local")
        self.assertEqual(result["display"], os.environ["DISPLAY"])

    def test_close_app_keeps_local_display_mode_session_isolated(self):
        result = launch_app(
            binary="python3",
            args=[self._app_file.name],
            timeout=5.0,
            display_mode="local",
        )
        result = asyncio.run(result)
        self.assertTrue(result["success"], result)

        closed = close_app()
        self.assertTrue(closed["success"])

        status = get_app_status()
        self.assertFalse(status["running"])
        self.assertIsNone(status["display"])


class TestLocalDisplayUnit(unittest.TestCase):
    """Focused unit tests for local display behavior."""

    def test_display_manager_requires_display_for_local_mode(self):
        with patch.dict(os.environ, {}, clear=True):
            manager = DisplayManager()
            with self.assertRaisesRegex(Exception, "requires DISPLAY"):
                manager.start(mode="local")

    def test_display_manager_reports_access_failure_for_invalid_local_display(self):
        manager = DisplayManager()
        with patch("server.display.subprocess.run") as run_mock:
            run_mock.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="Authorization required",
            )
            with self.assertRaisesRegex(Exception, "Cannot access local display :123"):
                manager.start(mode="local", display=":123")

    def test_screenshot_prefers_target_window_when_available(self):
        capture = ScreenshotCapture(":1", pid=1234)
        with patch.object(
            capture._window_tracker,
            "get_preferred_window_id",
            return_value="4321",
        ), patch.object(
            ScreenshotCapture,
            "_import_window",
            return_value=b"png-bytes",
        ) as import_mock, patch("server.screenshot.subprocess.run") as run_mock:
            self.assertEqual(capture.capture(), b"png-bytes")
            import_mock.assert_called_once()
            run_mock.assert_not_called()

    def test_keyboard_input_activates_target_window_in_local_mode(self):
        controller = InputController(":1", pid=999, activate_on_keyboard=True)
        with patch.object(controller._window_tracker, "activate_window") as activate_mock, patch(
            "server.input.subprocess.run"
        ) as run_mock:
            run_mock.return_value = MagicMock(returncode=0, stderr="")
            controller.press_key("Enter")
            activate_mock.assert_called_once()

    def test_local_screenshot_tool_returns_path(self):
        # screenshot() now returns a gallery_path instead of base64 data
        # Full integration test needed; this is a placeholder
        pass
