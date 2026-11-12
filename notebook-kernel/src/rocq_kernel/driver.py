"""Persistent ``coqidetop`` subprocess driver over standard I/O XML streams."""

from __future__ import annotations

import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import threading
import time

from .xml_protocol import (
    RocqValue,
    build_init_call,
    build_quit_call,
    parse_xml_response,
)


class RocqDriverError(RuntimeError):
    """Base exception for Rocq driver errors."""


class RocqBinaryNotFoundError(RocqDriverError):
    """Raised when ``coqidetop.opt`` or ``coqidetop`` cannot be located."""


class RocqTimeoutError(RocqDriverError):
    """Raised when a ``coqidetop`` command exceeds its timeout."""


class RocqProcessCrashedError(RocqDriverError):
    """Raised when the ``coqidetop`` subprocess terminates unexpectedly."""


def find_coqidetop_binary(explicit_path: str | Path | None = None) -> Path:
    """Locate ``coqidetop.opt`` or ``coqidetop`` on the host system."""
    candidates: list[str] = []
    if explicit_path:
        candidates.append(str(explicit_path))
    for env_var in ("COQIDETOP_BIN", "ROCQ_BIN"):
        env_val = os.environ.get(env_var, "").strip()
        if env_val:
            candidates.append(env_val)

    for name in ("coqidetop.opt", "coqidetop"):
        which_path = shutil.which(name)
        if which_path:
            candidates.append(which_path)

    for fallback in (
        "/opt/homebrew/bin/coqidetop.opt",
        "/opt/homebrew/bin/coqidetop",
        "/usr/local/bin/coqidetop.opt",
        "/usr/local/bin/coqidetop",
        "/usr/bin/coqidetop.opt",
        "/usr/bin/coqidetop",
    ):
        candidates.append(fallback)

    for cand in candidates:
        p = Path(cand).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return p.resolve()

    raise RocqBinaryNotFoundError(
        "Could not find 'coqidetop.opt' or 'coqidetop' on PATH. "
        "Install Coq / The Rocq Prover (e.g., 'brew install coq' or via opam) "
        "or set COQIDETOP_BIN."
    )


def detect_rocq_version(binary_path: str | Path | None = None) -> str:
    """Return the version banner of the installed Rocq / Coq toolchain."""
    try:
        bin_p = find_coqidetop_binary(binary_path)
    except RocqBinaryNotFoundError:
        return "not found"

    coqc_candidate = bin_p.parent / "coqc"
    cmd_bin = str(coqc_candidate) if coqc_candidate.is_file() else str(bin_p)
    try:
        res = subprocess.run(
            [cmd_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        out = (res.stdout or res.stderr or "").strip()
        if out:
            return out.splitlines()[0].strip()
    except Exception:
        pass
    return "unknown"


class RocqProcess:
    """Manages a persistent ``coqidetop`` XML protocol subprocess."""

    def __init__(
        self,
        binary_path: str | Path | None = None,
        cwd: str | Path | None = None,
        extra_args: list[str] | None = None,
        default_timeout: float = 30.0,
        custom_command: list[str] | None = None,
    ) -> None:
        self._binary_path = Path(binary_path) if binary_path else None
        self.cwd = Path(cwd).resolve() if cwd else Path.cwd()
        self.extra_args = list(extra_args or [])
        self.default_timeout = default_timeout
        self.custom_command = list(custom_command) if custom_command else None

        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self.root_state_id: int = 1

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> int:
        """Spawn the ``coqidetop`` subprocess and perform the ``Init`` handshake."""
        with self._lock:
            if self.is_alive:
                return self.root_state_id
            return self._start_unlocked()

    def _start_unlocked(self) -> int:
        self._kill_unlocked()
        if self.custom_command is not None:
            cmd = list(self.custom_command)
        else:
            bin_path = find_coqidetop_binary(self._binary_path)
            cmd = [str(bin_path), "-main-channel", "stdfds", *self.extra_args]

        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )

        init_val = self._send_call_unlocked(build_init_call(), timeout=self.default_timeout)
        if not init_val.ok or init_val.state_id is None:
            err = init_val.error_message or "Failed to initialize coqidetop STM"
            self._kill_unlocked()
            raise RocqDriverError(err)

        self.root_state_id = init_val.state_id
        return self.root_state_id

    def send_call(self, xml_req: str, timeout: float | None = None) -> RocqValue:
        """Send an XML ``<call>`` request and return the decoded ``RocqValue`` response."""
        with self._lock:
            if not self.is_alive:
                self._start_unlocked()
            return self._send_call_unlocked(
                xml_req,
                timeout=self.default_timeout if timeout is None else timeout,
            )

    def _send_call_unlocked(self, xml_req: str, timeout: float) -> RocqValue:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise RocqProcessCrashedError("coqidetop subprocess is not running")

        if proc.poll() is not None:
            raise RocqProcessCrashedError(
                f"coqidetop exited unexpectedly with code {proc.returncode}"
            )

        payload = (xml_req.strip() + "\n").encode("utf-8")
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except BrokenPipeError as exc:
            self._kill_unlocked()
            raise RocqProcessCrashedError("coqidetop stdin pipe closed unexpectedly") from exc

        stdout_fd = proc.stdout.fileno()
        deadline = time.monotonic() + max(0.05, float(timeout))
        chunks: list[bytes] = []
        accumulated = ""

        while "</value>" not in accumulated:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_unlocked()
                raise RocqTimeoutError(
                    f"coqidetop command timed out after {timeout:.2f}s"
                )

            ready, _, _ = select.select([stdout_fd], [], [], min(remaining, 0.25))
            if not ready:
                if proc.poll() is not None:
                    stderr_out = ""
                    if proc.stderr:
                        try:
                            stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
                        except Exception:
                            pass
                    self._kill_unlocked()
                    raise RocqProcessCrashedError(
                        f"coqidetop crashed (exit {proc.returncode})"
                        + (f": {stderr_out}" if stderr_out else "")
                    )
                continue

            chunk = os.read(stdout_fd, 65536)
            if not chunk:
                self._kill_unlocked()
                raise RocqProcessCrashedError("coqidetop closed stdout before returning <value>")
            chunks.append(chunk)
            accumulated = b"".join(chunks).decode("utf-8", errors="replace")

        # Drain any trailing feedback bytes immediately available on the descriptor
        while True:
            ready, _, _ = select.select([stdout_fd], [], [], 0.005)
            if not ready:
                break
            extra = os.read(stdout_fd, 65536)
            if not extra:
                break
            chunks.append(extra)
            accumulated = b"".join(chunks).decode("utf-8", errors="replace")

        return parse_xml_response(accumulated)

    def interrupt(self) -> None:
        """Send SIGINT to the subprocess group to interrupt long-running tactics."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except Exception:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass

    def kill(self) -> None:
        """Terminate and clean up the ``coqidetop`` subprocess."""
        with self._lock:
            self._kill_unlocked()

    def _kill_unlocked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write((build_quit_call() + "\n").encode("utf-8"))
                    proc.stdin.flush()
            except Exception:
                pass
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
