#!/usr/bin/env python3
"""gui-user: External Computer-Use MCP Server.

Launches, observes, and interacts with arbitrary X11 applications
via AT-SPI2 accessibility tree and xdotool input injection.

IMPORTANT: This is a stdio MCP server - NEVER use print() or write to stdout!
All logging must go to stderr or a file.
"""

import functools
import logging
import os
import sys
import time
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP

from .accessibility import AccessibilityTree
from .deps import check_dependencies
from .display import DisplayManager
from .errors import AppNotRunning, GuiUserError
from .input import InputController
from .process import ProcessManager
from .screenshot import ScreenshotCapture
from .wait import IdleWaiter

# Configure logging to stderr (NEVER stdout for stdio MCP servers)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("gui-user")

# Validate system dependencies at import time
check_dependencies()

# Create MCP server
mcp = FastMCP("gui-user")


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

@dataclass
class AppSession:
    display: DisplayManager
    process: ProcessManager
    accessibility: AccessibilityTree | None
    input: InputController
    screenshot: ScreenshotCapture
    waiter: IdleWaiter


_session: AppSession | None = None


def _require_session() -> AppSession:
    global _session
    if _session is None:
        raise AppNotRunning("No app is running. Call launch_app first.")
    return _session


def _require_accessibility(s: AppSession) -> AccessibilityTree:
    if s.accessibility is None:
        raise AppNotRunning(
            "AT-SPI accessibility not available for this app. "
            "Use screenshot() + coordinates instead."
        )
    return s.accessibility


def _handle_errors(func):
    """Decorator: catch GuiUserError subclasses → {success: False, message: ...}."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except GuiUserError as e:
            return {"success": False, "message": str(e)}
    return wrapper


# ---------------------------------------------------------------------------
# App Lifecycle
# ---------------------------------------------------------------------------

@mcp.tool()
@_handle_errors
def launch_app(
    binary: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    working_dir: str | None = None,
    width: int = 1280,
    height: int = 1024,
    timeout: float = 10.0,
) -> dict:
    """Launch any application under a virtual X11 display.

    Args:
        binary: Path to executable or name on PATH (e.g., "my_qt_app", "python3").
        args: Command-line arguments for the binary.
        env: Extra environment variables (merged with display env).
        working_dir: Working directory for the process.
        width: Virtual display width in pixels.
        height: Virtual display height in pixels.
        timeout: Seconds to wait for the app to register with AT-SPI.
    """
    global _session

    # Close existing session if any
    if _session is not None:
        close_app()

    dm = DisplayManager()
    display = dm.start(width=width, height=height)

    # Merge environments: os.environ + display env + user overrides
    merged_env = {**os.environ, **dm.env, **(env or {})}

    pm = ProcessManager()
    pid = pm.launch(binary, args=args or [], env=merged_env, working_dir=working_dir)
    logger.info(f"App launched: {binary} (pid={pid}, display={display})")

    # Try to connect AT-SPI (retry until timeout)
    accessibility = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1.0)
        if pm.poll() is not None:
            stdout, stderr = pm.get_output()
            pm.terminate()
            dm.stop()
            return {
                "success": False,
                "message": f"App exited immediately. stderr: {stderr[:500]}",
            }
        try:
            accessibility = AccessibilityTree(pid=pid, display_env=dm.env)
            break
        except Exception as e:
            logger.debug(f"AT-SPI not ready yet: {e}")

    if accessibility is None:
        logger.warning("AT-SPI not available; running in screenshot-only mode")

    _session = AppSession(
        display=dm,
        process=pm,
        accessibility=accessibility,
        input=InputController(display),
        screenshot=ScreenshotCapture(display),
        waiter=IdleWaiter(pid),
    )

    return {
        "success": True,
        "message": "App launched" + (" (screenshot-only mode)" if accessibility is None else ""),
        "pid": pid,
        "display": display,
    }


@mcp.tool()
@_handle_errors
def close_app() -> dict:
    """Close the running application and virtual display."""
    global _session
    if _session is None:
        return {"success": True, "message": "No app was running"}

    _session.process.terminate()
    _session.display.stop()
    _session = None
    return {"success": True, "message": "App closed"}


@mcp.tool()
@_handle_errors
def get_app_status() -> dict:
    """Check if the application is running and get diagnostics."""
    if _session is None:
        return {"running": False, "pid": None, "exit_code": None,
                "display": None, "stdout": "", "stderr": ""}

    stdout, stderr = _session.process.get_output()
    return {
        "running": _session.process.is_running,
        "pid": _session.process.pid,
        "exit_code": _session.process.poll(),
        "display": _session.display.display,
        "stdout": stdout[-500:] if stdout else "",
        "stderr": stderr[-500:] if stderr else "",
    }


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

@mcp.tool()
@_handle_errors
def screenshot(output_path: str | None = None) -> dict:
    """Capture a screenshot of the application.

    Args:
        output_path: Optional file path to save the PNG.

    Returns base64-encoded PNG image data.
    """
    s = _require_session()
    b64 = s.screenshot.capture_base64()
    if output_path:
        s.screenshot.capture_to_file(output_path)
    return {"success": True, "image_base64": b64, "path": output_path}


@mcp.tool()
@_handle_errors
def list_ui_elements(
    role: str | None = None,
    name: str | None = None,
    visible_only: bool = True,
) -> dict:
    """List UI elements from the accessibility tree.

    Args:
        role: Filter by role substring (e.g., "button", "text", "label").
        name: Filter by name/label substring.
        visible_only: Only return visible elements.
    """
    s = _require_session()
    tree = _require_accessibility(s)
    elements = tree.list_elements(filter_role=role, filter_name=name, visible_only=visible_only)
    return {
        "success": True,
        "elements": [e.to_dict() for e in elements],
        "count": len(elements),
    }


@mcp.tool()
@_handle_errors
def find_element(
    text: str | None = None,
    role: str | None = None,
    index: int = 0,
) -> dict:
    """Find a UI element by text content and/or role.

    Args:
        text: Text to search for in element name or text content.
        role: Role substring to match (e.g., "button").
        index: Return the nth match (0-based).
    """
    s = _require_session()
    tree = _require_accessibility(s)
    elem = tree.find_element(text=text, role=role, index=index)
    if elem is None:
        return {"success": False, "message": f"Element not found (text={text!r}, role={role!r})"}
    return {"success": True, "element": elem.to_dict()}


@mcp.tool()
@_handle_errors
def get_element_info(
    text: str | None = None,
    role: str | None = None,
    at_x: int | None = None,
    at_y: int | None = None,
) -> dict:
    """Get detailed info about an element by text/role or coordinates.

    Args:
        text: Text to search for in element name or content.
        role: Role substring to match.
        at_x: X screen coordinate (use with at_y for coordinate lookup).
        at_y: Y screen coordinate.
    """
    s = _require_session()
    tree = _require_accessibility(s)

    if at_x is not None and at_y is not None:
        elem = tree.get_element_at(at_x, at_y)
    else:
        elem = tree.find_element(text=text, role=role)

    if elem is None:
        return {"success": False, "message": "Element not found"}
    return {"success": True, "element": elem.to_dict()}


# ---------------------------------------------------------------------------
# Interaction — Mouse
# ---------------------------------------------------------------------------

@mcp.tool()
@_handle_errors
def click(x: int, y: int, button: str = "left") -> dict:
    """Click at screen coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        button: "left", "right", or "middle".
    """
    s = _require_session()
    s.input.click(x, y, button)
    return {"success": True, "message": f"Clicked at ({x}, {y})"}


@mcp.tool()
@_handle_errors
def click_element(
    text: str | None = None,
    role: str | None = None,
    index: int = 0,
    button: str = "left",
) -> dict:
    """Find a UI element and click its center.

    Args:
        text: Text to search for.
        role: Role substring (e.g., "button").
        index: Which match to click (0-based).
        button: "left", "right", or "middle".
    """
    s = _require_session()
    tree = _require_accessibility(s)
    elem = tree.find_element(text=text, role=role, index=index)
    if elem is None:
        return {"success": False, "message": f"Element not found (text={text!r}, role={role!r})"}
    s.input.click(*elem.center, button)
    return {"success": True, "message": f"Clicked [{elem.role}] {elem.name!r} at {elem.center}"}


@mcp.tool()
@_handle_errors
def double_click(x: int, y: int, button: str = "left") -> dict:
    """Double-click at screen coordinates."""
    s = _require_session()
    s.input.double_click(x, y, button)
    return {"success": True, "message": f"Double-clicked at ({x}, {y})"}


@mcp.tool()
@_handle_errors
def double_click_element(
    text: str | None = None,
    role: str | None = None,
    index: int = 0,
    button: str = "left",
) -> dict:
    """Find a UI element and double-click its center."""
    s = _require_session()
    tree = _require_accessibility(s)
    elem = tree.find_element(text=text, role=role, index=index)
    if elem is None:
        return {"success": False, "message": f"Element not found (text={text!r}, role={role!r})"}
    s.input.double_click(*elem.center, button)
    return {"success": True, "message": f"Double-clicked [{elem.role}] {elem.name!r}"}


@mcp.tool()
@_handle_errors
def hover(x: int, y: int) -> dict:
    """Move the mouse to screen coordinates."""
    s = _require_session()
    s.input.mouse_move(x, y)
    return {"success": True, "message": f"Moved mouse to ({x}, {y})"}


@mcp.tool()
@_handle_errors
def hover_element(
    text: str | None = None,
    role: str | None = None,
    index: int = 0,
) -> dict:
    """Find a UI element and move the mouse to its center."""
    s = _require_session()
    tree = _require_accessibility(s)
    elem = tree.find_element(text=text, role=role, index=index)
    if elem is None:
        return {"success": False, "message": f"Element not found (text={text!r}, role={role!r})"}
    s.input.mouse_move(*elem.center)
    return {"success": True, "message": f"Hovered [{elem.role}] {elem.name!r}"}


# ---------------------------------------------------------------------------
# Interaction — Keyboard
# ---------------------------------------------------------------------------

@mcp.tool()
@_handle_errors
def type_text(text: str) -> dict:
    """Type text into the currently focused widget.

    Args:
        text: The text to type.
    """
    s = _require_session()
    s.input.type_text(text)
    return {"success": True, "message": f"Typed {len(text)} characters"}


@mcp.tool()
@_handle_errors
def press_key(key: str, modifiers: list[str] | None = None) -> dict:
    """Press a key, optionally with modifiers.

    Args:
        key: Key name (e.g., "Enter", "Tab", "Escape", "a", "F1").
        modifiers: Optional list of modifiers ("Ctrl", "Shift", "Alt", "Meta").
    """
    s = _require_session()
    s.input.press_key(key, modifiers)
    mod_str = "+".join(modifiers) + "+" if modifiers else ""
    return {"success": True, "message": f"Pressed {mod_str}{key}"}


# ---------------------------------------------------------------------------
# Wait
# ---------------------------------------------------------------------------

@mcp.tool()
@_handle_errors
def wait_for_idle(timeout: float = 5.0) -> dict:
    """Wait for the application's CPU usage to settle.

    Args:
        timeout: Maximum seconds to wait.
    """
    s = _require_session()
    s.waiter.wait_for_idle(timeout=timeout)
    return {"success": True, "message": "App is idle"}


@mcp.tool()
@_handle_errors
def wait_for_element(
    text: str | None = None,
    role: str | None = None,
    timeout: float = 10.0,
) -> dict:
    """Poll until a UI element appears in the accessibility tree.

    Args:
        text: Text to search for.
        role: Role substring to match.
        timeout: Maximum seconds to wait.
    """
    s = _require_session()
    tree = _require_accessibility(s)
    elem = s.waiter.wait_for_element(tree, text=text, role=role, timeout=timeout)
    return {"success": True, "element": elem.to_dict()}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logger.info("Starting gui-user MCP Server")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
