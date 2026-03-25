"""Xvfb + D-Bus + AT-SPI session management."""

import atexit
import logging
import os
import shutil
import subprocess
import time

from .errors import DisplayError

logger = logging.getLogger("gui-user.display")

_ATSPI_REGISTRYD_PATHS = [
    "/usr/libexec/at-spi2-registryd",
    "/usr/lib/at-spi2-core/at-spi2-registryd",
]


def _find_atspi_registryd() -> str | None:
    path = shutil.which("at-spi2-registryd")
    if path:
        return path
    for p in _ATSPI_REGISTRYD_PATHS:
        if os.path.isfile(p):
            return p
    return None


class DisplayManager:
    """Manages Xvfb virtual display with D-Bus session and AT-SPI accessibility."""

    def __init__(self):
        self._xvfb_process: subprocess.Popen | None = None
        self._dbus_process: subprocess.Popen | None = None
        self._atspi_process: subprocess.Popen | None = None
        self._display: str | None = None
        self._dbus_address: str | None = None

    def start(self, width: int = 1280, height: int = 1024, depth: int = 24) -> str:
        """Start Xvfb, D-Bus session daemon, and AT-SPI registry.

        Returns the display string (e.g. ':99').
        """
        if self._xvfb_process is not None:
            raise DisplayError("Display already started")

        # Find free display number
        display_num = 99
        while os.path.exists(f"/tmp/.X{display_num}-lock"):
            display_num += 1
        self._display = f":{display_num}"

        try:
            self._start_xvfb(width, height, depth)
            self._start_dbus()
            self._start_atspi_registryd()
        except Exception:
            self.stop()
            raise

        atexit.register(self.stop)
        logger.info(f"Display session started: {self._display}")
        return self._display

    def stop(self) -> None:
        """Stop all managed processes (AT-SPI, D-Bus, Xvfb) in reverse order."""
        for name, proc_attr in [
            ("at-spi2-registryd", "_atspi_process"),
            ("dbus-daemon", "_dbus_process"),
            ("Xvfb", "_xvfb_process"),
        ]:
            proc = getattr(self, proc_attr)
            if proc is not None:
                self._terminate_process(name, proc)
                setattr(self, proc_attr, None)
        self._display = None
        self._dbus_address = None

    @property
    def display(self) -> str | None:
        return self._display

    @property
    def is_running(self) -> bool:
        return self._xvfb_process is not None and self._xvfb_process.poll() is None

    @property
    def env(self) -> dict[str, str]:
        """Environment variables for child processes to use this display + accessibility."""
        if not self._display:
            return {}
        result = {
            "DISPLAY": self._display,
            "QT_QPA_PLATFORM": "xcb",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
            "GTK_MODULES": "gail:atk-bridge",
        }
        if self._dbus_address:
            result["DBUS_SESSION_BUS_ADDRESS"] = self._dbus_address
        return result

    def _start_xvfb(self, width: int, height: int, depth: int) -> None:
        screen = f"{width}x{height}x{depth}"
        self._xvfb_process = subprocess.Popen(
            ["Xvfb", self._display, "-screen", "0", screen],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)
        if self._xvfb_process.poll() is not None:
            raise DisplayError(f"Xvfb failed to start on {self._display}")
        logger.debug(f"Xvfb started on {self._display} ({screen})")

    def _start_dbus(self) -> None:
        base_env = {**os.environ, "DISPLAY": self._display}
        self._dbus_process = subprocess.Popen(
            ["dbus-daemon", "--session", "--print-address", "--nofork"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=base_env,
        )
        # Read the bus address from the first line of stdout, then close the pipe
        line = self._dbus_process.stdout.readline().decode().strip()
        self._dbus_process.stdout.close()
        if not line:
            raise DisplayError("dbus-daemon did not produce a session bus address")
        self._dbus_address = line
        logger.debug(f"D-Bus session: {self._dbus_address}")

    def _start_atspi_registryd(self) -> None:
        path = _find_atspi_registryd()
        if not path:
            logger.warning(
                "at-spi2-registryd not found; AT-SPI may not work. "
                "Install: sudo apt install at-spi2-core"
            )
            return
        child_env = {**os.environ, **self.env}
        self._atspi_process = subprocess.Popen(
            [path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
        )
        time.sleep(0.5)
        if self._atspi_process.poll() is not None:
            logger.warning("at-spi2-registryd exited immediately; AT-SPI may not work")
            self._atspi_process = None
        else:
            logger.debug(f"at-spi2-registryd started (pid={self._atspi_process.pid})")

    @staticmethod
    def _terminate_process(name: str, proc: subprocess.Popen, timeout: float = 3.0) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(f"{name} did not exit after {timeout}s, sending SIGKILL")
            proc.kill()
            proc.wait(timeout=2)
        except Exception as e:
            logger.warning(f"Error terminating {name}: {e}")
