"""The single interface through which the engine runs commands and touches files.

Everything that executes a program or changes the filesystem goes through
:class:`System`, so a different front end (or a test) can substitute its own
implementation. Commands are always argument lists; no shell is involved.
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a finished command."""

    returncode: int
    stdout: str
    stderr: str


LineHandler = Callable[[str], None]


class System(Protocol):
    """Command execution and file access used by the engine."""

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin_text: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> CommandResult:  # pragma: no cover - protocol
        ...

    def stream(
        self,
        argv: Sequence[str],
        *,
        on_stdout_line: LineHandler,
        on_stderr_line: LineHandler,
        stdin_text: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> int:  # pragma: no cover - protocol
        ...

    def which(self, name: str) -> str | None:  # pragma: no cover
        ...

    def home(self) -> Path:  # pragma: no cover
        ...


class LocalSystem:
    """:class:`System` for the machine the tool runs on."""

    def __init__(self, *, umask: int = 0o077) -> None:
        self._umask = umask
        self._children: set[subprocess.Popen] = set()
        self._children_lock = threading.Lock()

    # -- commands ---------------------------------------------------------

    def _child_env(self, env: Mapping[str, str] | None) -> dict[str, str]:
        merged = dict(os.environ)
        if env:
            merged.update(env)
        return merged

    def _preexec(self) -> None:  # runs in the child before exec
        os.umask(self._umask)
        try:
            os.nice(5)
        except OSError:
            pass

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin_text: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command to completion and capture its output."""
        completed = subprocess.run(
            list(argv),
            input=stdin_text if stdin_text is not None else "",
            capture_output=True,
            text=True,
            errors="replace",
            env=self._child_env(env),
            cwd=cwd,
            timeout=timeout,
            preexec_fn=self._preexec,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def stream(
        self,
        argv: Sequence[str],
        *,
        on_stdout_line: LineHandler,
        on_stderr_line: LineHandler,
        stdin_text: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> int:
        """Run a long command, passing each output line to a handler.

        The child gets its own session so that a dropped terminal does not
        send it a hangup signal; it finishes its current request and Reprint's
        saved progress stays consistent.
        """
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
            env=self._child_env(env),
            cwd=cwd,
            preexec_fn=self._preexec,
            start_new_session=True,
        )
        with self._children_lock:
            self._children.add(process)
        try:
            assert process.stdin is not None
            if stdin_text:
                process.stdin.write(stdin_text)
            process.stdin.close()

            def pump(pipe: Iterable[str], handler: LineHandler) -> None:
                for line in pipe:
                    handler(line.rstrip("\n"))

            assert process.stderr is not None and process.stdout is not None
            err_thread = threading.Thread(target=pump, args=(process.stderr, on_stderr_line), daemon=True)
            err_thread.start()
            pump(process.stdout, on_stdout_line)
            err_thread.join()
            return process.wait()
        finally:
            # On Ctrl+C (or any error) while the child still runs, stop it here,
            # before the exception unwinds further.
            if process.poll() is None:
                self._stop(process)
            with self._children_lock:
                self._children.discard(process)

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        """Stop a child's process group and wait, so Reprint's state lock is free afterwards."""
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()

    def stop_children(self) -> None:
        """Stop running children and wait for them (used on Ctrl+C)."""
        with self._children_lock:
            children = list(self._children)
        for child in children:
            self._stop(child)

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def home(self) -> Path:
        return Path.home()

    # -- files --------------------------------------------------------------

    @staticmethod
    def make_private_dir(path: Path) -> Path:
        """Create ``path`` (and parents) and make it readable only by us."""
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    @staticmethod
    def write_private_file(path: Path, text: str) -> None:
        """Atomically write ``text`` to ``path`` with mode 0600."""
        temporary = path.with_name(f".{path.name}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)

    @staticmethod
    def write_file(path: Path, text: str, mode: int = 0o644) -> None:
        """Atomically write a site file with an ordinary web-readable mode."""
        temporary = path.with_name(f".{path.name}.wpmigrate-tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
        os.chmod(path, mode)

    @staticmethod
    def disk_free(path: Path) -> int:
        """Free bytes on the filesystem holding ``path`` (or its nearest parent)."""
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe).free

    @staticmethod
    def is_writable_dir(path: Path) -> bool:
        return path.is_dir() and os.access(path, os.W_OK | os.X_OK)

    @staticmethod
    def move(source: Path, target: Path) -> None:
        """Move a file or directory, falling back to copy+delete across filesystems."""
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))

    @staticmethod
    def remove(path: Path) -> None:
        """Delete a file, symlink or directory tree."""
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)

    @staticmethod
    def make_web_readable(root: Path) -> None:
        """Give site files the usual modes: directories 0755, files 0644.

        Downloads are written privately (umask 077); once they are placed in
        the site folder the web server needs to read them. Executable bits
        are kept, and wp-config.php is handled separately.
        """
        for directory, subdirectories, files in os.walk(root):
            os.chmod(directory, 0o755)
            for name in files:
                full = os.path.join(directory, name)
                if os.path.islink(full):
                    continue
                mode = os.stat(full).st_mode
                executable = mode & stat.S_IXUSR
                os.chmod(full, 0o755 if executable else 0o644)
            for name in list(subdirectories):
                if os.path.islink(os.path.join(directory, name)):
                    subdirectories.remove(name)
