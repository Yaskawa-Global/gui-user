"""GuiUser — Python API for GUI testing via AT-SPI and xdotool.

This is the standalone test-script interface to gui-user's functionality.
Import and use this class directly in Python test scripts without needing
the MCP server.

Example:
    from gui_user import GuiUser

    app = GuiUser("/path/to/my_app", width=800, height=1280)
    app.wait_for_idle()
    app.click_element("Jobs")
    assert app.is_element_visible("Job List")
    assert app.find_text_on_screen("LONGJOB")
    app.close()
"""

import os
import time
from dataclasses import asdict

from server.accessibility import AccessibilityTree, ElementInfo
from server.display import DisplayManager
from server.errors import GuiUserError
from server.input import InputController
from server.process import ProcessManager
from server.screenshot import ScreenshotCapture
from server.wait import IdleWaiter


class GuiUser:
    """High-level API for launching, observing, and interacting with GUI apps."""

    def __init__(
        self,
        binary: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        working_dir: str | None = None,
        width: int = 800,
        height: int = 1280,
        timeout: float = 15.0,
        display_mode: str = "xvfb",
        vnc: bool = False,
        screenshot_dir: str | None = None,
    ):
        """Launch an application and connect to it.

        Args:
            binary: Path to executable.
            args: Command-line arguments.
            env: Extra environment variables.
            working_dir: Working directory for the process.
            width: Display width in pixels.
            height: Display height in pixels.
            timeout: Seconds to wait for AT-SPI registration.
            display_mode: "xvfb" for virtual display, "local" for real display.
            vnc: Start VNC server for observation.
            screenshot_dir: Directory for auto-saved screenshots. Defaults to .gui-user/screenshots/.
        """
        self._display = DisplayManager()
        self._resolved_display = self._display.start(width=width, height=height, mode=display_mode)

        if vnc and display_mode != "local":
            self._display.start_vnc()

        merged_env = {**os.environ, **self._display.env, **(env or {})}
        self._process = ProcessManager()
        self._pid = self._process.launch(binary, args=args or [], env=merged_env, working_dir=working_dir)

        self._input = InputController(self._resolved_display, pid=self._pid)
        self._screenshot = ScreenshotCapture(self._resolved_display, pid=self._pid)
        self._waiter = IdleWaiter(self._pid)

        self._screenshot_dir = screenshot_dir or os.path.join(os.getcwd(), ".gui-user", "screenshots")
        os.makedirs(self._screenshot_dir, exist_ok=True)

        # Try to connect AT-SPI
        self._accessibility: AccessibilityTree | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if self._process.poll() is not None:
                raise GuiUserError(f"App exited immediately (exit code {self._process.poll()})")
            try:
                self._accessibility = AccessibilityTree(pid=self._pid, display_env=self._display.env)
                break
            except Exception:
                pass

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def display(self) -> str:
        return self._resolved_display

    @property
    def vnc_display(self) -> str | None:
        return self._display.vnc_display if self._display.vnc_running else None

    @property
    def is_running(self) -> bool:
        return self._process.is_running

    @property
    def has_accessibility(self) -> bool:
        return self._accessibility is not None

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def close(self) -> None:
        """Close the app and tear down the display."""
        self._process.terminate()
        self._display.stop()

    def close_app(self) -> None:
        """Close the app but keep the display running."""
        self._process.terminate()
        self._accessibility = None

    # -----------------------------------------------------------------------
    # Wait
    # -----------------------------------------------------------------------

    def wait_for_idle(self, timeout: float = 10.0) -> None:
        """Wait for the app's CPU usage to settle."""
        self._waiter.wait_for_idle(timeout=timeout)

    def wait_for_element(self, text: str | None = None, role: str | None = None, timeout: float = 10.0) -> ElementInfo:
        """Poll until an AT-SPI element appears."""
        self._require_accessibility()
        return self._waiter.wait_for_element(self._accessibility, text=text, role=role, timeout=timeout)

    def wait_for_text_visible(self, text: str, timeout: float = 10.0, exact: bool = False) -> dict | None:
        """Poll until OCR finds text on screen. Returns the match or None on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = self.find_text_on_screen(text, exact=exact)
            if matches:
                return matches[0]
            time.sleep(1.0)
        return None

    def wait_for_element_state(self, text: str, state: str, timeout: float = 10.0, role: str | None = None) -> bool:
        """Poll until an AT-SPI element has a specific state (e.g. 'checked', 'enabled')."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            elem = self.get_element(text, role=role)
            if elem and state in elem.states:
                return True
            time.sleep(0.5)
        return False

    def wait_for_element_gone(self, text: str, timeout: float = 10.0, role: str | None = None) -> bool:
        """Poll until an AT-SPI element is no longer visible."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            elem = self.get_element(text, role=role)
            if elem is None:
                return True
            time.sleep(0.5)
        return False

    # -----------------------------------------------------------------------
    # Observation — AT-SPI
    # -----------------------------------------------------------------------

    def list_elements(self, role: str | None = None, name: str | None = None,
                      visible_only: bool = True, max_results: int = 0) -> list[ElementInfo]:
        """List UI elements from the accessibility tree."""
        self._require_accessibility()
        return self._accessibility.list_elements(
            filter_role=role, filter_name=name, visible_only=visible_only, max_results=max_results
        )

    def get_element(self, text: str | None = None, role: str | None = None, index: int = 0) -> ElementInfo | None:
        """Find an AT-SPI element by text/role. Returns None if not found."""
        self._require_accessibility()
        return self._accessibility.find_element(text=text, role=role, index=index)

    def find_any_element(self, texts: list[str], role: str | None = None) -> tuple[str, ElementInfo] | None:
        """Find the first visible AT-SPI element whose text matches any candidate.

        Returns a tuple of `(matched_text, element)` using the candidate string that
        produced the match, or `None` if none of the candidates are found.
        """
        self._require_accessibility()
        for text in texts:
            elem = self._accessibility.find_element(text=text, role=role)
            if elem is not None and "visible" in elem.states:
                return (text, elem)
        return None

    def is_element_visible(self, text: str, role: str | None = None) -> bool:
        """Check if an AT-SPI element with the given text is visible."""
        self._require_accessibility()
        elem = self._accessibility.find_element(text=text, role=role)
        return elem is not None and "visible" in elem.states

    def get_element_states(self, text: str, role: str | None = None) -> list[str]:
        """Get the state list of an AT-SPI element (e.g. ['enabled', 'checked', 'visible'])."""
        elem = self.get_element(text, role=role)
        return elem.states if elem else []

    def get_element_value(self, text: str, role: str | None = None) -> float | None:
        """Get the numeric value of a slider/spinbox element."""
        elem = self.get_element(text, role=role)
        return elem.value if elem else None

    def get_element_bounds(self, text: str, role: str | None = None) -> tuple[int, int, int, int] | None:
        """Get (x, y, width, height) bounds of an element."""
        elem = self.get_element(text, role=role)
        return elem.bounds if elem else None

    def get_element_center(self, text: str, role: str | None = None) -> tuple[int, int] | None:
        """Get (x, y) center of an element."""
        elem = self.get_element(text, role=role)
        return elem.center if elem else None

    def count_elements(self, role: str | None = None, name: str | None = None) -> int:
        """Count visible elements matching role/name filters."""
        return len(self.list_elements(role=role, name=name))

    # -----------------------------------------------------------------------
    # Observation — Screenshot & OCR
    # -----------------------------------------------------------------------

    def screenshot(self, path: str | None = None, region: tuple[int, int, int, int] | None = None) -> str:
        """Take a screenshot and return the file path."""
        png_bytes = self._screenshot.capture(region=region)
        if path is None:
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]
            path = os.path.join(self._screenshot_dir, f"{ts}.png")
        with open(path, "wb") as f:
            f.write(png_bytes)
        return path

    def screenshot_with_grid(self, path: str | None = None, region: tuple[int, int, int, int] | None = None) -> str:
        """Take a screenshot with coordinate grid overlay."""
        png_bytes = self._screenshot.capture(region=region)
        offset = (region[0], region[1]) if region else (0, 0)
        grid_bytes = self._screenshot.add_grid(png_bytes, spacing=100, offset=offset)
        if path is None:
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]
            path = os.path.join(self._screenshot_dir, f"{ts}_grid.png")
        with open(path, "wb") as f:
            f.write(grid_bytes)
        return path

    def ocr(self, region: tuple[int, int, int, int] | None = None) -> list[dict]:
        """Run OCR on the current screen and return all text elements with positions."""
        png_bytes = self._screenshot.capture(region=region)
        elements = self._screenshot.ocr(png_bytes)
        if region:
            rx, ry = region[0], region[1]
            for elem in elements:
                elem["bounds"][0] += rx
                elem["bounds"][1] += ry
                elem["center"][0] += rx
                elem["center"][1] += ry
        return elements

    def find_text_on_screen(self, text: str, exact: bool = False) -> list[dict]:
        """Find text on screen via OCR. Returns list of matches with positions."""
        elements = self.ocr()
        matches = []
        for elem in elements:
            if exact:
                if elem["text"].lower() == text.lower():
                    matches.append(elem)
            else:
                if text.lower() in elem["text"].lower():
                    matches.append(elem)
        return matches

    def is_text_visible(self, text: str, exact: bool = False) -> bool:
        """Check if text is visible on screen via OCR."""
        return len(self.find_text_on_screen(text, exact=exact)) > 0

    def get_screen_text(self, region: tuple[int, int, int, int] | None = None) -> list[str]:
        """Get all text visible on screen (or in a region) via OCR."""
        return [elem["text"] for elem in self.ocr(region=region)]

    def read_text_field(self, field_name: str) -> str | None:
        """Read the current text content of a named text field via AT-SPI."""
        self._require_accessibility()
        elem = self._accessibility.find_element(text=field_name, role="text")
        return elem.text if elem else None

    # -----------------------------------------------------------------------
    # Assertion helpers
    # -----------------------------------------------------------------------

    def assert_element_visible(self, text: str, role: str | None = None, message: str = "") -> ElementInfo:
        """Assert an AT-SPI element is visible. Raises AssertionError if not."""
        elem = self.get_element(text, role=role)
        if elem is None or "visible" not in elem.states:
            msg = message or f"Element not visible: text={text!r}, role={role!r}"
            raise AssertionError(msg)
        return elem

    def assert_element_not_visible(self, text: str, role: str | None = None, message: str = "") -> None:
        """Assert an AT-SPI element is NOT visible."""
        elem = self.get_element(text, role=role)
        if elem is not None and "visible" in elem.states:
            msg = message or f"Element unexpectedly visible: text={text!r}, role={role!r}"
            raise AssertionError(msg)

    def assert_element_state(self, text: str, state: str, role: str | None = None, message: str = "") -> None:
        """Assert an element has a specific state (e.g. 'checked', 'enabled')."""
        states = self.get_element_states(text, role=role)
        if state not in states:
            msg = message or f"Element {text!r} does not have state {state!r} (has: {states})"
            raise AssertionError(msg)

    def assert_element_not_state(self, text: str, state: str, role: str | None = None, message: str = "") -> None:
        """Assert an element does NOT have a specific state."""
        states = self.get_element_states(text, role=role)
        if state in states:
            msg = message or f"Element {text!r} unexpectedly has state {state!r}"
            raise AssertionError(msg)

    def assert_text_visible(self, text: str, exact: bool = False, message: str = "") -> None:
        """Assert text is visible on screen via OCR."""
        if not self.is_text_visible(text, exact=exact):
            msg = message or f"Text not visible on screen: {text!r}"
            raise AssertionError(msg)

    def assert_text_not_visible(self, text: str, exact: bool = False, message: str = "") -> None:
        """Assert text is NOT visible on screen via OCR."""
        if self.is_text_visible(text, exact=exact):
            msg = message or f"Text unexpectedly visible on screen: {text!r}"
            raise AssertionError(msg)

    def assert_element_value(self, text: str, expected: float, role: str | None = None, message: str = "") -> None:
        """Assert an element's numeric value (e.g. slider)."""
        actual = self.get_element_value(text, role=role)
        if actual != expected:
            msg = message or f"Element {text!r} value: expected {expected}, got {actual}"
            raise AssertionError(msg)

    # -----------------------------------------------------------------------
    # Interaction — Mouse
    # -----------------------------------------------------------------------

    def click(self, x: int, y: int, button: str = "left") -> None:
        """Click at screen coordinates."""
        self._input.click(x, y, button)

    def click_element(self, text: str, role: str | None = None, index: int = 0, button: str = "left") -> None:
        """Find an AT-SPI element and click its center."""
        self._require_accessibility()
        elem = self._accessibility.find_element(text=text, role=role, index=index)
        if elem is None:
            raise GuiUserError(f"Element not found: text={text!r}, role={role!r}")
        self._input.click(*elem.center, button)

    def click_text_on_screen(self, text: str, index: int = 0, exact: bool = False, button: str = "left") -> None:
        """Find text via OCR and click it."""
        matches = self.find_text_on_screen(text, exact=exact)
        if not matches:
            raise GuiUserError(f"Text not found on screen: {text!r}")
        if index >= len(matches):
            raise GuiUserError(f"Text {text!r} found {len(matches)} time(s), but index {index} requested")
        self._input.click(*matches[index]["center"], button)

    def long_press(self, x: int, y: int, duration_ms: int = 1000, button: str = "left") -> None:
        """Press and hold at coordinates."""
        self._input.long_press(x, y, duration_ms, button)

    def double_click(self, x: int, y: int, button: str = "left") -> None:
        """Double-click at coordinates."""
        self._input.double_click(x, y, button)

    def double_click_element(self, text: str, role: str | None = None, index: int = 0, button: str = "left") -> None:
        """Find an AT-SPI element and double-click its center."""
        self._require_accessibility()
        elem = self._accessibility.find_element(text=text, role=role, index=index)
        if elem is None:
            raise GuiUserError(f"Element not found: text={text!r}, role={role!r}")
        self._input.double_click(*elem.center, button)

    def hover(self, x: int, y: int) -> None:
        """Move mouse to coordinates."""
        self._input.mouse_move(x, y)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 500) -> None:
        """Drag from one position to another."""
        self._input.drag(from_x, from_y, to_x, to_y, duration_ms)

    def scroll(self, x: int, y: int, clicks: int = 3, direction: str = "down") -> None:
        """Scroll the mouse wheel at a position."""
        self._input.scroll(x, y, clicks, direction)

    # -----------------------------------------------------------------------
    # Interaction — Keyboard
    # -----------------------------------------------------------------------

    def type_text(self, text: str) -> None:
        """Type text into the focused widget."""
        self._input.type_text(text)

    def press_key(self, key: str, modifiers: list[str] | None = None) -> None:
        """Press a key with optional modifiers."""
        self._input.press_key(key, modifiers)

    # -----------------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------------

    def _require_accessibility(self) -> None:
        if self._accessibility is None:
            raise GuiUserError("AT-SPI accessibility not available for this app")
