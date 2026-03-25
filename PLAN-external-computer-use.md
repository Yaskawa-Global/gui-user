# Overall Plan: Refactor gui-user to External Computer-Use Architecture

## Context

The existing qt-pilot MCP server is tightly coupled to Python/PySide6 apps: it monkeypatches `QApplication.__init__` to inject an in-process harness that uses `QTest` for interaction and `QWidget.findChild(name)` for discovery. None of this works for compiled C++ Qt/QML binaries.

**Goal**: Replace the in-process approach with an external "computer-use" architecture that can drive **any X11 application** — compiled C++ Qt, QML, GTK, Electron, etc. — using AT-SPI2 for structured UI discovery and xdotool for input injection, with screenshot+vision as a complementary observation channel.

**Key architectural shift**: From in-process introspection → external observation + interaction.

---

## New Module Structure

```
server/
  main.py               (REWRITE - MCP tools as thin orchestrators)
  display.py             (NEW - Xvfb + D-Bus session lifecycle)
  process.py             (NEW - binary launch/monitor/terminate)
  accessibility.py       (NEW - AT-SPI2 tree queries)
  input.py               (NEW - xdotool X11 input injection)
  screenshot.py          (NEW - X11 screenshot capture)
  wait.py                (NEW - idle detection / polling)
  errors.py              (NEW - exception hierarchy)
  harness.py             (DELETE)
```

---

## Phase 0: Preparation & Infrastructure ✅ COMPLETED

### 0.1 Create `errors.py`
- Exception classes: `AppNotRunning`, `ElementNotFound`, `DisplayError`, `InputError`, `AccessibilityError`, `TimeoutError`

### 0.2 Update `requirements.txt`
- Remove `PySide6>=6.6.0`
- Add `PyGObject>=3.42.0`, `Pillow>=9.0.0`
- Document system deps in comments: `xdotool`, `at-spi2-core`, `Xvfb`

### 0.3 Dependency check function
- Startup validation: check for `Xvfb`, `xdotool`, `dbus-daemon`, `gi.repository.Atspi` importability
- Return actionable error messages (e.g., "Install xdotool: `sudo apt install xdotool`")

---

## Phase 1: Launch Layer (Display + Process)

### 1.1 `display.py` — Xvfb + D-Bus Session

**Critical risk**: AT-SPI requires a D-Bus session bus. Must validate this works inside Xvfb early.

- `DisplayManager` class:
  - `start(width, height, depth, display_num)` → returns `:NN`
  - `stop()`, `is_running`, `display`, `env` properties
- **D-Bus session setup** (critical path):
  - Launch `dbus-run-session` as wrapper, OR manually start `dbus-daemon --session` and export `DBUS_SESSION_BUS_ADDRESS`
  - Start `at-spi2-registryd` explicitly
  - Set env vars: `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1`, `QT_ACCESSIBILITY=1`, `GTK_MODULES=gail:atk-bridge`
- Cleanup on `atexit` and signal handlers
- Configurable screen resolution (passed through from `launch_app`)

### 1.2 `process.py` — Binary Launch

- `ProcessManager` class:
  - `launch(binary, args, env, working_dir, display_env)` → PID
  - `terminate(timeout)`, `kill()`, `is_running`, `poll()`, `get_output()`
- Merge environment: `os.environ` + display env + accessibility env + user overrides
- Non-blocking stdout/stderr capture (thread-based draining)
- Graceful shutdown: SIGTERM → wait → SIGKILL
- Wait-for-window: poll AT-SPI until app has at least one window (configurable timeout)

### 1.3 Spike: Validate AT-SPI inside Xvfb
- **Do this first** before building other modules
- Script: launch Xvfb → start dbus-session → start at-spi2-registryd → launch a Qt app with accessibility env vars → call `Atspi.get_desktop(0)` → verify app appears in tree
- If this fails, the entire AT-SPI approach needs rethinking

---

## Phase 2: Observation Layer

### 2.1 `accessibility.py` — AT-SPI2 Tree

- `AccessibilityTree` class:
  - `__init__(pid)` — find application by PID in AT-SPI desktop
  - `list_elements(filter_role, filter_name, visible_only)` → `list[ElementInfo]`
  - `find_element(text, role, index)` → `ElementInfo | None`
  - `get_element_at(x, y)` → `ElementInfo | None`
  - `get_element_info(text, role, at_x, at_y)` → detailed dict
- `ElementInfo` dataclass:
  - `role`, `name`, `description`, `bounds` (x,y,w,h), `center` (x,y), `states`, `actions`, `text`, `value`, `children_count`, `depth`
- Extracted from AT-SPI: `get_role_name()`, `get_name()`, `get_state_set()`, `Component.get_extents()`, `Text` interface, `Action` interface
- Graceful degradation: if AT-SPI tree is empty, log warning and fall back to screenshot-only mode

### 2.2 `screenshot.py` — X11 Capture

- `ScreenshotCapture` class:
  - `capture()` → PNG bytes
  - `capture_to_file(path)` → path
  - `capture_base64()` → base64 string (for MCP image responses)
- Primary: `xdotool getactivewindow` + ImageMagick `import -window <id> png:-`
- Fallback: `import -window root png:-` (full screen)
- Set `DISPLAY` correctly for capture commands

---

## Phase 3: Interaction Layer

### 3.1 `input.py` — xdotool Input Injection

- `InputController` class:
  - `click(x, y, button)`, `double_click(x, y, button)`
  - `mouse_move(x, y)`, `mouse_down(x, y)`, `mouse_up(x, y)`
  - `type_text(text)` — `xdotool type --delay 12 "text"`
  - `press_key(key, modifiers)` — `xdotool key ctrl+s`
  - `key_down(key)`, `key_up(key)`
- Key name mapping: Qt names → xdotool keysym names (e.g., "Enter" → "Return")
- Button mapping: "left"/"right"/"middle" → 1/3/2
- Modifier combination syntax: `xdotool key ctrl+shift+z`
- Error handling: check return codes, raise `InputError`

### 3.2 `wait.py` — Idle Detection

- `IdleWaiter` class:
  - `wait_for_idle(pid, display, timeout)` → bool
  - `wait_for_element(text, role, timeout)` → `ElementInfo | None`
- Heuristics: CPU usage drop (`/proc/<pid>/stat`), screenshot stability (two captures, compare hashes), AT-SPI busy states

---

## Phase 4: MCP Server Rewrite (`main.py`)

### 4.1 Session Management
- `AppSession` class holding all managers: `display_manager`, `process_manager`, `accessibility`, `input_controller`, `screenshot`, `idle_waiter`
- Module-level `_session: AppSession | None`
- Guard: `_require_session()` raises `AppNotRunning`

### 4.2 New MCP Tool Surface

| Tool | Description |
|---|---|
| `launch_app(binary, args, env, working_dir, width, height, timeout)` | Launch any binary under Xvfb |
| `close_app()` | Terminate app + Xvfb |
| `get_app_status()` | PID, running, exit_code, stderr |
| `screenshot()` | Capture screen, return base64 image + optional file |
| `list_ui_elements(role?, name?, visible_only?)` | AT-SPI tree dump |
| `find_element(text?, role?)` | Find element, return coords + info |
| `get_element_info(text?, role?, at_x?, at_y?)` | Detailed element properties |
| `click(x, y, button?)` | Click at coordinates |
| `click_element(text?, role?, button?)` | Find element via AT-SPI + click center |
| `type_text(text)` | Type into focused widget |
| `press_key(key, modifiers?)` | Key press via xdotool |
| `hover(x, y)` / `hover_element(text?, role?)` | Mouse move |
| `double_click(x, y)` / `double_click_element(text?, role?)` | Double click |
| `wait_for_idle(timeout?)` | Wait for UI to settle |
| `wait_for_element(text?, role?, timeout?)` | Poll until element appears |

### 4.3 Removed Tools (replaced)
- `click_widget(name)` → `click_element(text=label)` (visible label, not objectName)
- `find_widgets(pattern)` → `list_ui_elements(name=pattern)`
- `trigger_action(name)` → `click_element(text=..., role="menu item")` or AT-SPI Action interface
- `list_actions()` → `list_ui_elements(role="menu item")`

---

## Phase 5: Testing & Verification

### 5.1 AT-SPI Spike (do first, in Phase 1.3)
- Validate end-to-end: Xvfb + dbus + at-spi2 + Qt app → AT-SPI tree visible

### 5.2 Unit Tests
- `DisplayManager` start/stop/cleanup
- `ProcessManager` launch/terminate/output
- `InputController` key mapping table completeness
- `AccessibilityTree` element serialization (mocked AT-SPI nodes)
- `ScreenshotCapture` PNG output

### 5.3 Integration Tests
- Qt Widgets C++ app: launch → list elements → click button → verify state
- QML app: launch → verify AT-SPI tree → interact
- GTK app (e.g., `gnome-calculator`): prove toolkit-agnostic
- Python Qt app: backward compatibility via `launch_app(binary="python3", args=["script.py"])`
- Full workflow: launch → list → find → click → wait → screenshot → verify → close

### 5.4 Edge Cases
- App with no accessibility support (graceful degradation)
- Modal dialogs, popup menus, multi-window apps
- QML apps lacking explicit `Accessible.name` (role-based discovery still works)

---

## Phase 6: Cleanup

- Delete `server/harness.py`
- Remove all Unix socket IPC code from `main.py`
- Remove `PySide6` from `requirements.txt`
- Update README.md, `.claude-plugin/plugin.json`
- Consistent error response format: `{"success": bool, "message": str, ...}`

---

## Risks

| Risk | Mitigation |
|---|---|
| AT-SPI D-Bus not working inside Xvfb | **Validate first** (Phase 1.3 spike). Use `dbus-run-session` wrapper. |
| QML apps have poor AT-SPI support | QML needs `Accessible.name`/`Accessible.role` in QML code. Document. Fall back to screenshot. |
| xdotool timing (click before window ready) | Use `--sync` flag, `wait_for_idle`, configurable delay |
| Loss of `QAction.trigger()` capability | Replace with AT-SPI Action interface `do_action` or menu click-through |

---

## Implementation Order

```
Phase 0 (errors, requirements, dep check)
  → Phase 1.3 (AT-SPI spike — validate feasibility FIRST)
  → Phase 1.1 (display.py)
  → Phase 1.2 (process.py)
  → Phase 2.1 (accessibility.py) + Phase 2.2 (screenshot.py) + Phase 3.1 (input.py)  [parallel]
  → Phase 3.2 (wait.py)
  → Phase 4 (main.py rewrite)
  → Phase 5 (testing)
  → Phase 6 (cleanup)
```

**Files to modify**: `server/main.py`, `requirements.txt`, `README.md`, `.claude-plugin/plugin.json`
**Files to create**: `server/display.py`, `server/process.py`, `server/accessibility.py`, `server/input.py`, `server/screenshot.py`, `server/wait.py`, `server/errors.py`
**Files to delete**: `server/harness.py`
