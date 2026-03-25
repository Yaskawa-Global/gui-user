#!/usr/bin/env python3
"""gui-user: External Computer-Use MCP Server.

Launches, observes, and interacts with arbitrary X11 applications
via AT-SPI2 accessibility tree and xdotool input injection.

IMPORTANT: This is a stdio MCP server - NEVER use print() or write to stdout!
All logging must go to stderr or a file.
"""

import asyncio
import functools
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Literal

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
#
# Display session is long-lived (persists across app launch/close cycles).
# App state is short-lived (created per launch_app, cleared on close_app).
# ---------------------------------------------------------------------------

@dataclass
class AppState:
    process: ProcessManager
    accessibility: AccessibilityTree | None
    input: InputController
    screenshot: ScreenshotCapture
    waiter: IdleWaiter


@dataclass
class DisplaySession:
    manager: DisplayManager
    resolved_display: str


_display: DisplaySession | None = None
_app: AppState | None = None


def _require_display() -> DisplaySession:
    if _display is None:
        raise AppNotRunning("No display session. Call launch_app first.")
    return _display


def _require_app() -> AppState:
    if _app is None:
        raise AppNotRunning("No app is running. Call launch_app first.")
    return _app


def _require_accessibility(s: AppState) -> AccessibilityTree:
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
async def launch_app(
    binary: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    working_dir: str | None = None,
    width: int = 1280,
    height: int = 1024,
    timeout: float = 10.0,
    display_mode: Literal["xvfb", "local"] = "xvfb",
    display: str | None = None,
    vnc: bool = False,
) -> dict:
    """Launch any application under an isolated or local X11 display.

    The display session (Xvfb + D-Bus) persists across app restarts —
    only the app process is replaced on each launch_app call.
    Call stop_display() to tear down the display when you're done.

    Args:
        binary: Path to executable or name on PATH (e.g., "my_qt_app", "python3").
        args: Command-line arguments for the binary.
        env: Extra environment variables (merged with display env).
        working_dir: Working directory for the process.
        width: Virtual display width in pixels for Xvfb mode.
        height: Virtual display height in pixels for Xvfb mode.
        timeout: Seconds to wait for the app to register with AT-SPI.
        display_mode: "xvfb" for an isolated virtual display, "local" to reuse a visible X11 display.
        display: Explicit X11 display string for local mode (e.g. ":0"). Defaults to inherited DISPLAY.
        vnc: Start x11vnc for view-only observation of the Xvfb display (ignored in local mode).
    """
    global _display, _app

    try:
        # Close existing app if any (but keep the display)
        if _app is not None:
            _close_app_only()

        # Reuse existing display if compatible, otherwise create a new one
        if _display is not None:
            dm = _display.manager
            if dm.display_mode != display_mode or not dm.is_running:
                _stop_display_only()
                _display = None

        if _display is None:
            dm = DisplayManager()
            resolved_display = dm.start(
                width=width,
                height=height,
                mode=display_mode,
                display=display,
            )
            if vnc and display_mode != "local":
                dm.start_vnc()
            _display = DisplaySession(manager=dm, resolved_display=resolved_display)
        else:
            dm = _display.manager
            resolved_display = _display.resolved_display
            # Start VNC if requested and not already running
            if vnc and display_mode != "local" and not dm.vnc_running:
                dm.start_vnc()

        # Merge environments: os.environ + display env + user overrides
        merged_env = {**os.environ, **dm.env, **(env or {})}

        pm = ProcessManager()
        pid = pm.launch(binary, args=args or [], env=merged_env, working_dir=working_dir)
        logger.info(
            "App launched: %s (pid=%s, mode=%s, display=%s)",
            binary, pid, dm.display_mode, resolved_display,
        )

        # Try to connect AT-SPI (retry until timeout)
        accessibility = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            if pm.poll() is not None:
                stdout, stderr = pm.get_output()
                pm.terminate()
                return {
                    "success": False,
                    "message": f"App exited immediately. stderr: {stderr[:500]}",
                    "display_mode": dm.display_mode,
                    "display": resolved_display,
                    "warnings": dm.warnings,
                }
            try:
                accessibility = AccessibilityTree(pid=pid, display_env=dm.env)
                break
            except Exception as e:
                logger.debug(f"AT-SPI not ready yet: {e}")

        if accessibility is None:
            logger.warning("AT-SPI not available; running in screenshot-only mode")

        _app = AppState(
            process=pm,
            accessibility=accessibility,
            input=InputController(
                resolved_display, pid=pid,
                activate_on_keyboard=(dm.display_mode == "local"),
            ),
            screenshot=ScreenshotCapture(resolved_display, pid=pid),
            waiter=IdleWaiter(pid),
        )

        message = "App launched"
        if accessibility is None:
            message += " (screenshot-only mode)"

        result = {
            "success": True,
            "message": message,
            "pid": pid,
            "display_mode": dm.display_mode,
            "display": resolved_display,
            "warnings": dm.warnings,
        }
        if dm.vnc_running:
            result["vnc_display"] = dm.vnc_display
        return result
    except GuiUserError as e:
        return {"success": False, "message": str(e)}


def _close_app_only() -> None:
    """Terminate the app process without touching the display."""
    global _app
    if _app is not None:
        _app.process.terminate()
        _app = None


def _stop_display_only() -> None:
    """Tear down the display session."""
    global _display
    if _display is not None:
        _display.manager.stop()
        _display = None


@mcp.tool()
@_handle_errors
def close_app() -> dict:
    """Close the running application (the display session stays alive for reuse)."""
    global _app
    if _app is None:
        return {"success": True, "message": "No app was running"}
    _close_app_only()
    return {"success": True, "message": "App closed (display still running)"}


@mcp.tool()
@_handle_errors
def stop_display() -> dict:
    """Tear down the display session (Xvfb, D-Bus, VNC). Also closes any running app."""
    _close_app_only()
    _stop_display_only()
    return {"success": True, "message": "Display session stopped"}


@mcp.tool()
@_handle_errors
def get_app_status() -> dict:
    """Check if the application is running and get diagnostics."""
    if _app is None:
        display_info = {}
        if _display is not None:
            display_info = {
                "display_mode": _display.manager.display_mode,
                "display": _display.resolved_display,
                "display_running": _display.manager.is_running,
                "vnc_display": _display.manager.vnc_display if _display.manager.vnc_running else None,
            }
        return {"running": False, "pid": None, "exit_code": None,
                "stdout": "", "stderr": "", **display_info}

    stdout, stderr = _app.process.get_output()
    dm = _display.manager if _display else None
    return {
        "running": _app.process.is_running,
        "pid": _app.process.pid,
        "exit_code": _app.process.poll(),
        "display_mode": dm.display_mode if dm else None,
        "display": _display.resolved_display if _display else None,
        "vnc_display": dm.vnc_display if dm and dm.vnc_running else None,
        "stdout": stdout[-500:] if stdout else "",
        "stderr": stderr[-500:] if stderr else "",
    }


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

@mcp.tool()
@_handle_errors
def screenshot(
    output_path: str | None = None,
    region_x: int | None = None,
    region_y: int | None = None,
    region_width: int | None = None,
    region_height: int | None = None,
) -> dict:
    """Capture a screenshot of the application.

    Every screenshot is auto-saved to .gui-user/screenshots/
    in the current working directory with a timestamp filename.
    Returns the file path (use Read tool to view the image).

    Args:
        output_path: Optional additional file path to save the PNG.
        region_x: X coordinate of crop region (use all four region_* params together).
        region_y: Y coordinate of crop region.
        region_width: Width of crop region.
        region_height: Height of crop region.
    """
    s = _require_app()
    region = None
    if all(v is not None for v in (region_x, region_y, region_width, region_height)):
        region = (region_x, region_y, region_width, region_height)
    png_bytes = s.screenshot.capture(region=region)

    if output_path:
        with open(output_path, "wb") as f:
            f.write(png_bytes)

    # Auto-save to per-project gallery
    gallery_dir = os.path.join(os.getcwd(), ".gui-user", "screenshots")
    os.makedirs(gallery_dir, exist_ok=True)
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]
    gallery_path = os.path.join(gallery_dir, f"{ts}.png")
    with open(gallery_path, "wb") as f:
        f.write(png_bytes)

    return {"success": True, "gallery_path": gallery_path}


@mcp.tool()
@_handle_errors
def list_ui_elements(
    role: str | None = None,
    name: str | None = None,
    visible_only: bool = True,
    max_results: int = 0,
) -> dict:
    """List UI elements from the accessibility tree.

    Args:
        role: Filter by role substring (e.g., "button", "text", "label").
        name: Filter by name/label substring.
        visible_only: Only return visible elements (also skips invisible subtrees).
        max_results: Stop after this many matches (0 = unlimited).
    """
    s = _require_app()
    tree = _require_accessibility(s)
    elements = tree.list_elements(filter_role=role, filter_name=name, visible_only=visible_only, max_results=max_results)
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
    s = _require_app()
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
    s = _require_app()
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
    s = _require_app()
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
    s = _require_app()
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
    s = _require_app()
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
    s = _require_app()
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
    s = _require_app()
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
    s = _require_app()
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
    s = _require_app()
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
    s = _require_app()
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
    s = _require_app()
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
    s = _require_app()
    tree = _require_accessibility(s)
    elem = s.waiter.wait_for_element(tree, text=text, role=role, timeout=timeout)
    return {"success": True, "element": elem.to_dict()}


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

@mcp.tool()
def batch_actions(actions: list[dict]) -> dict:
    """Execute a sequence of UI actions in one call, returning all results.

    This avoids per-action round-trips for multi-step interactions.
    Each action is a dict with an "action" key and parameters matching
    the corresponding individual tool.

    Supported actions:
        {"action": "click", "x": int, "y": int, "button"?: str}
        {"action": "click_element", "text"?: str, "role"?: str, "index"?: int, "button"?: str}
        {"action": "double_click", "x": int, "y": int, "button"?: str}
        {"action": "double_click_element", "text"?: str, "role"?: str, "index"?: int, "button"?: str}
        {"action": "hover", "x": int, "y": int}
        {"action": "hover_element", "text"?: str, "role"?: str, "index"?: int}
        {"action": "type_text", "text": str}
        {"action": "press_key", "key": str, "modifiers"?: list[str]}
        {"action": "screenshot"}
        {"action": "wait", "ms": int}
        {"action": "wait_for_idle", "timeout"?: float}
        {"action": "wait_for_element", "text"?: str, "role"?: str, "timeout"?: float}

    Returns a list of results, one per action. Execution stops on the first
    failure and remaining actions are skipped.

    Example:
        batch_actions([
            {"action": "click_element", "text": "File", "role": "menu"},
            {"action": "wait", "ms": 300},
            {"action": "click_element", "text": "Save", "role": "menu item"},
            {"action": "screenshot"},
        ])
    """
    try:
        s = _require_app()
    except GuiUserError as e:
        return {"success": False, "message": str(e), "results": []}

    results = []
    dispatchers = _batch_dispatchers()

    for i, action_def in enumerate(actions):
        action_name = action_def.get("action")
        if not action_name:
            results.append({"success": False, "message": f"Action {i}: missing 'action' key"})
            return {"success": False, "message": f"Failed at action {i}", "results": results}

        dispatcher = dispatchers.get(action_name)
        if dispatcher is None:
            results.append({"success": False, "message": f"Action {i}: unknown action {action_name!r}"})
            return {"success": False, "message": f"Failed at action {i}", "results": results}

        try:
            params = {k: v for k, v in action_def.items() if k != "action"}
            result = dispatcher(s, **params)
            results.append(result)
            if not result.get("success", True):
                return {"success": False, "message": f"Failed at action {i}: {result.get('message', '')}", "results": results}
        except GuiUserError as e:
            results.append({"success": False, "message": str(e)})
            return {"success": False, "message": f"Failed at action {i}: {e}", "results": results}

    return {"success": True, "results": results}


def _batch_dispatchers() -> dict:
    """Map action names to functions that take (AppState, **params) → dict."""

    def _click(s, x, y, button="left"):
        s.input.click(x, y, button)
        return {"success": True, "message": f"Clicked ({x}, {y})"}

    def _click_element(s, text=None, role=None, index=0, button="left"):
        tree = _require_accessibility(s)
        elem = tree.find_element(text=text, role=role, index=index)
        if elem is None:
            return {"success": False, "message": f"Element not found (text={text!r}, role={role!r})"}
        s.input.click(*elem.center, button)
        return {"success": True, "message": f"Clicked [{elem.role}] {elem.name!r} at {elem.center}"}

    def _double_click(s, x, y, button="left"):
        s.input.double_click(x, y, button)
        return {"success": True, "message": f"Double-clicked ({x}, {y})"}

    def _double_click_element(s, text=None, role=None, index=0, button="left"):
        tree = _require_accessibility(s)
        elem = tree.find_element(text=text, role=role, index=index)
        if elem is None:
            return {"success": False, "message": f"Element not found (text={text!r}, role={role!r})"}
        s.input.double_click(*elem.center, button)
        return {"success": True, "message": f"Double-clicked [{elem.role}] {elem.name!r}"}

    def _hover(s, x, y):
        s.input.mouse_move(x, y)
        return {"success": True, "message": f"Hovered ({x}, {y})"}

    def _hover_element(s, text=None, role=None, index=0):
        tree = _require_accessibility(s)
        elem = tree.find_element(text=text, role=role, index=index)
        if elem is None:
            return {"success": False, "message": f"Element not found (text={text!r}, role={role!r})"}
        s.input.mouse_move(*elem.center)
        return {"success": True, "message": f"Hovered [{elem.role}] {elem.name!r}"}

    def _type_text(s, text):
        s.input.type_text(text)
        return {"success": True, "message": f"Typed {len(text)} chars"}

    def _press_key(s, key, modifiers=None):
        s.input.press_key(key, modifiers)
        mod_str = "+".join(modifiers) + "+" if modifiers else ""
        return {"success": True, "message": f"Pressed {mod_str}{key}"}

    def _screenshot(s):
        png_bytes = s.screenshot.capture()
        gallery_dir = os.path.join(os.getcwd(), ".gui-user", "screenshots")
        os.makedirs(gallery_dir, exist_ok=True)
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]
        gallery_path = os.path.join(gallery_dir, f"{ts}.png")
        with open(gallery_path, "wb") as f:
            f.write(png_bytes)
        return {"success": True, "gallery_path": gallery_path}

    def _wait(s, ms=500):
        time.sleep(ms / 1000.0)
        return {"success": True, "message": f"Waited {ms}ms"}

    def _wait_for_idle(s, timeout=5.0):
        s.waiter.wait_for_idle(timeout=timeout)
        return {"success": True, "message": "App is idle"}

    def _wait_for_element(s, text=None, role=None, timeout=10.0):
        tree = _require_accessibility(s)
        elem = s.waiter.wait_for_element(tree, text=text, role=role, timeout=timeout)
        return {"success": True, "element": elem.to_dict()}

    return {
        "click": _click,
        "click_element": _click_element,
        "double_click": _double_click,
        "double_click_element": _double_click_element,
        "hover": _hover,
        "hover_element": _hover_element,
        "type_text": _type_text,
        "press_key": _press_key,
        "screenshot": _screenshot,
        "wait": _wait,
        "wait_for_idle": _wait_for_idle,
        "wait_for_element": _wait_for_element,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logger.info("Starting gui-user MCP Server")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
