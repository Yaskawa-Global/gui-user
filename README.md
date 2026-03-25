# gui-user

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An MCP server for external computer-use. Launch, observe, and interact with any X11 application via AT-SPI2 accessibility tree and xdotool input injection.

Unlike in-process testing frameworks, gui-user works externally — it can drive compiled C++ Qt/QML apps, GTK apps, Electron apps, or anything that renders on X11.

## Prerequisites

### System packages

```bash
# Debian/Ubuntu
sudo apt install xvfb xdotool at-spi2-core dbus imagemagick libgirepository1.0-dev
```

### Python packages

```bash
pip install -r requirements.txt
```

## MCP Configuration

Add to your `.mcp.json` or Claude Code settings:

```json
{
  "gui-user": {
    "command": "python3",
    "args": ["/path/to/gui-user/server/main.py"]
  }
}
```

## Tools

| Tool | Description |
|---|---|
| `launch_app(binary, args, env, working_dir, width, height, timeout, display_mode, display)` | Launch any binary under an isolated Xvfb display or a visible local X11 display |
| `close_app()` | Terminate the app and display |
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

## Local Display Mode

`display_mode="local"` reuses a real X11 display so the operator can watch the app while the MCP drives it.

- This mode is opt-in. The default remains an isolated `Xvfb` session.
- Local mode is intended for X11 or XWayland displays only.
- Mouse, keyboard, and focus are shared with the operator, so runs are less deterministic.
- `width` and `height` are ignored in local mode because the existing desktop geometry is reused.
- For unattended or CI-style runs, prefer the default `Xvfb` mode.

## License

MIT License - see [LICENSE](LICENSE) file.
