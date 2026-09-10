from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from memory_harness.process import OutputLimitExceeded, run_process


FLOOD_SCRIPT = """
import os, sys
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
block = b"x" * 65536
while True:
    sys.stdout.buffer.write(block)
"""


def _still_running(pid: int) -> bool:
    """Treat a killed-but-unreaped Linux zombie as terminated."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        try:
            for line in status.read_text().splitlines():
                if line.startswith("State:") and "Z" in line:
                    return False
        except FileNotFoundError:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class ProcessTests(unittest.TestCase):
    def test_text_input_cwd_environment_and_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = (
                "import os,sys; from pathlib import Path; "
                "print(Path.cwd()); print(os.environ['MH_TEST_VALUE']); "
                "print(sys.stdin.read()); print('error-output',file=sys.stderr)"
            )
            # A child writes pipe output in its locale encoding (cp932 on Japanese
            # Windows) unless told otherwise; this test is about run_process, not that.
            env = dict(os.environ, MH_TEST_VALUE="日本語", PYTHONIOENCODING="utf-8")
            result = run_process([sys.executable, "-c", script], input="入力",
                                 cwd=temporary, env=env, text=True, encoding="utf-8",
                                 capture_output=True, timeout=5, shell=False)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.splitlines(), [str(Path(temporary).resolve()), "日本語", "入力"])
            self.assertEqual(result.stderr, "error-output\n")

    def test_binary_streams_and_explicit_stdio(self):
        result = run_process(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.write('stderr')"],
            input=b"\x00\xff\r\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
        )
        self.assertEqual(result.stdout, b"\x00\xff\r\n")
        self.assertEqual(result.stderr, b"stderr")

    def test_check_raises_with_captured_output(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            run_process([sys.executable, "-c", "print('failure'); raise SystemExit(3)"],
                        text=True, capture_output=True, check=True, timeout=5)
        self.assertEqual(caught.exception.returncode, 3)
        self.assertEqual(caught.exception.stdout, "failure\n")

    def test_shell_execution_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "shell=False"):
            run_process("echo should-not-run", shell=True)

    def test_output_beyond_limit_stops_the_child_with_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            script = Path(temporary) / "flood.py"
            script.write_text(FLOOD_SCRIPT)
            started = time.monotonic()
            with self.assertRaises(OutputLimitExceeded) as caught:
                run_process([sys.executable, str(script), str(pid_file)],
                            capture_output=True, timeout=30, max_output_bytes=1_000_000)
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual(caught.exception.limit, 1_000_000)
            self.assertGreater(len(caught.exception.output), 1_000_000)
            self.assertIn("上限", str(caught.exception))
            pid = int(pid_file.read_text())
            deadline = time.monotonic() + 2
            while _still_running(pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(_still_running(pid), "A flooding child must be stopped, not merely ignored")

    def test_output_limit_counts_stdout_and_stderr_together(self):
        script = "import sys; sys.stdout.buffer.write(b'o' * 600000); sys.stderr.buffer.write(b'e' * 600000)"
        result = run_process([sys.executable, "-c", script], capture_output=True, timeout=30,
                             max_output_bytes=1_200_000)
        self.assertEqual((len(result.stdout), len(result.stderr)), (600000, 600000))
        with self.assertRaises(OutputLimitExceeded) as caught:
            run_process([sys.executable, "-c", script], capture_output=True, timeout=30,
                        max_output_bytes=1_199_999)
        self.assertGreater(len(caught.exception.output) + len(caught.exception.stderr), 1_199_999)

    def test_output_limit_must_be_a_positive_integer(self):
        for value in (0, -1, True, 1.5, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "max_output_bytes"):
                run_process([sys.executable, "-c", "pass"], max_output_bytes=value)

    def test_timeout_contains_partial_bytes(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            run_process([sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(20)"],
                        text=True, capture_output=True, timeout=0.4)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn(b"ready", caught.exception.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion; Windows taskkill is best effort")
    def test_timeout_kills_child_holding_pipe_after_parent_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            parent_script = (
                "import subprocess,sys; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); "
                "Path(sys.argv[1]).write_text(str(child.pid)); print('parent-finished',flush=True)"
            )
            pid = None
            try:
                started = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    run_process([sys.executable, "-c", parent_script, str(pid_file)],
                                capture_output=True, timeout=0.4)
                self.assertLess(time.monotonic() - started, 3)
                self.assertIn(b"parent-finished", caught.exception.stdout)
                pid = int(pid_file.read_text())
                deadline = time.monotonic() + 1
                while _still_running(pid) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(_still_running(pid), "Inherited child must be killed with its process group")
            finally:
                if pid is None and pid_file.exists():
                    pid = int(pid_file.read_text())
                if pid is not None and _still_running(pid):
                    os.kill(pid, signal.SIGKILL)

    @unittest.skipIf(os.name == "nt", "POSIX detached-session boundary")
    def test_pipe_drain_is_bounded_even_when_child_escapes_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "escaped.pid"
            parent_script = (
                "import subprocess,sys,time; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'],start_new_session=True); "
                "Path(sys.argv[1]).write_text(str(child.pid)); print('escaped-child',flush=True); time.sleep(20)"
            )
            pid = None
            try:
                started = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    run_process([sys.executable, "-c", parent_script, str(pid_file)],
                                capture_output=True, timeout=0.4)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 3, "An escaped process must not keep output collection blocked")
                self.assertIn(b"escaped-child", caught.exception.stdout)
                pid = int(pid_file.read_text())
                self.assertTrue(_still_running(pid), "This test deliberately demonstrates the detached-child limit")
            finally:
                if pid is None and pid_file.exists():
                    pid = int(pid_file.read_text())
                if pid is not None and _still_running(pid):
                    os.kill(pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
