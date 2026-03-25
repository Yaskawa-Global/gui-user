"""Runtime dependency checker for gui-user."""

import shutil

from .errors import DependencyError


def check_dependencies() -> None:
    """Validate all required system and Python dependencies.

    Collects all missing items and raises a single DependencyError
    with actionable install instructions.
    """
    missing: list[str] = []

    # System binaries
    for binary, pkg in [
        ("Xvfb", "xvfb"),
        ("xdotool", "xdotool"),
        ("import", "imagemagick"),
        ("dbus-daemon", "dbus"),
    ]:
        if not shutil.which(binary):
            missing.append(f"  {binary} — install: sudo apt install {pkg}")

    # Python: PyGObject + AT-SPI
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # noqa: F401
    except (ImportError, ValueError) as e:
        missing.append(
            f"  gi.repository.Atspi — {e}\n"
            f"    install: sudo apt install at-spi2-core libgirepository1.0-dev "
            f"&& pip install PyGObject"
        )

    # Python: Pillow
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append("  Pillow — install: pip install Pillow")

    if missing:
        raise DependencyError("Missing dependencies:\n" + "\n".join(missing))
