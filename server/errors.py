"""Exception hierarchy for gui-user."""


class GuiUserError(Exception):
    """Base exception for all gui-user errors."""


class AppNotRunning(GuiUserError):
    """No application session is active."""


class ElementNotFound(GuiUserError):
    """AT-SPI element matching the query was not found."""


class DisplayError(GuiUserError):
    """Xvfb or display-related failure."""


class InputError(GuiUserError):
    """xdotool input injection failure."""


class AccessibilityError(GuiUserError):
    """AT-SPI2 / D-Bus accessibility failure."""


class IdleTimeout(GuiUserError):
    """Timed out waiting for the UI to settle or an element to appear."""


class DependencyError(GuiUserError):
    """A required system dependency is missing."""
