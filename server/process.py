"""Binary process launch and management."""

import logging
import os
import shutil
import signal
import subprocess
import threading
import time

from .errors import DisplayError
from .session import pid_alive

logger = logging.getLogger("gui-user.process")


class ProcessManager:
    """Launch, monitor, and terminate an application binary."""

    # poll() cannot recover a real exit code for a process we did not spawn; this stands
    # in for "exited, code unknown".
    ADOPTED_EXIT_UNKNOWN = -1

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._adopted_pid: int | None = None
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._drain_threads: list[threading.Thread] = []

    def launch(
        self,
        binary: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        working_dir: str | None = None,
        capture_output: bool = True,
    ) -> int:
        """Launch a binary and return its PID.

        Args:
            binary: Path to executable or name on PATH.
            args: Command-line arguments.
            env: Full environment dict (typically from DisplayManager.env merged with os.environ).
            working_dir: Working directory for the process.
            capture_output: Pipe stdout/stderr so get_output() can report them. Set False
                for a detached launch: the pipes die with the launching process, and the
                app then takes SIGPIPE on its next write.
        """
        if self.is_running:
            raise DisplayError("A process is already running; terminate it first")

        # Resolve binary
        resolved = binary if os.path.isfile(binary) else shutil.which(binary)
        if not resolved:
            raise DisplayError(f"Binary not found: {binary}")

        args = args or []
        full_env = {**os.environ, **(env or {})}

        self._stdout_lines = []
        self._stderr_lines = []
        self._drain_threads = []

        sink = subprocess.PIPE if capture_output else subprocess.DEVNULL
        self._process = subprocess.Popen(
            [resolved] + args,
            cwd=working_dir,
            env=full_env,
            stdout=sink,
            stderr=sink,
            # Detached launches outlive this process; a new session stops them taking a
            # terminal's SIGHUP/SIGINT with it.
            start_new_session=not capture_output,
        )

        # Drain stdout/stderr in background threads to prevent pipe deadlocks
        if capture_output:
            for pipe, target in [
                (self._process.stdout, self._stdout_lines),
                (self._process.stderr, self._stderr_lines),
            ]:
                t = threading.Thread(target=self._drain, args=(pipe, target), daemon=True)
                t.start()
                self._drain_threads.append(t)

        logger.info(f"Launched {resolved} (pid={self._process.pid})")
        return self._process.pid

    def adopt(self, pid: int) -> int:
        """Take on a process started elsewhere, identified only by PID.

        stdout/stderr are not available for an adopted process — the pipes belong to
        whoever launched it.
        """
        if self.is_running:
            raise DisplayError("A process is already running; terminate it first")
        if not pid_alive(pid):
            raise DisplayError(f"No live process with pid {pid} to attach to")

        self._adopted_pid = pid
        self._stdout_lines = []
        self._stderr_lines = []
        logger.info(f"Attached to existing process (pid={pid})")
        return pid

    def terminate(self, timeout: float = 5.0) -> None:
        """Graceful shutdown: SIGTERM, wait, then SIGKILL if needed."""
        if self._adopted_pid is not None:
            self._terminate_adopted(timeout)
            return
        if self._process is None:
            return
        if self._process.poll() is not None:
            self._cleanup()
            return

        try:
            self._process.terminate()
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(f"Process did not exit after {timeout}s, sending SIGKILL")
            self._process.kill()
            self._process.wait(timeout=2)
        except Exception as e:
            logger.warning(f"Error terminating process: {e}")

        self._cleanup()

    def kill(self) -> None:
        """Immediately SIGKILL the process."""
        if self._adopted_pid is not None:
            self._signal_adopted(signal.SIGKILL)
            self._wait_adopted(2.0)
            self._cleanup()
            return
        if self._process and self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=2)
        self._cleanup()

    @property
    def is_running(self) -> bool:
        if self._adopted_pid is not None:
            return pid_alive(self._adopted_pid)
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        if self._adopted_pid is not None:
            return self._adopted_pid
        return self._process.pid if self._process else None

    @property
    def adopted(self) -> bool:
        return self._adopted_pid is not None

    def poll(self) -> int | None:
        """Return exit code if the process has exited, None if still running.

        For an adopted process the exit code is not recoverable, so ADOPTED_EXIT_UNKNOWN
        stands in for "exited".
        """
        if self._adopted_pid is not None:
            return None if pid_alive(self._adopted_pid) else self.ADOPTED_EXIT_UNKNOWN
        if self._process is None:
            return None
        return self._process.poll()

    def _terminate_adopted(self, timeout: float) -> None:
        if not pid_alive(self._adopted_pid):
            self._cleanup()
            return
        self._signal_adopted(signal.SIGTERM)
        if not self._wait_adopted(timeout):
            logger.warning(f"Process did not exit after {timeout}s, sending SIGKILL")
            self._signal_adopted(signal.SIGKILL)
            self._wait_adopted(2.0)
        self._cleanup()

    def _signal_adopted(self, sig: int) -> None:
        try:
            os.kill(self._adopted_pid, sig)
        except OSError as e:
            logger.warning(f"Could not signal adopted pid {self._adopted_pid}: {e}")

    def _wait_adopted(self, timeout: float) -> bool:
        """Poll until the adopted process is gone. Returns True if it exited in time."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not pid_alive(self._adopted_pid):
                return True
            time.sleep(0.1)
        return not pid_alive(self._adopted_pid)

    def get_output(self) -> tuple[str, str]:
        """Return (stdout, stderr) collected so far."""
        return (
            "\n".join(self._stdout_lines),
            "\n".join(self._stderr_lines),
        )

    def _cleanup(self) -> None:
        for t in self._drain_threads:
            t.join(timeout=1.0)
        self._drain_threads = []
        self._process = None
        self._adopted_pid = None

    @staticmethod
    def _drain(pipe, target: list[str]) -> None:
        for line in iter(pipe.readline, b""):
            target.append(line.decode(errors="replace").rstrip("\n"))
        pipe.close()
