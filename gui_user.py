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

import logging
import os
import time
from dataclasses import asdict

from server.accessibility import AccessibilityTree, ElementInfo
from server.display import DisplayManager
from server.errors import DisplayError, GuiUserError
from server.input import InputController
from server.process import ProcessManager
from server.screenshot import ScreenshotCapture
from server.session import clear_session, read_session, write_session

logger = logging.getLogger("gui-user")
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
        vnc_scale: str | float | None = None,
        screenshot_dir: str | None = None,
        session_file: str | None = None,
        detached: bool = False,
        reuse_display: bool = True,
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
            vnc_scale: Shrink the VNC stream, e.g. 0.8 -- for watching a display taller than
                the monitor. The display itself keeps its real size.
            screenshot_dir: Directory for auto-saved screenshots. Defaults to .gui-user/screenshots/.
            session_file: Where to record the session descriptor so another process can
                attach() to this app. Defaults to .gui-user/session.json.
            detached: Leave the display and app running when this process exits, so a
                later attach() can pick them up. Forfeits stdout/stderr capture.
            reuse_display: Take over the display from a previous session if it is still
                running, rather than creating another, and stop any others that are up. Set
                False only to deliberately run a second display alongside the first.
        """
        self._display = DisplayManager()

        #  Reuse the display from a previous launch if one is still up, and sweep any others.
        #  Virtual displays are meant to be one at a time: a detached session outlives the
        #  command that started it by design, so without this every launch that nothing later
        #  claims leaves an Xvfb behind. They accumulate invisibly until a viewer attaches to
        #  the wrong one and shows an empty screen.
        reused = None
        if display_mode == "xvfb" and reuse_display:
            reused = self._reusable_display(session_file)

        if reused is not None:
            self._resolved_display = self._display.adopt(
                reused["display"], mode="xvfb",
                dbus_address=reused.get("dbus_address"), take_ownership=True,
            )
        else:
            self._resolved_display = self._display.start(
                width=width, height=height, mode=display_mode, detached=detached
            )

        if display_mode == "xvfb":
            swept = DisplayManager.sweep_other_displays(keep=self._resolved_display)
            if swept:
                logger.info(f"Swept {len(swept)} stale display(s): {', '.join(swept)}")

        if vnc and display_mode != "local" and not self._display.vnc_display:
            self._display.start_vnc(scale=vnc_scale)

        # remembered so relaunch_app() can repeat this launch on the same display
        self._launch = {
            "binary": binary, "args": args or [], "env": env or {},
            "working_dir": working_dir, "detached": detached,
            "display_mode": display_mode, "session_file": session_file,
            "screenshot_dir": screenshot_dir, "timeout": timeout,
        }

        merged_env = {**os.environ, **self._display.env, **(env or {})}
        self._process = ProcessManager()
        try:
            self._pid = self._process.launch(
                binary,
                args=args or [],
                env=merged_env,
                working_dir=working_dir,
                capture_output=not detached,
            )

            self._wire(screenshot_dir, timeout)
        except Exception:
            # An app that will not start would otherwise leave its display behind, and a
            # detached one has no exit handler to catch it later: every failed attempt would
            # cost another Xvfb.
            self._display.stop()
            raise

        self._session_file = write_session(
            session_file,
            display=self._resolved_display,
            display_mode=display_mode,
            dbus_address=self._display.dbus_address,
            app_pid=self._pid,
            binary=binary,
            working_dir=working_dir,
            vnc_display=self._display.vnc_display,
        )

    @staticmethod
    def _reusable_display(session_file: str | None) -> dict | None:
        """The previous session's display, if it is still running and can be taken over.

        Reuse is only safe when the D-Bus address is known -- AT-SPI is reached through it,
        and an orphaned Xvfb with no descriptor cannot tell us what its address was. Such a
        display is swept rather than adopted.
        """
        try:
            data = read_session(session_file)
        except Exception:
            return None

        display = data.get("display")
        if not display or data.get("display_mode") != "xvfb" or not data.get("dbus_address"):
            return None

        live = {d for _, d in DisplayManager.running_xvfb_displays()}
        if display not in live:
            return None

        logger.info(f"Reusing display {display} from the previous session")
        return data

    @classmethod
    def attach(
        cls,
        session_file: str | None = None,
        timeout: float = 5.0,
        screenshot_dir: str | None = None,
        take_ownership: bool = False,
    ) -> "GuiUser":
        """Attach to an app already launched by another process.

        Reads the session descriptor written at launch (display, D-Bus address, PID) and
        rebuilds the input/screenshot/accessibility handles against it. The display and
        the app keep belonging to whoever started them: close() detaches rather than
        tearing the session down.

        `take_ownership` transfers that responsibility to this process, so close() ends the
        session for good. A detached launch outlives its launcher deliberately, which leaves
        nobody able to end it — this is how a later process cleans one up.
        """
        data = read_session(session_file)

        self = cls.__new__(cls)
        self._display = DisplayManager()
        self._resolved_display = self._display.adopt(
            data["display"],
            mode=data.get("display_mode") or "xvfb",
            dbus_address=data.get("dbus_address"),
            take_ownership=take_ownership,
        )
        self._process = ProcessManager()
        self._pid = self._process.adopt(int(data["app_pid"]))
        self._session_file = session_file
        self._wire(screenshot_dir, timeout)
        return self

    def _wire(self, screenshot_dir: str | None, timeout: float) -> None:
        """Build the input/screenshot/wait/accessibility handles for the current app."""
        self._input = InputController(self._resolved_display, pid=self._pid)
        self._screenshot = ScreenshotCapture(self._resolved_display, pid=self._pid)
        self._waiter = IdleWaiter(self._pid)

        self._screenshot_dir = screenshot_dir or os.path.join(os.getcwd(), ".gui-user", "screenshots")
        os.makedirs(self._screenshot_dir, exist_ok=True)

        # Try to connect AT-SPI.  An app that is already up answers on the first attempt,
        # so try before sleeping — attaching should not cost a second per call.
        self._accessibility: AccessibilityTree | None = None
        deadline = time.monotonic() + timeout
        while True:
            if self._process.poll() is not None:
                raise GuiUserError(f"App exited (exit code {self._process.poll()})")
            try:
                self._accessibility = AccessibilityTree(pid=self._pid, display_env=self._display.env)
                break
            except Exception:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(1.0)

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
        """Close the app and tear down the display.

        On an attached session this only detaches — the app is left running, since the
        process that launched it owns its lifetime. Use close_app() to end it explicitly,
        or attach(take_ownership=True) to make this end the whole session.
        """
        if self.attached and not self._display.owns_adopted:
            self._display.stop()
            self._accessibility = None
            return
        self._process.terminate()
        self._display.stop()
        self._accessibility = None
        clear_session(self._session_file)

    def relaunch_app(self, env: dict[str, str] | None = None,
                     timeout: float | None = None) -> int:
        """Restart the application on the display this session already has.

        The display, its D-Bus session and the AT-SPI registry all stay up, so whatever is
        watching -- a VNC viewer, a screen recording -- survives the restart instead of
        having the window vanish from under it. Everything about the launch is repeated
        except `env`, which replaces the extra environment if given (for settings that only
        take effect at startup).

        Returns the new PID. Not available on an attached session: the app belongs to
        whoever launched it.
        """
        if self.attached:
            raise DisplayError("cannot relaunch an app this session only attached to")

        launch = self._launch
        if env is not None:
            launch["env"] = env

        self._process.terminate()
        merged_env = {**os.environ, **self._display.env, **launch["env"]}
        self._pid = self._process.launch(
            launch["binary"],
            args=launch["args"],
            env=merged_env,
            working_dir=launch["working_dir"],
            capture_output=not launch["detached"],
        )

        self._wire(launch["screenshot_dir"],
                   launch["timeout"] if timeout is None else timeout)

        self._session_file = write_session(
            launch["session_file"],
            display=self._resolved_display,
            display_mode=launch["display_mode"],
            dbus_address=self._display.dbus_address,
            app_pid=self._pid,
            binary=launch["binary"],
            working_dir=launch["working_dir"],
            vnc_display=self._display.vnc_display,
        )
        return self._pid

    def close_app(self) -> None:
        """Close the app but keep the display running."""
        self._process.terminate()
        self._accessibility = None
        clear_session(self._session_file)

    @property
    def attached(self) -> bool:
        """True if this session was attach()ed to an app launched elsewhere."""
        return self._process.adopted

    # -----------------------------------------------------------------------
    # Wait
    # -----------------------------------------------------------------------

    def wait_for_idle(self, timeout: float = 10.0) -> None:
        """Wait for the app's CPU usage to settle."""
        self._waiter.wait_for_idle(timeout=timeout)

    def wait_for_element(
        self,
        text: str | None = None,
        role: str | None = None,
        timeout: float = 10.0,
        exact: bool = False,
        include_text: bool = True,
    ) -> ElementInfo:
        """Poll until an AT-SPI element appears."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            elem = self.get_element(text=text, role=role, exact=exact, include_text=include_text)
            if elem is not None:
                return elem
            time.sleep(0.5)
        raise GuiUserError(
            f"Element not found before timeout: text={text!r}, role={role!r}, exact={exact}, include_text={include_text}"
        )

    def wait_for_text_visible(self, text: str, timeout: float = 10.0, exact: bool = False) -> dict | None:
        """Poll until OCR finds text on screen. Returns the match or None on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = self.find_text_on_screen(text, exact=exact)
            if matches:
                return matches[0]
            time.sleep(1.0)
        return None

    def wait_for_element_state(
        self,
        text: str,
        state: str,
        timeout: float = 10.0,
        role: str | None = None,
        exact: bool = False,
        include_text: bool = True,
    ) -> bool:
        """Poll until an AT-SPI element has a specific state (e.g. 'checked', 'enabled')."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            elem = self.get_element(text, role=role, exact=exact, include_text=include_text)
            if elem and state in elem.states:
                return True
            time.sleep(0.5)
        return False

    def wait_for_element_gone(
        self,
        text: str,
        timeout: float = 10.0,
        role: str | None = None,
        exact: bool = False,
        include_text: bool = True,
    ) -> bool:
        """Poll until an AT-SPI element is no longer visible."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            elem = self.get_element(text, role=role, exact=exact, include_text=include_text)
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

    def get_element(
        self,
        text: str | None = None,
        role: str | None = None,
        index: int = 0,
        exact: bool = False,
        include_text: bool = True,
        max_depth: int | None = None,
    ) -> ElementInfo | None:
        """Find an AT-SPI element by text/role. Returns None if not found."""
        self._require_accessibility()
        return self._accessibility.find_element(
            text=text,
            role=role,
            index=index,
            exact=exact,
            include_text=include_text,
            max_depth=max_depth,
        )

    def get_descendant(
        self,
        root_text: str | None = None,
        root_role: str | None = None,
        text: str | None = None,
        role: str | None = None,
        root_index: int = 0,
        index: int = 0,
        root_exact: bool = False,
        exact: bool = False,
        root_include_text: bool = True,
        include_text: bool = True,
        max_depth: int | None = None,
        root_max_depth: int | None = None,
    ) -> ElementInfo | None:
        """Find an AT-SPI element under a matching root subtree."""
        self._require_accessibility()
        return self._accessibility.find_descendant(
            root_text=root_text,
            root_role=root_role,
            text=text,
            role=role,
            root_index=root_index,
            index=index,
            root_exact=root_exact,
            exact=exact,
            root_include_text=root_include_text,
            include_text=include_text,
            max_depth=max_depth,
            root_max_depth=root_max_depth,
        )

    def list_descendants(
        self,
        root_text: str | None = None,
        root_role: str | None = None,
        role: str | None = None,
        name: str | None = None,
        root_index: int = 0,
        root_exact: bool = False,
        exact: bool = False,
        root_include_text: bool = True,
        include_text: bool = True,
        max_depth: int | None = None,
        root_max_depth: int | None = None,
        max_results: int = 0,
    ) -> list[ElementInfo]:
        """List AT-SPI descendants under a matching root subtree."""
        self._require_accessibility()
        return self._accessibility.list_descendants(
            root_text=root_text,
            root_role=root_role,
            role=role,
            name=name,
            root_index=root_index,
            root_exact=root_exact,
            exact=exact,
            root_include_text=root_include_text,
            include_text=include_text,
            max_depth=max_depth,
            root_max_depth=root_max_depth,
            max_results=max_results,
        )

    def find_any_element(
        self,
        texts: list[str],
        role: str | None = None,
        exact: bool = False,
        include_text: bool = True,
    ) -> tuple[str, ElementInfo] | None:
        """Find the first visible AT-SPI element whose text matches any candidate.

        Returns a tuple of `(matched_text, element)` using the candidate string that
        produced the match, or `None` if none of the candidates are found.
        """
        self._require_accessibility()
        return self._accessibility.find_any_element(
            texts=texts,
            role=role,
            exact=exact,
            include_text=include_text,
        )

    def is_element_visible(
        self,
        text: str,
        role: str | None = None,
        exact: bool = False,
        include_text: bool = True,
        max_depth: int | None = None,
    ) -> bool:
        """Check if an AT-SPI element with the given text is visible."""
        self._require_accessibility()
        elem = self._accessibility.find_element(
            text=text,
            role=role,
            exact=exact,
            include_text=include_text,
            max_depth=max_depth,
        )
        return elem is not None and "visible" in elem.states

    def get_element_states(
        self,
        text: str,
        role: str | None = None,
        exact: bool = False,
        include_text: bool = True,
        max_depth: int | None = None,
    ) -> list[str]:
        """Get the state list of an AT-SPI element (e.g. ['enabled', 'checked', 'visible'])."""
        elem = self.get_element(text, role=role, exact=exact, include_text=include_text, max_depth=max_depth)
        return elem.states if elem else []

    def get_element_value(
        self,
        text: str,
        role: str | None = None,
        exact: bool = False,
        include_text: bool = True,
    ) -> float | None:
        """Get the numeric value of a slider/spinbox element."""
        elem = self.get_element(text, role=role, exact=exact, include_text=include_text)
        return elem.value if elem else None

    def get_element_bounds(
        self,
        text: str,
        role: str | None = None,
        exact: bool = False,
        include_text: bool = True,
    ) -> tuple[int, int, int, int] | None:
        """Get (x, y, width, height) bounds of an element."""
        elem = self.get_element(text, role=role, exact=exact, include_text=include_text)
        return elem.bounds if elem else None

    def get_element_center(
        self,
        text: str,
        role: str | None = None,
        exact: bool = False,
        include_text: bool = True,
    ) -> tuple[int, int] | None:
        """Get (x, y) center of an element."""
        elem = self.get_element(text, role=role, exact=exact, include_text=include_text)
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

    def read_text_field(self, field_name: str, exact: bool = False, include_text: bool = True) -> str | None:
        """Read the current text content of a named text field via AT-SPI."""
        self._require_accessibility()
        elem = self._accessibility.find_element(
            text=field_name,
            role="text",
            exact=exact,
            include_text=include_text,
        )
        return elem.text if elem else None

    # -----------------------------------------------------------------------
    # Assertion helpers
    # -----------------------------------------------------------------------

    def assert_element_visible(
        self,
        text: str,
        role: str | None = None,
        message: str = "",
        exact: bool = False,
        include_text: bool = True,
    ) -> ElementInfo:
        """Assert an AT-SPI element is visible. Raises AssertionError if not."""
        elem = self.get_element(text, role=role, exact=exact, include_text=include_text)
        if elem is None or "visible" not in elem.states:
            msg = message or f"Element not visible: text={text!r}, role={role!r}"
            raise AssertionError(msg)
        return elem

    def assert_element_not_visible(
        self,
        text: str,
        role: str | None = None,
        message: str = "",
        exact: bool = False,
        include_text: bool = True,
    ) -> None:
        """Assert an AT-SPI element is NOT visible."""
        elem = self.get_element(text, role=role, exact=exact, include_text=include_text)
        if elem is not None and "visible" in elem.states:
            msg = message or f"Element unexpectedly visible: text={text!r}, role={role!r}"
            raise AssertionError(msg)

    def assert_element_state(
        self,
        text: str,
        state: str,
        role: str | None = None,
        message: str = "",
        exact: bool = False,
        include_text: bool = True,
    ) -> None:
        """Assert an element has a specific state (e.g. 'checked', 'enabled')."""
        states = self.get_element_states(text, role=role, exact=exact, include_text=include_text)
        if state not in states:
            msg = message or f"Element {text!r} does not have state {state!r} (has: {states})"
            raise AssertionError(msg)

    def assert_element_not_state(
        self,
        text: str,
        state: str,
        role: str | None = None,
        message: str = "",
        exact: bool = False,
        include_text: bool = True,
    ) -> None:
        """Assert an element does NOT have a specific state."""
        states = self.get_element_states(text, role=role, exact=exact, include_text=include_text)
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

    def assert_element_value(
        self,
        text: str,
        expected: float,
        role: str | None = None,
        message: str = "",
        exact: bool = False,
        include_text: bool = True,
    ) -> None:
        """Assert an element's numeric value (e.g. slider)."""
        actual = self.get_element_value(text, role=role, exact=exact, include_text=include_text)
        if actual != expected:
            msg = message or f"Element {text!r} value: expected {expected}, got {actual}"
            raise AssertionError(msg)

    # -----------------------------------------------------------------------
    # Interaction — Mouse
    # -----------------------------------------------------------------------

    def click(self, x: int, y: int, button: str = "left") -> None:
        """Click at screen coordinates."""
        self._input.click(x, y, button)

    def click_element(
        self,
        text: str,
        role: str | None = None,
        index: int = 0,
        button: str = "left",
        exact: bool = False,
        include_text: bool = True,
        max_depth: int | None = None,
    ) -> None:
        """Find an AT-SPI element and click its center."""
        self._require_accessibility()
        elem = self._accessibility.find_element(
            text=text,
            role=role,
            index=index,
            exact=exact,
            include_text=include_text,
            max_depth=max_depth,
        )
        if elem is None:
            raise GuiUserError(f"Element not found: text={text!r}, role={role!r}, exact={exact}")
        self._input.click(*elem.center, button)

    def click_descendant(
        self,
        root_text: str | None = None,
        root_role: str | None = None,
        text: str | None = None,
        role: str | None = None,
        root_index: int = 0,
        index: int = 0,
        button: str = "left",
        root_exact: bool = False,
        exact: bool = False,
        root_include_text: bool = True,
        include_text: bool = True,
        max_depth: int | None = None,
        root_max_depth: int | None = None,
    ) -> None:
        """Find an AT-SPI descendant under a matching root subtree and click its center."""
        self._require_accessibility()
        elem = self._accessibility.find_descendant(
            root_text=root_text,
            root_role=root_role,
            text=text,
            role=role,
            root_index=root_index,
            index=index,
            root_exact=root_exact,
            exact=exact,
            root_include_text=root_include_text,
            include_text=include_text,
            max_depth=max_depth,
            root_max_depth=root_max_depth,
        )
        if elem is None:
            raise GuiUserError(
                "Descendant element not found: "
                f"root_text={root_text!r}, root_role={root_role!r}, text={text!r}, role={role!r}, exact={exact}"
            )
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

    def double_click_element(
        self,
        text: str,
        role: str | None = None,
        index: int = 0,
        button: str = "left",
        exact: bool = False,
        include_text: bool = True,
    ) -> None:
        """Find an AT-SPI element and double-click its center."""
        self._require_accessibility()
        elem = self._accessibility.find_element(
            text=text,
            role=role,
            index=index,
            exact=exact,
            include_text=include_text,
        )
        if elem is None:
            raise GuiUserError(f"Element not found: text={text!r}, role={role!r}, exact={exact}")
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
