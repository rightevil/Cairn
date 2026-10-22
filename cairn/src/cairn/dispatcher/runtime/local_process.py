from __future__ import annotations

import logging
import codecs
import os
import signal
import subprocess
import threading
from contextlib import suppress
from pathlib import Path

from cairn.dispatcher.runtime.process import ProcessResult

LOG = logging.getLogger(__name__)

READ_CHUNK_SIZE = 65536
STREAM_JOIN_TIMEOUT_SECONDS = 5.0
FORCE_KILL_REAP_TIMEOUT_SECONDS = 2.0
CAPTURE_LIMIT = 4 * 1024 * 1024


class LocalProcess:
    """Runs a worker command as a host subprocess.

    Mirrors the container ManagedProcess surface (start/communicate/kill/cancel) but
    executes on the dispatcher host: its own process group so children are killed as a
    group, a Python-enforced timeout instead of the ``timeout`` coreutil, and a
    SIGTERM -> grace -> SIGKILL shutdown so the CLI can flush its session before dying.
    """

    def __init__(
        self,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        timeout_seconds: int | None = None,
        term_grace_seconds: int = 5,
    ):
        self.command = command
        self.env = env
        self._cwd = cwd
        self._timeout_seconds = timeout_seconds
        self._term_grace = max(1.0, float(term_grace_seconds))
        self._process: subprocess.Popen[str] | None = None
        self._stdout_chunks: list[str] = []
        self._stderr_chunks: list[str] = []
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._kill_lock = threading.Lock()
        self._job = None
        self.on_started = None
        self.stdout_path: Path | None = None
        self.stderr_path: Path | None = None
        self._stream_error: OSError | None = None
        self._prefix = {"stdout": "", "stderr": ""}
        self._dropped = {"stdout": False, "stderr": False}

    def start(self) -> None:
        if os.name == "nt":
            from cairn.dispatcher.runtime.windows_job import WindowsJob
            self._job = WindowsJob()
        flags = (subprocess.CREATE_NO_WINDOW | 0x00000004) if os.name == "nt" else 0
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=self._cwd,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
                creationflags=flags,
            )
        except Exception:
            if self._job is not None:
                self._job.close()
            raise
        try:
            if self.on_started is not None:
                self.on_started(self)
            if self._job is not None:
                self._job.assign_and_resume(self._process.pid)
        except Exception:
            self._process.kill()
            self._process.wait()
            if self._job is not None:
                self._job.close()
            raise
        self._stdout_thread = threading.Thread(
            target=self._drain, args=(self._process.stdout, self._stdout_chunks, "stdout"), daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._drain, args=(self._process.stderr, self._stderr_chunks, "stderr"), daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._process is not None
        wait_for = float(self._timeout_seconds) if self._timeout_seconds is not None else timeout
        try:
            self._process.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            self._timed_out = True
            self._terminate()
        with suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=FORCE_KILL_REAP_TIMEOUT_SECONDS)
        if self._job is not None:
            with self._kill_lock:
                self._job.close()
        elif self._process.poll() is not None:
            self._signal_group(self._process, signal.SIGTERM)
            self._signal_group(self._process, signal.SIGKILL)
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=STREAM_JOIN_TIMEOUT_SECONDS)
        returncode = self._process.returncode
        if returncode is None:
            returncode = 137 if self._timed_out else 1
        stdout = self._captured(self._stdout_chunks, "stdout")
        stderr = self._captured(self._stderr_chunks, "stderr")
        if self._stream_error:
            returncode = returncode or 1
            stderr += f"\nCould not persist worker stream: {self._stream_error}"
        return ProcessResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        self._terminate()

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self._terminate()

    def _terminate(self) -> None:
        with self._kill_lock:
            if self._job is not None:
                self._job.terminate()
                return
            process = self._process
            if process is None or process.poll() is not None:
                return
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=self._term_grace)
                return
            except subprocess.TimeoutExpired:
                pass
            self._signal_group(process, signal.SIGKILL)

    @staticmethod
    def _signal_group(process: subprocess.Popen[str], sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            with suppress(ProcessLookupError, PermissionError, ValueError):
                process.send_signal(sig)

    def _captured(self, sink: list[str], stream: str) -> str:
        text = "".join(sink)
        return self._prefix[stream] + "\n[Middle output retained in the stream log]\n" + text if self._dropped[stream] else text

    def _drain(self, pipe, sink: list[str], stream: str) -> None:
        output = None
        size = 0
        try:
            path = getattr(self, stream + "_path")
            if path is not None:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    output = path.open("w", encoding="utf-8")
                except OSError as exc:
                    self._stream_error = exc
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            for raw in iter(lambda: pipe.buffer.read1(READ_CHUNK_SIZE), b""):
                chunk = decoder.decode(raw)
                if not self._prefix[stream]:
                    self._prefix[stream] = chunk
                sink.append(chunk)
                size += len(chunk)
                while size > CAPTURE_LIMIT and len(sink) > 1:
                    size -= len(sink.pop(0))
                    self._dropped[stream] = True
                if output:
                    try:
                        output.write(chunk)
                        output.flush()
                    except OSError as exc:
                        self._stream_error = exc
                        with suppress(OSError):
                            output.close()
                        output = None
            remaining = decoder.decode(b"", final=True)
            if remaining:
                sink.append(remaining)
                if output:
                    output.write(remaining)
        except (ValueError, OSError):
            pass
        finally:
            if output:
                with suppress(OSError):
                    output.close()
            with suppress(Exception):
                pipe.close()
