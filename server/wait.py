"""Idle detection and element polling."""

import logging
import time

from .errors import AppNotRunning, IdleTimeout

logger = logging.getLogger("gui-user.wait")

_CPU_IDLE_THRESHOLD = 5  # jiffies delta considered "idle"


class IdleWaiter:
    """Wait for UI to settle or for elements to appear."""

    def __init__(self, pid: int):
        self._pid = pid

    def wait_for_idle(
        self, timeout: float = 5.0, poll_interval: float = 0.3
    ) -> bool:
        """Wait until the app's CPU usage stabilizes.

        Returns True if idle detected. Raises IdleTimeout if timeout exceeded.
        """
        deadline = time.monotonic() + timeout
        prev_cpu = self._get_cpu_time()

        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            curr_cpu = self._get_cpu_time()
            delta = curr_cpu - prev_cpu
            if delta < _CPU_IDLE_THRESHOLD:
                logger.debug(f"App idle (cpu delta={delta})")
                return True
            prev_cpu = curr_cpu

        raise IdleTimeout(f"App PID {self._pid} did not idle within {timeout}s")

    def wait_for_element(
        self,
        accessibility_tree,
        text: str | None = None,
        role: str | None = None,
        timeout: float = 10.0,
        poll_interval: float = 0.5,
    ):
        """Poll until an element matching text/role appears.

        Returns the ElementInfo, or raises IdleTimeout.
        """
        deadline = time.monotonic() + timeout
        polls = 0

        while time.monotonic() < deadline:
            # Periodically refresh the tree root in case app structure changed
            if polls > 0 and polls % 5 == 0:
                try:
                    accessibility_tree.refresh()
                except Exception:
                    pass

            elem = accessibility_tree.find_element(text=text, role=role)
            if elem is not None:
                return elem

            time.sleep(poll_interval)
            polls += 1

        raise IdleTimeout(
            f"Element (text={text!r}, role={role!r}) not found within {timeout}s"
        )

    def _get_cpu_time(self) -> int:
        """Read combined user+system CPU time from /proc/{pid}/stat."""
        try:
            with open(f"/proc/{self._pid}/stat") as f:
                fields = f.read().split()
                return int(fields[13]) + int(fields[14])  # utime + stime
        except FileNotFoundError:
            raise AppNotRunning(f"Process {self._pid} no longer exists")
        except (IndexError, ValueError) as e:
            raise AppNotRunning(f"Cannot read CPU time for PID {self._pid}: {e}")
