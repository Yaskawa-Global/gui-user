# Qt Pilot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An MCP server for headless Qt/PySide6 GUI testing. Enables AI assistants like Claude to visually test and interact with Qt desktop applications.

**Repository:** [github.com/neatobandit0/qt-pilot](https://github.com/neatobandit0/qt-pilot)

## Features

- **Launch Qt apps headlessly** via Xvfb virtual display
- **Capture screenshots** for visual verification
- **Simulate interactions**: clicks, hovers, keyboard input
- **Widget discovery** by object name
- **App health monitoring** with stderr capture
- **Full Qt introspection** via QTest and Qt APIs

## Installation

### From GitHub

```bash
git clone https://github.com/neatobandit0/qt-pilot.git ~/.claude/plugins/qt-pilot
pip install -r ~/.claude/plugins/qt-pilot/requirements.txt
```

### Manual Installation

Copy the plugin to your Claude plugins directory:
```bash
cp -r qt-pilot ~/.claude/plugins/
```

Then add to your `~/.claude.json`:
```json
{
  "mcpServers": {
    "qt-pilot": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/qt-pilot/server/main.py"]
    }
  }
}
```

### Dependencies

```bash
pip install mcp PySide6
```

Also requires `Xvfb` for headless display:
```bash
# Debian/Ubuntu
sudo apt install xvfb

# RHEL/CentOS/Fedora
sudo yum install xorg-x11-server-Xvfb

# macOS (via Homebrew)
brew install xquartz
```

## MCP Tools

### `launch_app`
Launch a Qt application headlessly.

```python
# Script mode
launch_app(script_path="/path/to/test_gui.py")

# Module mode
launch_app(module="myapp.main", working_dir="/path/to/project")
```

### `capture_screenshot`
Capture the current window.

```python
capture_screenshot(output_path="/tmp/screenshot.png")
```

### `click_widget`
Click a widget by its object name.

```python
click_widget(widget_name="submit_button", button="left")
```

### `hover_widget`
Hover over a widget.

```python
hover_widget(widget_name="menu_item")
```

### `type_text`
Type text into a widget or focused widget.

```python
type_text(text="hello world", widget_name="search_input")
type_text(text="hello")  # Types into currently focused widget
```

### `press_key`
Simulate a key press with optional modifiers.

```python
press_key(key="Enter")
press_key(key="S", modifiers=["Ctrl"])  # Ctrl+S
press_key(key="Tab")
```

### `find_widgets`
List widgets matching a name pattern.

```python
find_widgets(name_pattern="*")  # All named widgets
find_widgets(name_pattern="btn_*")  # Widgets starting with "btn_"
```

### `get_widget_info`
Get detailed widget information.

```python
get_widget_info(widget_name="submit_button")
# Returns: type, visible, enabled, size, position, text, checked state, etc.
```

### `get_app_status`
Check if the application is still running and get diagnostics.

```python
get_app_status()
# Returns: {"running": true, "exit_code": null, "stderr": "", "display": ":99"}
```

### `wait_for_idle`
Wait for the Qt event queue to settle after actions.

```python
click_widget(widget_name="load_button")
wait_for_idle(timeout=5.0)  # Wait for async operations to complete
capture_screenshot()
```

### `close_app`
Close the running application.

```python
close_app()
```

## Requirements for Target Applications

For widget interactions to work, your Qt application must:

1. **Set object names** on interactive widgets:
   ```python
   button = QPushButton("Click Me")
   button.setObjectName("my_button")  # Required for widget discovery
   ```

2. **Use QApplication** (not QCoreApplication)

3. **Show at least one window**

## Architecture

```
┌─────────────────────────────┐
│  AI Assistant (Claude)      │
└─────────────┬───────────────┘
              │ MCP Protocol (stdio)
              ▼
┌─────────────────────────────┐
│  MCP Server (main.py)       │
│  - Tool definitions         │
│  - Process management       │
└─────────────┬───────────────┘
              │ Unix Socket (IPC)
              ▼
┌─────────────────────────────┐
│  Test Harness (harness.py)  │
│  - Runs inside Xvfb         │
│  - QTest interactions       │
│  - Widget introspection     │
├─────────────────────────────┤
│  Your Qt Application        │
└─────────────────────────────┘
```

## Example Workflow

```python
# 1. Launch a test app
launch_app(module="myapp.main", working_dir="/path/to/project")

# 2. List available widgets
find_widgets()

# 3. Interact with the UI
click_widget(widget_name="login_button")
wait_for_idle()

# 4. Type into a field
type_text(text="user@example.com", widget_name="email_input")
press_key(key="Tab")
type_text(text="password123", widget_name="password_input")

# 5. Submit and capture result
click_widget(widget_name="submit_button")
wait_for_idle(timeout=3.0)
capture_screenshot(output_path="/tmp/result.png")

# 6. Clean up
close_app()
```

## Troubleshooting

### "Widget not found"
- Ensure the widget has `setObjectName()` called
- Use `find_widgets()` to list available widget names

### "No app is running"
- Call `launch_app()` first
- Check that the script/module path is correct

### App crashes silently
- Use `get_app_status()` to check for errors
- The `stderr` field contains crash information

### Screenshots are blank
- Ensure the application creates and shows a window
- Use `wait_for_idle()` after launch for window to render

## License

MIT License - see [LICENSE](LICENSE) file.
