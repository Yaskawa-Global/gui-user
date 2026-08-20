"""Display session management for Xvfb and local X11 backends."""

import atexit
import logging
import os
import shutil
import signal
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
        self._adopted = False
        self._owns_adopted = False

    def start(
        self,
        width: int = 1280,
        height: int = 1024,
        depth: int = 24,
        mode: str = "xvfb",
        display: str | None = None,
        detached: bool = False,
    ) -> str:
        """Start the configured display backend, D-Bus session daemon, and AT-SPI registry.

        Args:
            detached: Leave the session running when this process exits, instead of
                tearing it down at exit. Use when a short-lived process starts a session
                that something else (a CLI, a test runner) will attach() to later.

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

        if not detached:
            atexit.register(self.stop)
        logger.info(
            f"Display session started: {self._display}" + (" (detached)" if detached else "")
        )
        return self._display

    def adopt(
        self,
        display: str,
        mode: str = "xvfb",
        dbus_address: str | None = None,
        take_ownership: bool = False,
    ) -> str:
        """Take on a display session started by another process.

        No processes are started. By default the session is not owned: stop() detaches and
        the process that started it stays responsible for its lifetime.

        `take_ownership` makes stop() tear it down instead, for the case where the starting
        process is gone and nothing else can. A detached session outlives its launcher by
        design, so without this there is no one left to end it and the sessions pile up. A
        "local" display — the caller's own desktop — is never torn down whatever this says.
        """
        if self._display is not None:
            raise DisplayError("Display already started")

        self._display = display
        self._display_mode = mode
        self._dbus_address = dbus_address
        self._adopted = True
        self._owns_adopted = take_ownership and mode != "local"
        self._warnings = []
        logger.info(f"Attached to existing display session: {display}"
                    + (" (taking ownership)" if self._owns_adopted else ""))
        return self._display

    def start_vnc(self, port: int = 0, scale: str | float | None = None) -> str:
        """Start x11vnc in view-only mode for operator observation.

        Args:
            port: VNC port (0 = auto-select). VNC viewers connect to this port.
            scale: Shrink (or enlarge) the *stream*, e.g. 0.8 or "4/5". The display keeps its
                real pixel size and the app under test is unaffected -- only the picture sent
                to viewers changes. For watching a display taller than the monitor.

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
        if scale:
            args.extend(["-scale", str(scale)])
        if port > 0:
            args.extend(["-rfbport", str(port)])
        else:
            args.extend(["-autoport", "5900"])

        # x11vnc 0.9.16 exits immediately if it sees a Wayland session in the
        # environment, even when pointed at an Xvfb X11 display — scrub the vars.
        vnc_env = dict(os.environ)
        vnc_env.pop("WAYLAND_DISPLAY", None)
        vnc_env.pop("XDG_SESSION_TYPE", None)
        self._vnc_process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=vnc_env,
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

    def _detect_vnc_port(self, attempts: int = 10) -> int:
        """Detect the port x11vnc actually bound to (it is given -autoport, not a fixed one).

        Matched by socket inode rather than by scanning for "a listener above 5900":
        /proc/<pid>/net/tcp is the whole network namespace's socket table despite living
        under the pid, so scanning it returns whatever unrelated process happens to be
        listening on a high port -- a plausible-looking number that connects to nothing.
        """
        if self._vnc_process is None:
            return 5900

        for attempt in range(attempts):
            inodes = self._socket_inodes(self._vnc_process.pid)
            if inodes:
                port = self._listening_port_for(inodes)
                if port:
                    return port
            time.sleep(0.2)     # x11vnc may not have bound yet

        logger.warning("Could not determine the x11vnc port; assuming 5900")
        return 5900

    @staticmethod
    def _socket_inodes(pid: int) -> set:
        inodes = set()
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    target = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if target.startswith("socket:["):
                    inodes.add(target[len("socket:["):-1])
        except OSError:
            pass
        return inodes

    @staticmethod
    def _listening_port_for(inodes: set) -> int | None:
        """The local port of a LISTENing socket owned by one of `inodes`."""
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(table) as f:
                    next(f)                                  # header
                    for line in f:
                        parts = line.split()
                        # local_address, state, ... inode
                        if len(parts) > 9 and parts[3] == "0A" and parts[9] in inodes:
                            return int(parts[1].split(":")[1], 16)
            except (OSError, StopIteration, ValueError):
                continue
        return None

    @property
    def vnc_running(self) -> bool:
        return self._vnc_process is not None and self._vnc_process.poll() is None

    @property
    def vnc_display(self) -> str | None:
        if self.vnc_running:
            return self._vnc_display
        return None

    def stop(self) -> None:
        """Stop all managed processes (VNC, AT-SPI, D-Bus, Xvfb) in reverse order.

        An adopted session is only detached from — the process that started it owns it —
        unless it was adopted with take_ownership, in which case it is torn down here.
        """
        if self._adopted:
            if self._owns_adopted:
                terminate_session_processes(self._display)
            else:
                logger.info(f"Detaching from adopted display {self._display} (left running)")
            self._display = None
            self._display_mode = None
            self._dbus_address = None
            self._adopted = False
            self._owns_adopted = False
            return

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
        if self._display_mode == "local" or self._adopted:
            return self._display is not None
        return self._xvfb_process is not None and self._xvfb_process.poll() is None

    @property
    def adopted(self) -> bool:
        return self._adopted

    @property
    def owns_adopted(self) -> bool:
        """True if this process took responsibility for an adopted session's lifetime."""
        return self._owns_adopted

    @property
    def dbus_address(self) -> str | None:
        return self._dbus_address

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

    @staticmethod
    def running_xvfb_displays() -> list:
        """Every Xvfb display this user has running, as (pid, display), oldest first.

        Only displays numbered from 99 up are reported -- that is the range allocated here,
        and it keeps the sweep away from an Xvfb someone else's tooling is running.
        """
        found = []
        try:
            pids = subprocess.run(["pgrep", "-x", "-u", str(os.getuid()), "Xvfb"],
                                  capture_output=True, text=True).stdout.split()
        except OSError:
            return found

        for pid in pids:
            try:
                argv = open(f"/proc/{pid}/cmdline", "rb").read().decode().split("\0")
                started = os.stat(f"/proc/{pid}").st_ctime
            except OSError:
                continue
            display = next((a for a in argv if a.startswith(":") and a[1:].isdigit()), None)
            if display is None or int(display[1:]) < 99:
                continue
            found.append((started, int(pid), display))

        return [(pid, display) for _, pid, display in sorted(found)]

    @classmethod
    def sweep_other_displays(cls, keep: str | None = None) -> list:
        """Stop every virtual display except `keep`; returns the displays stopped.

        Virtual displays are meant to be one at a time. A detached session deliberately
        outlives the command that started it, so a launch that nothing later claims leaves
        its display behind -- and they accumulate silently until a viewer attaches to the
        wrong one and shows an empty screen.
        """
        stopped = []
        for pid, display in cls.running_xvfb_displays():
            if display == keep:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                stopped.append(display)
                logger.info(f"Swept stale display {display} (pid {pid})")
            except (ProcessLookupError, PermissionError) as e:
                logger.debug(f"Could not stop {display} (pid {pid}): {e}")
        return stopped

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


def session_processes(display: str) -> list[tuple[int, str]]:
    """The (pid, name) of the processes making up a display session.

    Used to end a session whose launching process is gone, so there are no Popen handles
    left to terminate. Xvfb and x11vnc name the display on their command line; the D-Bus
    and AT-SPI daemons are found by the DISPLAY they were started under, which is what
    distinguishes them from the caller's own desktop daemons.
    """
    if not display:
        return []

    found: list[tuple[int, str]] = []
    for pid_dir in os.listdir("/proc"):
        if not pid_dir.isdigit():
            continue
        pid = int(pid_dir)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if not argv or not argv[0]:
            continue
        name = os.path.basename(argv[0])

        if name in ("Xvfb", "x11vnc"):
            if display in argv[1:]:
                found.append((pid, name))
        elif name in ("dbus-daemon", "at-spi2-registryd", "at-spi-bus-launcher"):
            try:
                with open(f"/proc/{pid}/environ", "rb") as f:
                    env = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            if f"DISPLAY={display}\0" in env + "\0":
                found.append((pid, name))
    return found


def terminate_session_processes(display: str, timeout: float = 3.0) -> int:
    """End every process belonging to `display`. Returns how many were signalled."""
    import signal

    processes = session_processes(display)
    if not processes:
        logger.info(f"No processes found for display {display}")
        return 0

    # dependants first, so nothing is left talking to a display that has gone
    order = {"x11vnc": 0, "at-spi2-registryd": 1, "at-spi-bus-launcher": 2,
             "dbus-daemon": 3, "Xvfb": 4}
    for pid, name in sorted(processes, key=lambda p: order.get(p[1], 9)):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            logger.warning(f"Not permitted to stop {name} ({pid}) on {display}")

    deadline = time.time() + timeout
    while time.time() < deadline and session_processes(display):
        time.sleep(0.1)

    for pid, name in session_processes(display):
        logger.warning(f"{name} ({pid}) did not exit, sending SIGKILL")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    logger.info(f"Display session {display} stopped ({len(processes)} processes)")
    return len(processes)
