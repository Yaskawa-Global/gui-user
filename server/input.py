"""X11 input injection via xdotool."""

import logging
import os
import subprocess

from .errors import InputError
from .window import WindowTracker

logger = logging.getLogger("gui-user.input")

_BUTTON_MAP = {"left": "1", "middle": "2", "right": "3"}

_MODIFIER_MAP = {
    "Ctrl": "ctrl", "Control": "ctrl",
    "Shift": "shift",
    "Alt": "alt",
    "Meta": "super", "Super": "super",
}

_KEY_MAP = {
    # Navigation
    "Enter": "Return", "Return": "Return",
    "Tab": "Tab", "Escape": "Escape",
    "Backspace": "BackSpace", "Delete": "Delete",
    "Space": "space", "Insert": "Insert",
    "Up": "Up", "Down": "Down",
    "Left": "Left", "Right": "Right",
    "Home": "Home", "End": "End",
    "PageUp": "Page_Up", "PageDown": "Page_Down",
    # Function keys
    "F1": "F1", "F2": "F2", "F3": "F3", "F4": "F4",
    "F5": "F5", "F6": "F6", "F7": "F7", "F8": "F8",
    "F9": "F9", "F10": "F10", "F11": "F11", "F12": "F12",
    # Punctuation → X11 keysym names
    ",": "comma", ".": "period",
    ";": "semicolon", ":": "colon",
    "/": "slash", "\\": "backslash",
    "-": "minus", "+": "plus", "=": "equal",
    "[": "bracketleft", "]": "bracketright",
    "'": "apostrophe", '"': "quotedbl",
    "`": "grave", "~": "asciitilde",
    "!": "exclam", "@": "at",
    "#": "numbersign", "$": "dollar",
    "%": "percent", "^": "asciicircum",
    "&": "ampersand", "*": "asterisk",
    "(": "parenleft", ")": "parenright",
    "_": "underscore",
    "{": "braceleft", "}": "braceright",
    "<": "less", ">": "greater",
    "?": "question", "|": "bar",
}


class InputController:
    """Inject mouse and keyboard events via xdotool."""

    def __init__(self, display: str, pid: int | None = None, activate_on_keyboard: bool = False):
        self._env = {**os.environ, "DISPLAY": display}
        self._window_tracker = WindowTracker(display, pid) if pid is not None else None
        self._activate_on_keyboard = activate_on_keyboard

    def click(self, x: int, y: int, button: str = "left") -> None:
        btn = _BUTTON_MAP.get(button, "1")
        self._run("mousemove", "--sync", str(x), str(y))
        self._run("click", btn)

    def double_click(self, x: int, y: int, button: str = "left") -> None:
        btn = _BUTTON_MAP.get(button, "1")
        self._run("mousemove", "--sync", str(x), str(y))
        self._run("click", "--repeat", "2", "--delay", "50", btn)

    def mouse_move(self, x: int, y: int) -> None:
        self._run("mousemove", "--sync", str(x), str(y))

    def type_text(self, text: str, delay_ms: int = 12) -> None:
        self._focus_target_window()
        self._run("type", "--clearmodifiers", "--delay", str(delay_ms), "--", text)

    def press_key(self, key: str, modifiers: list[str] | None = None) -> None:
        self._focus_target_window()
        keysym = self._resolve_key(key)
        parts = []
        for mod in (modifiers or []):
            resolved = _MODIFIER_MAP.get(mod, mod.lower())
            parts.append(resolved)
        parts.append(keysym)
        combo = "+".join(parts)
        self._run("key", "--clearmodifiers", combo)

    def _resolve_key(self, key: str) -> str:
        if key in _KEY_MAP:
            return _KEY_MAP[key]
        # Single char or raw keysym name — pass through
        return key

    def _focus_target_window(self) -> None:
        if not self._activate_on_keyboard or self._window_tracker is None:
            return
        self._window_tracker.activate_window()

    def _run(self, *args: str) -> None:
        try:
            result = subprocess.run(
                ["xdotool"] + list(args),
                env=self._env,
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                raise InputError(f"xdotool {args[0]} failed: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            raise InputError(f"xdotool {args[0]} timed out")
