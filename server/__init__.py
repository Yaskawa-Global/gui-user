"""gui-user: External Computer-Use MCP Server.

Drives arbitrary X11 applications via AT-SPI2 accessibility tree
and xdotool input injection.
"""

from .errors import (
    GuiUserError,
    AppNotRunning,
    ElementNotFound,
    DisplayError,
    InputError,
    AccessibilityError,
    IdleTimeout,
    DependencyError,
)
