# gui-user

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An MCP server for external computer-use. Launch, observe, and interact with any X11 application via AT-SPI2 accessibility tree and xdotool input injection.

Unlike in-process testing frameworks, gui-user works externally — it can drive compiled C++ Qt/QML apps, GTK apps, Electron apps, or anything that renders on X11.

## Installation

### 1. System packages

```bash
# Debian/Ubuntu — required
sudo apt install xvfb xdotool at-spi2-core dbus imagemagick libgirepository1.0-dev

# Optional — for VNC observation of the headless display
sudo apt install x11vnc tigervnc-viewer
```

### 2. Install gui-user

Clone the repo and install in development mode:

```bash
git clone <repo-url> gui-user
cd gui-user
pip install -e .
```

This puts `gui-user-mcp` on your `$PATH` as the MCP server entry point.

### 3. Configure Claude Code

Add gui-user as a **user-scope** MCP server (available in all projects):

```bash
claude mcp add gui-user -s user -- gui-user-mcp
```

Or for a **single project only**, run from the project directory:

```bash
claude mcp add gui-user -- gui-user-mcp
```

Alternatively, you can create `.mcp.json` in the project root (this is shared via source control):

```json
{
  "mcpServers": {
    "gui-user": {
      "command": "gui-user-mcp"
    }
  }
}
```

Verify the server is connected:

```bash
claude mcp list
```

If using VS Code, reload the window (Ctrl+Shift+P → "Developer: Reload Window") after adding the server, then start a new conversation. Type `/mcp` in the chat panel to confirm gui-user appears.

## Tools

| Tool | Description |
|---|---|
| `launch_app(binary, args, env, working_dir, width, height, timeout, display_mode, display, vnc)` | Launch any binary under an isolated Xvfb display or a visible local X11 display |
| `close_app()` | Close the app (display session stays alive for reuse) |
| `stop_display()` | Tear down the display session (Xvfb, D-Bus, VNC) |
| `get_app_status()` | Check if app is running, get PID/exit code/stderr |
| `screenshot(output_path?)` | Capture screen as base64 PNG |
| `list_ui_elements(role?, name?, visible_only?)` | Enumerate AT-SPI accessibility tree |
| `find_element(text?, role?, index?)` | Find element by label/role |
| `get_element_info(text?, role?, at_x?, at_y?)` | Detailed element properties or coordinate lookup |
| `click(x, y, button?)` | Click at screen coordinates |
| `click_element(text?, role?, index?, button?)` | Find element and click its center |
| `double_click(x, y, button?)` | Double-click at coordinates |
| `double_click_element(text?, role?, index?, button?)` | Find element and double-click |
| `hover(x, y)` | Move mouse to coordinates |
| `hover_element(text?, role?, index?)` | Move mouse to element center |
| `type_text(text)` | Type text into focused widget |
| `press_key(key, modifiers?)` | Key press (e.g., `press_key("s", ["Ctrl"])`) |
| `wait_for_idle(timeout?)` | Wait for CPU usage to settle |
| `wait_for_element(text?, role?, timeout?)` | Poll until element appears |
| `batch_actions(actions)` | Execute a sequence of actions in one call (avoids per-action round-trips) |

## Example Workflow

```python
# Launch any binary in the default isolated Xvfb session
launch_app(binary="/usr/bin/gnome-calculator")

# Launch on the operator's visible X11 desktop instead
launch_app(
    binary="/usr/bin/gnome-calculator",
    display_mode="local",
)

# Or target a specific local display explicitly
launch_app(
    binary="/usr/bin/gnome-calculator",
    display_mode="local",
    display=":1",
)

# Discover UI elements
list_ui_elements()

# Find and click a button by its visible label
click_element(text="7", role="button")
click_element(text="+", role="button")
click_element(text="3", role="button")
click_element(text="=", role="button")

# Type text
type_text(text="hello world")

# Keyboard shortcuts
press_key(key="s", modifiers=["Ctrl"])

# Screenshot
screenshot(output_path="/tmp/result.png")

# Clean up
close_app()
```

## Architecture

```
AI Assistant (Claude)
    │ MCP Protocol (stdio)
    ▼
MCP Server (main.py)
    │ Orchestrates:
    ├── DisplayManager  (Xvfb/local X11 + D-Bus + AT-SPI)
    ├── ProcessManager  (binary launch/monitor)
    ├── AccessibilityTree (AT-SPI2 element discovery)
    ├── ScreenshotCapture (ImageMagick import)
    ├── InputController (xdotool mouse/keyboard)
    └── IdleWaiter      (CPU-based idle detection)
    │
    ▼
Target Application (any X11 binary)
```

## Key Differences from qt-pilot

This project was forked from [qt-pilot](https://github.com/neatobandit0/qt-pilot) and redesigned:

| | qt-pilot | gui-user |
|---|---|---|
| Target apps | Python/PySide6 only | Any X11 binary |
| Discovery | `objectName` (requires code changes) | AT-SPI accessibility tree (no code changes) |
| Interaction | In-process QTest | External xdotool |
| Architecture | Monkeypatch + socket IPC | External observation + input injection |

## Running Tests

```bash
python3 -m unittest tests.test_integration tests.test_local_display -v
```

## Observing the Headless Display (VNC)

Pass `vnc=True` to `launch_app` to start a view-only VNC server alongside the Xvfb display. This lets the operator watch what the AI is doing without interfering.

```python
launch_app(binary="my_app", vnc=True)
# Response includes: "vnc_display": "localhost:5900"
```

To connect, run from any terminal:

```bash
gui-user-view
```

This auto-detects the running x11vnc and opens a VNC viewer. If x11vnc isn't running yet, it starts one on the first Xvfb display it finds. You can also pass a specific port: `gui-user-view 5902`

To connect manually: `vncviewer localhost:<port>`

**Requirements**: `sudo apt install x11vnc tigervnc-viewer`

### Helper Commands

These are installed on your `$PATH` by `pip install`:

| Command | Description |
|---|---|
| `gui-user-view` | Auto-detect the running Xvfb display and open a VNC viewer. Starts x11vnc if needed. |
| `gui-user-stop` | Kill any lingering Xvfb, x11vnc, and at-spi2-registryd processes. Useful for cleanup after crashes or interrupted sessions. |

The underlying shell scripts (`view-display.sh`, `stop-display.sh`) are also available in the repo root.

### Display Lifecycle

The display session (Xvfb + D-Bus + VNC) persists across app restarts. This means:

- `launch_app()` creates the display on first call, reuses it on subsequent calls
- `close_app()` terminates only the app — the display and VNC stay alive
- `stop_display()` tears down everything (Xvfb, D-Bus, VNC)

This lets the operator connect the VNC viewer once and watch across multiple app launch/close cycles.

### Screenshot Gallery

Every `screenshot()` call auto-saves a timestamped PNG to `.gui-user/screenshots/` in the current working directory. Browse this folder to review the full visual history of a session.

## Local Display Mode

`display_mode="local"` reuses a real X11 display so the operator can watch the app while the MCP drives it.

- This mode is opt-in. The default remains an isolated `Xvfb` session.
- Local mode is intended for X11 or XWayland displays only.
- Mouse, keyboard, and focus are shared with the operator, so runs are less deterministic.
- `width` and `height` are ignored in local mode because the existing desktop geometry is reused.
- For unattended or CI-style runs, prefer the default `Xvfb` mode.

## License

MIT License - see [LICENSE](LICENSE) file.
