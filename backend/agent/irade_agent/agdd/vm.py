"""Per-workspace VM access through the Project Agora runtime manager (hypervisor-for-agora).

Each AGDD workspace owns one Cloud Hypervisor VM (an Agora "canvas" id from a reserved range).
The manager is root-owned; we only speak its documented Unix-socket protocol:
  terminal open/poll/input/resize/close  -> VSOCK 5001 PTY (UID 1000, HOME=/workspace, NAT egress, apt bridge)
  settings_get / resources_set / disk_set / reset / delete
Automation runs commands in dedicated PTY sessions and reads results back with unique markers + base64,
so arbitrary binary/long output survives the PTY and the 256 KiB guest buffer.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass

CANVAS_BASE = int(os.environ.get("AGDD_VM_ID_BASE", "1900000000"))  # Agora canvases use small ids; tests use 2147483xxx
SYSTEM_OWNER = 1
INPUT_CHUNK = 6000           # manager limit: 8192 bytes per input
READ_CHUNK = 96 * 1024       # bytes of file per base64 read (< 256 KiB PTY buffer after encoding)
POLL_INTERVAL = 0.4
KEEPALIVE = 60               # guest reaps sessions idle > 120 s


class RuntimeError_(Exception):
    pass


def owner_for(user_id: str) -> int:
    """Stable positive 31-bit owner id per user (owner 1 is reserved for AGDD automation)."""
    return 2 + int(uuid.UUID(user_id).hex[:8], 16) % 2_000_000_000


class RuntimeClient:
    def __init__(self, socket_path: str | None = None):
        self.path = socket_path or os.environ.get("AGORA_RUNTIME_SOCKET", "/run/agora-runtime/manager.sock")

    def call(self, request: dict, timeout: float = 60) -> dict:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.path)
            s.sendall(json.dumps(request, ensure_ascii=True).encode() + b"\n")
            with s.makefile("rb") as f:
                while True:
                    line = f.readline(4 * 1024 * 1024)
                    if not line:
                        raise RuntimeError_("runtime manager closed the connection")
                    frame = json.loads(line)
                    if isinstance(frame, dict) and frame.get("type") == "output":
                        continue  # streaming frames are not used here
                    return frame
        except (OSError, ValueError) as e:
            raise RuntimeError_(f"runtime manager unavailable: {e}") from e
        finally:
            s.close()

    def available(self) -> bool:
        return os.path.exists(self.path)

    # ---- management ----
    def settings(self, canvas_id: int) -> dict:
        return self.call({"canvasId": canvas_id, "action": "settings_get"})

    def set_resources(self, canvas_id: int, template: str, revision: int) -> dict:
        return self.call({"canvasId": canvas_id, "action": "resources_set", "template": template, "revision": revision},
                         timeout=180)

    def set_disk(self, canvas_id: int, size_gib: int, revision: int) -> dict:
        return self.call({"canvasId": canvas_id, "action": "disk_set", "sizeGiB": size_gib, "revision": revision},
                         timeout=600)

    def reset(self, canvas_id: int) -> dict:
        return self.call({"canvasId": canvas_id, "action": "reset"}, timeout=120)

    def delete(self, canvas_id: int) -> dict:
        return self.call({"canvasId": canvas_id, "action": "delete"}, timeout=300)

    # ---- terminal ----
    def terminal(self, canvas_id: int, owner: int, name: str, operation: str, data: str = "",
                 cols: int = 160, rows: int = 48, offset: int = 0) -> dict:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", name):
            raise ValueError("invalid terminal name")
        return self.call({"canvasId": canvas_id, "action": "terminal", "owner": owner, "terminal": name,
                          "operation": operation, "data": data, "cols": cols, "rows": rows, "offset": offset},
                         timeout=240 if operation == "open" else 30)

    def attach_terminal(self, canvas_id: int, owner: int, name: str, cols: int = 120, rows: int = 32,
                        offset: int = 0, timeout: float = 30.0) -> socket.socket:
        """Open a persistent streaming connection (`terminal_attach`) to the Agora runtime manager."""
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", name):
            raise ValueError("invalid terminal name")
        cols = max(2, min(300, int(cols)))
        rows = max(2, min(150, int(rows)))
        offset = max(0, int(offset))
        req = {
            "canvasId": canvas_id,
            "action": "terminal_attach",
            "owner": owner,
            "terminal": name,
            "operation": "open",
            "data": "",
            "cols": cols,
            "rows": rows,
            "offset": offset,
        }
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(self.path)
            s.sendall(json.dumps(req, ensure_ascii=True).encode() + b"\n")
            return s
        except OSError as e:
            s.close()
            raise RuntimeError_(f"runtime manager unavailable: {e}") from e


def _locked(fn):
    def wrapper(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper


@dataclass
class CommandResult:
    exit_code: int
    output: str
    timed_out: bool = False


class VmShell:
    """A dedicated automation PTY in the workspace VM. Commands run via bash with output captured to files."""

    def __init__(self, client: RuntimeClient, canvas_id: int, name: str = "agdd", owner: int = SYSTEM_OWNER,
                 home: str = "/workspace"):
        self.client, self.canvas_id, self.name, self.owner = client, canvas_id, name, owner
        self.home = home.rstrip("/")
        self.tmp = f"{self.home}/.agdd/tmp"
        self.offset = 0
        self.opened = False
        self.lock = threading.RLock()  # one command at a time per PTY

    def _term(self, op, data=""):
        r = self.client.terminal(self.canvas_id, self.owner, self.name, op, data, offset=self.offset)
        if r.get("error"):
            raise RuntimeError_(f"terminal {op} failed: {r.get('message') or r['error']}")
        return r

    def open(self) -> None:
        r = self._term("open")
        self.offset = r.get("offset", 0)
        self.opened = True
        # quiet, deterministic shell for automation
        self._send(f"stty -echo; export PS1='' PROMPT_COMMAND='' HISTFILE=/dev/null; mkdir -p {self.tmp}\n")
        self._read_until(None, 3)

    def _send(self, text: str) -> None:
        data = text.encode()
        for i in range(0, len(data), INPUT_CHUNK):
            self._term("input", data[i:i + INPUT_CHUNK].decode(errors="ignore"))

    def _read_until(self, marker: str | None, timeout: float) -> str:
        buf = b""
        end = time.monotonic() + timeout
        delay = 0.03  # adaptive polling: quick commands return in tens of ms, long ones back off to POLL_INTERVAL
        while time.monotonic() < end:
            r = self._term("poll")
            if r.get("expired"):
                self.opened = False
                raise RuntimeError_("terminal session expired")
            if r.get("data"):
                buf += base64.b64decode(r["data"])
            self.offset = r.get("offset", self.offset)
            if marker and marker.encode() in buf:
                return buf.decode(errors="replace")
            if r.get("data"):
                delay = 0.03
            time.sleep(delay)
            delay = min(POLL_INTERVAL, delay * 1.6)
        if marker:
            raise TimeoutError(marker)
        return buf.decode(errors="replace")

    def ensure(self):
        if not self.opened:
            self.open()

    @_locked
    def put_file(self, path: str, content: bytes | str) -> None:
        """Upload through base64 chunks (terminal input is limited to 8 KiB per request)."""
        self.ensure()
        raw = content.encode() if isinstance(content, str) else content
        b64 = base64.b64encode(raw).decode()
        tmp = f"{self.tmp}/{uuid.uuid4().hex}.b64"
        self.run(f": > {tmp}", timeout=30)
        for i in range(0, len(b64), 4000):
            self.run(f"printf %s '{b64[i:i + 4000]}' >> {tmp}", timeout=30)
        self.run(f"mkdir -p \"$(dirname {sh_quote(path)})\" && base64 -d {tmp} > {sh_quote(path)} && rm -f {tmp}",
                 timeout=30, check=True)

    @_locked
    def read_file(self, path: str, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        r = self.run(f"stat -c %s {sh_quote(path)} 2>/dev/null || echo -1", timeout=30, capture=False)
        size = int((r.output.strip().splitlines() or ["-1"])[-1])
        if size < 0:
            raise FileNotFoundError(path)
        out = b""
        for off in range(0, min(size, max_bytes), READ_CHUNK):
            r = self.run(f"tail -c +{off + 1} {sh_quote(path)} | head -c {READ_CHUNK} | base64 -w0", timeout=60,
                         capture=False)
            out += base64.b64decode(r.output.strip())
        return out

    @_locked
    def run(self, command: str, timeout: float = 600, check: bool = False, capture: bool = True,
            on_progress=None) -> CommandResult:
        """Run ``command`` in bash; returns exit code and combined output (read from a file, not the PTY)."""
        self.ensure()
        tag = uuid.uuid4().hex[:12]
        out_file = f"{self.tmp}/{tag}.out"
        done = f"__AGDD_DONE_{tag}_"
        begin = f"__AGDD_BEGIN_{tag}__"
        script = f"( {command} ) > {out_file} 2>&1 < /dev/null; echo \"{done}$?__\"\n" if capture else \
            f"echo {begin}; ( {command} ) 2>/dev/null < /dev/null; _rc=$?; echo; echo \"{done}${{_rc}}__\"\n"
        self._send(script)
        buf = ""
        end = time.monotonic() + timeout
        last_keep = time.monotonic()
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                self._send("\x03")  # Ctrl+C
                return CommandResult(124, f"timeout after {timeout}s", True)
            try:
                buf += self._read_until(done, min(remaining, KEEPALIVE))
                break
            except TimeoutError:
                if on_progress and time.monotonic() - last_keep > 5:
                    on_progress()
                    last_keep = time.monotonic()
        m = re.search(re.escape(done) + r"(\d+)__", buf)
        code = int(m.group(1)) if m else 1
        if not capture:
            body = buf[:buf.find(done)]
            body = body[body.rfind(begin) + len(begin):] if begin in body else body
            return CommandResult(code, body.strip())
        text = self.read_file(out_file).decode(errors="replace")
        self._send(f"rm -f {out_file}\n")
        if check and code != 0:
            raise RuntimeError_(f"command failed ({code}): {text[-800:]}")
        return CommandResult(code, text)

    def run_inline(self, command: str, timeout: float = 120) -> CommandResult:
        """One round trip: output is streamed through the PTY (base64, so no control characters). Use for
        small results (< ~150 KiB); larger output should use run()."""
        r = self.run(f"set -o pipefail; ( {command} ) 2>&1 | base64 -w0", timeout=timeout, capture=False)
        try:
            text = base64.b64decode("".join(r.output.split())).decode(errors="replace")
        except ValueError:
            text = r.output
        return CommandResult(r.exit_code, text, r.timed_out)

    def close(self):
        if self.opened:
            try:
                self._term("close")
            finally:
                self.opened = False


def sh_quote(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"
