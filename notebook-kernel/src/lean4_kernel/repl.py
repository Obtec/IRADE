"""Low-level process manager and framed JSON client for leanprover-community/repl."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any

REPL_GIT_URL = "https://github.com/leanprover-community/repl.git"


class REPLError(RuntimeError):
    """Base exception for Lean 4 REPL failures."""


class REPLBinaryNotFoundError(REPLError):
    """Raised when the Lean 4 REPL executable cannot be found or built."""


class REPLProcessCrashedError(REPLError):
    """Raised when the underlying Lean 4 REPL subprocess exits unexpectedly."""


class REPLTimeoutError(REPLError):
    """Raised when a REPL command exceeds the configured timeout."""


@dataclass(frozen=True)
class REPLRawResult:
    """Parsed JSON response from ``repl`` plus any raw stdout emitted prior to JSON."""

    data: dict[str, Any]
    aux_stdout: str = ""


def default_repo_root() -> Path:
    """Locate the proof-notebook repository root when running from source or env."""
    env_repo = os.environ.get("LEAN_REPL_REPO")
    if env_repo:
        p = Path(env_repo).expanduser().resolve()
        if p.exists():
            return p
    # src/lean4_kernel/repl.py -> parents[2] is repository root
    candidate = Path(__file__).resolve().parents[2]
    return candidate


def default_vendor_repl_dir() -> Path:
    """Return the path to ``vendor/repl`` if available."""
    env_dir = os.environ.get("LEAN_REPL_SOURCE_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return default_repo_root() / "vendor" / "repl"


def find_lake_project(start_dir: Path, exclude_dirs: set[Path] | None = None) -> Path | None:
    """Walk upward from ``start_dir`` to find an enclosing Lake project directory."""
    excluded = {p.resolve() for p in (exclude_dirs or set()) if p.exists()}
    cur = start_dir.expanduser().resolve()
    for candidate in (cur, *cur.parents):
        if candidate in excluded:
            return None
        if (candidate / "lakefile.toml").is_file() or (candidate / "lakefile.lean").is_file():
            return candidate
    return None


def read_toolchain(project_dir: Path) -> str | None:
    """Read ``lean-toolchain`` content from a directory if present."""
    tc_file = project_dir / "lean-toolchain"
    if tc_file.is_file():
        return tc_file.read_text(encoding="utf-8").strip()
    return None


def extract_version_tag(toolchain: str) -> str | None:
    """Extract ``v4.X.Y...`` tag from a Lean toolchain string like ``leanprover/lean4:v4.35.0``."""
    colon_part = toolchain.split(":", 1)[-1].strip()
    if colon_part.startswith("v4."):
        return colon_part
    if re.match(r"^4\.\d+", colon_part):
        return f"v{colon_part}"
    return None


def _augmented_env() -> dict[str, str]:
    """Ensure ``~/.elan/bin`` and Homebrew paths are present for GUI-launched Jupyter hosts."""
    env = os.environ.copy()
    path_parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    extra_dirs = [
        str(Path.home() / ".elan" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    for d in extra_dirs:
        if d not in path_parts and Path(d).is_dir():
            path_parts.insert(0, d)
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def detect_lean_version(cwd: Path | None = None) -> str:
    """Return a short human-readable Lean version string (e.g. ``4.35.0``)."""
    env = _augmented_env()
    lean_bin = shutil.which("lean", path=env.get("PATH")) or "lean"
    try:
        proc = subprocess.run(
            [lean_bin, "--version"],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            m = re.search(r"version\s+([^\s,)]+)", proc.stdout)
            if m:
                return m.group(1)
    except OSError:
        pass
    return "4"


def resolve_lean_sysroot(cwd: Path | None = None) -> str | None:
    """Resolve ``lean --print-prefix`` once so ``repl`` avoids spawning ``lean`` per cell."""
    existing = os.environ.get("LEAN_SYSROOT")
    if existing:
        return existing
    env = _augmented_env()
    lean_bin = shutil.which("lean", path=env.get("PATH")) or "lean"
    try:
        proc = subprocess.run(
            [lean_bin, "--print-prefix"],
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            prefix = proc.stdout.strip()
            if prefix:
                return prefix
    except OSError:
        pass
    return None


class REPLBinaryManager:
    """Resolves and builds toolchain-compatible ``repl`` executables."""

    def __init__(
        self,
        vendor_dir: Path | None = None,
        cache_root: Path | None = None,
    ) -> None:
        self.vendor_dir = (vendor_dir or default_vendor_repl_dir()).resolve()
        default_cache = Path(
            os.environ.get(
                "LEAN4_KERNEL_CACHE_DIR",
                Path.home() / ".cache" / "lean4-kernel" / "repl",
            )
        )
        self.cache_root = (cache_root or default_cache).expanduser().resolve()

    @staticmethod
    def _binary_path(repl_project_dir: Path) -> Path:
        exe_name = "repl.exe" if os.name == "nt" else "repl"
        return repl_project_dir / ".lake" / "build" / "bin" / exe_name

    def resolve_binary(
        self,
        project_dir: Path | None = None,
        auto_build: bool = True,
    ) -> Path:
        """Return a built ``repl`` binary compatible with ``project_dir`` (if any)."""
        explicit_bin = os.environ.get("LEAN_REPL_BIN")
        if explicit_bin:
            p = Path(explicit_bin).expanduser().resolve()
            if p.is_file():
                return p
            raise REPLBinaryNotFoundError(
                f"LEAN_REPL_BIN points to a non-existent file: {p}"
            )

        target_toolchain = read_toolchain(project_dir) if project_dir else None
        vendor_toolchain = read_toolchain(self.vendor_dir)
        vendor_bin = self._binary_path(self.vendor_dir)

        # Use vendor/repl directly if no conflicting project toolchain is requested.
        if target_toolchain is None or target_toolchain == vendor_toolchain:
            if vendor_bin.is_file():
                return vendor_bin
            if auto_build and self.vendor_dir.is_dir():
                return self.build_in_dir(self.vendor_dir)

        # Otherwise build/use a toolchain-matched REPL in cache_root.
        if target_toolchain is not None:
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", target_toolchain)
            cached_dir = self.cache_root / safe_name
            cached_bin = self._binary_path(cached_dir)
            if cached_bin.is_file():
                return cached_bin
            if auto_build:
                return self._build_toolchain_cached(cached_dir, target_toolchain)

        if vendor_bin.is_file():
            return vendor_bin

        raise REPLBinaryNotFoundError(
            "Lean 4 REPL binary not found. Run `lean4-kernel build-repl` first."
        )

    def build_in_dir(self, repl_dir: Path) -> Path:
        """Ensure ``repl_dir`` is initialized and run ``lake build repl``."""
        if not (repl_dir / "lakefile.toml").is_file() and not (
            repl_dir / "lakefile.lean"
        ).is_file():
            repl_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", REPL_GIT_URL, str(repl_dir)],
                check=True,
            )
            tag = self._detect_system_lean_tag()
            if tag:
                subprocess.run(
                    ["git", "-C", str(repl_dir), "checkout", tag],
                    check=False,
                )

        env = _augmented_env()
        lake_bin = shutil.which("lake", path=env.get("PATH")) or "lake"
        proc = subprocess.run(
            [lake_bin, "build", "repl"],
            cwd=str(repl_dir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise REPLBinaryNotFoundError(
                f"Failed to build Lean 4 REPL in {repl_dir}:\n"
                f"{proc.stdout}\n{proc.stderr}".strip()
            )
        bin_path = self._binary_path(repl_dir)
        if not bin_path.is_file():
            raise REPLBinaryNotFoundError(
                f"`lake build repl` succeeded, but binary not found at {bin_path}"
            )
        return bin_path

    def _build_toolchain_cached(self, cached_dir: Path, toolchain: str) -> Path:
        cached_dir.parent.mkdir(parents=True, exist_ok=True)
        source_url = (
            str(self.vendor_dir)
            if (self.vendor_dir / ".git").exists()
            else REPL_GIT_URL
        )
        if not cached_dir.exists():
            subprocess.run(
                ["git", "clone", source_url, str(cached_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
        tag = extract_version_tag(toolchain)
        if tag:
            checkout = subprocess.run(
                ["git", "-C", str(cached_dir), "checkout", tag],
                capture_output=True,
                text=True,
                check=False,
            )
            if checkout.returncode != 0 and source_url != REPL_GIT_URL:
                subprocess.run(
                    ["git", "-C", str(cached_dir), "remote", "set-url", "origin", REPL_GIT_URL],
                    check=False,
                )
                subprocess.run(
                    ["git", "-C", str(cached_dir), "fetch", "--tags"],
                    capture_output=True,
                    check=False,
                )
                subprocess.run(
                    ["git", "-C", str(cached_dir), "checkout", tag],
                    capture_output=True,
                    check=False,
                )
        # Force exact lean-toolchain match so .olean headers align with the Lake project.
        (cached_dir / "lean-toolchain").write_text(toolchain + "\n", encoding="utf-8")
        return self.build_in_dir(cached_dir)

    @staticmethod
    def _detect_system_lean_tag() -> str | None:
        ver = detect_lean_version()
        if ver and ver != "4":
            return ver if ver.startswith("v") else f"v{ver}"
        return None


def parse_repl_json_block(lines: list[str]) -> tuple[dict[str, Any] | None, str]:
    """Attempt to parse a multi-line block emitted before a blank line on stdout.

    Returns ``(parsed_dict, aux_stdout)``. If ``lines`` is pure non-JSON stdout text
    (for instance, raw C stdout prior to the JSON payload), returns ``(None, raw_text)``.
    """
    text = "\n".join(lines).strip()
    if not text:
        return None, ""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj, ""
    except json.JSONDecodeError:
        pass

    brace_idx = text.find("{")
    if brace_idx > 0:
        prefix = text[:brace_idx]
        candidate = text[brace_idx:]
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj, prefix
        except json.JSONDecodeError:
            pass

    return None, "\n".join(lines)


class REPLProcess:
    """Manages a persistent ``leanprover-community/repl`` subprocess."""

    def __init__(
        self,
        binary_manager: REPLBinaryManager | None = None,
        working_dir: Path | None = None,
        project_dir: Path | None = None,
    ) -> None:
        self.binary_manager = binary_manager or REPLBinaryManager()
        self.working_dir = (working_dir or Path.cwd()).expanduser().resolve()
        if project_dir is not None:
            self.project_dir: Path | None = project_dir.expanduser().resolve()
        elif os.environ.get("LEAN_PROJECT_DIR"):
            self.project_dir = Path(os.environ["LEAN_PROJECT_DIR"]).expanduser().resolve()
        else:
            self.project_dir = find_lake_project(
                self.working_dir,
                exclude_dirs={self.binary_manager.vendor_dir},
            )

        self._proc: subprocess.Popen[str] | None = None
        self._response_queue: queue.Queue[REPLRawResult | BaseException] = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._req_lock = threading.Lock()
        self._repl_bin: Path | None = None

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def repl_bin(self) -> Path | None:
        return self._repl_bin

    def start(self) -> None:
        """Spawn the REPL subprocess if not already running."""
        if self.is_alive:
            return
        self.kill()

        repl_bin = self.binary_manager.resolve_binary(project_dir=self.project_dir)
        self._repl_bin = repl_bin

        env = _augmented_env()
        if self.project_dir is not None:
            lake_bin = shutil.which("lake", path=env.get("PATH")) or "lake"
            cmd = [lake_bin, "env", str(repl_bin)]
            cwd = self.project_dir
        else:
            cmd = [str(repl_bin)]
            cwd = self.working_dir
            sysroot = resolve_lean_sysroot(cwd=self.binary_manager.vendor_dir)
            if sysroot:
                env["LEAN_SYSROOT"] = sysroot

        self._stderr_lines.clear()
        self._drain_queue()

        popen_kwargs: dict[str, Any] = {}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True

        self._proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **popen_kwargs,
        )

        threading.Thread(
            target=self._stdout_reader,
            args=(self._proc,),
            name="lean4-repl-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._stderr_reader,
            args=(self._proc,),
            name="lean4-repl-stderr",
            daemon=True,
        ).start()

    def _stdout_reader(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        block_lines: list[str] = []
        aux_chunks: list[str] = []
        try:
            while True:
                line = proc.stdout.readline()
                if line == "":
                    # EOF reached
                    if block_lines:
                        parsed, aux = parse_repl_json_block(block_lines)
                        if aux:
                            aux_chunks.append(aux)
                        if parsed is not None:
                            self._response_queue.put(
                                REPLRawResult(
                                    data=parsed,
                                    aux_stdout="\n".join(aux_chunks).strip(),
                                )
                            )
                            return
                    self._response_queue.put(
                        EOFError("Lean 4 REPL closed stdout unexpectedly.")
                    )
                    return

                stripped_nl = line.rstrip("\r\n")
                if stripped_nl.strip() == "":
                    if not block_lines:
                        continue
                    parsed, aux = parse_repl_json_block(block_lines)
                    block_lines = []
                    if parsed is not None:
                        if aux:
                            aux_chunks.append(aux)
                        self._response_queue.put(
                            REPLRawResult(
                                data=parsed,
                                aux_stdout="\n".join(aux_chunks).strip(),
                            )
                        )
                        aux_chunks = []
                    elif aux:
                        aux_chunks.append(aux)
                else:
                    block_lines.append(stripped_nl)
        except BaseException as exc:  # pragma: no cover - defensive
            self._response_queue.put(exc)

    def _stderr_reader(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                self._stderr_lines.append(line.rstrip("\r\n"))
        except BaseException:  # pragma: no cover - defensive
            pass

    def recent_stderr(self) -> str:
        return "\n".join(self._stderr_lines).strip()

    def _drain_queue(self) -> None:
        while True:
            try:
                self._response_queue.get_nowait()
            except queue.Empty:
                break

    def request(
        self,
        payload: dict[str, Any],
        timeout: float | None = None,
    ) -> REPLRawResult:
        """Send a single JSON request to ``repl`` and return the parsed JSON response."""
        with self._req_lock:
            if not self.is_alive:
                self.start()

            assert self._proc is not None and self._proc.stdin is not None
            self._drain_queue()

            # Single-line compact JSON followed by one blank line (`\n\n`).
            wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"
            try:
                self._proc.stdin.write(wire)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                stderr = self.recent_stderr()
                self.kill()
                raise REPLProcessCrashedError(
                    f"Failed writing to Lean 4 REPL stdin: {exc}\n{stderr}".strip()
                ) from exc

            deadline = (time.monotonic() + timeout) if timeout is not None else None
            try:
                while True:
                    wait_slice = 0.05
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self.kill()
                            raise REPLTimeoutError(
                                f"Lean 4 REPL timed out after {timeout:.1f}s (process restarted)."
                            )
                        wait_slice = min(wait_slice, remaining)

                    try:
                        item = self._response_queue.get(timeout=wait_slice)
                    except queue.Empty:
                        continue

                    if isinstance(item, REPLRawResult):
                        return item
                    if isinstance(item, EOFError):
                        # Wait briefly for stderr thread to finish collecting diagnostics
                        time.sleep(0.02)
                        stderr = self.recent_stderr()
                        self.kill()
                        raise REPLProcessCrashedError(
                            f"Lean 4 REPL exited unexpectedly.\n{stderr}".strip()
                        ) from item
                    if isinstance(item, BaseException):
                        self.kill()
                        raise REPLProcessCrashedError(
                            f"Lean 4 REPL reader error: {item}\n{self.recent_stderr()}".strip()
                        ) from item
            except KeyboardInterrupt:
                # Immediately terminate stuck REPL subprocess so CPU stops spinning.
                self.kill()
                raise

    def kill(self) -> None:
        """Terminate the REPL subprocess (and its process group) cleanly."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if proc.poll() is None:
                if os.name != "nt":
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:  # pragma: no cover
                    proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:  # pragma: no cover
                        proc.kill()
                    proc.wait(timeout=1.0)
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass
