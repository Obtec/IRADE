"""Web-based Lean 4 + Python Proof Workspace Studio & Project-Agora R&D Bridge.

Provides:
1. ``AgoraRedisClient`` & ``WorkspaceLifecycleManager``:
   Redis-backed HOT/COLD lifecycle state machine for Proof Workspaces modeled after
   ``project-agora-BE``'s ``CanvasLifecycleMemory``. When a workspace is opened in a tab,
   it is mounted into Redis (``HOT``); while active, edits and cell executions update Redis
   in real time; when closed/unmounted, the latest snapshot is flushed to persistent disk
   storage, evicted from Redis, and its live kernels are shut down (``COLD``).
2. ``ProofKernelPool``: Bounded LRU pool of live ``lean4`` (Lean 4 REPL) and ``python``
   (``ipykernel``) Jupyter kernels per ``(user_id, workspace_id, language)``.
3. ``NotebookStore``: Persistent disk store for workspaces containing Lean 4 proof cells
   (``lean4``), Python code cells (``python``), and Markdown cells (``markdown``), with
   ``.ipynb`` and ``.lean`` export/import.
4. ``AgoraBridge``: Integration with ``project-agora`` authentication and collaborative canvases.
5. ``ProofNotebookHTTPServer``: Threaded HTTP server serving the VS Code Workbench UI and REST API.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import queue
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover - optional dependency for AgoraBridge
    httpx = None  # type: ignore[assignment]
from jupyter_client import KernelManager

from .magics import MAGIC_NAMES
from .unicode_map import UNICODE_ABBREVIATIONS

MAGIC_HELP: dict[str, str] = {
    "%env": "현재 Lean 4 환경(env) 스냅샷, proofState, 선언 목록 조회",
    "%goals": "현재 열린 증명 목표(open goals) 조회",
    "%history": "기록된 모든 환경(env) 및 증명 상태(proofState) 히스토리 조회",
    "%undo": "직전 셀의 (env, proofState) 상태로 되돌리기",
    "%reset": "루트 환경으로 초기화 (%reset --hard 시 REPL 재시작)",
    "%infoview": "직전 셀의 대화형 Lean 4 InfoView 열기 또는 모드 설정",
    "%tactic": "활성 proofState에 단일 전술(tactic) 실행",
    "%%tactic": "셀 매직: 여러 줄의 전술 스크립트를 proofState에서 실행",
    "%load": "외부 .lean 소스 파일을 REPL File Mode로 로드",
    "%pickle": "현재 env 또는 proofState를 .olean 스냅샷 파일로 저장",
    "%unpickle": ".olean 스냅샷 파일에서 env 또는 proofState 복원",
    "%project": "Lake 프로젝트 디렉터리 확인 또는 전환",
    "%config": "커널 옵션(all_tactics, infotree, timeout 등) 조회/변경",
    "%help": "전체 커널 매직 명령어 도움말 출력",
}
for _m in MAGIC_NAMES:
    MAGIC_HELP.setdefault(_m, f"Lean 4 Kernel Magic {_m}")

try:
    from agora_canvas import AgoraClient
    from agora_canvas.client import AgoraError
except Exception:  # pragma: no cover - optional fallback if wheel not installed
    AgoraClient = None  # type: ignore[assignment]

    class AgoraError(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, message: str, body: Any = None) -> None:
            super().__init__(f"HTTP {status_code}: {message}")
            self.status_code = status_code
            self.message = message
            self.body = body


STATIC_DIR = Path(__file__).resolve().parent / "static"

MAX_KERNELS = 16
MAX_OUTPUT_ITEMS = 64
MAX_STREAM_CHARS = 128 * 1024
MAX_HTML_CHARS = 512 * 1024
MAX_IMAGE_CHARS = 2 * 1024 * 1024
DEFAULT_EXEC_TIMEOUT_S = 45.0
WORKSPACE_REDIS_TTL_S = 3600

LEAN_KEYWORDS: tuple[str, ...] = (
    "theorem",
    "lemma",
    "def",
    "abbrev",
    "example",
    "structure",
    "inductive",
    "class",
    "instance",
    "axiom",
    "opaque",
    "variable",
    "namespace",
    "section",
    "end",
    "open",
    "import",
    "where",
    "by",
    "have",
    "let",
    "show",
    "suffices",
    "calc",
    "match",
    "with",
    "if",
    "then",
    "else",
    "fun",
    "forall",
    "exists",
    "intro",
    "intros",
    "apply",
    "exact",
    "refine",
    "rw",
    "simp",
    "omega",
    "decide",
    "cases",
    "rcases",
    "induction",
    "constructor",
    "left",
    "right",
    "ext",
    "funext",
    "congr",
    "ring",
    "linarith",
    "aesop",
    "contradiction",
    "assumption",
    "trivial",
    "rfl",
    "#check",
    "#eval",
    "#print",
    "#reduce",
    "#synth",
)

PYTHON_KEYWORDS: tuple[str, ...] = (
    "import",
    "from",
    "def",
    "class",
    "return",
    "for",
    "while",
    "if",
    "elif",
    "else",
    "try",
    "except",
    "finally",
    "with",
    "as",
    "lambda",
    "yield",
    "assert",
    "print",
    "range",
    "len",
    "enumerate",
    "zip",
    "sum",
    "min",
    "max",
    "sorted",
    "list",
    "dict",
    "set",
    "tuple",
)


def _ensure_elan_in_path(env: dict[str, str] | None = None) -> dict[str, str]:
    """Ensure ``~/.elan/bin`` is present in PATH for child Lean/REPL processes."""
    merged = dict(os.environ if env is None else env)
    elan_bin = Path.home() / ".elan" / "bin"
    current_path = merged.get("PATH", "")
    if elan_bin.is_dir() and str(elan_bin) not in current_path.split(os.pathsep):
        merged["PATH"] = f"{elan_bin}{os.pathsep}{current_path}" if current_path else str(elan_bin)
    return merged


def _read_agora_be_env() -> dict[str, str]:
    """Parse ``project-agora-BE/.env`` if present on disk for shared Redis/TLS discovery."""
    values: dict[str, str] = {}
    candidates = [
        Path(__file__).resolve().parents[3] / "project-agora-BE" / ".env",
        Path("/home/steam/Github/project-agora-BE/.env"),
    ]
    for env_path in candidates:
        if env_path.is_file():
            try:
                for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip('"').strip("'")
                break
            except OSError:
                pass
    return values


def _detect_agora_ca_cert() -> str | None:
    """Auto-detect Project-Agora local root CA certificate if present on disk."""
    explicit = os.environ.get("AGORA_CA_CERT", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit

    candidates: list[Path] = [
        Path.home() / ".local" / "share" / "project-agora" / "tls-20261013" / "root-ca.pem",
        Path("/home/steam/.local/share/project-agora/tls-20261013/root-ca.pem"),
    ]
    be_env = _read_agora_be_env()
    if be_env.get("NGINX_TLS_CERT_DIR"):
        candidates.insert(0, Path(be_env["NGINX_TLS_CERT_DIR"]) / "root-ca.pem")

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _sanitize_id(value: str, fallback: str = "default") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (value or "").strip())
    return cleaned[:96] or fallback


def _truncate_text(value: Any, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n... [output truncated]", True


def _normalize_language(raw_lang: Any, raw_cell_type: Any = None, source: str = "") -> str:
    """Normalize cell language to ``'lean4'``, ``'rocq'``, ``'python'``, or ``'markdown'``."""
    ctype = str(raw_cell_type or "").strip().lower()
    if ctype in ("markdown", "text", "md"):
        return "markdown"
    if ctype in ("python", "py", "python3"):
        return "python"
    if ctype in ("rocq", "coq"):
        return "rocq"
    if ctype in ("lean4", "lean", "proof"):
        return "lean4"

    lang = str(raw_lang or "").strip().lower()
    if lang in ("python", "python3", "py"):
        return "python"
    if lang in ("rocq", "coq"):
        return "rocq"
    if lang in ("lean4", "lean"):
        return "lean4"

    first_line = (source or "").lstrip().splitlines()[0].strip().lower() if (source or "").strip() else ""
    if first_line in ("#!python", "#!python3", "%%python", "%%py"):
        return "python"
    if first_line in ("#!rocq", "#!coq", "%%rocq", "%%coq"):
        return "rocq"
    return "lean4"


def _normalize_cell_output(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    output_type = str(raw.get("output_type") or "").strip()
    if output_type == "stream":
        return {
            "output_type": "stream",
            "name": "stderr" if raw.get("name") == "stderr" else "stdout",
            "text": str(raw.get("text") or ""),
        }
    if output_type in ("display_data", "execute_result"):
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        normalized_data: dict[str, str] = {}
        for mime in ("text/plain", "text/html", "text/markdown", "image/svg+xml", "image/png"):
            if isinstance(data.get(mime), str) and data.get(mime):
                normalized_data[mime] = data[mime]
            elif isinstance(data.get(mime), list):
                normalized_data[mime] = "".join(str(x) for x in data[mime])
        out: dict[str, Any] = {
            "output_type": output_type,
            "data": normalized_data,
            "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        }
        if output_type == "execute_result":
            out["execution_count"] = (
                raw.get("execution_count") if isinstance(raw.get("execution_count"), int) else None
            )
        return out
    if output_type == "error":
        tb = raw.get("traceback")
        return {
            "output_type": "error",
            "ename": str(raw.get("ename") or "ExecutionError"),
            "evalue": str(raw.get("evalue") or ""),
            "traceback": [str(line) for line in tb] if isinstance(tb, list) else [],
        }
    return None


def _normalize_cell(raw: Any, index: int = 0) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    cell_id = _sanitize_id(str(raw.get("id") or ""), fallback=f"cell-{index + 1}-{uuid.uuid4().hex[:6]}")
    raw_type = str(raw.get("cellType") or raw.get("cell_type") or "code").strip().lower()
    source_val = raw.get("source", "")
    if isinstance(source_val, list):
        source = "".join(str(line) for line in source_val)
    else:
        source = str(source_val or "")

    meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    raw_lang = raw.get("language") or meta.get("language")
    language = _normalize_language(raw_lang, raw_type, source)
    cell_type = "markdown" if language == "markdown" else "code"

    exec_count = raw.get("executionCount", raw.get("execution_count"))
    if not isinstance(exec_count, int):
        exec_count = None

    raw_outputs = raw.get("outputs") if isinstance(raw.get("outputs"), list) else []
    outputs = []
    if cell_type == "code":
        for item in raw_outputs[:MAX_OUTPUT_ITEMS]:
            norm = _normalize_cell_output(item)
            if norm is not None:
                outputs.append(norm)

    last_status = str(raw.get("lastRunStatus") or "idle")
    if last_status not in ("idle", "running", "ok", "error"):
        last_status = "idle"

    return {
        "id": cell_id,
        "cellType": cell_type,
        "language": language,
        "source": source,
        "executionCount": exec_count if cell_type == "code" else None,
        "outputs": outputs,
        "lastRunStatus": last_status if cell_type == "code" else "idle",
    }


class AgoraRedisClient:
    """Zero-dependency RESP2 client for Project-Agora Redis (TLS + ACL auth) with in-memory fallback.

    Connects directly to ``agora-redis-primary`` (``172.20.0.2:6379`` or ``REDIS_HOST``) using
    ``project-agora-BE/.env`` credentials and TLS CA certificate. If the Redis container is
    unreachable (e.g. in isolated unit test environments), it transparently falls back to an
    in-memory RESP store with identical key/hash/set semantics.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        ca_cert: str | None = None,
        use_tls: bool | None = None,
        force_memory: bool = False,
    ) -> None:
        be_env = _read_agora_be_env()
        self.host = (
            host
            or os.environ.get("PROOF_REDIS_HOST")
            or os.environ.get("REDIS_HOST")
            or "172.20.0.2"
        )
        self.port = int(
            port
            or os.environ.get("PROOF_REDIS_PORT")
            or os.environ.get("REDIS_PORT")
            or 6379
        )
        self.username = (
            username
            if username is not None
            else (os.environ.get("REDIS_USER") or be_env.get("REDIS_USER") or "agora_user")
        )
        self.password = (
            password
            if password is not None
            else (
                os.environ.get("REDIS_USER_PASSWORD")
                or be_env.get("REDIS_USER_PASSWORD")
                or os.environ.get("REDIS_PASSWORD")
                or be_env.get("REDIS_PASSWORD")
                or ""
            )
        )
        self.ca_cert = (
            ca_cert
            if ca_cert is not None
            else (os.environ.get("REDIS_TLS_CA_CERT") or be_env.get("REDIS_TLS_CA_CERT") or "")
        )
        if use_tls is None:
            tls_env = os.environ.get("REDIS_TLS_ENABLED") or be_env.get("REDIS_TLS_ENABLED") or "true"
            self.use_tls = str(tls_env).lower() in ("1", "true", "yes")
        else:
            self.use_tls = bool(use_tls)

        self._lock = threading.RLock()
        self._mem_kv: dict[str, str] = {}
        self._mem_hashes: dict[str, dict[str, str]] = {}
        self._mem_sets: dict[str, set[str]] = {}
        self._live_available: bool | None = False if force_memory else None
        self._backend_name: str = "memory-fallback" if force_memory else "unprobed"

    def _open_conn(self) -> tuple[socket.socket, Any]:
        raw_sock = socket.create_connection((self.host, self.port), timeout=1.5)
        if self.use_tls:
            if self.ca_cert and Path(self.ca_cert).is_file():
                ctx = ssl.create_default_context(cafile=self.ca_cert)
                ctx.check_hostname = False
            else:
                ctx = ssl._create_unverified_context()
            conn: socket.socket = ctx.wrap_socket(raw_sock, server_hostname=self.host)
        else:
            conn = raw_sock
        reader = conn.makefile("rb")
        if self.password:
            if self.username:
                self._send_cmd(conn, "AUTH", self.username, self.password)
            else:
                self._send_cmd(conn, "AUTH", self.password)
            _ = self._read_resp(reader)
        return conn, reader

    @staticmethod
    def _send_cmd(conn: socket.socket, *parts: Any) -> None:
        chunks = [f"*{len(parts)}\r\n".encode("utf-8")]
        for part in parts:
            b = str(part).encode("utf-8")
            chunks.append(f"${len(b)}\r\n".encode("utf-8"))
            chunks.append(b + b"\r\n")
        conn.sendall(b"".join(chunks))

    @classmethod
    def _read_resp(cls, reader: Any) -> Any:
        line = reader.readline()
        if not line:
            raise ConnectionError("Empty response from Redis")
        prefix = line[:1]
        payload = line[1:-2].decode("utf-8", errors="replace")
        if prefix == b"+":
            return payload
        if prefix == b"-":
            raise RuntimeError(f"Redis error: {payload}")
        if prefix == b":":
            return int(payload)
        if prefix == b"$":
            length = int(payload)
            if length < 0:
                return None
            data = reader.read(length)
            reader.read(2)  # trailing \r\n
            return data.decode("utf-8", errors="replace")
        if prefix == b"*":
            count = int(payload)
            if count < 0:
                return None
            return [cls._read_resp(reader) for _ in range(count)]
        raise RuntimeError(f"Unexpected RESP prefix: {prefix!r}")

    def _exec(self, *parts: Any) -> Any:
        with self._lock:
            if self._live_available is False:
                raise ConnectionError("Live Redis disabled")
            try:
                conn, reader = self._open_conn()
                try:
                    self._send_cmd(conn, *parts)
                    res = self._read_resp(reader)
                    self._live_available = True
                    self._backend_name = f"redis-tls://{self.host}:{self.port}" if self.use_tls else f"redis://{self.host}:{self.port}"
                    return res
                finally:
                    try:
                        reader.close()
                    except Exception:
                        pass
                    try:
                        conn.close()
                    except Exception:
                        pass
            except Exception:
                self._live_available = False
                self._backend_name = "memory-fallback"
                raise

    def ping(self) -> bool:
        try:
            return self._exec("PING") == "PONG"
        except Exception:
            return False

    def info(self) -> dict[str, Any]:
        with self._lock:
            if self._live_available is None:
                self.ping()
            return {
                "liveRedis": bool(self._live_available),
                "backend": self._backend_name,
                "host": self.host,
                "port": self.port,
                "tls": self.use_tls,
            }

    def get(self, key: str) -> str | None:
        try:
            val = self._exec("GET", key)
            return str(val) if val is not None else None
        except Exception:
            with self._lock:
                return self._mem_kv.get(key)

    def set(self, key: str, value: str, ex: int | None = WORKSPACE_REDIS_TTL_S) -> bool:
        try:
            if ex and ex > 0:
                self._exec("SET", key, value, "EX", int(ex))
            else:
                self._exec("SET", key, value)
            return True
        except Exception:
            with self._lock:
                self._mem_kv[key] = str(value)
                return True

    def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        try:
            return int(self._exec("DEL", *keys) or 0)
        except Exception:
            with self._lock:
                removed = 0
                for k in keys:
                    if k in self._mem_kv:
                        del self._mem_kv[k]
                        removed += 1
                    if k in self._mem_hashes:
                        del self._mem_hashes[k]
                        removed += 1
                    if k in self._mem_sets:
                        del self._mem_sets[k]
                        removed += 1
                return removed

    def hset(self, key: str, mapping: dict[str, Any], ex: int | None = WORKSPACE_REDIS_TTL_S) -> None:
        if not mapping:
            return
        flat: list[Any] = []
        for k, v in mapping.items():
            flat.extend([str(k), str(v)])
        try:
            self._exec("HSET", key, *flat)
            if ex and ex > 0:
                self._exec("EXPIRE", key, int(ex))
        except Exception:
            with self._lock:
                h = self._mem_hashes.setdefault(key, {})
                for k, v in mapping.items():
                    h[str(k)] = str(v)

    def hgetall(self, key: str) -> dict[str, str]:
        try:
            raw = self._exec("HGETALL", key)
            if isinstance(raw, list):
                it = iter(raw)
                return {str(k): str(v) for k, v in zip(it, it)}
            return {}
        except Exception:
            with self._lock:
                return dict(self._mem_hashes.get(key, {}))

    def sadd(self, key: str, member: str, ex: int | None = WORKSPACE_REDIS_TTL_S) -> None:
        try:
            self._exec("SADD", key, member)
            if ex and ex > 0:
                self._exec("EXPIRE", key, int(ex))
        except Exception:
            with self._lock:
                self._mem_sets.setdefault(key, set()).add(str(member))

    def srem(self, key: str, member: str) -> None:
        try:
            self._exec("SREM", key, member)
        except Exception:
            with self._lock:
                s = self._mem_sets.get(key)
                if s:
                    s.discard(str(member))

    def scard(self, key: str) -> int:
        try:
            return int(self._exec("SCARD", key) or 0)
        except Exception:
            with self._lock:
                return len(self._mem_sets.get(key, set()))


class ProofKernelPool:
    """Bounded LRU pool of live ``lean4`` and ``python`` Jupyter kernels per ``(user_id, workspace_id, language)``.

    Mirrors the kernel management pattern in ``hypervisor-for-agora/runtime/guest/notebook_worker.py``
    while supporting both Lean 4 proof verification (``lean4_kernel``) and Python 3 computation
    (``ipykernel_launcher``) inside the same workspace.
    """

    def __init__(self, max_kernels: int = MAX_KERNELS) -> None:
        self.max_kernels = max(1, int(max_kernels))
        self._lock = threading.RLock()
        self._exec_locks: dict[str, threading.Lock] = {}
        self._kernels: dict[str, tuple[KernelManager, Any]] = {}
        self._access_times: dict[str, float] = {}
        self._exec_counts: dict[str, int] = {}
        self._started_at: dict[str, float] = {}
        self._key_langs: dict[str, str] = {}

    @staticmethod
    def _make_key(user_id: str, notebook_id: str, language: str = "lean4") -> str:
        raw_l = str(language or "").lower()
        if raw_l in ("python", "python3", "py"):
            lang = "python"
        elif raw_l in ("rocq", "coq"):
            lang = "rocq"
        else:
            lang = "lean4"
        return f"{_sanitize_id(user_id, 'local')}::{_sanitize_id(notebook_id, 'default')}::{lang}"

    def _get_exec_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lock = self._exec_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._exec_locks[key] = lock
            return lock

    def _shutdown_key_unlocked(self, key: str) -> None:
        pair = self._kernels.pop(key, None)
        self._access_times.pop(key, None)
        self._exec_counts.pop(key, None)
        self._started_at.pop(key, None)
        self._key_langs.pop(key, None)
        if not pair:
            return
        km, kc = pair
        try:
            kc.stop_channels()
        except Exception:
            pass
        try:
            km.shutdown_kernel(now=True)
        except Exception:
            pass

    def _get_or_start_kernel(self, key: str, language: str = "lean4") -> tuple[KernelManager, Any]:
        raw_l = str(language or "").lower()
        if raw_l in ("python", "python3", "py"):
            lang = "python"
        elif raw_l in ("rocq", "coq"):
            lang = "rocq"
        else:
            lang = "lean4"
        with self._lock:
            existing = self._kernels.get(key)
            if existing is not None:
                km, kc = existing
                try:
                    if km.is_alive():
                        self._access_times[key] = time.monotonic()
                        return km, kc
                except Exception:
                    pass
                self._shutdown_key_unlocked(key)

            # Evict LRU kernel if pool is full
            if len(self._kernels) >= self.max_kernels and self._access_times:
                lru_key = min(self._access_times, key=self._access_times.get)  # type: ignore[arg-type]
                self._shutdown_key_unlocked(lru_key)

            os.environ.update(_ensure_elan_in_path())
            if lang == "python":
                km = KernelManager(kernel_name="python3")
                try:
                    km.kernel_spec.argv = [
                        sys.executable,
                        "-m",
                        "ipykernel_launcher",
                        "-f",
                        "{connection_file}",
                    ]
                    km.kernel_spec.env = _ensure_elan_in_path()
                except Exception:
                    pass
            elif lang == "rocq":
                km = KernelManager(kernel_name="rocq")
                try:
                    km.kernel_spec.argv = [
                        sys.executable,
                        "-m",
                        "rocq_kernel",
                        "-f",
                        "{connection_file}",
                    ]
                    km.kernel_spec.env = _ensure_elan_in_path()
                except Exception:
                    pass
            else:
                km = KernelManager(kernel_name="lean4")
                try:
                    km.kernel_spec.argv = [
                        sys.executable,
                        "-m",
                        "lean4_kernel",
                        "-f",
                        "{connection_file}",
                    ]
                    km.kernel_spec.env = _ensure_elan_in_path()
                except Exception:
                    pass

            km.start_kernel(env=_ensure_elan_in_path())
            kc = km.client()
            kc.start_channels()
            try:
                kc.wait_for_ready(timeout=25)
            except Exception:
                self._shutdown_key_unlocked(key)
                raise

            now = time.monotonic()
            self._kernels[key] = (km, kc)
            self._access_times[key] = now
            self._exec_counts[key] = 0
            self._started_at[key] = time.time()
            self._key_langs[key] = lang
            return km, kc

    def _single_lang_status_unlocked(self, key: str, lang: str) -> dict[str, Any]:
        pair = self._kernels.get(key)
        alive = False
        if pair is not None:
            try:
                alive = bool(pair[0].is_alive())
            except Exception:
                alive = False
        exec_lock = self._exec_locks.get(key)
        busy = bool(exec_lock and exec_lock.locked())
        kname = "python3" if lang == "python" else ("rocq" if lang == "rocq" else "lean4")
        return {
            "kernelKey": key,
            "kernelName": kname,
            "language": lang,
            "alive": alive,
            "state": "busy" if busy else ("idle" if alive else "stopped"),
            "executionCount": self._exec_counts.get(key, 0),
            "startedAt": self._started_at.get(key),
        }

    def status(self, user_id: str, notebook_id: str, language: str = "lean4") -> dict[str, Any]:
        lean_key = self._make_key(user_id, notebook_id, "lean4")
        rocq_key = self._make_key(user_id, notebook_id, "rocq")
        py_key = self._make_key(user_id, notebook_id, "python")
        with self._lock:
            lean_st = self._single_lang_status_unlocked(lean_key, "lean4")
            rocq_st = self._single_lang_status_unlocked(rocq_key, "rocq")
            py_st = self._single_lang_status_unlocked(py_key, "python")
            raw_l = str(language).lower()
            primary = (
                py_st
                if raw_l == "python"
                else (rocq_st if raw_l in ("rocq", "coq") else lean_st)
            )
            any_busy = (
                lean_st["state"] == "busy"
                or rocq_st["state"] == "busy"
                or py_st["state"] == "busy"
            )
            any_alive = lean_st["alive"] or rocq_st["alive"] or py_st["alive"]
            return {
                "kernelKey": primary["kernelKey"],
                "kernelName": primary["kernelName"],
                "alive": any_alive,
                "state": "busy" if any_busy else ("idle" if any_alive else "stopped"),
                "executionCount": (
                    lean_st["executionCount"]
                    + rocq_st["executionCount"]
                    + py_st["executionCount"]
                ),
                "startedAt": (
                    lean_st["startedAt"]
                    or rocq_st["startedAt"]
                    or py_st["startedAt"]
                ),
                "activeKernels": len(self._kernels),
                "maxKernels": self.max_kernels,
                "kernels": {
                    "lean4": lean_st,
                    "rocq": rocq_st,
                    "python": py_st,
                },
            }

    def execute(
        self,
        user_id: str,
        notebook_id: str,
        code: str,
        cell_id: str | None = None,
        language: str = "lean4",
        timeout: float = DEFAULT_EXEC_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Execute a Lean 4 or Python cell in the workspace's kernel and return normalized outputs."""
        lang = _normalize_language(language, "code", code)
        if lang == "markdown":
            lang = "lean4"

        # Strip optional magic shebang `#!python` / `%%python` when running in Python kernel
        clean_code = code or ""
        if lang == "python":
            lines = clean_code.splitlines()
            if lines and lines[0].strip().lower() in ("#!python", "#!python3", "%%python", "%%py"):
                clean_code = "\n".join(lines[1:])

        key = self._make_key(user_id, notebook_id, lang)
        exec_lock = self._get_exec_lock(key)
        with exec_lock:
            try:
                km, kc = self._get_or_start_kernel(key, lang)
            except Exception as exc:
                return {
                    "cellId": cell_id,
                    "language": lang,
                    "outputs": [],
                    "executionCount": None,
                    "exitCode": 1,
                    "timedOut": False,
                    "truncated": False,
                    "error": f"Failed to start {lang} kernel: {exc}",
                }

            msg_id = kc.execute(clean_code, allow_stdin=False, stop_on_error=True)
            outputs: list[dict[str, Any]] = []
            execution_count: int | None = None
            timed_out = False
            truncated = False
            had_error = False
            deadline = time.monotonic() + max(1.0, min(float(timeout), 180.0))

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    try:
                        km.interrupt_kernel()
                    except Exception:
                        pass
                    break

                try:
                    event = kc.get_iopub_msg(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                except Exception:
                    break

                if event.get("parent_header", {}).get("msg_id") != msg_id:
                    continue

                kind = event.get("msg_type", "")
                content = event.get("content", {}) or {}

                if kind == "status" and content.get("execution_state") == "idle":
                    break

                if kind == "execute_input":
                    if isinstance(content.get("execution_count"), int):
                        execution_count = content["execution_count"]
                    continue

                if len(outputs) >= MAX_OUTPUT_ITEMS:
                    truncated = True
                    continue

                if kind == "stream":
                    text, cut = _truncate_text(content.get("text", ""), MAX_STREAM_CHARS)
                    truncated = truncated or cut
                    stream_name = "stderr" if content.get("name") == "stderr" else "stdout"
                    if (
                        outputs
                        and outputs[-1].get("output_type") == "stream"
                        and outputs[-1].get("name") == stream_name
                    ):
                        combined, cut2 = _truncate_text(
                            outputs[-1].get("text", "") + text, MAX_STREAM_CHARS
                        )
                        outputs[-1]["text"] = combined
                        truncated = truncated or cut2
                    else:
                        outputs.append(
                            {
                                "output_type": "stream",
                                "name": stream_name,
                                "text": text,
                            }
                        )

                elif kind in ("display_data", "execute_result"):
                    data = content.get("data") or {}
                    norm_data: dict[str, str] = {}
                    for mime, limit in (
                        ("text/plain", MAX_STREAM_CHARS),
                        ("text/html", MAX_HTML_CHARS),
                        ("text/markdown", MAX_STREAM_CHARS),
                        ("image/svg+xml", MAX_IMAGE_CHARS),
                    ):
                        if mime in data and data[mime]:
                            val, cut = _truncate_text(data[mime], limit)
                            norm_data[mime] = val
                            truncated = truncated or cut
                    if "image/png" in data and data["image/png"]:
                        png_val = str(data["image/png"])
                        if len(png_val) <= MAX_IMAGE_CHARS:
                            norm_data["image/png"] = png_val
                        else:
                            truncated = True

                    if norm_data:
                        item: dict[str, Any] = {
                            "output_type": kind,
                            "data": norm_data,
                            "metadata": content.get("metadata") or {},
                        }
                        if kind == "execute_result":
                            ec = content.get("execution_count")
                            if isinstance(ec, int):
                                execution_count = ec
                            item["execution_count"] = execution_count
                        outputs.append(item)

                elif kind == "error":
                    had_error = True
                    default_err = "PythonError" if lang == "python" else "LeanError"
                    ename, _ = _truncate_text(content.get("ename", default_err), 256)
                    evalue, cut = _truncate_text(content.get("evalue", ""), 8192)
                    truncated = truncated or cut
                    tb_lines: list[str] = []
                    for line in (content.get("traceback") or [])[:40]:
                        tline, cut_tb = _truncate_text(line, 2048)
                        tb_lines.append(tline)
                        truncated = truncated or cut_tb
                    outputs.append(
                        {
                            "output_type": "error",
                            "ename": ename,
                            "evalue": evalue,
                            "traceback": tb_lines,
                        }
                    )

            with self._lock:
                self._access_times[key] = time.monotonic()
                if execution_count is not None:
                    self._exec_counts[key] = max(self._exec_counts.get(key, 0), execution_count)
                else:
                    self._exec_counts[key] = self._exec_counts.get(key, 0) + 1
                    execution_count = self._exec_counts[key]

            return {
                "cellId": cell_id,
                "language": lang,
                "outputs": outputs,
                "executionCount": execution_count,
                "exitCode": 124 if timed_out else (1 if had_error else 0),
                "timedOut": timed_out,
                "truncated": truncated,
                "error": "Execution timed out" if timed_out else None,
            }

    def complete(
        self,
        user_id: str,
        notebook_id: str,
        code: str,
        cursor_pos: int | None = None,
        language: str = "lean4",
    ) -> dict[str, Any]:
        """Provide fast Unicode + Lean 4 / Python + live kernel completion at ``cursor_pos``."""
        lang = _normalize_language(language, "code", code)
        text = code or ""
        pos = len(text) if cursor_pos is None else max(0, min(int(cursor_pos), len(text)))
        before = text[:pos]

        # 1. Backslash Unicode abbreviation completion (instant, no ZMQ round-trip needed)
        bs_idx = before.rfind("\\")
        if bs_idx != -1 and " " not in before[bs_idx:] and "\n" not in before[bs_idx:]:
            token_with_bs = before[bs_idx:]
            prefix = before[bs_idx + 1 :]
            exact = UNICODE_ABBREVIATIONS.get(token_with_bs) or UNICODE_ABBREVIATIONS.get(prefix)
            matches: list[dict[str, str]] = []
            if exact:
                matches.append({"insert": exact, "label": f"\\{prefix} → {exact}", "kind": "unicode"})
            for abbr, sym in sorted(UNICODE_ABBREVIATIONS.items()):
                clean_abbr = abbr.lstrip("\\")
                if clean_abbr.startswith(prefix) and clean_abbr != prefix:
                    matches.append({"insert": sym, "label": f"\\{clean_abbr} → {sym}", "kind": "unicode"})
                    if len(matches) >= 30:
                        break
            return {
                "cursorStart": bs_idx,
                "cursorEnd": pos,
                "matches": matches,
                "exactSymbol": exact,
                "mode": "unicode",
            }

        # 2. Word / magic completion
        m = re.search(r"(%%?[A-Za-z0-9_]*|#[A-Za-z0-9_]*|[A-Za-z0-9_'.]+)$", before)
        token = m.group(1) if m else ""
        start = pos - len(token)
        items: list[dict[str, str]] = []
        seen: set[str] = set()

        # Check live kernel shell channel if kernel is already running and idle
        key = self._make_key(user_id, notebook_id, lang)
        exec_lock = self._get_exec_lock(key)
        if not exec_lock.locked():
            with self._lock:
                pair = self._kernels.get(key)
            if pair is not None:
                _, kc = pair
                try:
                    msg_id = kc.complete(text, pos)
                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        reply = kc.get_shell_msg(timeout=0.25)
                        if reply.get("parent_header", {}).get("msg_id") == msg_id:
                            content = reply.get("content", {}) or {}
                            if content.get("status") == "ok":
                                start = int(content.get("cursor_start", start))
                                pos = int(content.get("cursor_end", pos))
                                for cand in content.get("matches", [])[:40]:
                                    cand_str = str(cand)
                                    if cand_str not in seen:
                                        seen.add(cand_str)
                                        items.append(
                                            {
                                                "insert": cand_str,
                                                "label": cand_str,
                                                "kind": "kernel",
                                            }
                                        )
                            break
                except Exception:
                    pass

        if token:
            if lang == "python":
                for kw in PYTHON_KEYWORDS:
                    if kw.startswith(token) and kw not in seen:
                        seen.add(kw)
                        items.append({"insert": kw, "label": kw, "kind": "python"})
            else:
                for magic_name, desc in MAGIC_HELP.items():
                    if magic_name.startswith(token) and magic_name not in seen:
                        seen.add(magic_name)
                        items.append({"insert": magic_name, "label": f"{magic_name} — {desc}", "kind": "magic"})
                for kw in LEAN_KEYWORDS:
                    kw_clean = kw.strip()
                    if kw_clean.startswith(token) and kw_clean not in seen:
                        seen.add(kw_clean)
                        items.append({"insert": kw_clean, "label": kw_clean, "kind": "keyword"})

        return {
            "cursorStart": start,
            "cursorEnd": pos,
            "matches": items[:40],
            "exactSymbol": None,
            "mode": "identifier",
        }

    def inspect_symbol(
        self,
        user_id: str,
        notebook_id: str,
        code: str,
        cursor_pos: int | None = None,
        detail_level: int = 0,
        language: str = "lean4",
    ) -> dict[str, Any]:
        """Inspect the Lean 4 or Python identifier at ``cursor_pos`` on the shell channel."""
        lang = _normalize_language(language, "code", code)
        text = code or ""
        pos = len(text) if cursor_pos is None else max(0, min(int(cursor_pos), len(text)))
        key = self._make_key(user_id, notebook_id, lang)
        exec_lock = self._get_exec_lock(key)
        with exec_lock:
            try:
                _, kc = self._get_or_start_kernel(key, lang)
                msg_id = kc.inspect(text, pos, detail_level=detail_level)
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    reply = kc.get_shell_msg(timeout=0.35)
                    if reply.get("parent_header", {}).get("msg_id") == msg_id:
                        content = reply.get("content", {}) or {}
                        data = content.get("data") or {}
                        return {
                            "found": bool(content.get("found")),
                            "text": str(data.get("text/plain") or ""),
                            "html": str(data.get("text/html") or ""),
                        }
            except Exception as exc:
                return {"found": False, "text": "", "html": "", "error": str(exc)}
        return {"found": False, "text": "", "html": ""}

    def interrupt(self, user_id: str, notebook_id: str, language: str | None = None) -> dict[str, Any]:
        langs = [language] if language in ("lean4", "rocq", "python") else ["lean4", "rocq", "python"]
        interrupted_any = False
        for lang in langs:
            key = self._make_key(user_id, notebook_id, str(lang))
            with self._lock:
                pair = self._kernels.get(key)
            if pair is not None:
                km, _ = pair
                try:
                    km.interrupt_kernel()
                    interrupted_any = True
                except Exception:
                    pass
        return {"interrupted": interrupted_any, "status": self.status(user_id, notebook_id)}

    def reset(self, user_id: str, notebook_id: str, language: str | None = None) -> dict[str, Any]:
        """Restart the workspace's Lean 4, Rocq, and/or Python kernels from a clean environment."""
        langs = [language] if language in ("lean4", "rocq", "python") else ["lean4", "rocq", "python"]
        for lang in langs:
            key = self._make_key(user_id, notebook_id, str(lang))
            exec_lock = self._get_exec_lock(key)
            with exec_lock:
                with self._lock:
                    was_running = key in self._kernels
                    self._shutdown_key_unlocked(key)
                if was_running or lang == "lean4":
                    try:
                        self._get_or_start_kernel(key, str(lang))
                    except Exception:
                        pass
        return {"reset": True, "status": self.status(user_id, notebook_id)}

    def shutdown_workspace(self, notebook_id: str) -> int:
        """Shut down all live kernels (Lean 4 & Python) associated with ``notebook_id`` when unmounted to COLD."""
        safe_nb = _sanitize_id(notebook_id, "default")
        marker = f"::{safe_nb}::"
        stopped = 0
        with self._lock:
            matching = [k for k in list(self._kernels.keys()) if marker in k]
            for k in matching:
                self._shutdown_key_unlocked(k)
                stopped += 1
        return stopped

    def shutdown_all(self) -> None:
        with self._lock:
            keys = list(self._kernels.keys())
            for key in keys:
                self._shutdown_key_unlocked(key)


def _default_seed_notebook() -> dict[str, Any]:
    """Reference template with verified Lean 4 proof (including linearzed ``omega``) + Python cell."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "id": "lean4-agora-rd-studio",
        "title": "Lean 4 Formal Verification & R&D Workspace",
        "description": (
            "Interactive cell-by-cell Lean 4 theorem proving and Python computation workspace "
            "integrated with Project-Agora collaborative canvases."
        ),
        "kernelName": "lean4",
        "language": "lean4",
        "linkedCanvasId": None,
        "linkedCanvasItemId": None,
        "createdAt": now_iso,
        "updatedAt": now_iso,
        "cells": [
            {
                "id": "cell-defs-lean",
                "cellType": "code",
                "language": "lean4",
                "source": (
                    "def sumUpTo : Nat → Nat\n"
                    "  | 0 => 0\n"
                    "  | n + 1 => (n + 1) + sumUpTo n\n\n"
                    "#eval sumUpTo 10"
                ),
                "executionCount": None,
                "outputs": [],
                "lastRunStatus": "idle",
            },
            {
                "id": "cell-induction-lean",
                "cellType": "code",
                "language": "lean4",
                "source": (
                    "theorem sumUpTo_double (n : Nat) : 2 * sumUpTo n = n * (n + 1) := by\n"
                    "  induction n with\n"
                    "  | zero =>\n"
                    "    simp [sumUpTo]\n"
                    "  | succ k ih =>\n"
                    "    simp [sumUpTo, Nat.mul_add, Nat.add_mul] at *\n"
                    "    omega"
                ),
                "executionCount": None,
                "outputs": [],
                "lastRunStatus": "idle",
            },
        ],
    }


class NotebookStore:
    """Persistent disk store (COLD storage) for Proof Workspaces / Notebooks."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        base = (
            Path(data_dir)
            if data_dir
            else Path(os.environ.get("PROOF_NOTEBOOK_DATA_DIR", Path.home() / ".cache" / "proof-notebook"))
        )
        self.base_dir = base.resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _user_dir(self, user_id: str) -> Path:
        safe_user = _sanitize_id(user_id, "local-researcher")
        d = self.base_dir / "users" / safe_user / "notebooks"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _shared_dir(self) -> Path:
        d = self.base_dir / "shared_notebooks"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _notebook_path(self, user_id: str, notebook_id: str) -> Path:
        safe_nb = _sanitize_id(notebook_id, "default")
        return self._user_dir(user_id) / f"{safe_nb}.json"

    def _shared_notebook_path(self, notebook_id: str) -> Path:
        safe_nb = _sanitize_id(notebook_id, "default")
        return self._shared_dir() / f"{safe_nb}.json"

    def list_notebooks(self, user_id: str) -> list[dict[str, Any]]:
        """List all persisted workspaces on disk without creating any pre-populated example seed."""
        with self._lock:
            seen_ids: set[str] = set()
            items: list[dict[str, Any]] = []
            candidate_paths = list(sorted(self._user_dir(user_id).glob("*.json"))) + list(
                sorted(self._shared_dir().glob("*.json"))
            )
            for path in candidate_paths:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    nb_id = str(data.get("id") or path.stem)
                    if nb_id in seen_ids:
                        continue
                    shared_path = self._shared_notebook_path(nb_id)
                    if shared_path.is_file():
                        try:
                            data = json.loads(shared_path.read_text(encoding="utf-8"))
                        except Exception:
                            pass
                    seen_ids.add(nb_id)
                    raw_cells = data.get("cells") if isinstance(data.get("cells"), list) else []
                    cells = [_normalize_cell(c, idx) for idx, c in enumerate(raw_cells)]
                    lean_count = sum(1 for c in cells if c["cellType"] == "code" and c["language"] == "lean4")
                    py_count = sum(1 for c in cells if c["cellType"] == "code" and c["language"] == "python")
                    md_count = sum(1 for c in cells if c["cellType"] == "markdown")
                    items.append(
                        {
                            "id": nb_id,
                            "title": data.get("title") or path.stem,
                            "description": data.get("description") or "",
                            "cellCount": len(cells),
                            "codeCellCount": lean_count + py_count,
                            "leanCellCount": lean_count,
                            "pythonCellCount": py_count,
                            "markdownCellCount": md_count,
                            "linkedCanvasId": data.get("linkedCanvasId"),
                            "linkedCanvasItemId": data.get("linkedCanvasItemId"),
                            "updatedAt": data.get("updatedAt") or "",
                        }
                    )
                except Exception:
                    continue
            items.sort(key=lambda x: x.get("updatedAt") or "", reverse=True)
            return items

    def get_notebook(self, user_id: str, notebook_id: str) -> dict[str, Any] | None:
        with self._lock:
            shared_path = self._shared_notebook_path(notebook_id)
            user_path = self._notebook_path(user_id, notebook_id)
            target_path = shared_path if shared_path.is_file() else (user_path if user_path.is_file() else None)
            if target_path is None:
                return None
            raw = json.loads(target_path.read_text(encoding="utf-8"))
            return self._normalize_notebook(raw, fallback_id=notebook_id)

    def save_notebook(self, user_id: str, notebook_data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            nb_id = _sanitize_id(
                str(notebook_data.get("id") or ""),
                fallback=f"ws-{uuid.uuid4().hex[:8]}",
            )
            shared_path = self._shared_notebook_path(nb_id)
            existing_path = self._notebook_path(user_id, nb_id)
            existing: dict[str, Any] = {}
            for p in (shared_path, existing_path):
                if p.is_file():
                    try:
                        existing = json.loads(p.read_text(encoding="utf-8"))
                        break
                    except Exception:
                        existing = {}

            merged = {**existing, **notebook_data, "id": nb_id}
            normalized = self._normalize_notebook(merged, fallback_id=nb_id)
            normalized["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            serialized = json.dumps(normalized, ensure_ascii=False, indent=2)
            existing_path.write_text(serialized, encoding="utf-8")
            shared_path.write_text(serialized, encoding="utf-8")
            return normalized

    def create_notebook(
        self,
        user_id: str,
        title: str | None = None,
        description: str | None = None,
        cells: list[dict[str, Any]] | None = None,
        linked_canvas_id: str | None = None,
        default_language: str = "lean4",
    ) -> dict[str, Any]:
        """Create a clean workspace without pre-filled example code."""
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        nb_id = f"ws-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        lang = "python" if str(default_language or "").lower() in ("python", "python3", "py") else "lean4"
        default_cells = cells if cells is not None else [
            {
                "id": f"cell-{uuid.uuid4().hex[:8]}",
                "cellType": "code",
                "language": lang,
                "source": "",
                "executionCount": None,
                "outputs": [],
                "lastRunStatus": "idle",
            },
        ]
        payload = {
            "id": nb_id,
            "title": (title or "Untitled Workspace").strip(),
            "description": (description or "").strip(),
            "kernelName": "lean4",
            "language": lang,
            "linkedCanvasId": linked_canvas_id,
            "linkedCanvasItemId": None,
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "cells": default_cells,
        }
        return self.save_notebook(user_id, payload)

    def delete_notebook(self, user_id: str, notebook_id: str) -> bool:
        with self._lock:
            path = self._notebook_path(user_id, notebook_id)
            shared_path = self._shared_notebook_path(notebook_id)
            deleted = False
            if path.is_file():
                path.unlink()
                deleted = True
            if shared_path.is_file():
                shared_path.unlink()
                deleted = True
            return deleted

    def update_cell_execution(
        self,
        user_id: str,
        notebook_id: str,
        cell_id: str,
        code: str | None,
        exec_result: dict[str, Any],
        language: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            nb = self.get_notebook(user_id, notebook_id)
            if nb is None:
                return None
            status = "ok" if exec_result.get("exitCode", 1) == 0 else "error"
            resolved_lang = _normalize_language(
                language or exec_result.get("language"),
                "code",
                code or "",
            )
            matched = False
            for cell in nb["cells"]:
                if cell["id"] == cell_id:
                    if code is not None:
                        cell["source"] = code
                    cell["cellType"] = "code"
                    cell["language"] = resolved_lang
                    cell["outputs"] = exec_result.get("outputs", [])
                    cell["executionCount"] = exec_result.get("executionCount")
                    cell["lastRunStatus"] = status
                    matched = True
                    break
            if not matched and code is not None:
                nb["cells"].append(
                    _normalize_cell(
                        {
                            "id": cell_id,
                            "cellType": "code",
                            "language": resolved_lang,
                            "source": code,
                            "outputs": exec_result.get("outputs", []),
                            "executionCount": exec_result.get("executionCount"),
                            "lastRunStatus": status,
                        },
                        len(nb["cells"]),
                    )
                )
            return self.save_notebook(user_id, nb)

    @staticmethod
    def _normalize_notebook(raw: dict[str, Any], fallback_id: str = "default") -> dict[str, Any]:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        nb_id = _sanitize_id(str(raw.get("id") or ""), fallback=fallback_id)
        raw_cells = raw.get("cells") if isinstance(raw.get("cells"), list) else []
        cells = [_normalize_cell(c, idx) for idx, c in enumerate(raw_cells)]
        if not cells:
            cells = [
                _normalize_cell(
                    {
                        "id": f"cell-1-{uuid.uuid4().hex[:6]}",
                        "cellType": "code",
                        "language": str(raw.get("language") or "lean4"),
                        "source": "",
                    },
                    0,
                )
            ]
        return {
            "id": nb_id,
            "title": str(raw.get("title") or "Untitled Workspace").strip()[:160],
            "description": str(raw.get("description") or "").strip()[:1000],
            "kernelName": "lean4",
            "language": str(raw.get("language") or "lean4"),
            "linkedCanvasId": raw.get("linkedCanvasId") or None,
            "linkedCanvasItemId": raw.get("linkedCanvasItemId") or None,
            "createdAt": str(raw.get("createdAt") or now_iso),
            "updatedAt": str(raw.get("updatedAt") or now_iso),
            "cells": cells,
        }

    @staticmethod
    def to_ipynb(notebook: dict[str, Any]) -> dict[str, Any]:
        """Serialize internal workspace dict to Jupyter nbformat v4.5 JSON."""
        nb_cells: list[dict[str, Any]] = []
        for idx, cell in enumerate(notebook.get("cells", [])):
            ctype = "markdown" if cell.get("cellType") == "markdown" else "code"
            lang = _normalize_language(cell.get("language"), ctype, str(cell.get("source") or ""))
            source_text = str(cell.get("source") or "")
            source_lines = source_text.splitlines(keepends=True) if source_text else []
            entry: dict[str, Any] = {
                "id": _sanitize_id(cell.get("id") or f"cell-{idx + 1}"),
                "cell_type": ctype,
                "metadata": {"language": lang} if ctype == "code" else {},
                "source": source_lines,
            }
            if ctype == "code":
                entry["execution_count"] = cell.get("executionCount")
                nb_outputs: list[dict[str, Any]] = []
                for out in cell.get("outputs") or []:
                    otype = out.get("output_type")
                    if otype == "stream":
                        nb_outputs.append(
                            {
                                "output_type": "stream",
                                "name": out.get("name", "stdout"),
                                "text": str(out.get("text") or "").splitlines(keepends=True),
                            }
                        )
                    elif otype in ("display_data", "execute_result"):
                        item: dict[str, Any] = {
                            "output_type": otype,
                            "data": dict(out.get("data") or {}),
                            "metadata": dict(out.get("metadata") or {}),
                        }
                        if otype == "execute_result":
                            item["execution_count"] = out.get("execution_count")
                        nb_outputs.append(item)
                    elif otype == "error":
                        nb_outputs.append(
                            {
                                "output_type": "error",
                                "ename": out.get("ename", "ExecutionError"),
                                "evalue": out.get("evalue", ""),
                                "traceback": list(out.get("traceback") or []),
                            }
                        )
                entry["outputs"] = nb_outputs
            nb_cells.append(entry)

        return {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {
                "title": notebook.get("title", "Lean 4 Proof Workspace"),
                "kernelspec": {
                    "display_name": "Lean 4",
                    "language": "lean4",
                    "name": "lean4",
                },
                "language_info": {
                    "name": "lean4",
                    "file_extension": ".lean",
                    "mimetype": "text/x-lean4",
                },
                "agora_proof_notebook": {
                    "id": notebook.get("id"),
                    "linkedCanvasId": notebook.get("linkedCanvasId"),
                    "linkedCanvasItemId": notebook.get("linkedCanvasItemId"),
                },
            },
            "cells": nb_cells,
        }

    @classmethod
    def from_ipynb(cls, raw_ipynb: dict[str, Any], title: str | None = None) -> dict[str, Any]:
        """Parse a Jupyter ``.ipynb`` JSON payload into internal workspace structure."""
        meta = raw_ipynb.get("metadata") if isinstance(raw_ipynb.get("metadata"), dict) else {}
        resolved_title = (
            title
            or meta.get("title")
            or "Imported Workspace"
        )
        raw_cells = raw_ipynb.get("cells") if isinstance(raw_ipynb.get("cells"), list) else []
        cells = [_normalize_cell(c, idx) for idx, c in enumerate(raw_cells)]
        return cls._normalize_notebook(
            {
                "id": f"ws-{int(time.time())}-{uuid.uuid4().hex[:6]}",
                "title": str(resolved_title),
                "cells": cells,
            }
        )

    @staticmethod
    def to_lean_source(notebook: dict[str, Any]) -> str:
        """Export a Proof Workspace to a standalone ``.lean`` file (wrapping Python cells in doc/comment blocks)."""
        title = notebook.get("title") or "Lean 4 Proof Workspace"
        chunks: list[str] = [
            f"/-!\n# {title}\nExported from Lean 4 Proof Studio × Project-Agora R&D Workbench\n-/\n"
        ]
        for idx, cell in enumerate(notebook.get("cells", []), start=1):
            source = str(cell.get("source") or "").strip()
            if not source:
                continue
            ctype = cell.get("cellType")
            lang = cell.get("language", "lean4")
            if ctype == "markdown":
                safe_comment = source.replace("-/", "- /")
                chunks.append(f"/-!\n{safe_comment}\n-/\n")
            elif lang == "python":
                safe_py = source.replace("-/", "- /")
                chunks.append(f"/-\n[Python Cell {idx}]\n{safe_py}\n-/\n")
            else:
                if source.startswith("%"):
                    chunks.append(f"-- [Notebook Magic Cell {idx}]: {source.splitlines()[0]}\n")
                else:
                    chunks.append(f"-- [Proof Cell {idx}]\n{source}\n")
        return "\n".join(chunks).rstrip() + "\n"


class WorkspaceLifecycleManager:
    """Manages Proof Workspace HOT/COLD lifecycle in Redis modeled after ``CanvasLifecycleMemory``.

    Lifecycle states:
    - ``COLD``: Stored on persistent disk (``NotebookStore``); not occupying Redis memory or live kernels.
    - ``HOT``: Mounted in Redis (``proof:workspace:<id>:doc``, ``:meta``, ``:clients``) when opened as a
      workspace tab in the editor. Live edits and Lean 4 / Python executions update Redis immediately.
      When unmounted (tab closed by all active clients), the latest Redis snapshot is flushed to disk,
      evicted from Redis, and the workspace's kernels are shut down.
    """

    def __init__(
        self,
        store: NotebookStore,
        kernel_pool: ProofKernelPool,
        redis_client: AgoraRedisClient | None = None,
    ) -> None:
        self.store = store
        self.kernel_pool = kernel_pool
        self.redis = redis_client or AgoraRedisClient()
        self._lock = threading.RLock()
        self._generations: dict[str, int] = {}

    @staticmethod
    def _doc_key(workspace_id: str) -> str:
        return f"proof:workspace:{_sanitize_id(workspace_id)}:doc"

    @staticmethod
    def _meta_key(workspace_id: str) -> str:
        return f"proof:workspace:{_sanitize_id(workspace_id)}:meta"

    @staticmethod
    def _clients_key(workspace_id: str) -> str:
        return f"proof:workspace:{_sanitize_id(workspace_id)}:clients"

    def redis_info(self) -> dict[str, Any]:
        return self.redis.info()

    def get_lifecycle_status(self, workspace_id: str) -> dict[str, Any]:
        ws_id = _sanitize_id(workspace_id)
        doc_raw = self.redis.get(self._doc_key(ws_id))
        meta = self.redis.hgetall(self._meta_key(ws_id))
        active_clients = self.redis.scard(self._clients_key(ws_id))
        in_redis = doc_raw is not None
        r_info = self.redis.info()
        return {
            "workspaceId": ws_id,
            "state": "HOT" if in_redis else "COLD",
            "inRedis": in_redis,
            "redisBackend": r_info["backend"],
            "liveRedis": r_info["liveRedis"],
            "cacheGeneration": int(meta.get("cacheGeneration") or self._generations.get(ws_id, 0)),
            "activeClients": active_clients,
            "mountedAt": meta.get("mountedAt"),
            "updatedAt": meta.get("updatedAt"),
        }

    def mount_workspace(
        self,
        user_id: str,
        workspace_id: str,
        client_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Allocate/load a workspace into Redis (``HOT`` state) when opened in the VS Code editor."""
        ws_id = _sanitize_id(workspace_id)
        cid = _sanitize_id(client_id or "anon", "anon")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        with self._lock:
            cached_raw = self.redis.get(self._doc_key(ws_id))
            if cached_raw:
                try:
                    doc = NotebookStore._normalize_notebook(json.loads(cached_raw), fallback_id=ws_id)
                    self.redis.sadd(self._clients_key(ws_id), cid, ex=WORKSPACE_REDIS_TTL_S)
                    self.redis.hset(
                        self._meta_key(ws_id),
                        {"updatedAt": now_iso, "lifecycleState": "HOT", "lastUserId": user_id},
                        ex=WORKSPACE_REDIS_TTL_S,
                    )
                    return doc, self.get_lifecycle_status(ws_id)
                except Exception:
                    pass

            # COLD -> HOT transition: read from persistent disk and upload to Redis
            disk_doc = self.store.get_notebook(user_id, ws_id)
            if disk_doc is None:
                return None, self.get_lifecycle_status(ws_id)

            gen = self._generations.get(ws_id, 0) + 1
            self._generations[ws_id] = gen
            serialized = json.dumps(disk_doc, ensure_ascii=False)
            self.redis.set(self._doc_key(ws_id), serialized, ex=WORKSPACE_REDIS_TTL_S)
            self.redis.hset(
                self._meta_key(ws_id),
                {
                    "workspaceId": ws_id,
                    "lifecycleState": "HOT",
                    "cacheGeneration": str(gen),
                    "mountedAt": now_iso,
                    "updatedAt": now_iso,
                    "lastUserId": user_id,
                },
                ex=WORKSPACE_REDIS_TTL_S,
            )
            self.redis.sadd(self._clients_key(ws_id), cid, ex=WORKSPACE_REDIS_TTL_S)
            return disk_doc, self.get_lifecycle_status(ws_id)

    def update_workspace(
        self,
        user_id: str,
        workspace_data: dict[str, Any],
        client_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Update workspace in Redis (``HOT``) and persist checkpoint to disk."""
        with self._lock:
            saved = self.store.save_notebook(user_id, workspace_data)
            ws_id = saved["id"]
            now_iso = saved["updatedAt"]
            gen = self._generations.get(ws_id, 0) or 1
            self._generations[ws_id] = gen
            self.redis.set(self._doc_key(ws_id), json.dumps(saved, ensure_ascii=False), ex=WORKSPACE_REDIS_TTL_S)
            meta_updates: dict[str, Any] = {
                "workspaceId": ws_id,
                "lifecycleState": "HOT",
                "cacheGeneration": str(gen),
                "updatedAt": now_iso,
                "lastUserId": user_id,
            }
            existing_meta = self.redis.hgetall(self._meta_key(ws_id))
            if not existing_meta.get("mountedAt"):
                meta_updates["mountedAt"] = now_iso
            self.redis.hset(self._meta_key(ws_id), meta_updates, ex=WORKSPACE_REDIS_TTL_S)
            if client_id:
                self.redis.sadd(self._clients_key(ws_id), _sanitize_id(client_id, "anon"), ex=WORKSPACE_REDIS_TTL_S)
            return saved, self.get_lifecycle_status(ws_id)

    def update_cell_execution(
        self,
        user_id: str,
        workspace_id: str,
        cell_id: str,
        code: str | None,
        exec_result: dict[str, Any],
        language: str | None = None,
        client_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Update cell execution result in Redis (``HOT``) and checkpoint to disk."""
        ws_id = _sanitize_id(workspace_id)
        with self._lock:
            doc, _ = self.mount_workspace(user_id, ws_id, client_id=client_id)
            if doc is None:
                return None, self.get_lifecycle_status(ws_id)
            updated = self.store.update_cell_execution(
                user_id,
                ws_id,
                cell_id,
                code,
                exec_result,
                language=language,
            )
            if updated is None:
                return None, self.get_lifecycle_status(ws_id)
            return self.update_workspace(user_id, updated, client_id=client_id)

    def unmount_workspace(
        self,
        user_id: str,
        workspace_id: str,
        client_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Flush workspace from Redis (``HOT``) back to disk (``COLD``) and evict from Redis when closed."""
        ws_id = _sanitize_id(workspace_id)
        with self._lock:
            if client_id:
                self.redis.srem(self._clients_key(ws_id), _sanitize_id(client_id, "anon"))
            remaining_clients = self.redis.scard(self._clients_key(ws_id))

            flushed = False
            stopped_kernels = 0
            if force or remaining_clients <= 0:
                cached_raw = self.redis.get(self._doc_key(ws_id))
                if cached_raw:
                    try:
                        doc = json.loads(cached_raw)
                        if isinstance(doc, dict):
                            self.store.save_notebook(user_id, doc)
                            flushed = True
                    except Exception:
                        pass
                self.redis.delete(
                    self._doc_key(ws_id),
                    self._meta_key(ws_id),
                    self._clients_key(ws_id),
                )
                stopped_kernels = self.kernel_pool.shutdown_workspace(ws_id)

            status = self.get_lifecycle_status(ws_id)
            status["flushedToDisk"] = flushed
            status["stoppedKernels"] = stopped_kernels
            return status

    def list_workspaces(self, user_id: str) -> list[dict[str, Any]]:
        """List all workspaces enriched with their live Redis HOT/COLD lifecycle state."""
        items = self.store.list_notebooks(user_id)
        for item in items:
            item["lifecycle"] = self.get_lifecycle_status(item["id"])
        return items


class AgoraBridge:
    """Client bridge connecting the Proof Workspace Studio with ``project-agora``."""

    def __init__(
        self,
        api_url: str | None = None,
        public_url: str | None = None,
        ca_cert: str | None = None,
    ) -> None:
        self.api_url = (
            api_url
            or os.environ.get("AGORA_API_URL")
            or "https://127.0.0.1:8444"
        ).rstrip("/")
        self.public_url = (
            public_url
            or os.environ.get("AGORA_PUBLIC_URL")
            or self.api_url
        ).rstrip("/")
        self.ca_cert = ca_cert if ca_cert is not None else _detect_agora_ca_cert()

    def _ssl_verify(self) -> ssl.SSLContext | bool:
        if self.ca_cert and Path(self.ca_cert).is_file():
            return ssl.create_default_context(cafile=self.ca_cert)
        if os.environ.get("AGORA_TLS_INSECURE", "").lower() in ("1", "true", "yes"):
            return False
        return True

    def _http_client(self, token: str | None = None, timeout: float = 10.0) -> Any:
        if httpx is None:
            raise RuntimeError("httpx is required for AgoraBridge HTTP client but is not installed")
        headers: dict[str, str] = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx.Client(
            base_url=self.api_url,
            headers=headers,
            verify=self._ssl_verify(),
            timeout=timeout,
        )

    def _sdk_client(self, token: str) -> Any:
        if AgoraClient is None:
            raise RuntimeError("agora-canvas SDK is not installed in this Python environment")
        return AgoraClient(
            api_url=self.api_url,
            token=token,
            ca_cert=self.ca_cert,
            timeout=15.0,
        )

    def check_health(self) -> dict[str, Any]:
        """Probe Project-Agora Auth Service health endpoint."""
        try:
            with self._http_client(timeout=3.5) as client:
                resp = client.get("/api/auth/health")
                data = resp.json() if resp.content else {}
                return {
                    "reachable": resp.status_code == 200,
                    "statusCode": resp.status_code,
                    "apiUrl": self.api_url,
                    "publicUrl": self.public_url,
                    "caCert": self.ca_cert,
                    "service": data,
                }
        except Exception as exc:
            return {
                "reachable": False,
                "apiUrl": self.api_url,
                "publicUrl": self.public_url,
                "caCert": self.ca_cert,
                "error": str(exc),
            }

    def login(self, email: str, password: str) -> dict[str, Any]:
        """Authenticate against Project-Agora ``/api/auth/login``."""
        with self._http_client() as client:
            resp = client.post(
                "/api/auth/login",
                json={"email": email.strip(), "password": password},
            )
            body = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                msg = (
                    (body.get("error") or {}).get("message")
                    if isinstance(body.get("error"), dict)
                    else body.get("message")
                ) or f"Login failed (HTTP {resp.status_code})"
                raise AgoraError(resp.status_code, str(msg), body)
            return body.get("data") if isinstance(body.get("data"), dict) else body

    def verify_mfa(self, mfa_token: str, code: str) -> dict[str, Any]:
        """Complete Project-Agora MFA login step."""
        with self._http_client() as client:
            resp = client.post(
                "/api/auth/mfa/verify",
                json={"mfa_token": mfa_token, "code": code.strip()},
            )
            body = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                msg = (
                    (body.get("error") or {}).get("message")
                    if isinstance(body.get("error"), dict)
                    else body.get("message")
                ) or f"MFA verification failed (HTTP {resp.status_code})"
                raise AgoraError(resp.status_code, str(msg), body)
            return body.get("data") if isinstance(body.get("data"), dict) else body

    def get_me(self, token: str) -> dict[str, Any]:
        """Fetch current authenticated user profile from Project-Agora ``/api/auth/me``."""
        with self._http_client(token=token) as client:
            resp = client.get("/api/auth/me")
            body = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                msg = (
                    (body.get("error") or {}).get("message")
                    if isinstance(body.get("error"), dict)
                    else body.get("message")
                ) or f"Invalid session (HTTP {resp.status_code})"
                raise AgoraError(resp.status_code, str(msg), body)
            return body.get("data") if isinstance(body.get("data"), dict) else body

    def list_canvases(self, token: str, query: str = "") -> list[dict[str, Any]]:
        """List accessible canvases in Project-Agora."""
        if AgoraClient is not None:
            with self._sdk_client(token) as sdk:
                result = sdk.search_canvases(q=query or None, size=30)
                if isinstance(result, dict):
                    for k in ("content", "items", "canvases"):
                        if isinstance(result.get(k), list):
                            return result[k]
                if isinstance(result, list):
                    return result
        with self._http_client(token=token) as client:
            params: dict[str, Any] = {"page": 0, "size": 30}
            if query:
                params["q"] = query
            resp = client.get("/api/canvases", params=params)
            body = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                raise AgoraError(resp.status_code, "Failed to list canvases", body)
            data = body.get("data", body)
            if isinstance(data, dict):
                for k in ("content", "items", "canvases"):
                    if isinstance(data.get(k), list):
                        return data[k]
            return data if isinstance(data, list) else []

    def create_canvas(
        self,
        token: str,
        title: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Create a new collaborative R&D canvas in Project-Agora."""
        if AgoraClient is not None:
            with self._sdk_client(token) as sdk:
                return sdk.create_canvas(
                    title=title,
                    description=description or "Lean 4 Formal Verification & Research Canvas",
                )
        with self._http_client(token=token) as client:
            resp = client.post(
                "/api/canvases",
                json={"title": title, "description": description},
            )
            body = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                raise AgoraError(resp.status_code, "Failed to create canvas", body)
            return body.get("data") if isinstance(body.get("data"), dict) else body

    def export_notebook_to_canvas(
        self,
        token: str,
        canvas_id: str,
        notebook: dict[str, Any],
        x: float = 120.0,
        y: float = 120.0,
        include_summary_card: bool = True,
    ) -> dict[str, Any]:
        """Export a Proof Workspace to a Project-Agora canvas using ``AgoraClient``."""
        agora_cells = []
        verified_theorems: list[str] = []
        for idx, cell in enumerate(notebook.get("cells", [])):
            norm = _normalize_cell(cell, idx)
            agora_cells.append(
                {
                    "id": norm["id"],
                    "cellType": norm["cellType"],
                    "language": norm["language"],
                    "source": norm["source"],
                    "executionCount": norm["executionCount"],
                    "outputs": norm["outputs"],
                    "lastRunStatus": norm["lastRunStatus"],
                }
            )
            if norm["cellType"] == "code":
                for m in re.finditer(r"\b(theorem|lemma|def|structure|inductive)\s+([A-Za-z0-9_'.]+)", norm["source"]):
                    status_icon = "✅" if norm["lastRunStatus"] == "ok" else "⏳"
                    verified_theorems.append(f"- {status_icon} `{m.group(1)} {m.group(2)}`")

        custom_data = {
            "title": notebook.get("title") or "Lean 4 Proof Workspace",
            "kernelName": "lean4",
            "language": "lean4",
            "proofNotebookId": notebook.get("id"),
            "cells": agora_cells,
        }

        existing_item_id = notebook.get("linkedCanvasItemId")
        created_items: list[dict[str, Any]] = []

        with self._sdk_client(token) as sdk:
            nb_item: dict[str, Any] | None = None
            if existing_item_id:
                try:
                    nb_item = sdk.patch_item(
                        canvas_id,
                        existing_item_id,
                        label=notebook.get("title") or "Lean 4 Proof Workspace",
                        customData=custom_data,
                    )
                except Exception:
                    nb_item = None

            if nb_item is None:
                nb_item = sdk.create_item(
                    canvas_id,
                    type="notebook",
                    x=float(x),
                    y=float(y),
                    width=760,
                    height=620,
                    label=notebook.get("title") or "Lean 4 Proof Workspace",
                    customData=custom_data,
                )
            created_items.append(nb_item)

            if include_summary_card:
                summary_lines = [
                    f"### ∀ {notebook.get('title') or 'Lean 4 Proof Summary'}",
                    "- **Kernels**: `lean4` (`Lean 4 + REPL`) + `python3` (`ipykernel`)",
                    f"- **Cells**: {len(agora_cells)} total",
                ]
                if verified_theorems:
                    summary_lines.append("\n**Declarations & Theorems:**")
                    summary_lines.extend(verified_theorems[:15])
                summary_text = "\n".join(summary_lines)
                summary_item = sdk.create_item(
                    canvas_id,
                    type="text",
                    x=float(x) + 800.0,
                    y=float(y),
                    width=380,
                    height=300,
                    label=f"{notebook.get('title') or 'Proof'} — Verification Summary",
                    content=summary_text,
                    customData={
                        "text": summary_text,
                        "proofNotebookId": notebook.get("id"),
                    },
                )
                created_items.append(summary_item)

        linked_item_id = (
            nb_item.get("id") or nb_item.get("itemId")
            if isinstance(nb_item, dict)
            else None
        )
        return {
            "canvasId": canvas_id,
            "notebookItemId": linked_item_id,
            "canvasUrl": f"{self.public_url}/canvas/{canvas_id}",
            "items": created_items,
        }

    def import_canvas_to_notebook(
        self,
        token: str,
        canvas_id: str,
    ) -> dict[str, Any]:
        """Import ``notebook``, ``code``, and ``text`` objects from a Project-Agora canvas."""
        with self._sdk_client(token) as sdk:
            canvas_data = sdk.get_canvas(canvas_id)

        title = str(canvas_data.get("title") or f"Canvas {canvas_id[:8]} Workspace")
        raw_objects = (
            canvas_data.get("objects")
            or canvas_data.get("items")
            or (canvas_data.get("data") or {}).get("objects")
            or []
        )

        extracted_cells: list[dict[str, Any]] = []
        linked_item_id: str | None = None

        for idx, obj in enumerate(raw_objects if isinstance(raw_objects, list) else []):
            if not isinstance(obj, dict):
                continue
            obj_type = str(obj.get("type") or "").lower()
            custom = obj.get("customData") if isinstance(obj.get("customData"), dict) else {}

            if obj_type == "notebook" and isinstance(custom.get("cells"), list):
                if linked_item_id is None:
                    linked_item_id = obj.get("id") or obj.get("itemId")
                for c_idx, cell in enumerate(custom["cells"]):
                    extracted_cells.append(_normalize_cell(cell, len(extracted_cells) + c_idx))
            elif obj_type == "code":
                code_str = str(custom.get("code") or obj.get("content") or "").strip()
                lang = _normalize_language(custom.get("language"), "code", code_str)
                if code_str:
                    extracted_cells.append(
                        _normalize_cell(
                            {
                                "id": f"canvas-code-{idx + 1}",
                                "cellType": "code",
                                "language": lang,
                                "source": code_str,
                            },
                            len(extracted_cells),
                        )
                    )
            elif obj_type in ("text", "note", "markdown"):
                text_str = str(custom.get("text") or obj.get("content") or obj.get("label") or "").strip()
                if text_str:
                    extracted_cells.append(
                        _normalize_cell(
                            {
                                "id": f"canvas-text-{idx + 1}",
                                "cellType": "markdown",
                                "language": "markdown",
                                "source": text_str,
                            },
                            len(extracted_cells),
                        )
                    )

        if not extracted_cells:
            extracted_cells = [
                _normalize_cell(
                    {
                        "id": "canvas-empty-cell",
                        "cellType": "code",
                        "language": "lean4",
                        "source": "",
                    },
                    0,
                )
            ]

        return {
            "title": f"{title}",
            "description": str(canvas_data.get("description") or f"Imported from Project-Agora canvas {canvas_id}"),
            "linkedCanvasId": canvas_id,
            "linkedCanvasItemId": linked_item_id,
            "cells": extracted_cells,
        }


class RealtimeNotebookHub:
    """In-memory pub/sub and collaborator presence hub for real-time workspace sync."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[str, set[queue.Queue[dict[str, Any]]]] = {}
        self._presence: dict[str, dict[str, dict[str, Any]]] = {}

    def subscribe(self, notebook_id: str) -> queue.Queue[dict[str, Any]]:
        nb_key = _sanitize_id(notebook_id, "default")
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=128)
        with self._lock:
            self._subscribers.setdefault(nb_key, set()).add(q)
        return q

    def unsubscribe(self, notebook_id: str, q: queue.Queue[dict[str, Any]]) -> None:
        nb_key = _sanitize_id(notebook_id, "default")
        with self._lock:
            subs = self._subscribers.get(nb_key)
            if subs and q in subs:
                subs.remove(q)

    def update_presence(
        self,
        notebook_id: str,
        client_id: str,
        user: dict[str, Any],
        active_cell_id: str | None = None,
    ) -> list[dict[str, Any]]:
        nb_key = _sanitize_id(notebook_id, "default")
        cid = _sanitize_id(client_id, "anon")
        now = time.time()
        with self._lock:
            room = self._presence.setdefault(nb_key, {})
            stale = [k for k, v in room.items() if now - float(v.get("updatedAt", 0)) > 45.0]
            for k in stale:
                room.pop(k, None)
            room[cid] = {
                "clientId": cid,
                "userId": user.get("userId", "local"),
                "nickname": user.get("nickname", "Researcher"),
                "mode": user.get("mode", "local"),
                "activeCellId": active_cell_id,
                "updatedAt": now,
            }
            snapshot = list(room.values())
        self.publish(
            nb_key,
            "presence.updated",
            {"collaborators": snapshot},
            sender_client_id=cid,
        )
        return snapshot

    def remove_presence(self, notebook_id: str, client_id: str) -> list[dict[str, Any]]:
        nb_key = _sanitize_id(notebook_id, "default")
        cid = _sanitize_id(client_id, "anon")
        with self._lock:
            room = self._presence.get(nb_key) or {}
            room.pop(cid, None)
            snapshot = list(room.values())
        self.publish(
            nb_key,
            "presence.updated",
            {"collaborators": snapshot},
            sender_client_id=cid,
        )
        return snapshot

    def get_presence(self, notebook_id: str) -> list[dict[str, Any]]:
        nb_key = _sanitize_id(notebook_id, "default")
        now = time.time()
        with self._lock:
            room = self._presence.get(nb_key) or {}
            stale = [k for k, v in room.items() if now - float(v.get("updatedAt", 0)) > 45.0]
            for k in stale:
                room.pop(k, None)
            return list(room.values())

    def publish(
        self,
        notebook_id: str,
        event_type: str,
        payload: dict[str, Any],
        sender_client_id: str | None = None,
        sender_user: dict[str, Any] | None = None,
    ) -> int:
        nb_key = _sanitize_id(notebook_id, "default")
        envelope = {
            "type": event_type,
            "notebookId": nb_key,
            "workspaceId": nb_key,
            "senderClientId": sender_client_id,
            "sender": {
                "userId": (sender_user or {}).get("userId"),
                "nickname": (sender_user or {}).get("nickname"),
            }
            if sender_user
            else None,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "data": payload,
        }
        with self._lock:
            subs = list(self._subscribers.get(nb_key, set()))
        delivered = 0
        for q in subs:
            try:
                q.put_nowait(envelope)
                delivered += 1
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(envelope)
                    delivered += 1
                except Exception:
                    pass
        return delivered


class ProofStudioApp:
    """Application container holding the kernel pool, store, Redis lifecycle manager, realtime hub, and Agora bridge."""

    def __init__(
        self,
        data_dir: Path | str | None = None,
        agora_api_url: str | None = None,
        agora_public_url: str | None = None,
        agora_ca_cert: str | None = None,
        max_kernels: int = MAX_KERNELS,
        redis_client: AgoraRedisClient | None = None,
    ) -> None:
        self.kernel_pool = ProofKernelPool(max_kernels=max_kernels)
        self.notebook_store = NotebookStore(data_dir=data_dir)
        self.lifecycle = WorkspaceLifecycleManager(
            store=self.notebook_store,
            kernel_pool=self.kernel_pool,
            redis_client=redis_client,
        )
        self.realtime = RealtimeNotebookHub()
        self.agora = AgoraBridge(
            api_url=agora_api_url,
            public_url=agora_public_url,
            ca_cert=agora_ca_cert,
        )
        self._token_cache_lock = threading.Lock()
        self._token_user_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def sync_linked_canvas_async(
        self,
        token: str | None,
        user_id: str,
        notebook: dict[str, Any],
        sender_client_id: str | None = None,
    ) -> None:
        """Automatically push live workspace changes to a linked Project-Agora canvas in the background."""
        canvas_id = notebook.get("linkedCanvasId")
        if not token or not canvas_id:
            return

        def _worker() -> None:
            try:
                res = self.agora.export_notebook_to_canvas(
                    token,
                    canvas_id=str(canvas_id),
                    notebook=notebook,
                    include_summary_card=False,
                )
                item_id = res.get("notebookItemId")
                if item_id and item_id != notebook.get("linkedCanvasItemId"):
                    notebook["linkedCanvasItemId"] = item_id
                    self.lifecycle.update_workspace(user_id, notebook, client_id=sender_client_id)
                self.realtime.publish(
                    notebook["id"],
                    "canvas.synced",
                    {
                        "canvasId": canvas_id,
                        "notebookItemId": item_id,
                        "canvasUrl": res.get("canvasUrl"),
                        "syncedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                    sender_client_id=sender_client_id,
                )
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def resolve_user(self, headers: Any) -> dict[str, Any]:
        """Resolve user identity from Project-Agora JWT Bearer token or local researcher header."""
        auth_header = ""
        local_user_header = ""
        if hasattr(headers, "get"):
            auth_header = str(headers.get("Authorization") or "").strip()
            local_user_header = str(headers.get("X-Proof-User") or "").strip()

        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
            if token:
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
                now = time.monotonic()
                with self._token_cache_lock:
                    cached = self._token_user_cache.get(token_hash)
                    if cached and (now - cached[0]) < 120.0:
                        return {**cached[1], "token": token}
                try:
                    me = self.agora.get_me(token)
                    uid = str(
                        me.get("id")
                        or me.get("user_id")
                        or me.get("userId")
                        or me.get("email")
                        or f"agora-{token_hash[:10]}"
                    )
                    profile = {
                        "userId": _sanitize_id(uid, f"agora-{token_hash[:10]}"),
                        "email": me.get("email") or "",
                        "nickname": me.get("nickname") or me.get("name") or "Agora Researcher",
                        "tagNumber": me.get("tag_number") or me.get("tagNumber") or "",
                        "mode": "agora",
                        "authenticated": True,
                    }
                    with self._token_cache_lock:
                        self._token_user_cache[token_hash] = (now, profile)
                    return {**profile, "token": token}
                except Exception:
                    return {
                        "userId": f"agora-{token_hash[:12]}",
                        "email": "",
                        "nickname": "Agora Token User",
                        "tagNumber": "",
                        "mode": "agora-offline",
                        "authenticated": True,
                        "token": token,
                    }

        local_id = _sanitize_id(local_user_header, "local-researcher")
        return {
            "userId": local_id,
            "email": "",
            "nickname": local_user_header or "Local Researcher",
            "tagNumber": "LOCAL",
            "mode": "local",
            "authenticated": False,
            "token": None,
        }

    def shutdown(self) -> None:
        self.kernel_pool.shutdown_all()


class _ProofStudioRequestHandler(BaseHTTPRequestHandler):
    server: "ProofNotebookHTTPServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("PROOF_NOTEBOOK_QUIET") == "1":
            return
        super().log_message(fmt, *args)

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("Origin", "*")
        self.send_header("Access-Control-Allow-Origin", origin or "*")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Proof-User, X-Proof-Client",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Credentials", "true")

    def _client_id(self) -> str:
        return _sanitize_id(str(self.headers.get("X-Proof-Client") or ""), fallback="anon")

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        filename: str | None = None,
    ) -> None:
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename)
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 16 * 1024 * 1024))
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        app = self.server.app

        try:
            if path == "/api/health":
                agora_status = app.agora.check_health()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "UP",
                        "service": "Lean 4 Proof Notebook Studio",
                        "agora": agora_status,
                        "redis": app.lifecycle.redis_info(),
                    },
                )
                return

            if path == "/api/auth/session":
                user = app.resolve_user(self.headers)
                agora_status = app.agora.check_health()
                safe_user = {k: v for k, v in user.items() if k != "token"}
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "user": safe_user,
                        "agora": agora_status,
                        "redis": app.lifecycle.redis_info(),
                    },
                )
                return

            if path == "/api/unicode":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "abbreviations": UNICODE_ABBREVIATIONS,
                        "magics": MAGIC_HELP,
                    },
                )
                return

            if path in ("/api/notebooks", "/api/workspaces"):
                user = app.resolve_user(self.headers)
                workspaces = app.lifecycle.list_workspaces(user["userId"])
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "workspaces": workspaces,
                        "notebooks": workspaces,
                        "redis": app.lifecycle.redis_info(),
                    },
                )
                return

            m_nb = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)$", path)
            if m_nb:
                nb_id = urllib.parse.unquote(m_nb.group(1))
                user = app.resolve_user(self.headers)
                client_id = self._client_id()
                should_mount = (query.get("mount") or ["1"])[0] != "0"
                if should_mount:
                    nb, lifecycle = app.lifecycle.mount_workspace(user["userId"], nb_id, client_id=client_id)
                else:
                    nb = app.notebook_store.get_notebook(user["userId"], nb_id)
                    lifecycle = app.lifecycle.get_lifecycle_status(nb_id)
                if nb is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Workspace not found"})
                    return
                kernel_status = app.kernel_pool.status(user["userId"], nb["id"])
                collaborators = app.realtime.get_presence(nb["id"])
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "workspace": nb,
                        "notebook": nb,
                        "lifecycle": lifecycle,
                        "kernel": kernel_status,
                        "collaborators": collaborators,
                    },
                )
                return

            m_events = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/events$", path)
            if m_events:
                nb_id = urllib.parse.unquote(m_events.group(1))
                q_token = (query.get("token") or [""])[0].strip()
                q_user = (query.get("user") or [""])[0].strip()
                q_client = (query.get("clientId") or [""])[0].strip()
                synth_headers = dict(self.headers)
                if q_token and not synth_headers.get("Authorization"):
                    synth_headers["Authorization"] = f"Bearer {q_token}"
                if q_user and not synth_headers.get("X-Proof-User"):
                    synth_headers["X-Proof-User"] = q_user
                user = app.resolve_user(synth_headers)
                client_id = _sanitize_id(q_client or self._client_id(), fallback=f"c-{uuid.uuid4().hex[:6]}")

                self.send_response(HTTPStatus.OK)
                self._send_cors_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                sub_q = app.realtime.subscribe(nb_id)
                app.realtime.update_presence(nb_id, client_id, user)
                _, lifecycle = app.lifecycle.mount_workspace(user["userId"], nb_id, client_id=client_id)
                try:
                    init_evt = {
                        "type": "connected",
                        "notebookId": nb_id,
                        "workspaceId": nb_id,
                        "clientId": client_id,
                        "lifecycle": lifecycle,
                        "collaborators": app.realtime.get_presence(nb_id),
                    }
                    self.wfile.write(f"data: {json.dumps(init_evt, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()

                    if (query.get("once") or [""])[0] == "1":
                        return

                    while True:
                        try:
                            evt = sub_q.get(timeout=15.0)
                            self.wfile.write(f"data: {json.dumps(evt, ensure_ascii=False)}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    self.close_connection = True
                    app.realtime.unsubscribe(nb_id, sub_q)
                    app.realtime.remove_presence(nb_id, client_id)
                return

            m_status = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/kernel$", path)
            if m_status:
                nb_id = urllib.parse.unquote(m_status.group(1))
                user = app.resolve_user(self.headers)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "kernel": app.kernel_pool.status(user["userId"], nb_id),
                        "lifecycle": app.lifecycle.get_lifecycle_status(nb_id),
                    },
                )
                return

            m_ipynb = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/export\.ipynb$", path)
            if m_ipynb:
                nb_id = urllib.parse.unquote(m_ipynb.group(1))
                user = app.resolve_user(self.headers)
                nb = app.notebook_store.get_notebook(user["userId"], nb_id)
                if nb is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Workspace not found"})
                    return
                ipynb_dict = app.notebook_store.to_ipynb(nb)
                raw_bytes = json.dumps(ipynb_dict, ensure_ascii=False, indent=2).encode("utf-8")
                self._send_bytes(
                    HTTPStatus.OK,
                    raw_bytes,
                    "application/x-ipynb+json; charset=utf-8",
                    filename=f"{nb['id']}.ipynb",
                )
                return

            m_lean = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/export\.lean$", path)
            if m_lean:
                nb_id = urllib.parse.unquote(m_lean.group(1))
                user = app.resolve_user(self.headers)
                nb = app.notebook_store.get_notebook(user["userId"], nb_id)
                if nb is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Workspace not found"})
                    return
                lean_src = app.notebook_store.to_lean_source(nb).encode("utf-8")
                self._send_bytes(
                    HTTPStatus.OK,
                    lean_src,
                    "text/plain; charset=utf-8",
                    filename=f"{nb['id']}.lean",
                )
                return

            if path == "/api/agora/canvases":
                user = app.resolve_user(self.headers)
                if not user.get("token"):
                    self._send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"error": "Project-Agora Bearer token required to list canvases"},
                    )
                    return
                q = (query.get("q") or [""])[0]
                canvases = app.agora.list_canvases(user["token"], query=q)
                self._send_json(HTTPStatus.OK, {"canvases": canvases})
                return

            # Static files & SPA entry
            self._serve_static(path)

        except AgoraError as exc:
            self._send_json(exc.status_code, {"error": exc.message, "details": exc.body})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        app = self.server.app

        try:
            body = self._read_json_body()

            if path == "/api/auth/login":
                email = str(body.get("email") or "").strip()
                password = str(body.get("password") or "")
                if not email or not password:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Email and password are required"})
                    return
                result = app.agora.login(email, password)
                self._send_json(HTTPStatus.OK, result)
                return

            if path == "/api/auth/mfa":
                mfa_token = str(body.get("mfa_token") or body.get("mfaToken") or "")
                code = str(body.get("code") or "")
                result = app.agora.verify_mfa(mfa_token, code)
                self._send_json(HTTPStatus.OK, result)
                return

            if path in ("/api/notebooks", "/api/workspaces"):
                user = app.resolve_user(self.headers)
                client_id = self._client_id()
                nb = app.notebook_store.create_notebook(
                    user["userId"],
                    title=body.get("title"),
                    description=body.get("description"),
                    cells=body.get("cells") if isinstance(body.get("cells"), list) else None,
                    linked_canvas_id=body.get("linkedCanvasId"),
                    default_language=str(body.get("language") or "lean4"),
                )
                mounted_nb, lifecycle = app.lifecycle.mount_workspace(
                    user["userId"], nb["id"], client_id=client_id
                )
                final_nb = mounted_nb or nb
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "workspace": final_nb,
                        "notebook": final_nb,
                        "lifecycle": lifecycle,
                        "kernel": app.kernel_pool.status(user["userId"], final_nb["id"]),
                    },
                )
                return

            if path in ("/api/notebooks/import-ipynb", "/api/workspaces/import-ipynb"):
                user = app.resolve_user(self.headers)
                client_id = self._client_id()
                raw_ipynb = body.get("ipynb") if isinstance(body.get("ipynb"), dict) else body
                parsed_nb = app.notebook_store.from_ipynb(raw_ipynb, title=body.get("title"))
                saved, lifecycle = app.lifecycle.update_workspace(
                    user["userId"], parsed_nb, client_id=client_id
                )
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "workspace": saved,
                        "notebook": saved,
                        "lifecycle": lifecycle,
                        "kernel": app.kernel_pool.status(user["userId"], saved["id"]),
                    },
                )
                return

            m_mount = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/mount$", path)
            if m_mount:
                nb_id = urllib.parse.unquote(m_mount.group(1))
                user = app.resolve_user(self.headers)
                client_id = str(body.get("clientId") or self._client_id())
                nb, lifecycle = app.lifecycle.mount_workspace(user["userId"], nb_id, client_id=client_id)
                if nb is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Workspace not found"})
                    return
                app.realtime.publish(
                    nb_id,
                    "workspace.lifecycle",
                    {"lifecycle": lifecycle},
                    sender_client_id=client_id,
                    sender_user=user,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "workspace": nb,
                        "notebook": nb,
                        "lifecycle": lifecycle,
                        "kernel": app.kernel_pool.status(user["userId"], nb_id),
                    },
                )
                return

            m_unmount = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/unmount$", path)
            if m_unmount:
                nb_id = urllib.parse.unquote(m_unmount.group(1))
                user = app.resolve_user(self.headers)
                client_id = str(body.get("clientId") or self._client_id())
                force = bool(body.get("force", True))
                lifecycle = app.lifecycle.unmount_workspace(
                    user["userId"], nb_id, client_id=client_id, force=force
                )
                app.realtime.remove_presence(nb_id, client_id)
                app.realtime.publish(
                    nb_id,
                    "workspace.lifecycle",
                    {"lifecycle": lifecycle},
                    sender_client_id=client_id,
                    sender_user=user,
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "unmounted": lifecycle["state"] == "COLD",
                        "lifecycle": lifecycle,
                        "kernel": app.kernel_pool.status(user["userId"], nb_id),
                    },
                )
                return

            m_pres = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/presence$", path)
            if m_pres:
                nb_id = urllib.parse.unquote(m_pres.group(1))
                user = app.resolve_user(self.headers)
                client_id = str(body.get("clientId") or self._client_id())
                active_cell = body.get("activeCellId")
                collaborators = app.realtime.update_presence(
                    nb_id,
                    client_id=client_id,
                    user=user,
                    active_cell_id=str(active_cell) if active_cell else None,
                )
                self._send_json(HTTPStatus.OK, {"collaborators": collaborators})
                return

            m_exec = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/execute$", path)
            if m_exec:
                nb_id = urllib.parse.unquote(m_exec.group(1))
                user = app.resolve_user(self.headers)
                client_id = str(body.get("clientId") or self._client_id())
                code = str(body.get("code") or "")
                cell_id = str(body.get("cellId") or "") or None
                timeout = float(body.get("timeout") or DEFAULT_EXEC_TIMEOUT_S)

                # Determine cell language from request body or existing cell in workspace
                req_lang = body.get("language")
                if not req_lang and cell_id:
                    ws_doc, _ = app.lifecycle.mount_workspace(user["userId"], nb_id, client_id=client_id)
                    if ws_doc:
                        for c in ws_doc.get("cells", []):
                            if c.get("id") == cell_id:
                                req_lang = c.get("language")
                                break
                lang = _normalize_language(req_lang, "code", code)

                if cell_id:
                    app.realtime.publish(
                        nb_id,
                        "cell.executing",
                        {"cellId": cell_id, "code": code, "language": lang},
                        sender_client_id=client_id,
                        sender_user=user,
                    )

                result = app.kernel_pool.execute(
                    user["userId"],
                    nb_id,
                    code=code,
                    cell_id=cell_id,
                    language=lang,
                    timeout=timeout,
                )
                updated_nb = None
                lifecycle = app.lifecycle.get_lifecycle_status(nb_id)
                if cell_id and body.get("persist", True):
                    updated_nb, lifecycle = app.lifecycle.update_cell_execution(
                        user["userId"],
                        nb_id,
                        cell_id,
                        code,
                        result,
                        language=lang,
                        client_id=client_id,
                    )
                result["kernel"] = app.kernel_pool.status(user["userId"], nb_id, language=lang)
                result["lifecycle"] = lifecycle

                app.realtime.publish(
                    nb_id,
                    "cell.executed",
                    {
                        "cellId": cell_id,
                        "language": lang,
                        "code": code,
                        "outputs": result.get("outputs", []),
                        "executionCount": result.get("executionCount"),
                        "exitCode": result.get("exitCode", 0),
                        "lastRunStatus": "ok" if result.get("exitCode", 1) == 0 else "error",
                        "kernel": result["kernel"],
                        "lifecycle": lifecycle,
                    },
                    sender_client_id=client_id,
                    sender_user=user,
                )
                if updated_nb and updated_nb.get("linkedCanvasId"):
                    app.sync_linked_canvas_async(
                        user.get("token"),
                        user["userId"],
                        updated_nb,
                        sender_client_id=client_id,
                    )

                self._send_json(HTTPStatus.OK, result)
                return

            m_comp = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/complete$", path)
            if m_comp:
                nb_id = urllib.parse.unquote(m_comp.group(1))
                user = app.resolve_user(self.headers)
                code = str(body.get("code") or "")
                cursor_pos = body.get("cursorPos")
                language = str(body.get("language") or "lean4")
                res = app.kernel_pool.complete(
                    user["userId"],
                    nb_id,
                    code=code,
                    cursor_pos=int(cursor_pos) if isinstance(cursor_pos, int) else None,
                    language=language,
                )
                self._send_json(HTTPStatus.OK, res)
                return

            m_insp = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/inspect$", path)
            if m_insp:
                nb_id = urllib.parse.unquote(m_insp.group(1))
                user = app.resolve_user(self.headers)
                code = str(body.get("code") or "")
                cursor_pos = body.get("cursorPos")
                detail = int(body.get("detailLevel") or 0)
                language = str(body.get("language") or "lean4")
                res = app.kernel_pool.inspect_symbol(
                    user["userId"],
                    nb_id,
                    code=code,
                    cursor_pos=int(cursor_pos) if isinstance(cursor_pos, int) else None,
                    detail_level=detail,
                    language=language,
                )
                self._send_json(HTTPStatus.OK, res)
                return

            m_int = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/interrupt$", path)
            if m_int:
                nb_id = urllib.parse.unquote(m_int.group(1))
                user = app.resolve_user(self.headers)
                res = app.kernel_pool.interrupt(user["userId"], nb_id, language=body.get("language"))
                self._send_json(HTTPStatus.OK, res)
                return

            m_rst = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)/reset$", path)
            if m_rst:
                nb_id = urllib.parse.unquote(m_rst.group(1))
                user = app.resolve_user(self.headers)
                res = app.kernel_pool.reset(user["userId"], nb_id, language=body.get("language"))
                self._send_json(HTTPStatus.OK, res)
                return

            if path == "/api/agora/canvases":
                user = app.resolve_user(self.headers)
                if not user.get("token"):
                    self._send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"error": "Project-Agora Bearer token required to create a canvas"},
                    )
                    return
                title = str(body.get("title") or "Lean 4 Formal Verification Canvas").strip()
                description = str(body.get("description") or "").strip()
                canvas = app.agora.create_canvas(user["token"], title=title, description=description)
                self._send_json(HTTPStatus.CREATED, {"canvas": canvas})
                return

            m_exp = re.match(r"^/api/agora/canvases/([^/]+)/export$", path)
            if m_exp:
                canvas_id = urllib.parse.unquote(m_exp.group(1))
                user = app.resolve_user(self.headers)
                client_id = self._client_id()
                if not user.get("token"):
                    self._send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"error": "Project-Agora Bearer token required to export to canvas"},
                    )
                    return
                nb_id = str(body.get("notebookId") or body.get("workspaceId") or "")
                nb = app.notebook_store.get_notebook(user["userId"], nb_id) if nb_id else None
                if nb is None and isinstance(body.get("notebook"), dict):
                    nb, _ = app.lifecycle.update_workspace(user["userId"], body["notebook"], client_id=client_id)
                if nb is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Workspace not found"})
                    return
                export_res = app.agora.export_notebook_to_canvas(
                    user["token"],
                    canvas_id=canvas_id,
                    notebook=nb,
                    x=float(body.get("x") or 120.0),
                    y=float(body.get("y") or 120.0),
                    include_summary_card=bool(body.get("includeSummaryCard", True)),
                )
                nb["linkedCanvasId"] = canvas_id
                if export_res.get("notebookItemId"):
                    nb["linkedCanvasItemId"] = export_res["notebookItemId"]
                saved_nb, lifecycle = app.lifecycle.update_workspace(user["userId"], nb, client_id=client_id)
                self._send_json(
                    HTTPStatus.OK,
                    {"export": export_res, "notebook": saved_nb, "workspace": saved_nb, "lifecycle": lifecycle},
                )
                return

            m_imp = re.match(r"^/api/agora/canvases/([^/]+)/import$", path)
            if m_imp:
                canvas_id = urllib.parse.unquote(m_imp.group(1))
                user = app.resolve_user(self.headers)
                client_id = self._client_id()
                if not user.get("token"):
                    self._send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"error": "Project-Agora Bearer token required to import from canvas"},
                    )
                    return
                imported = app.agora.import_canvas_to_notebook(user["token"], canvas_id)
                nb = app.notebook_store.create_notebook(
                    user["userId"],
                    title=imported["title"],
                    description=imported["description"],
                    cells=imported["cells"],
                    linked_canvas_id=canvas_id,
                )
                if imported.get("linkedCanvasItemId"):
                    nb["linkedCanvasItemId"] = imported["linkedCanvasItemId"]
                nb, lifecycle = app.lifecycle.update_workspace(user["userId"], nb, client_id=client_id)
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "workspace": nb,
                        "notebook": nb,
                        "lifecycle": lifecycle,
                        "kernel": app.kernel_pool.status(user["userId"], nb["id"]),
                    },
                )
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {path}"})

        except AgoraError as exc:
            self._send_json(exc.status_code, {"error": exc.message, "details": exc.body})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        app = self.server.app

        try:
            m_nb = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)$", path)
            if not m_nb:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {path}"})
                return
            nb_id = urllib.parse.unquote(m_nb.group(1))
            user = app.resolve_user(self.headers)
            client_id = self._client_id()
            body = self._read_json_body()
            payload = (
                body.get("workspace")
                if isinstance(body.get("workspace"), dict)
                else (body.get("notebook") if isinstance(body.get("notebook"), dict) else body)
            )
            payload["id"] = nb_id
            saved, lifecycle = app.lifecycle.update_workspace(user["userId"], payload, client_id=client_id)

            app.realtime.publish(
                nb_id,
                "notebook.saved",
                {"notebook": saved, "workspace": saved, "lifecycle": lifecycle},
                sender_client_id=client_id,
                sender_user=user,
            )
            if saved.get("linkedCanvasId"):
                app.sync_linked_canvas_async(
                    user.get("token"),
                    user["userId"],
                    saved,
                    sender_client_id=client_id,
                )

            self._send_json(
                HTTPStatus.OK,
                {
                    "workspace": saved,
                    "notebook": saved,
                    "lifecycle": lifecycle,
                    "kernel": app.kernel_pool.status(user["userId"], nb_id),
                    "collaborators": app.realtime.get_presence(nb_id),
                },
            )
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        app = self.server.app

        try:
            m_nb = re.match(r"^/api/(?:notebooks|workspaces)/([^/]+)$", path)
            if not m_nb:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown endpoint: {path}"})
                return
            nb_id = urllib.parse.unquote(m_nb.group(1))
            user = app.resolve_user(self.headers)
            client_id = self._client_id()
            app.lifecycle.unmount_workspace(user["userId"], nb_id, client_id=client_id, force=True)
            deleted = app.notebook_store.delete_notebook(user["userId"], nb_id)
            if not deleted:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Workspace not found"})
                return
            self._send_json(HTTPStatus.OK, {"deleted": True, "id": nb_id})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _serve_static(self, url_path: str) -> None:
        rel = url_path.lstrip("/")
        if not rel or rel == "index.html":
            target = STATIC_DIR / "index.html"
        elif rel.startswith("static/"):
            target = (STATIC_DIR / rel[len("static/") :]).resolve()
        else:
            target = (STATIC_DIR / rel).resolve()

        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            target = STATIC_DIR / "index.html"
            if not target.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Static assets not found"})
                return

        ctype, _ = mimetypes.guess_type(str(target))
        if not ctype:
            ctype = "text/html; charset=utf-8"
        elif ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype = f"{ctype}; charset=utf-8"

        self._send_bytes(HTTPStatus.OK, target.read_bytes(), ctype)


class ProofNotebookHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        app: ProofStudioApp,
    ) -> None:
        self.app = app
        super().__init__(server_address, _ProofStudioRequestHandler)

    def server_close(self) -> None:
        try:
            self.app.shutdown()
        finally:
            super().server_close()


def create_server(
    host: str = "127.0.0.1",
    port: int = 8890,
    data_dir: Path | str | None = None,
    agora_api_url: str | None = None,
    agora_public_url: str | None = None,
    agora_ca_cert: str | None = None,
    max_kernels: int = MAX_KERNELS,
    redis_client: AgoraRedisClient | None = None,
) -> ProofNotebookHTTPServer:
    """Create a configured ``ProofNotebookHTTPServer`` instance."""
    app = ProofStudioApp(
        data_dir=data_dir,
        agora_api_url=agora_api_url,
        agora_public_url=agora_public_url,
        agora_ca_cert=agora_ca_cert,
        max_kernels=max_kernels,
        redis_client=redis_client,
    )
    return ProofNotebookHTTPServer((host, int(port)), app)


def serve(
    host: str = "127.0.0.1",
    port: int = 8890,
    data_dir: Path | str | None = None,
    agora_api_url: str | None = None,
    agora_public_url: str | None = None,
    agora_ca_cert: str | None = None,
    max_kernels: int = MAX_KERNELS,
) -> int:
    """Run the Lean 4 Proof Workspace Studio web server in the foreground."""
    server = create_server(
        host=host,
        port=port,
        data_dir=data_dir,
        agora_api_url=agora_api_url,
        agora_public_url=agora_public_url,
        agora_ca_cert=agora_ca_cert,
        max_kernels=max_kernels,
    )
    bound_host, bound_port = server.server_address[:2]
    print(f"∀ Lean 4 Proof Workspace Studio running at http://{bound_host}:{bound_port}")
    print(f"  Project-Agora API Bridge: {server.app.agora.api_url}")
    print(f"  Redis Lifecycle Backend : {server.app.lifecycle.redis_info()['backend']}")
    print(f"  Workspace Storage Dir   : {server.app.notebook_store.base_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Proof Workspace Studio and live kernels...")
    finally:
        server.server_close()
    return 0
