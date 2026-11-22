"""Tests for AGDD WebSocket terminal transport (`ws_terminal` + `api` + `FakeRuntime`)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import statistics
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import psycopg.types.json  # noqa: F401
except ModuleNotFoundError:
    import types

    _pg = types.ModuleType("psycopg")
    _pg_types = types.ModuleType("psycopg.types")
    _pg_json = types.ModuleType("psycopg.types.json")
    _pg_json.Jsonb = lambda v: v
    _pg.types = _pg_types
    _pg_types.json = _pg_json
    sys.modules["psycopg"] = _pg
    sys.modules["psycopg.types"] = _pg_types
    sys.modules["psycopg.types.json"] = _pg_json

from fake_runtime import FakeRuntime
from irade_agent.agdd import api, pgstore, vm_service, ws_terminal
from irade_agent.agdd.vm import RuntimeClient, VmShell

USER_ID = "7c467abc-85c0-4bf2-9bb6-05b6c755be05"
OTHER_USER_ID = "11111111-2222-4333-8444-555555555555"
API_TOKEN = "test-agdd-token"


class _DummyConn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class WsTestClient:
    """Minimal RFC 6455 client for testing the stdlib WebSocket server."""

    def __init__(self, host: str, port: int, path: str, headers: dict[str, str] | None = None):
        self.sock = socket.create_connection((host, port), timeout=5.0)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req_headers = {
            "Host": f"{host}:{port}",
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": key,
            **(headers or {}),
        }
        raw = f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in req_headers.items()) + "\r\n"
        self.sock.sendall(raw.encode("ascii"))
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("connection closed during handshake")
            buf.extend(chunk)
        head, _, rest = buf.partition(b"\r\n\r\n")
        self.buf = bytearray(rest)
        status_line = head.decode("latin1").splitlines()[0]
        self.status_code = int(status_line.split()[1])
        self.resp_headers = {}
        for line in head.decode("latin1").splitlines()[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                self.resp_headers[k.strip().lower()] = v.strip()
        if self.status_code == 101:
            expected = base64.b64encode(hashlib.sha1((key + ws_terminal.WS_GUID).encode("ascii")).digest()).decode("ascii")
            assert self.resp_headers.get("sec-websocket-accept") == expected

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def send_json(self, obj: dict) -> None:
        self.send_text(json.dumps(obj))

    def send_close(self, code: int = 1000) -> None:
        self._send_frame(0x8, struct.pack("!H", code))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        header.extend(mask)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(16384)
            if not chunk:
                raise EOFError("eof")
            self.buf.extend(chunk)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def recv_frame(self, timeout: float = 5.0) -> tuple[int, bytes]:
        self.sock.settimeout(timeout)
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            payload = self._recv_exact(length) if length else b""
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            return opcode, payload

    def recv_json(self, timeout: float = 5.0) -> dict:
        while True:
            op, payload = self.recv_frame(timeout)
            if op == 0x8:
                raise EOFError(f"closed:{struct.unpack('!H', payload[:2])[0] if len(payload) >= 2 else 1000}")
            if op == 0x1:
                return json.loads(payload.decode("utf-8"))

    def read_until_text(self, marker: str, timeout: float = 5.0) -> tuple[str, int]:
        acc = ""
        last_offset = 0
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            f = self.recv_json(timeout=max(0.1, end - time.monotonic()))
            if isinstance(f.get("offset"), int):
                last_offset = f["offset"]
            if f.get("data"):
                acc += base64.b64decode(f["data"]).decode("utf-8", "replace")
            if marker in acc:
                return acc, last_offset
        raise AssertionError(f"marker {marker!r} not found in {acc!r}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "m.sock")
    rt = FakeRuntime(sock_path, str(tmp_path / "vms"))
    monkeypatch.setenv("AGORA_RUNTIME_SOCKET", sock_path)
    monkeypatch.setenv("AGDD_EXECUTION", "vm")
    monkeypatch.setenv("API_TOKEN", API_TOKEN)

    api._TERM_TICKETS = ws_terminal.TerminalTicketStore(ttl=15.0)
    api._TERM_WS_SLOTS = threading.BoundedSemaphore(64)

    role_holder = {"allow": True}
    monkeypatch.setattr(api.infra, "pg", lambda readonly=False: _DummyConn())
    monkeypatch.setattr(
        api.Handler,
        "_link_irade",
        lambda self, ws, ctx: "ws-uuid-1",
    )

    def fake_require_role(conn, ws, user, minimum):
        if not role_holder["allow"]:
            raise PermissionError("revoked")
        return "OWNER"

    monkeypatch.setattr(pgstore, "require_role", fake_require_role)
    monkeypatch.setattr(vm_service, "canvas_for", lambda ws: 1900000001)

    class _Srv(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = _Srv(("127.0.0.1", 0), api.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    yield {
        "host": "127.0.0.1",
        "port": port,
        "rt": rt,
        "sock_path": sock_path,
        "role": role_holder,
    }

    srv.shutdown()
    srv.server_close()
    rt.stop()


def _post_ticket(env, ws="irade-1", name="main", user=USER_ID):
    req = urllib.request.Request(
        f"http://{env['host']}:{env['port']}/workspaces/{ws}/vm/terminal/{name}/ticket",
        data=b"{}",
        method="POST",
        headers={
            "authorization": f"Bearer {API_TOKEN}",
            "x-user-id": user,
            "x-agdd-irade-ws": "1",
            "x-agdd-role": "OWNER",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _connect_ws(env, ws="irade-1", name="main", headers=None):
    return WsTestClient(env["host"], env["port"], f"/workspaces/{ws}/vm/terminal/{name}/ws", headers=headers)


def test_ticket_single_use_expiry_and_bindings(env):
    tk = _post_ticket(env, ws="irade-1", name="main")["ticket"]

    # Wrong workspace -> rejected with 1008, and ticket was popped (cannot be replayed).
    c_bad = _connect_ws(env, ws="irade-2", name="main")
    assert c_bad.status_code == 101
    c_bad.send_json({"type": "attach", "ticket": tk, "cols": 80, "rows": 24, "offset": 0})
    err = c_bad.recv_json()
    assert err["type"] == "error" and err["retryable"] is False
    c_bad.close()

    # Replaying the same ticket on the right workspace also fails.
    c_replay = _connect_ws(env, ws="irade-1", name="main")
    c_replay.send_json({"type": "attach", "ticket": tk, "cols": 80, "rows": 24, "offset": 0})
    err2 = c_replay.recv_json()
    assert err2["type"] == "error" and err2["retryable"] is False
    c_replay.close()

    # Gateway user mismatch rejected.
    tk2 = _post_ticket(env, ws="irade-1", name="main", user=USER_ID)["ticket"]
    c_user = _connect_ws(env, headers={"Authorization": f"Bearer {API_TOKEN}", "X-User-Id": OTHER_USER_ID})
    c_user.send_json({"type": "attach", "ticket": tk2, "cols": 80, "rows": 24, "offset": 0})
    assert c_user.recv_json()["type"] == "error"
    c_user.close()

    # Expired ticket rejected.
    api._TERM_TICKETS = ws_terminal.TerminalTicketStore(ttl=-0.1)
    tk_exp = _post_ticket(env)["ticket"]
    c_exp = _connect_ws(env)
    c_exp.send_json({"type": "attach", "ticket": tk_exp, "cols": 80, "rows": 24, "offset": 0})
    assert c_exp.recv_json()["type"] == "error"
    c_exp.close()


def test_origin_validation(env):
    c_bad = _connect_ws(env, headers={"Origin": "https://evil.example"})
    assert c_bad.status_code == 403
    c_bad.close()

    c_ok = _connect_ws(env, headers={"Origin": f"http://127.0.0.1:{env['port']}"})
    assert c_ok.status_code == 101
    c_ok.send_close()
    c_ok.close()

    assert ws_terminal.origin_matches_host("https://irade.p-e.kr", "irade.p-e.kr:443")
    assert ws_terminal.origin_matches_host("https://irade.p-e.kr:8453", "irade.p-e.kr:8453")
    assert not ws_terminal.origin_matches_host("https://irade.p-e.kr:9999", "irade.p-e.kr:8453")


def test_interactive_echo_latency_utf8_resize_and_reconnect(env):
    tk = _post_ticket(env)["ticket"]
    ws = _connect_ws(env, headers={
        "Origin": f"http://127.0.0.1:{env['port']}",
        "Authorization": f"Bearer {API_TOKEN}",
        "X-User-Id": USER_ID,
    })
    assert ws.status_code == 101
    ws.send_json({"type": "attach", "ticket": tk, "cols": 90, "rows": 30, "offset": 0})
    ready = ws.recv_json()
    assert ready["type"] == "ready"
    offset = ready["offset"]

    # Measure 10 echo round-trips.
    rtts_ms = []
    for i in range(10):
        marker = f"ECHO_{i}_{int(time.monotonic_ns())}"
        t0 = time.perf_counter()
        ws.send_json({"operation": "input", "data": f"echo {marker}\r", "cols": 90, "rows": 30})
        _, offset = ws.read_until_text(marker + "\r\n", timeout=3.0)
        rtts_ms.append((time.perf_counter() - t0) * 1000.0)
    assert statistics.median(rtts_ms) < 100.0

    # Multi-byte Korean UTF-8 round-trip.
    ws.send_json({"operation": "input", "data": "echo 안녕터미널\r", "cols": 90, "rows": 30})
    out, offset = ws.read_until_text("안녕터미널\r\n", timeout=3.0)
    assert "안녕터미널" in out

    # Resize updates PTY window size.
    ws.send_json({"operation": "resize", "data": "", "cols": 132, "rows": 41})
    ws.send_json({"operation": "input", "data": "stty size\r", "cols": 132, "rows": 41})
    out_sz, offset = ws.read_until_text("41 132", timeout=3.0)
    assert "41 132" in out_sz
    if "$ " not in out_sz.split("41 132", 1)[1]:
        _, offset = ws.read_until_text("$ ", timeout=3.0)

    # Clean disconnect preserves shell session and resumes at offset without duplicating old output.
    ws.send_close()
    ws.close()
    time.sleep(0.05)

    tk2 = _post_ticket(env)["ticket"]
    ws2 = _connect_ws(env)
    ws2.send_json({"type": "attach", "ticket": tk2, "cols": 132, "rows": 41, "offset": offset})
    ready2 = ws2.recv_json()
    assert ready2["type"] == "ready"
    assert ready2["data"] == ""
    offset = ready2["offset"]
    ws2.send_json({"operation": "input", "data": "echo RESUMED_OK\r", "cols": 132, "rows": 41})
    out_res, offset = ws2.read_until_text("RESUMED_OK\r\n", timeout=3.0)
    assert "41 132" not in out_res

    # Output emitted while disconnected is delivered upon reconnect at offset.
    ws2.send_json({"operation": "input", "data": "sh -c 'sleep 0.15; printf \"MISSED_%s\\n\" \"OK\"'\r", "cols": 132, "rows": 41})
    _, offset = ws2.read_until_text("printf", timeout=3.0)
    ws2.send_close()
    ws2.close()
    time.sleep(0.3)

    tk3 = _post_ticket(env)["ticket"]
    ws3 = _connect_ws(env)
    ws3.send_json({"type": "attach", "ticket": tk3, "cols": 132, "rows": 41, "offset": offset})
    ready3 = ws3.recv_json()
    assert ready3["type"] == "ready"
    missed = base64.b64decode(ready3["data"]).decode("utf-8", "replace")
    if "MISSED_OK" not in missed:
        more, _ = ws3.read_until_text("MISSED_OK", timeout=3.0)
        missed += more
    assert "MISSED_OK" in missed
    assert "RESUMED_OK" not in missed
    ws3.send_close()
    ws3.close()


def test_automation_watch_mode_is_readonly_and_does_not_spawn_pty(env):
    # Watching nonexistent automation PTY returns expired=True and creates no session.
    tk = _post_ticket(env, name="agdd-missing")["ticket"]
    ws = _connect_ws(env, name="agdd-missing")
    ws.send_json({"type": "attach", "ticket": tk, "cols": 80, "rows": 24, "offset": 0})
    frame = ws.recv_json()
    assert frame.get("expired") is True
    ws.close()
    assert env["rt"].state.sessions == {}

    # Existing automation PTY streams output while ignoring client input/resize.
    sh = VmShell(RuntimeClient(env["sock_path"]), 1900000001, "agdd-live")
    try:
        sh.ensure()
        tk2 = _post_ticket(env, name="agdd-live")["ticket"]
        ws2 = _connect_ws(env, name="agdd-live")
        ws2.send_json({"type": "attach", "ticket": tk2, "cols": 40, "rows": 10, "offset": 0})
        ready = ws2.recv_json()
        assert ready["type"] == "ready"

        ws2.send_json({"operation": "input", "data": "echo SHOULD_NOT_RUN\r", "cols": 40, "rows": 10})
        res = sh.run_inline("echo WATCH_STREAM_OK", timeout=5)
        assert res.exit_code == 0 and "WATCH_STREAM_OK" in res.output

        # Wait for stream output from automation PTY and verify injected input never ran.
        deadline = time.monotonic() + 3.0
        seen = ""
        while time.monotonic() < deadline and "AGDD_DONE" not in seen:
            f = ws2.recv_json(timeout=2.0)
            if f.get("data"):
                seen += base64.b64decode(f["data"]).decode("utf-8", "replace")
        assert "AGDD_DONE" in seen
        assert "SHOULD_NOT_RUN" not in seen
        ws2.send_close()
        ws2.close()
    finally:
        sh.close()


def test_automation_watch_attach_error_returns_expired_not_fatal_error(env, monkeypatch):
    """When an automation PTY disappears mid-attach, watch mode emits expired=True instead of an error frame."""

    class _DyingAttachClient:
        def __init__(self, *a, **k):
            pass

        def terminal(self, *a, **k):
            return {"data": "", "offset": 0}

        def attach_terminal(self, cid, owner, name, cols=80, rows=24, offset=0, mirror=True):
            raise OSError("pty closed during attach")

    monkeypatch.setattr("irade_agent.agdd.vm.RuntimeClient", _DyingAttachClient)

    tk = _post_ticket(env, name="agdd-dying")["ticket"]
    ws = _connect_ws(env, name="agdd-dying")
    ws.send_json({"type": "attach", "ticket": tk, "cols": 80, "rows": 24, "offset": 0})
    frame = ws.recv_json()
    assert frame.get("type") == "output" and frame.get("expired") is True
    assert frame.get("type") != "error"
    ws.close()
