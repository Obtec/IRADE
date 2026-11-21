"""Test double for the Agora runtime manager socket: same request shapes, local bash PTYs instead of VMs.

Run standalone:  python fake_runtime.py /path/manager.sock /path/workspace-root
"""
import base64
import json
import os
import pty
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

MAX_BUFFER = 262144


class Session:
    def __init__(self, cwd, cols, rows):
        self.master, slave = pty.openpty()
        env = {"HOME": cwd, "TERM": "xterm-256color", "LANG": "C.UTF-8",
               "PATH": f"{cwd}/.local/bin:/usr/local/bin:/usr/bin:/bin", "PS1": "$ "}
        self.proc = subprocess.Popen(["bash", "--noprofile", "--norc", "-i"], stdin=slave, stdout=slave, stderr=slave,
                                     cwd=cwd, env=env, start_new_session=True)
        os.close(slave)
        os.set_blocking(self.master, False)
        self.buffer = bytearray()
        self.start = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while self.proc.poll() is None:
            try:
                data = os.read(self.master, 65536)
                with self.lock:
                    self.buffer += data
                    if len(self.buffer) > MAX_BUFFER:
                        drop = len(self.buffer) - MAX_BUFFER
                        del self.buffer[:drop]
                        self.start += drop
            except BlockingIOError:
                time.sleep(0.01)
            except OSError:
                break

    def result(self, offset):
        with self.lock:
            begin = max(offset, self.start)
            data = bytes(self.buffer[begin - self.start:])
            return {"data": base64.b64encode(data).decode(), "offset": begin + len(data),
                    "truncated": offset < self.start, "exited": self.proc.poll() is not None,
                    "exitCode": self.proc.poll()}


class State:
    def __init__(self, root):
        self.root = Path(root)
        self.sessions = {}
        self.lock = threading.Lock()
        self.requests = []
        self.revision = {}


def make_handler(state: State):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            req = json.loads(self.rfile.readline())
            state.requests.append({k: v for k, v in req.items() if k != "data"})
            cid = req.get("canvasId")
            act = req.get("action")
            ws = state.root if os.environ.get("FAKE_FLAT") == "1" else state.root / str(cid)
            ws.mkdir(parents=True, exist_ok=True)
            if act == "settings_get":
                res = {"revision": state.revision.get(cid, 0), "resourceTemplate": "small",
                       "resourceTemplates": ["small", "medium", "large"], "diskGiB": 6, "diskSizesGiB": [6, 12, 24],
                       "languages": {"python": {"engine": "py312"}}}
            elif act in ("resources_set", "disk_set"):
                state.revision[cid] = state.revision.get(cid, 0) + 1
                res = {"ok": True, "revision": state.revision[cid]}
            elif act in ("reset", "delete"):
                res = {act: True}
            elif act == "terminal":
                key = (cid, req["owner"], req["terminal"])
                op = req["operation"]
                with state.lock:
                    s = state.sessions.get(key)
                    if op == "open" and s is None:
                        s = state.sessions[key] = Session(str(ws), req["cols"], req["rows"])
                if s is None:
                    res = {"expired": True}
                else:
                    if op == "input":
                        os.write(s.master, req["data"].encode())
                    elif op == "close":
                        s.proc.kill()
                        state.sessions.pop(key, None)
                    time.sleep(0.02)
                    res = s.result(req["offset"])
            else:
                res = {"error": "RUNTIME_CONFIG_INVALID", "message": "unsupported in fake"}
            self.wfile.write(json.dumps(res).encode() + b"\n")
    return Handler


class FakeRuntime:
    def __init__(self, sock_path, root):
        self.state = State(root)
        Path(sock_path).unlink(missing_ok=True)
        self.server = socketserver.ThreadingUnixStreamServer(sock_path, make_handler(self.state))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        for s in self.state.sessions.values():
            s.proc.kill()
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    rt = FakeRuntime(sys.argv[1], sys.argv[2])
    os.chmod(sys.argv[1], 0o666)
    print("fake runtime ready", flush=True)
    threading.Event().wait()
