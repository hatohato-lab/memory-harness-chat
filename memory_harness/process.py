"""Bounded subprocess execution with best-effort descendant termination.

POSIX children share a new session/process group; timeout kills that group.
Windows uses a new process group and bounded ``taskkill /T /F``. This is not an
OS sandbox: detached descendants can escape POSIX groups, and Windows taskkill
can fail (permissions, missing command, or an already exited parent). Pipe
collection remains bounded even when a descendant survives and holds a pipe,
and the bytes kept from stdout and stderr are capped, so a child that never
stops writing cannot exhaust memory.
"""

from __future__ import annotations

import errno
import locale
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


_CLEANUP_TIMEOUT = 0.5
_TASKKILL_TIMEOUT = 1.0
_POLL_INTERVAL = 0.05
_READ_SIZE = 65536
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024


class OutputLimitExceeded(subprocess.SubprocessError):
    """stdout and stderr together passed ``max_output_bytes``; the child was stopped."""

    def __init__(self, cmd, limit: int, output=None, stderr=None):
        super().__init__(cmd, limit)
        self.cmd = cmd
        self.limit = limit
        self.output = output
        self.stderr = stderr

    def __str__(self) -> str:
        return f"子プロセスの出力が上限 {self.limit} バイトを超えたため停止しました"

    @property
    def stdout(self):
        return self.output


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        taskkill = str(Path(system_root) / "System32" / "taskkill.exe")
        try:
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, shell=False,
                timeout=_TASKKILL_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            # The group may still exist after its original leader has exited.
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass


def _close_quietly(stream) -> None:
    try:
        stream.close()
    except OSError:
        pass


class _Pipes:
    """Reader threads for stdout/stderr and a writer thread for stdin.

    Each thread closes its own pipe. The Windows C runtime serialises close()
    behind a blocked read()/write() on the same descriptor, so closing from
    the main thread could wait on a descendant that still holds the pipe.
    """

    def __init__(self, process: subprocess.Popen, input, limit: int):
        self._lock = threading.Lock()
        self._chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        self._size = 0
        self._limit = limit
        self._threads: list[threading.Thread] = []
        self.overflow = threading.Event()
        self.changed = threading.Event()
        self.write_error: OSError | None = None
        for name in ("stdout", "stderr"):
            stream = getattr(process, name)
            if stream is not None:
                self._start(self._read, name, stream)
        if process.stdin is not None:
            self._start(self._write, process.stdin, input)

    def _start(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _read(self, name: str, stream) -> None:
        try:
            while True:
                data = stream.read(_READ_SIZE)
                if not data:
                    return
                with self._lock:
                    self._chunks[name].append(data)
                    self._size += len(data)
                    if self._size > self._limit:
                        self.overflow.set()
                        return
        except (OSError, ValueError):
            return
        finally:
            _close_quietly(stream)
            self.changed.set()

    def _write(self, stream, data) -> None:
        try:
            view = memoryview(data or b"")
            while view:
                view = view[stream.write(view):]
        except BrokenPipeError:
            pass
        except OSError as exc:
            # Windows reports a child that exited or closed its stdin as EINVAL.
            if exc.errno != errno.EINVAL:
                self.write_error = exc
        finally:
            _close_quietly(stream)
            self.changed.set()

    def finished(self) -> bool:
        return not any(thread.is_alive() for thread in self._threads)

    def wait(self, timeout: float) -> None:
        if self.changed.wait(timeout):
            self.changed.clear()

    def join(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def collected(self, process: subprocess.Popen) -> tuple[bytes | None, bytes | None]:
        with self._lock:
            return (b"".join(self._chunks["stdout"]) if process.stdout is not None else None,
                    b"".join(self._chunks["stderr"]) if process.stderr is not None else None)


def _wait(process: subprocess.Popen, pipes: _Pipes, timeout: float | None) -> None:
    """Return once the child exited and every pipe reached EOF."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if pipes.overflow.is_set():
            raise OutputLimitExceeded(process.args, pipes._limit)
        if process.poll() is not None and pipes.finished():
            return
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        step = _POLL_INTERVAL if remaining is None else min(_POLL_INTERVAL, remaining)
        if process.returncode is None:
            try:
                process.wait(timeout=step)
            except subprocess.TimeoutExpired:
                pass
        else:
            pipes.wait(step)


def _settle(process: subprocess.Popen, pipes: _Pipes) -> tuple[bytes | None, bytes | None]:
    """Collect what is available without waiting forever for inherited pipes."""
    pipes.join(_CLEANUP_TIMEOUT)
    try:
        process.wait(timeout=_CLEANUP_TIMEOUT)
    except subprocess.TimeoutExpired:
        pass
    return pipes.collected(process)


def run_process(args, *, input=None, capture_output=False, timeout=None,
                check=False, shell=False, text=None, encoding=None, errors=None,
                universal_newlines=None, max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
                **kwargs) -> subprocess.CompletedProcess:
    """A ``subprocess.run``-like interface with bounded process-tree cleanup.

Supports explicit stdin/stdout/stderr, cwd and env through Popen keyword options.
Shell execution is prohibited. Output collection uses raw binary pipes internally
and converts to text on success; TimeoutExpired carries partial bytes, matching
subprocess.run. Captured stdout and stderr may hold at most ``max_output_bytes``
together: past that the process tree is stopped and OutputLimitExceeded carries
the partial bytes. The timeout may be exceeded by up to roughly one second of
POSIX cleanup, or two seconds on Windows. Surviving detached children are a
documented best-effort limitation, not a reason to wait indefinitely.
"""
    if shell is not False:
        raise ValueError("run_process requires shell=False")
    if text is not None and universal_newlines is not None and bool(text) != bool(universal_newlines):
        raise ValueError("text and universal_newlines disagree")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes < 1:
        raise ValueError("max_output_bytes must be a positive integer")
    text_mode = bool(text or universal_newlines or encoding or errors)
    codec = encoding or locale.getpreferredencoding(False)
    error_mode = errors or "strict"
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input cannot both be supplied")
        kwargs["stdin"] = subprocess.PIPE
        if text_mode:
            if not isinstance(input, str):
                raise TypeError("Text mode input must be a string")
            input = input.encode(codec, error_mode)
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("capture_output cannot be combined with stdout/stderr")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if "start_new_session" in kwargs or "process_group" in kwargs:
        raise ValueError("run_process manages its own process group")
    kwargs["bufsize"] = 0
    if os.name == "nt":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(args, shell=False, **kwargs)
    pipes = _Pipes(process, input, max_output_bytes)
    try:
        _wait(process, pipes, timeout)
    except BaseException as exc:
        _kill_tree(process)
        stdout, stderr = _settle(process, pipes)
        if isinstance(exc, subprocess.TimeoutExpired):
            raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr) from None
        if isinstance(exc, OutputLimitExceeded):
            raise OutputLimitExceeded(args, max_output_bytes, output=stdout, stderr=stderr) from None
        raise
    stdout, stderr = _settle(process, pipes)
    if pipes.write_error is not None:
        raise pipes.write_error
    if text_mode:
        def decode(value):
            return None if value is None else value.decode(codec, error_mode).replace("\r\n", "\n").replace("\r", "\n")
        stdout, stderr = decode(stdout), decode(stderr)
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result
