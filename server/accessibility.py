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
        exact: bool = False,
        include_text: bool = True,
        max_depth: int | None = None,
    ) -> ElementInfo | None:
        """Find the nth element matching text and/or role. Returns None if not found."""
        try:
            match = self._find_matching_node(
                self._app_node,
                0,
                text=text,
                role=role,
                index=index,
                exact=exact,
                include_text=include_text,
                max_depth=max_depth,
            )
            if match is None:
                return None
            node, depth = match
            return self._build_element_info(node, depth)
        except Exception as e:
            raise AccessibilityError(f"Failed to find element: {e}") from e

    def find_descendant(
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
        """Find the nth descendant of a matching root node."""
        try:
            root_match = self._find_matching_node(
                self._app_node,
                0,
                text=root_text,
                role=root_role,
                index=root_index,
                exact=root_exact,
                include_text=root_include_text,
                max_depth=root_max_depth,
            )
            if root_match is None:
                return None

            root_node, root_depth = root_match
            for child_index in range(self._safe(root_node.get_child_count, 0)):
                child = root_node.get_child_at_index(child_index)
                if child is None:
                    continue
                child_max_depth = None if max_depth is None else max_depth - 1
                match = self._find_matching_node(
                    child,
                    root_depth + 1,
                    text=text,
                    role=role,
                    index=index,
                    exact=exact,
                    include_text=include_text,
                    max_depth=child_max_depth,
                )
                if match is not None:
                    node, depth = match
                    return self._build_element_info(node, depth)
            return None
        except Exception as e:
            raise AccessibilityError(f"Failed to find descendant: {e}") from e

    def find_any_element(
        self,
        texts: list[str],
        role: str | None = None,
        exact: bool = False,
        include_text: bool = True,
    ) -> tuple[str, ElementInfo] | None:
        """Find the first element matching any candidate label."""
        try:
            if not texts:
                return None

            role_lower = role.lower() if role else None
            normalized = [(text, text.lower()) for text in texts]

            for node, depth in self._walk(self._app_node, 0, skip_invisible=True):
                if role_lower:
                    node_role = self._node_role_name(node).lower()
                    if role_lower not in node_role:
                        continue

                matched_text = self._match_any_text(
                    node,
                    normalized,
                    exact=exact,
                    include_text=include_text,
                )
                if matched_text is not None:
                    return matched_text, self._build_element_info(node, depth)

            return None
        except Exception as e:
            raise AccessibilityError(f"Failed to find any element: {e}") from e

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
        """List descendants of a matching root node."""
        try:
            root_match = self._find_matching_node(
                self._app_node,
                0,
                text=root_text,
                role=root_role,
                index=root_index,
                exact=root_exact,
                include_text=root_include_text,
                max_depth=root_max_depth,
            )
            if root_match is None:
                return []

            root_node, root_depth = root_match
            results = []
            role_lower = role.lower() if role else None
            name_lower = name.lower() if name else None

            for child_index in range(self._safe(root_node.get_child_count, 0)):
                child = root_node.get_child_at_index(child_index)
                if child is None:
                    continue
                child_max_depth = None if max_depth is None else max_depth - 1
                for node, depth in self._walk(
                    child,
                    root_depth + 1,
                    skip_invisible=True,
                    max_depth=child_max_depth,
                ):

                    if role_lower:
                        node_role = self._node_role_name(node).lower()
                        if role_lower not in node_role:
                            continue

                    if name_lower and not self._node_text_matches(
                        node,
                        name_lower,
                        exact=exact,
                        include_text=include_text,
                    ):
                        continue

                    results.append(self._build_element_info(node, depth))
                    if max_results > 0 and len(results) >= max_results:
                        return results

            return results
        except Exception as e:
            raise AccessibilityError(f"Failed to list descendants: {e}") from e

    def get_element_at(self, x: int, y: int) -> ElementInfo | None:
        """Get the most specific element at screen coordinates (x, y)."""
        try:
            # Strategy 1: AT-SPI hit test (with timeout to avoid hangs)
            try:
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
                def _hit_test():
                    comp = self._app_node.get_component_iface()
                    if comp:
                        return comp.get_accessible_at_point(
                            x, y, self._atspi.CoordType.SCREEN
                        )
                    return None
                with ThreadPoolExecutor(max_workers=1) as pool:
                    hit = pool.submit(_hit_test).result(timeout=3)
                if hit:
                    return self._build_element_info(hit, -1)
            except (FuturesTimeout, Exception) as e:
                logger.debug(f"AT-SPI hit test failed or timed out: {e}")

            # Strategy 2: Manual scan — find deepest element containing point
            best = None
            best_depth = -1
            for node, depth in self._walk(self._app_node, 0, skip_invisible=True):
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

    def _walk(
        self,
        node,
        depth: int,
        skip_invisible: bool = False,
        max_depth: int | None = None,
    ) -> Iterator[tuple]:
        """Recursively yield (node, depth) for the entire subtree.

        Args:
            skip_invisible: If True, do not descend into nodes that lack
                            the 'visible' or 'showing' state.
            max_depth: Maximum relative depth from the starting node.
        """
        if node is None or depth > _MAX_DEPTH:
            return
        if max_depth is not None and max_depth < 0:
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
        if max_depth == 0:
            return
        try:
            count = node.get_child_count()
        except Exception:
            return
        for i in range(count):
            try:
                child = node.get_child_at_index(i)
                child_max_depth = None if max_depth is None else max_depth - 1
                yield from self._walk(
                    child,
                    depth + 1,
                    skip_invisible=skip_invisible,
                    max_depth=child_max_depth,
                )
            except Exception:
                continue

    def _build_element_info(self, node, depth: int) -> ElementInfo:
        """Extract all available information from an AT-SPI node."""
        role = self._safe(node.get_role_name, "unknown")
        name = self._strip_html(self._safe(node.get_name, ""))
        description = self._strip_html(self._safe(node.get_description, ""))

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

    def _find_matching_node(
        self,
        root_node,
        root_depth: int,
        text: str | None = None,
        role: str | None = None,
        index: int = 0,
        exact: bool = False,
        include_text: bool = True,
        max_depth: int | None = None,
    ):
        match_count = 0
        text_lower = text.lower() if text else None
        role_lower = role.lower() if role else None
        for node, depth in self._walk(root_node, root_depth, skip_invisible=True, max_depth=max_depth):
            if role_lower:
                node_role = self._node_role_name(node).lower()
                if role_lower not in node_role:
                    continue

            if text_lower and not self._node_text_matches(
                node,
                text_lower,
                exact=exact,
                include_text=include_text,
            ):
                continue

            if match_count == index:
                return node, depth
            match_count += 1
        return None

    def _node_role_name(self, node) -> str:
        return self._safe(node.get_role_name, "unknown")

    def _node_name(self, node) -> str:
        return self._strip_html(self._safe(node.get_name, ""))

    def _node_text(self, node) -> str:
        try:
            if node.get_text_iface():
                char_count = self._atspi.Text.get_character_count(node)
                if char_count > 0:
                    return self._atspi.Text.get_text(node, 0, char_count)
        except Exception:
            pass
        return ""

    def _node_text_matches(self, node, text_lower: str, exact: bool, include_text: bool = True) -> bool:
        name = self._node_name(node).lower()
        if exact:
            if text_lower == name:
                return True
        elif text_lower in name:
            return True

        if not include_text:
            return False

        value_text = self._node_text(node).lower()
        if exact:
            return text_lower == value_text
        return text_lower in value_text

    def _match_any_text(
        self,
        node,
        normalized_texts: list[tuple[str, str]],
        exact: bool,
        include_text: bool = True,
    ) -> str | None:
        name = self._node_name(node).lower()
        for original, lowered in normalized_texts:
            if exact:
                if lowered == name:
                    return original
            elif lowered in name:
                return original

        if not include_text:
            return None

        value_text = self._node_text(node).lower()
        for original, lowered in normalized_texts:
            if exact:
                if lowered == value_text:
                    return original
            elif lowered in value_text:
                return original
        return None

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remove HTML tags from a string."""
        if not text or '<' not in text:
            return text
        import re
        return re.sub(r'<[^>]*>', '', text)

    @staticmethod
    def _safe(func, default):
        try:
            return func()
        except Exception:
            return default
