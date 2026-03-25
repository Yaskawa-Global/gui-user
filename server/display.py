"""Display session management for Xvfb and local X11 backends."""

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
    """Manages X11 display sessions with D-Bus and AT-SPI accessibility."""

    def __init__(self):
        self._xvfb_process: subprocess.Popen | None = None
        self._dbus_process: subprocess.Popen | None = None
        self._atspi_process: subprocess.Popen | None = None
        self._vnc_process: subprocess.Popen | None = None
        self._vnc_display: str | None = None
        self._display: str | None = None
        self._display_mode: str | None = None
        self._dbus_address: str | None = None
        self._warnings: list[str] = []

    def start(
        self,
        width: int = 1280,
        height: int = 1024,
        depth: int = 24,
        mode: str = "xvfb",
        display: str | None = None,
    ) -> str:
        """Start the configured display backend, D-Bus session daemon, and AT-SPI registry.

        Returns the display string (e.g. ':99').
        """
        if self._display is not None:
            raise DisplayError("Display already started")

        self._display_mode = mode
        self._warnings = []

        try:
            if mode == "xvfb":
                self._display = self._allocate_xvfb_display()
                self._start_xvfb(width, height, depth)
                self._start_dbus()
                self._start_atspi_registryd()
            elif mode == "local":
                self._display = self._resolve_local_display(display)
                self._probe_local_display()
                # Reuse the desktop's existing D-Bus session
                self._dbus_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
                if not self._dbus_address:
                    logger.warning("No DBUS_SESSION_BUS_ADDRESS in environment; AT-SPI may not work")
                # Ensure AT-SPI registryd is running on the desktop session
                self._ensure_atspi_registryd()
                self._warnings.append(
                    "Local display mode shares mouse, keyboard, and focus with the operator."
                )
            else:
                raise DisplayError(f"Unsupported display mode: {mode}")
        except Exception:
            self.stop()
            raise

        atexit.register(self.stop)
        logger.info(f"Display session started: {self._display}")
        return self._display

    def start_vnc(self, port: int = 0) -> str:
        """Start x11vnc in view-only mode for operator observation.

        Args:
            port: VNC port (0 = auto-select). VNC viewers connect to this port.

        Returns the VNC display string (e.g. "localhost:5900").
        """
        if self._vnc_process is not None:
            if self._vnc_process.poll() is None:
                return self._vnc_display
            self._vnc_process = None

        if not shutil.which("x11vnc"):
            logger.warning("x11vnc not found; VNC observation not available. Install: sudo apt install x11vnc")
            return None

        args = [
            "x11vnc",
            "-display", self._display,
            "-viewonly",
            "-shared",
            "-nopw",
            "-forever",
            "-noxdamage",
            "-q",
        ]
        if port > 0:
            args.extend(["-rfbport", str(port)])
        else:
            args.extend(["-autoport", "5900"])

        self._vnc_process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        # Give x11vnc a moment to bind
        time.sleep(0.5)
        if self._vnc_process.poll() is not None:
            logger.warning("x11vnc exited immediately; VNC not available")
            self._vnc_process = None
            return None

        # Determine the port — read from /proc or parse output
        actual_port = port if port > 0 else self._detect_vnc_port()
        self._vnc_display = f"localhost:{actual_port}"
        logger.info(f"x11vnc started: {self._vnc_display} (view-only)")
        return self._vnc_display

    def _detect_vnc_port(self) -> int:
        """Detect the port x11vnc bound to."""
        if self._vnc_process is None:
            return 5900
        # Check /proc/<pid>/net/tcp for listening ports
        try:
            import re
            with open(f"/proc/{self._vnc_process.pid}/net/tcp") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 4 and parts[3] == "0A":  # LISTEN state
                        port = int(parts[1].split(":")[1], 16)
                        if port >= 5900:
                            return port
        except Exception:
            pass
        return 5900

    @property
    def vnc_running(self) -> bool:
        return self._vnc_process is not None and self._vnc_process.poll() is None

    @property
    def vnc_display(self) -> str | None:
        if self.vnc_running:
            return self._vnc_display
        return None

    def stop(self) -> None:
        """Stop all managed processes (VNC, AT-SPI, D-Bus, Xvfb) in reverse order."""
        for name, proc_attr in [
            ("x11vnc", "_vnc_process"),
            ("at-spi2-registryd", "_atspi_process"),
            ("dbus-daemon", "_dbus_process"),
            ("Xvfb", "_xvfb_process"),
        ]:
            proc = getattr(self, proc_attr)
            if proc is not None:
                self._terminate_process(name, proc)
                setattr(self, proc_attr, None)
        self._display = None
        self._display_mode = None
        self._dbus_address = None
        self._vnc_display = None
        self._warnings = []

    @property
    def display(self) -> str | None:
        return self._display

    @property
    def display_mode(self) -> str | None:
        return self._display_mode

    @property
    def is_running(self) -> bool:
        if self._display_mode == "local":
            return self._display is not None
        return self._xvfb_process is not None and self._xvfb_process.poll() is None

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

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

    def _allocate_xvfb_display(self) -> str:
        display_num = 99
        while os.path.exists(f"/tmp/.X{display_num}-lock"):
            display_num += 1
        return f":{display_num}"

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

    def _resolve_local_display(self, display: str | None) -> str:
        resolved = display or os.environ.get("DISPLAY")
        if not resolved:
            raise DisplayError(
                "Local display mode requires DISPLAY to be set or an explicit display argument."
            )
        return resolved

    def _probe_local_display(self) -> None:
        env = {**os.environ, "DISPLAY": self._display}
        try:
            result = subprocess.run(
                ["xdotool", "getmouselocation"],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise DisplayError(
                f"Timed out probing local display {self._display}. "
                "Check that the X11 display is reachable and authorized."
            ) from exc

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise DisplayError(
                f"Cannot access local display {self._display}: {detail}. "
                "Check DISPLAY/XAUTHORITY and X11 access permissions."
            )

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

    def _ensure_atspi_registryd(self) -> None:
        """Ensure the AT-SPI registry daemon is running on the current D-Bus session.

        For local mode: checks if org.a11y.Bus is already available, and if not,
        starts at-spi2-registryd and enables accessibility.
        """
        child_env = {**os.environ, **self.env}
        # Check if AT-SPI bus is already available
        result = subprocess.run(
            ["dbus-send", "--session", "--dest=org.a11y.Bus",
             "--type=method_call", "--print-reply",
             "/org/a11y/bus", "org.a11y.Bus.GetAddress"],
            env=child_env, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.debug("AT-SPI bus already available on desktop session")
            return

        logger.info("AT-SPI bus not found on desktop session; starting at-spi2-registryd")
        self._start_atspi_registryd()

        # Enable accessibility flag so GTK/Qt apps register
        subprocess.run(
            ["dbus-send", "--session", "--dest=org.a11y.Status",
             "--type=method_call",
             "/org/a11y/bus", "org.freedesktop.DBus.Properties.Set",
             "string:org.a11y.Status", "string:IsEnabled",
             "variant:boolean:true"],
            env=child_env, capture_output=True, text=True, timeout=5,
        )
        logger.debug("Set org.a11y.Status.IsEnabled = true")

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
