"""Stdlib RFC 6455 WebSocket server + persistent Agora runtime `terminal_attach` bridge."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import select
import socket
import struct
import threading
import time
import urllib.parse
from dataclasses import dataclass

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_WS_FRAME = 32768
MAX_UNIX_BUF = 262144


class ProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TerminalTicket:
    ws: str
    raw_ws: str
    user: str
    name: str
    canvas_id: int
    owner: int
    watch: bool
    expires: float


class TerminalTicketStore:
    """Single-use, short-lived ticket store binding WS attaches to an authorized workspace/terminal/user."""

    def __init__(self, ttl: float = 15.0, max_tickets: int = 1024):
        self.ttl = ttl
        self.max_tickets = max_tickets
        self._tickets: dict[str, TerminalTicket] = {}
        self._lock = threading.Lock()

    def _purge_locked(self, now: float) -> None:
        expired = [k for k, v in self._tickets.items() if v.expires < now]
        for k in expired:
            del self._tickets[k]

    def issue(self, ws: str, raw_ws: str, user: str, name: str, canvas_id: int, owner: int, watch: bool) -> str:
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            if len(self._tickets) >= self.max_tickets:
                raise RuntimeError("terminal ticket capacity reached")
            secret = secrets.token_urlsafe(32)
            self._tickets[secret] = TerminalTicket(
                ws=ws,
                raw_ws=raw_ws,
                user=user,
                name=name,
                canvas_id=canvas_id,
                owner=owner,
                watch=watch,
                expires=now + self.ttl,
            )
            return secret

    def consume(self, secret: str, raw_ws: str, name: str, gateway_user: str | None = None) -> TerminalTicket | None:
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            ticket = self._tickets.pop(secret, None)
        if ticket is None or ticket.expires < now or ticket.raw_ws != raw_ws or ticket.name != name:
            return None
        if gateway_user is not None and ticket.user != gateway_user:
            return None
        return ticket


def origin_matches_host(origin: str | None, host: str | None) -> bool:
    if not origin:
        return True
    try:
        o = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    if o.scheme not in ("http", "https") or not o.hostname:
        return False
    if not host:
        return False
    try:
        h = urllib.parse.urlsplit(f"http://{host}")
    except ValueError:
        return False
    if not h.hostname or o.hostname.lower() != h.hostname.lower():
        return False
    if h.port is not None:
        o_port = o.port or (443 if o.scheme == "https" else 80)
        if o_port != h.port:
            return False
    return True


def _build_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", n))
    return bytes(header) + payload


class WebSocketConn:
    """Minimal thread-safe RFC 6455 server connection over a raw TCP socket."""

    def __init__(self, sock: socket.socket, initial_buf: bytes = b""):
        self.sock = sock
        self._buf = bytearray(initial_buf)
        self._write_lock = threading.Lock()
        self._closed = False
        self._close_sent = False
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def _fill(self, deadline: float | None) -> None:
        if deadline is not None:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("websocket read timed out")
            readable, _, _ = select.select([self.sock], [], [], rem)
            if not readable:
                raise TimeoutError("websocket read timed out")
        chunk = self.sock.recv(16384)
        if not chunk:
            raise EOFError("websocket connection closed")
        self._buf.extend(chunk)

    def _try_pop_frame(self, total_pending: int) -> tuple[bool, int, bytes] | None:
        buf = self._buf
        if len(buf) < 2:
            return None
        b1, b2 = buf[0], buf[1]
        fin = bool(b1 & 0x80)
        if b1 & 0x70:
            self.send_close(1002)
            raise ProtocolError(1002, "reserved bits must be 0")
        opcode = b1 & 0x0F
        if not (b2 & 0x80):
            self.send_close(1002)
            raise ProtocolError(1002, "client frames must be masked")
        length = b2 & 0x7F
        pos = 2
        if length == 126:
            if len(buf) < pos + 2:
                return None
            length = struct.unpack_from("!H", buf, pos)[0]
            pos += 2
        elif length == 127:
            if len(buf) < pos + 8:
                return None
            length = struct.unpack_from("!Q", buf, pos)[0]
            pos += 8
        if opcode >= 0x8:
            if not fin or length > 125:
                self.send_close(1002)
                raise ProtocolError(1002, "invalid control frame")
        else:
            if length > MAX_WS_FRAME or total_pending + length > MAX_WS_FRAME:
                self.send_close(1009)
                raise ProtocolError(1009, "message exceeds max frame size")
        if len(buf) < pos + 4 + length:
            return None
        mask = bytes(buf[pos:pos + 4])
        pos += 4
        raw = bytes(buf[pos:pos + length])
        del buf[:pos + length]
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(raw)) if length else b""
        return fin, opcode, payload

    def recv_message(self, timeout: float | None = None) -> tuple[str, str | bytes]:
        deadline = None if timeout is None else time.monotonic() + timeout
        fragments: list[bytes] = []
        msg_opcode: int | None = None
        total_len = 0
        while True:
            frame = self._try_pop_frame(total_len)
            if frame is None:
                self._fill(deadline)
                continue
            fin, opcode, payload = frame
            if opcode == 0x9:
                self.send_pong(payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1000
                self.send_close(code)
                return ("close", payload)
            if opcode == 0x2:
                self.send_close(1003)
                raise ProtocolError(1003, "binary frames are not supported")
            if opcode == 0x1:
                if msg_opcode is not None:
                    self.send_close(1002)
                    raise ProtocolError(1002, "unexpected text frame during continuation")
                msg_opcode = 0x1
                fragments.append(payload)
                total_len += len(payload)
            elif opcode == 0x0:
                if msg_opcode is None:
                    self.send_close(1002)
                    raise ProtocolError(1002, "unexpected continuation frame")
                fragments.append(payload)
                total_len += len(payload)
            else:
                self.send_close(1002)
                raise ProtocolError(1002, f"unsupported opcode {opcode}")
            if fin:
                full = b"".join(fragments)
                try:
                    return ("text", full.decode("utf-8", errors="strict"))
                except UnicodeDecodeError as e:
                    self.send_close(1007)
                    raise ProtocolError(1007, "invalid utf-8 payload") from e

    def send_frame(self, opcode: int, payload: bytes) -> None:
        wire = _build_frame(opcode, payload)
        with self._write_lock:
            if self._closed:
                raise OSError("websocket closed")
            self.sock.sendall(wire)

    def send_text(self, text: str) -> None:
        self.send_frame(0x1, text.encode("utf-8"))

    def send_json(self, obj: dict) -> None:
        self.send_text(json.dumps(obj, ensure_ascii=False))

    def send_ping(self, payload: bytes = b"") -> None:
        self.send_frame(0x9, payload[:125])

    def send_pong(self, payload: bytes = b"") -> None:
        self.send_frame(0xA, payload[:125])

    def send_close(self, code: int = 1000, reason: str = "") -> None:
        payload = struct.pack("!H", code) + reason.encode("utf-8")[:123]
        with self._write_lock:
            if self._closed or self._close_sent:
                return
            self._close_sent = True
            try:
                self.sock.sendall(_build_frame(0x8, payload))
            except OSError:
                pass

    def close(self) -> None:
        with self._write_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _http_error(handler, code: int, message: str) -> None:
    data = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _close_sock(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def handle_websocket(
    handler,
    raw_ws: str,
    name: str,
    ticket_store: TerminalTicketStore,
    runtime_factory,
    active_slots: threading.BoundedSemaphore,
    check_access=None,
) -> None:
    upgrade = (handler.headers.get("upgrade") or "").strip().lower()
    conn_tokens = [t.strip().lower() for t in (handler.headers.get("connection") or "").split(",")]
    version = (handler.headers.get("sec-websocket-version") or "").strip()
    key = (handler.headers.get("sec-websocket-key") or "").strip()
    if upgrade != "websocket" or "upgrade" not in conn_tokens or version != "13" or not key:
        _http_error(handler, 400, "websocket upgrade required")
        return

    if not origin_matches_host(handler.headers.get("origin"), handler.headers.get("host")):
        _http_error(handler, 403, "origin not allowed")
        return

    api_token = os.environ.get("API_TOKEN", "")
    auth = handler.headers.get("authorization", "")
    gateway_user: str | None = None
    if auth:
        if not api_token or auth != f"Bearer {api_token}":
            _http_error(handler, 401, "unauthorized")
            return
        gateway_user = handler.headers.get("x-user-id", "")
        if not re.fullmatch(r"[0-9a-f-]{36}", gateway_user):
            _http_error(handler, 401, "x-user-id required")
            return

    if not active_slots.acquire(blocking=False):
        _http_error(handler, 503, "terminal websocket capacity reached")
        return

    ws: WebSocketConn | None = None
    unix_sock: socket.socket | None = None
    try:
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
        handler.connection.sendall(handshake)
        handler.close_connection = True

        pre = bytearray()
        read1 = getattr(handler.rfile, "read1", None)
        if read1 is not None:
            handler.connection.setblocking(False)
            try:
                while True:
                    try:
                        chunk = read1(16384)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    pre.extend(chunk)
            finally:
                handler.connection.setblocking(True)
        handler.connection.settimeout(None)
        ws = WebSocketConn(handler.connection, bytes(pre))

        # Step A: Receive attach frame within 5 s and consume the single-use ticket.
        try:
            kind, raw_attach = ws.recv_message(timeout=5.0)
        except (TimeoutError, EOFError, OSError, ProtocolError):
            return
        if kind != "text":
            return
        try:
            attach_msg = json.loads(raw_attach)
        except (json.JSONDecodeError, ValueError):
            ws.send_json({"type": "error", "retryable": False, "message": "잘못된 attach 프레임입니다."})
            ws.send_close(1008)
            return
        if not isinstance(attach_msg, dict) or attach_msg.get("type") != "attach":
            ws.send_json({"type": "error", "retryable": False, "message": "첫 프레임은 attach여야 합니다."})
            ws.send_close(1008)
            return

        ticket = ticket_store.consume(str(attach_msg.get("ticket", "")), raw_ws, name, gateway_user)
        if ticket is None:
            ws.send_json({"type": "error", "retryable": False, "message": "터미널 인증 티켓이 유효하지 않거나 만료되었습니다."})
            ws.send_close(1008)
            return

        try:
            cols = max(2, min(300, int(attach_msg.get("cols", 120))))
            rows = max(2, min(150, int(attach_msg.get("rows", 32))))
            offset = max(0, int(attach_msg.get("offset", 0)))
        except (TypeError, ValueError):
            ws.send_json({"type": "error", "retryable": False, "message": "잘못된 터미널 크기 또는 오프셋입니다."})
            ws.send_close(1008)
            return

        # Step B: Watch probe + open persistent terminal_attach stream to runtime manager.
        rc = runtime_factory()
        if ticket.watch:
            try:
                probe = rc.terminal(ticket.canvas_id, ticket.owner, ticket.name, "poll", cols=160, rows=48, offset=offset)
            except Exception as e:
                ws.send_json({"type": "error", "retryable": False, "message": str(e)[:300]})
                ws.send_close(1011)
                return
            if probe.get("expired"):
                ws.send_json({"type": "output", "data": "", "offset": offset, "expired": True})
                ws.send_close(1000)
                return
            if probe.get("error"):
                ws.send_json({"type": "error", "retryable": False, "message": str(probe.get("message") or probe["error"])})
                ws.send_close(1011)
                return
            attach_cols, attach_rows = 160, 48
        else:
            attach_cols, attach_rows = cols, rows

        try:
            unix_sock = rc.attach_terminal(
                ticket.canvas_id,
                ticket.owner,
                ticket.name,
                cols=attach_cols,
                rows=attach_rows,
                offset=offset,
                timeout=30.0,
            )
        except Exception as e:
            if ticket.watch:
                ws.send_json({"type": "output", "data": "", "offset": offset, "expired": True})
                ws.send_close(1000)
            else:
                ws.send_json({"type": "error", "retryable": True, "message": str(e)[:300]})
                ws.send_close(1011)
            return

        unix_buf = bytearray()
        queued_cmds: list[str] = []
        boot_deadline = time.monotonic() + 240.0
        first_frame: dict | None = None
        while True:
            rem = boot_deadline - time.monotonic()
            if rem <= 0:
                ws.send_json({"type": "error", "retryable": True, "message": "VM 터미널 연결 시간 초과"})
                ws.send_close(1011)
                return
            readable, _, _ = select.select([unix_sock, ws.sock], [], [], min(15.0, rem))
            if not readable:
                try:
                    ws.send_ping()
                except OSError:
                    return
                continue
            if ws.sock in readable:
                try:
                    w_kind, w_payload = ws.recv_message(timeout=0.5)
                    if w_kind == "close":
                        return
                    if w_kind == "text":
                        queued_cmds.append(str(w_payload))
                except TimeoutError:
                    pass
                except (EOFError, OSError, ProtocolError):
                    return
            if unix_sock in readable:
                try:
                    chunk = unix_sock.recv(16384)
                except OSError:
                    chunk = b""
                if not chunk:
                    if ticket.watch:
                        ws.send_json({"type": "output", "data": "", "offset": offset, "expired": True})
                        ws.send_close(1000)
                    else:
                        ws.send_json({"type": "error", "retryable": True, "message": "런타임 스트림 연결이 종료되었습니다."})
                        ws.send_close(1011)
                    return
                unix_buf.extend(chunk)
                if len(unix_buf) > MAX_UNIX_BUF:
                    ws.send_json({"type": "error", "retryable": False, "message": "터미널 프레임 크기 초과"})
                    ws.send_close(1009)
                    return
                if b"\n" in unix_buf:
                    first_line, _, rest = unix_buf.partition(b"\n")
                    unix_buf = bytearray(rest)
                    try:
                        first_frame = json.loads(first_line)
                    except (json.JSONDecodeError, ValueError):
                        if ticket.watch:
                            ws.send_json({"type": "output", "data": "", "offset": offset, "expired": True})
                            ws.send_close(1000)
                        else:
                            ws.send_json({"type": "error", "retryable": True, "message": "잘못된 런타임 응답입니다."})
                            ws.send_close(1011)
                        return
                    break

        if not isinstance(first_frame, dict) or "error" in first_frame:
            if ticket.watch:
                ws.send_json({"type": "output", "data": "", "offset": offset, "expired": True})
                ws.send_close(1000)
            else:
                err_msg = (
                    str(first_frame.get("message") or first_frame["error"])
                    if isinstance(first_frame, dict)
                    else "런타임 오류"
                )
                ws.send_json({"type": "error", "retryable": False, "message": err_msg})
                ws.send_close(1011)
            return

        ws.send_json(first_frame)

        # Step C: Full-duplex bridge (unix_sock <-> ws).
        stop = threading.Event()
        unix_write_lock = threading.Lock()

        def forward_cmd(raw_text: str) -> None:
            if ticket.watch:
                return
            try:
                cmd = json.loads(raw_text)
            except (json.JSONDecodeError, ValueError):
                return
            if not isinstance(cmd, dict):
                return
            op = cmd.get("operation")
            if op not in ("input", "resize", "close"):
                return
            data = cmd.get("data", "")
            if not isinstance(data, str) or len(data.encode("utf-8")) > 8192:
                return
            try:
                c = max(2, min(300, int(cmd.get("cols", attach_cols))))
                r = max(2, min(150, int(cmd.get("rows", attach_rows))))
            except (TypeError, ValueError):
                return
            # guest/terminal.py requires the exact 4-key dict {"operation", "data", "cols", "rows"}.
            wire = json.dumps({"operation": op, "data": data, "cols": c, "rows": r}, ensure_ascii=False).encode("utf-8") + b"\n"
            with unix_write_lock:
                if not stop.is_set() and unix_sock is not None:
                    unix_sock.sendall(wire)

        for queued in queued_cmds:
            forward_cmd(queued)

        def emit_unix_line(raw_line: bytes) -> bool:
            if not raw_line:
                return True
            try:
                frame = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                return True
            if not isinstance(frame, dict):
                return True
            if "error" in frame:
                err_msg = str(frame.get("message") or frame["error"])
                if ticket.watch or "expired" in err_msg.lower():
                    ws.send_json({"type": "output", "data": "", "expired": True})
                    ws.send_close(1000)
                else:
                    ws.send_json({"type": "error", "retryable": True, "message": err_msg})
                    ws.send_close(1011)
                return False
            ws.send_json(frame)
            if frame.get("closed") or frame.get("exited"):
                ws.send_close(1000)
                return False
            return True

        def output_pump() -> None:
            nonlocal unix_buf
            last_check = time.monotonic()
            try:
                while b"\n" in unix_buf:
                    line, _, rest = unix_buf.partition(b"\n")
                    unix_buf = bytearray(rest)
                    if not emit_unix_line(line):
                        return
                while not stop.is_set():
                    readable, _, _ = select.select([unix_sock], [], [], 15.0)
                    if check_access is not None and time.monotonic() - last_check >= 30.0:
                        try:
                            check_access(ticket)
                        except PermissionError:
                            ws.send_json({"type": "error", "retryable": False,
                                          "message": "터미널 접근 권한이 만료되었습니다."})
                            ws.send_close(1008)
                            return
                        except Exception:
                            pass
                        last_check = time.monotonic()
                    if not readable:
                        try:
                            ws.send_ping()
                        except OSError:
                            return
                        continue
                    chunk = unix_sock.recv(16384)
                    if not chunk:
                        return
                    unix_buf.extend(chunk)
                    if len(unix_buf) > MAX_UNIX_BUF:
                        return
                    while b"\n" in unix_buf:
                        line, _, rest = unix_buf.partition(b"\n")
                        unix_buf = bytearray(rest)
                        if not emit_unix_line(line):
                            return
            except OSError:
                return
            finally:
                stop.set()
                _close_sock(unix_sock)
                ws.close()

        pump = threading.Thread(target=output_pump, daemon=True)
        pump.start()
        try:
            while not stop.is_set():
                try:
                    w_kind, w_payload = ws.recv_message(timeout=None)
                except (EOFError, OSError, ProtocolError):
                    break
                if w_kind == "close":
                    break
                if w_kind == "text":
                    forward_cmd(str(w_payload))
        finally:
            stop.set()
            _close_sock(unix_sock)
            ws.close()
            pump.join(timeout=2.0)
    finally:
        _close_sock(unix_sock)
        if ws is not None:
            ws.close()
        active_slots.release()
