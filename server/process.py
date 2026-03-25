"""Binary process launch and management."""

import logging
import os
import shutil
import subprocess
import threading

from .errors import DisplayError

logger = logging.getLogger("gui-user.process")


class ProcessManager:
    """Launch, monitor, and terminate an application binary."""

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._drain_threads: list[threading.Thread] = []

    def launch(
        self,
        binary: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        working_dir: str | None = None,
    ) -> int:
        """Launch a binary and return its PID.

        Args:
            binary: Path to executable or name on PATH.
            args: Command-line arguments.
            env: Full environment dict (typically from DisplayManager.env merged with os.environ).
            working_dir: Working directory for the process.
        """
        if self._process is not None and self._process.poll() is None:
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

        self._process = subprocess.Popen(
            [resolved] + args,
            cwd=working_dir,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Drain stdout/stderr in background threads to prevent pipe deadlocks
        for pipe, target in [
            (self._process.stdout, self._stdout_lines),
            (self._process.stderr, self._stderr_lines),
        ]:
            t = threading.Thread(target=self._drain, args=(pipe, target), daemon=True)
            t.start()
            self._drain_threads.append(t)

        logger.info(f"Launched {resolved} (pid={self._process.pid})")
        return self._process.pid

    def terminate(self, timeout: float = 5.0) -> None:
        """Graceful shutdown: SIGTERM, wait, then SIGKILL if needed."""
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
        if self._process and self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=2)
        self._cleanup()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def poll(self) -> int | None:
        """Return exit code if process has exited, None if still running."""
        if self._process is None:
            return None
        return self._process.poll()

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

    @staticmethod
    def _drain(pipe, target: list[str]) -> None:
        for line in iter(pipe.readline, b""):
            target.append(line.decode(errors="replace").rstrip("\n"))
        pipe.close()
