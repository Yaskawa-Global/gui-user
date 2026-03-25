"""AT-SPI2 accessibility tree queries for external UI element discovery."""

import logging
import os
from dataclasses import dataclass, asdict
from typing import Iterator

from .errors import AccessibilityError

logger = logging.getLogger("gui-user.accessibility")

# AT-SPI state names we care about
_STATE_NAMES = [
    "active", "armed", "busy", "checked", "collapsed", "editable",
    "enabled", "expandable", "expanded", "focusable", "focused",
    "horizontal", "iconified", "modal", "multi-line", "multiselectable",
    "opaque", "pressed", "resizable", "selectable", "selected",
    "sensitive", "showing", "single-line", "stale", "transient",
    "vertical", "visible",
]

_MAX_DEPTH = 50


@dataclass
class ElementInfo:
    """Information about a single UI element from the AT-SPI tree."""
    role: str
    name: str
    description: str
    bounds: tuple[int, int, int, int]  # (x, y, width, height)
    center: tuple[int, int]            # (cx, cy)
    states: list[str]
    actions: list[str]
    text: str
    value: float | None
    children_count: int
    depth: int

    def to_dict(self) -> dict:
        return asdict(self)


class AccessibilityTree:
    """Query the AT-SPI2 accessibility tree for a running application."""

    def __init__(self, pid: int, display_env: dict[str, str]):
        """Initialize AT-SPI connection and locate the app by PID.

        Args:
            pid: Process ID of the target application.
            display_env: Environment dict from DisplayManager.env.
        """
        self._pid = pid
        self._display_env = display_env

        # Probe AT-SPI in a subprocess first — Atspi.init() can fatally
        # abort the process (SIGTRAP) if the accessibility bus is unreachable.
        self._probe_atspi_bus(display_env, pid)

        # Safe to initialize in-process now
        os.environ.update(display_env)

        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        Atspi.init()
        self._atspi = Atspi

        self._app_node = self._find_app_node()
        if self._app_node is None:
            raise AccessibilityError(f"App PID {pid} not found in AT-SPI tree")

    @staticmethod
    def _probe_atspi_bus(display_env: dict[str, str], pid: int) -> None:
        """Probe AT-SPI bus in a subprocess to avoid fatal GLib aborts."""
        import subprocess, sys
        probe_code = """
import os, sys
os.environ.update(%(env)r)
try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    Atspi.init()
    desktop = Atspi.get_desktop(0)
    count = desktop.get_child_count()
    # Check if our target PID is visible
    for i in range(count):
        app = desktop.get_child_at_index(i)
        if app and app.get_process_id() == %(pid)d:
            sys.exit(0)
    # PID not found yet, but bus works
    sys.exit(1)
except Exception as e:
    sys.exit(2)
""" % {"env": display_env, "pid": pid}

        env = {**os.environ, **display_env}
        result = subprocess.run(
            [sys.executable, "-c", probe_code],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return  # PID found in AT-SPI tree
        if result.returncode == 1:
            raise AccessibilityError(f"AT-SPI bus works but app PID {pid} not found in tree")
        # returncode 2 or crash (signal)
        detail = result.stderr.strip()[:200] if result.stderr else "unknown"
        raise AccessibilityError(f"AT-SPI bus unreachable: {detail}")

    def refresh(self) -> None:
        """Re-find the app node in case the tree changed."""
        self._app_node = self._find_app_node()
        if self._app_node is None:
            raise AccessibilityError(f"App PID {self._pid} no longer in AT-SPI tree")

    def list_elements(
        self,
        filter_role: str | None = None,
        filter_name: str | None = None,
        visible_only: bool = True,
        max_results: int = 0,
    ) -> list[ElementInfo]:
        """Enumerate UI elements, optionally filtered by role/name.

        Args:
            max_results: Stop after collecting this many matches (0 = unlimited).
        """
        try:
            results = []
            for node, depth in self._walk(self._app_node, 0, skip_invisible=visible_only):
                info = self._build_element_info(node, depth)
                if visible_only and "visible" not in info.states:
                    continue
                if filter_role and filter_role.lower() not in info.role.lower():
                    continue
                if filter_name and filter_name.lower() not in info.name.lower():
                    continue
                results.append(info)
                if max_results > 0 and len(results) >= max_results:
                    break
            return results
        except Exception as e:
            raise AccessibilityError(f"Failed to list elements: {e}") from e

    def find_element(
        self,
        text: str | None = None,
        role: str | None = None,
        index: int = 0,
    ) -> ElementInfo | None:
        """Find the nth element matching text and/or role. Returns None if not found."""
        try:
            match_count = 0
            for node, depth in self._walk(self._app_node, 0, skip_invisible=True):
                info = self._build_element_info(node, depth)
                if role and role.lower() not in info.role.lower():
                    continue
                if text and text.lower() not in info.name.lower():
                    # Also check text content
                    if text.lower() not in info.text.lower():
                        continue
                if match_count == index:
                    return info
                match_count += 1
            return None
        except Exception as e:
            raise AccessibilityError(f"Failed to find element: {e}") from e

    def get_element_at(self, x: int, y: int) -> ElementInfo | None:
        """Get the most specific element at screen coordinates (x, y)."""
        try:
            # Strategy 1: AT-SPI hit test
            comp = self._app_node.get_component_iface()
            if comp:
                hit = comp.get_accessible_at_point(
                    x, y, self._atspi.CoordType.SCREEN
                )
                if hit:
                    return self._build_element_info(hit, -1)

            # Strategy 2: Manual scan — find deepest element containing point
            best = None
            best_depth = -1
            for node, depth in self._walk(self._app_node, 0):
                info = self._build_element_info(node, depth)
                bx, by, bw, bh = info.bounds
                if bw > 0 and bh > 0:
                    if bx <= x < bx + bw and by <= y < by + bh:
                        if depth > best_depth:
                            best = info
                            best_depth = depth
            return best
        except Exception as e:
            raise AccessibilityError(f"Failed to get element at ({x}, {y}): {e}") from e

    def _find_app_node(self):
        """Locate the application node by PID in the AT-SPI desktop.

        When multiple AT-SPI app entries share the same PID (e.g. a splash
        screen and a main window), prefer the one with the most children.
        """
        desktop = self._atspi.get_desktop(0)
        best = None
        best_children = -1
        for i in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(i)
                if app and app.get_process_id() == self._pid:
                    count = app.get_child_count()
                    if count > best_children:
                        best = app
                        best_children = count
            except Exception:
                continue
        return best

    def _walk(self, node, depth: int, skip_invisible: bool = False) -> Iterator[tuple]:
        """Recursively yield (node, depth) for the entire subtree.

        Args:
            skip_invisible: If True, do not descend into nodes that lack
                            the 'visible' or 'showing' state.
        """
        if node is None or depth > _MAX_DEPTH:
            return

        if skip_invisible and depth > 0:
            try:
                state_set = node.get_state_set()
                visible = state_set.contains(self._atspi.StateType.VISIBLE)
                showing = state_set.contains(self._atspi.StateType.SHOWING)
                if not (visible or showing):
                    return
            except Exception:
                pass

        yield (node, depth)
        try:
            count = node.get_child_count()
        except Exception:
            return
        for i in range(count):
            try:
                child = node.get_child_at_index(i)
                yield from self._walk(child, depth + 1, skip_invisible=skip_invisible)
            except Exception:
                continue

    def _build_element_info(self, node, depth: int) -> ElementInfo:
        """Extract all available information from an AT-SPI node."""
        role = self._safe(node.get_role_name, "unknown")
        name = self._safe(node.get_name, "")
        description = self._safe(node.get_description, "")

        # Bounds
        bounds = (0, 0, 0, 0)
        try:
            comp = node.get_component_iface()
            if comp:
                ext = comp.get_extents(self._atspi.CoordType.SCREEN)
                bounds = (ext.x, ext.y, ext.width, ext.height)
        except Exception:
            pass
        center = (bounds[0] + bounds[2] // 2, bounds[1] + bounds[3] // 2)

        # States
        states = []
        try:
            state_set = node.get_state_set()
            for state_name in _STATE_NAMES:
                state_type = getattr(
                    self._atspi.StateType, state_name.upper().replace("-", "_"), None
                )
                if state_type is not None and state_set.contains(state_type):
                    states.append(state_name)
        except Exception:
            pass

        # Actions
        actions = []
        try:
            action_iface = node.get_action_iface()
            if action_iface:
                for i in range(action_iface.get_action_count()):
                    actions.append(action_iface.get_action_name(i))
        except Exception:
            pass

        # Text content — use module-level Atspi.Text methods
        # (node.get_text_iface().get_text() conflicts with deprecated Accessible.get_text)
        text = ""
        try:
            if node.get_text_iface():
                char_count = self._atspi.Text.get_character_count(node)
                if char_count > 0:
                    text = self._atspi.Text.get_text(node, 0, char_count)
        except Exception:
            pass

        # Value
        value = None
        try:
            value_iface = node.get_value_iface()
            if value_iface:
                value = value_iface.get_current_value()
        except Exception:
            pass

        children_count = self._safe(node.get_child_count, 0)

        return ElementInfo(
            role=role,
            name=name,
            description=description,
            bounds=bounds,
            center=center,
            states=states,
            actions=actions,
            text=text,
            value=value,
            children_count=children_count,
            depth=depth,
        )

    @staticmethod
    def _safe(func, default):
        try:
            return func()
        except Exception:
            return default
