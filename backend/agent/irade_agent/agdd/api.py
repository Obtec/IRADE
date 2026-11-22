"""Control-plane API: RBAC, spec tables, Pre-Flight Gate, graphs, runs, HITL, UX overrides, research,
SSE log stream (DOC.md §2 Application & Control Layer)."""
from __future__ import annotations

import base64
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from psycopg.types.json import Jsonb

from . import multi_agent_service, spec_store, ux_workbench, vm_service, vm_sync
from . import infra, orchestrator, pgstore, preflight, router, ws_terminal
from .spec_engine import TABLES
from .ux_workbench import LockedOverrideError, Workbench

ROUTES: list[tuple[str, re.Pattern, str, object]] = []
_TERM_TICKETS = ws_terminal.TerminalTicketStore()
_TERM_WS_SLOTS = threading.BoundedSemaphore(64)
_TERM_WS_RX = re.compile(r"^/workspaces/(?P<ws>[^/]+)/vm/terminal/(?P<name>[A-Za-z0-9_-]{1,64})/ws$")
_STATIC_RX = re.compile(r"^/static/(?P<fname>[A-Za-z0-9_.-]+\.(?:js|css))$")
_VENDOR_RX = re.compile(r"^/static/vendor/(?P<rel>[A-Za-z0-9@._/-]+)$")
_VENDOR_TYPES = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2"}
_CDN_NPM = "https://cdn.jsdelivr.net/npm/"


def localize_vendor_urls(html: bytes, vendor_root: Path | None = None) -> bytes:
    """Point jsDelivr URLs at static/vendor/ for every package bundled locally (offline use).

    Packages that were not fetched (see ``fetch_vendor``) keep loading from the CDN.
    """
    from .fetch_vendor import PACKAGES, vendor_dir
    root = vendor_root or vendor_dir()
    for name, ver, *_ in PACKAGES:
        if (root / f"{name}@{ver}" / ".complete").is_file():
            html = html.replace(f"{_CDN_NPM}{name}@{ver}/".encode(), f"static/vendor/{name}@{ver}/".encode())
    return html


def _vendor_file(rel: str) -> Path | None:
    from .fetch_vendor import vendor_dir
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    root = vendor_dir().resolve()
    fpath = (root / rel).resolve()
    if not fpath.is_relative_to(root) or fpath.suffix not in _VENDOR_TYPES or not fpath.is_file():
        return None
    return fpath


def route(method, pattern, min_role=None):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), min_role, fn))
        return fn
    return deco


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _ws_engine(conn, ws):
    return pgstore.hydrate_engine(conn, ws)


# ---------------- workspace VM & CLI agents ----------------
def _engine(body) -> str:
    e = (body or {}).get("engine") or ("vm-cli" if vm_service.enabled() else "gateway")
    if e not in ("vm-cli", "gateway"):
        raise HttpError(422, "engine must be vm-cli or gateway")
    if e == "vm-cli" and not vm_service.enabled():
        raise HttpError(503, "VM runtime is not connected (AGORA_RUNTIME_SOCKET)")
    return e


def _vm_llm(ws, name, override=None):
    """Cheap structured steps through the workspace's selected CLI inside its VM."""
    import time as _t
    from .cli_agents import VmAgent
    from .vm import RuntimeClient, VmShell
    settings = vm_service.load_settings(ws, override)
    agent = VmAgent(VmShell(RuntimeClient(), vm_service.canvas_for(ws), name), settings)
    t0 = [_t.time()]

    def on_result(res):
        vm_service.record_usage(ws, None, name, settings, res, int((_t.time() - t0[0]) * 1000), cheap=True)
        t0[0] = _t.time()
    return agent.cheap_llm(on_result)


def _require_vm():
    if not vm_service.enabled():
        raise HttpError(503, "VM runtime is not connected (AGORA_RUNTIME_SOCKET)")


@route("GET", r"/workspaces/(?P<ws>[^/]+)/vm", "VIEWER")
def vm_status(ctx, ws, body):
    _require_vm()
    from .vm import RuntimeClient
    cid = vm_service.canvas_for(ws)
    return {"canvas_id": cid, "runtime": RuntimeClient().settings(cid), "repos_dir": vm_service.REPOS_DIR}


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/vm/resources", "OWNER")
def vm_resources(ctx, ws, body):
    _require_vm()
    from .vm import RuntimeClient
    rc, cid = RuntimeClient(), vm_service.canvas_for(ws)
    if "template" in body:
        r = rc.set_resources(cid, body["template"], int(body["revision"]))
    elif "sizeGiB" in body:
        r = rc.set_disk(cid, int(body["sizeGiB"]), int(body["revision"]))
    else:
        raise HttpError(422, "template or sizeGiB required")
    if r.get("error"):
        raise HttpError(409, r.get("message") or r["error"])
    return r


@route("POST", r"/workspaces/(?P<ws>[^/]+)/vm/reset", "OWNER")
def vm_reset(ctx, ws, body):
    _require_vm()
    from .vm import RuntimeClient
    return RuntimeClient().reset(vm_service.canvas_for(ws))


@route("POST", r"/workspaces/(?P<ws>[^/]+)/vm/terminal/(?P<name>[A-Za-z0-9_-]{1,64})", "EDITOR")
def vm_terminal(ctx, ws, body, name):
    """Web terminal relay: open/poll/input/resize/close on the user's own PTY in the workspace VM.
    Automation PTYs (agdd-*) belong to owner 1 and can only be watched (poll) by members."""
    _require_vm()
    from .vm import SYSTEM_OWNER, RuntimeClient, owner_for
    op = body.get("operation")
    if op not in ("open", "poll", "input", "resize", "close"):
        raise HttpError(422, "invalid operation")
    watch = name.startswith("agdd-")
    if watch and op not in ("open", "poll"):
        raise HttpError(403, "automation terminals are read-only")
    owner = SYSTEM_OWNER if watch else owner_for(ctx["user"])
    r = RuntimeClient().terminal(vm_service.canvas_for(ws), owner, name, "poll" if watch and op == "open" else op,
                                 str(body.get("data", ""))[:8000], int(body.get("cols", 120)), int(body.get("rows", 32)),
                                 int(body.get("offset", 0)))
    return r


@route("POST", r"/workspaces/(?P<ws>[^/]+)/vm/terminal/(?P<name>[A-Za-z0-9_-]{1,64})/ticket", "EDITOR")
def vm_terminal_ticket(ctx, ws, body, name):
    """Issue a single-use, short-lived ticket for attaching over WebSocket to the workspace VM terminal."""
    _require_vm()
    from .vm import SYSTEM_OWNER, owner_for
    watch = name.startswith("agdd-")
    owner = SYSTEM_OWNER if watch else owner_for(ctx["user"])
    try:
        secret = _TERM_TICKETS.issue(ws=ws, raw_ws=ctx.get("raw_ws", ws), user=ctx["user"], name=name,
                                     canvas_id=vm_service.canvas_for(ws), owner=owner, watch=watch)
    except RuntimeError as e:
        raise HttpError(503, str(e))
    return {"ticket": secret, "expiresIn": 15}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/agent-settings", "VIEWER")
def get_agent_settings(ctx, ws, body):
    from .cli_agents import EFFORTS, PROFILES
    s = vm_service.load_settings(ws)
    return {"settings": s.__dict__, "efforts": EFFORTS, "engine_default": "vm-cli" if vm_service.enabled() else "gateway",
            "clis": {k: {"models": list(p.models), "install": p.install, "login": p.login_hint}
                     for k, p in PROFILES.items()}}


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/agent-settings", "EDITOR")
def put_agent_settings(ctx, ws, body):
    try:
        saved = vm_service.save_settings(ws, body, ctx["user"]).__dict__
    except ValueError as e:
        raise HttpError(422, str(e))
    vm_sync.sync_workspace_context_async(ws)
    return saved


@route("GET", r"/workspaces/(?P<ws>[^/]+)/vm/clis", "VIEWER")
def vm_clis(ctx, ws, body):
    _require_vm()
    from .cli_agents import PROFILES
    q_ref = ((ctx.get("query") or {}).get("refresh") or [""])[0].lower() in ("1", "true", "yes")
    refresh = q_ref or bool((body or {}).get("refresh"))
    hit = _CLI_CACHE.get(ws)
    if hit and time.monotonic() - hit[0] < 600 and not refresh:
        return hit[1]
    sh = vm_service.shell(ws, "agdd-api")
    # Version probes start node runtimes (seconds each), so run them in parallel.
    out = sh.run_inline(" ".join(f"(echo {k}=$( {p.version_cmd()} 2>/dev/null | head -1 )) &" for k, p in PROFILES.items())
                        + " wait", timeout=120).output
    vers = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    res = {k: {"installed": bool(vers.get(k, "").strip()), "version": vers.get(k, "").strip(),
               "install": p.install, "login": p.login_hint} for k, p in PROFILES.items()}
    _CLI_CACHE[ws] = (time.monotonic(), res)
    return res


_CLI_CACHE: dict = {}
_LINKS: dict = {}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/vm/sessions/(?P<cli>claude|codex|agy)", "VIEWER")
def vm_sessions(ctx, ws, body, cli):
    _require_vm()
    from .cli_agents import PROFILES, parse_sessions
    out = vm_service.shell(ws, "agdd-api").run(PROFILES[cli].sessions_cmd(), timeout=120).output
    return {"sessions": parse_sessions(out), "raw": out[-4000:] if cli == "agy" else ""}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/vm/usage/(?P<cli>claude|codex|agy)", "VIEWER")
def vm_usage(ctx, ws, body, cli):
    """Remaining plan usage as reported by the CLI itself + rate-limit snapshots seen during runs + AGDD's own tally."""
    _require_vm()
    from .cli_agents import PROFILES, parse_codex_rate_limits
    out = vm_service.shell(ws, "agdd-api").run(PROFILES[cli].usage_cmd(), timeout=300).output
    parsed = parse_codex_rate_limits(out) if cli == "codex" else None
    with infra.pg() as c:
        if parsed:
            c.execute("INSERT INTO cli_usage_snapshots (workspace_id, cli, rate_limits, raw_text) VALUES (%s,%s,%s,%s) "
                      "ON CONFLICT (workspace_id, cli) DO UPDATE SET rate_limits=EXCLUDED.rate_limits, "
                      "raw_text=EXCLUDED.raw_text, captured_at=now()", (ws, cli, Jsonb(parsed), out[-8000:]))
        snap = c.execute("SELECT rate_limits, captured_at FROM cli_usage_snapshots WHERE workspace_id=%s AND cli=%s",
                         (ws, cli)).fetchone()
        tally = c.execute(
            "SELECT model_name, count(*) calls, sum(prompt_tokens) prompt, sum(cached_tokens) cached, "
            "sum(output_tokens) output, sum(thinking_tokens) thinking, sum(cost_usd) cost FROM node_executions e "
            "JOIN workflow_runs r USING (run_id) JOIN graph_definitions g ON g.graph_id=r.graph_id "
            "AND g.version=r.graph_version WHERE g.workspace_id=%s AND e.model_name LIKE %s "
            "AND e.created_at > now() - interval '7 days' GROUP BY 1", (ws, cli + ":%")).fetchall()
        c.commit()
    return {"cli": cli, "report": out[-6000:], "rate_limits": parsed or (snap or {}).get("rate_limits"),
            "rate_limits_at": (snap or {}).get("captured_at"), "agdd_7d": tally}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/vm/demo-repo", "EDITOR")
def vm_demo_repo(ctx, ws, body):
    """Creates /workspace/repos/demo (buggy calc + tests) inside the VM for a first run."""
    _require_vm()
    sh = vm_service.shell(ws, "agdd-api")
    d = "/workspace/repos/demo"
    from .cli_agents import PREREQ
    sh.run(PREREQ + f"rm -rf {d} && mkdir -p {d}/tests", timeout=900, check=True)
    sh.put_file(f"{d}/calc.py", DEMO_CALC)
    sh.put_file(f"{d}/tests/test_calc.py", DEMO_TEST)
    sh.put_file(f"{d}/.gitignore", ".agdd/\n__pycache__/\n.pytest_cache/\n")
    r = sh.run(f"cd {d} && git init -q && git add . && git -c user.name=agdd -c user.email=agdd@localhost "
               f"commit -qm 'demo: buggy calc' && (python3 -c 'import pytest' 2>/dev/null || "
               f"python3 -m pip install -q pytest)", timeout=600)
    if r.exit_code != 0:
        raise HttpError(500, r.output[-1500:])
    return {"repo_key": "demo", "path": d}


# ---------------- IDE (editable file view + execution in the VM repos) ----------------
IDE_SCRIPT = r"""
import base64, json, os, shutil, subprocess, sys
a = json.loads(base64.b64decode(sys.argv[1]))
HOME = a.get("home") or os.environ.get("HOME", "/workspace")
ROOT = os.path.join(HOME, "repos")
HAS_GIT = bool(shutil.which("git"))
def has_git_repo(d):
    return HAS_GIT and os.path.isdir(os.path.join(d, ".git"))
def git(repo, *args, check=False):
    if not HAS_GIT:
        return ""
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace")
    if check and r.returncode: raise SystemExit(r.stderr[-500:])
    return r.stdout if r.returncode == 0 else ""
def safe_path(repo, rel):
    norm = os.path.normpath(rel or "")
    if not norm or norm in (".", "..") or norm.startswith("..") or os.path.isabs(norm):
        raise SystemExit("bad path")
    parts = norm.replace("\\", "/").split("/")
    if ".git" in parts:
        raise SystemExit("protected git path")
    full = os.path.join(repo, norm)
    real_repo = os.path.realpath(repo)
    real_parent = os.path.realpath(os.path.dirname(full))
    if real_parent != real_repo and not real_parent.startswith(real_repo + os.sep):
        raise SystemExit("bad path")
    return norm.replace("\\", "/"), full
def ensure_repo(d):
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(d, ".gitignore")
    if not os.path.exists(gi):
        open(gi, "w", encoding="utf-8").write(".agdd/\n__pycache__/\n.pytest_cache/\nnode_modules/\n")
    readme = os.path.join(d, "README.md")
    if not os.path.exists(readme):
        open(readme, "w", encoding="utf-8").write("# Workspace Repository\n")
    if HAS_GIT and not os.path.isdir(os.path.join(d, ".git")):
        subprocess.run(["git", "-C", d, "init", "-q"], check=True)
        subprocess.run(["git", "-C", d, "add", "."], check=False)
        subprocess.run(["git", "-C", d, "-c", "user.name=agdd", "-c", "user.email=agdd@localhost", "commit", "-qm", "init"], check=False)

op = a["op"]
if op == "repos":
    out = []
    for n in sorted(os.listdir(ROOT)) if os.path.isdir(ROOT) else []:
        if n.startswith("."):
            continue
        d = os.path.join(ROOT, n)
        if os.path.isdir(d):
            if has_git_repo(d):
                out.append({"repo": n, "branch": git(d, "rev-parse", "--abbrev-ref", "HEAD").strip() or "main",
                            "dirty": bool(git(d, "status", "--porcelain").strip())})
            else:
                out.append({"repo": n, "branch": "local", "dirty": False})
    print(json.dumps(out)); raise SystemExit
repo = os.path.join(ROOT, a["repo"])
if op == "init_repo":
    ensure_repo(repo)
    br = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() if has_git_repo(repo) else "local"
    print(json.dumps({"repo": a["repo"], "branch": br or "main", "dirty": False}))
    raise SystemExit
if not os.path.isdir(repo): raise SystemExit("repo not found")
if op == "tree":
    if has_git_repo(repo):
        files = [f for f in git(repo, "ls-files", "-co", "--exclude-standard").splitlines() if f][:5000]
        status = {}
        for line in git(repo, "status", "--porcelain", "-uall").splitlines():
            status[line[3:].split(" -> ")[-1]] = line[:2].strip() or "M"
        dirs = sorted({os.path.dirname(f) for f in files if "/" in f})
        for rdir, dnames, _ in os.walk(repo):
            dnames[:] = [d for d in dnames if d not in (".git", ".agdd", "__pycache__", "node_modules", ".pytest_cache")]
            for d in dnames:
                rel = os.path.relpath(os.path.join(rdir, d), repo).replace(os.sep, "/")
                if rel != ".":
                    dirs.append(rel)
        log = []
        for line in git(repo, "log", "-n", "40", "--date=iso-strict", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s").splitlines():
            h, an, ad, s = (line.split("\x1f") + ["", "", "", ""])[:4]
            log.append({"sha": h, "author": an, "date": ad, "subject": s})
        print(json.dumps({"files": sorted(set(files) | set(k for k, v in status.items() if v != "D")),
                          "dirs": sorted(set(dirs)), "status": status, "log": log}))
    else:
        files, dirs = [], []
        skip_dirs = {".git", ".agdd", "__pycache__", "node_modules", ".pytest_cache"}
        for rdir, dnames, fnames in os.walk(repo):
            dnames[:] = sorted(d for d in dnames if d not in skip_dirs)
            for d in dnames:
                rel_d = os.path.relpath(os.path.join(rdir, d), repo).replace(os.sep, "/")
                if rel_d != ".":
                    dirs.append(rel_d)
            for fn in sorted(fnames):
                files.append(os.path.relpath(os.path.join(rdir, fn), repo).replace(os.sep, "/"))
                if len(files) >= 5000:
                    break
            if len(files) >= 5000:
                break
        print(json.dumps({"files": sorted(set(files)), "dirs": sorted(set(dirs)), "status": {}, "log": []}))
elif op == "file":
    path, full = safe_path(repo, a["path"])
    def txt(b):
        return None if b is None or b"\0" in b[:8000] else b[:262_144].decode("utf-8", "replace")
    cur = open(full, "rb").read() if os.path.isfile(full) else None
    head = None
    if has_git_repo(repo):
        r = subprocess.run(["git", "-C", repo, "show", "HEAD:" + path], capture_output=True)
        head = r.stdout if r.returncode == 0 else None
    print(json.dumps({"path": path, "content": txt(cur), "head": txt(head), "exists": cur is not None,
                      "binary": (cur is not None and txt(cur) is None), "size": len(cur or b""), "truncated": len(cur or b"") > 262_144}))
elif op == "write":
    path, full = safe_path(repo, a["path"])
    os.makedirs(os.path.dirname(full), exist_ok=True)
    content = str(a.get("content") or "")
    open(full, "w", encoding="utf-8").write(content)
    head = None
    if has_git_repo(repo):
        r = subprocess.run(["git", "-C", repo, "show", "HEAD:" + path], capture_output=True)
        head = r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    print(json.dumps({"path": path, "size": len(content.encode("utf-8")), "head": head, "ok": True}))
elif op == "delete":
    path, full = safe_path(repo, a["path"])
    if os.path.isdir(full):
        shutil.rmtree(full)
    elif os.path.isfile(full):
        os.unlink(full)
    else:
        raise SystemExit("file not found")
    print(json.dumps({"deleted": path, "ok": True}))
elif op == "rename":
    src, src_full = safe_path(repo, a["from"])
    dst, dst_full = safe_path(repo, a["to"])
    if not os.path.exists(src_full):
        raise SystemExit("source not found")
    os.makedirs(os.path.dirname(dst_full), exist_ok=True)
    os.replace(src_full, dst_full)
    print(json.dumps({"from": src, "to": dst, "ok": True}))
elif op == "mkdir":
    path, full = safe_path(repo, a["path"])
    os.makedirs(full, exist_ok=True)
    print(json.dumps({"dir": path, "ok": True}))
elif op == "run":
    path, full = safe_path(repo, a["path"])
    if not os.path.isfile(full):
        raise SystemExit("file not found")
    ext = os.path.splitext(path)[1].lower()
    cmd_map = {".py": ["python3", full], ".sh": ["bash", full], ".js": ["node", full],
               ".mjs": ["node", full], ".lean": ["lean", full], ".v": ["coqc", full]}
    cmd = cmd_map.get(ext)
    if not cmd:
        raise SystemExit("unsupported file type for direct run: " + (ext or "unknown"))
    try:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, errors="replace", timeout=30)
        print(json.dumps({"path": path, "exit_code": r.returncode, "stdout": r.stdout[:65536], "stderr": r.stderr[:65536]}))
    except FileNotFoundError:
        print(json.dumps({"path": path, "exit_code": 127, "stdout": "", "stderr": f"runtime not installed: {cmd[0]}"}))
    except subprocess.TimeoutExpired:
        print(json.dumps({"path": path, "exit_code": 124, "stdout": "", "stderr": "execution timed out (30s)"}))
elif op == "commit":
    sha = a["sha"]
    if not all(c in "0123456789abcdef" for c in sha) or not 7 <= len(sha) <= 40: raise SystemExit("bad sha")
    patch = git(repo, "show", "--stat", "--patch", "--format=%H%n%an <%ae>%n%ad%n%n%B", sha)[:120_000] if has_git_repo(repo) else ""
    print(json.dumps({"sha": sha, "patch": patch}))
elif op == "git_commit":
    if not has_git_repo(repo):
        raise SystemExit("not a git repository")
    msg = str(a.get("message") or "").strip()[:200]
    if not msg:
        raise SystemExit("commit message required")
    add_r = subprocess.run(["git", "-C", repo, "add", "-A"], capture_output=True, text=True, errors="replace")
    if add_r.returncode != 0:
        raise SystemExit((add_r.stderr or add_r.stdout or "git add failed")[-500:])
    diff_r = subprocess.run(["git", "-C", repo, "diff", "--cached", "--quiet"], capture_output=True)
    if diff_r.returncode == 0:
        print(json.dumps({"ok": False, "error": "커밋할 변경 사항이 없습니다"}))
    else:
        com_r = subprocess.run(["git", "-C", repo, "-c", "user.name=agdd", "-c", "user.email=agdd@localhost",
                                "commit", "-qm", msg], capture_output=True, text=True, errors="replace")
        if com_r.returncode != 0:
            raise SystemExit((com_r.stderr or com_r.stdout or "git commit failed")[-500:])
        sha = git(repo, "rev-parse", "HEAD").strip()
        print(json.dumps({"ok": True, "sha": sha, "subject": msg}))
elif op == "git_revert":
    if not has_git_repo(repo):
        raise SystemExit("not a git repository")
    path, full = safe_path(repo, a["path"])
    has_head = subprocess.run(["git", "-C", repo, "cat-file", "-e", "HEAD:" + path], capture_output=True)
    if has_head.returncode != 0:
        raise SystemExit("file not in HEAD")
    chk = subprocess.run(["git", "-C", repo, "checkout", "HEAD", "--", path], capture_output=True, text=True, errors="replace")
    if chk.returncode != 0:
        raise SystemExit((chk.stderr or chk.stdout or "git checkout failed")[-500:])
    print(json.dumps({"ok": True, "path": path}))
elif op == "diff":
    print(json.dumps({"patch": git(repo, "diff", "HEAD")[:120_000] if has_git_repo(repo) else ""}))
"""
_REPO_RX = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_IDE_HASH = __import__("hashlib").sha256(IDE_SCRIPT.encode()).hexdigest()[:12]
_IDE_READY: set = set()


def _ide(ws, **args):
    _require_vm()
    if "repo" in args and (not _REPO_RX.fullmatch(args["repo"]) or args["repo"] in (".", "..")):
        raise HttpError(400, "invalid repo")
    from .vm import RuntimeError_

    last_err = None
    for attempt in range(2):
        sh = vm_service.shell(ws, "agdd-ide")
        payload = {**args, "home": sh.home}
        arg = base64.b64encode(json.dumps(payload).encode()).decode()
        script = f"{sh.home}/.agdd/ide-{_IDE_HASH}.py"
        ready_key = (sh.canvas_id, sh.home)
        raw_out = ""
        try:
            if ready_key not in _IDE_READY:
                sh.run(f"mkdir -p {sh.home}/.agdd", timeout=30, check=True)
                sh.put_file(script, IDE_SCRIPT)
            r = sh.run_inline(f"python3 {script} {arg}", timeout=120)
            if r.exit_code != 0 and ("No such file" in r.output or "can't open file" in r.output):
                _IDE_READY.discard(ready_key)
                sh.run(f"mkdir -p {sh.home}/.agdd", timeout=30, check=True)
                sh.put_file(script, IDE_SCRIPT)
                r = sh.run_inline(f"python3 {script} {arg}", timeout=120)
            raw_out = r.output.strip()
            if r.exit_code != 0:
                _IDE_READY.discard(ready_key)
                raise HttpError(404 if "not found" in r.output else 400, raw_out[-500:] or "ide error")
            lines = [ln for ln in raw_out.splitlines() if ln.strip()]
            if not lines:
                _IDE_READY.discard(ready_key)
                raise RuntimeError_("empty ide helper output (check VM disk space)")
            data = json.loads(lines[-1])
            _IDE_READY.add(ready_key)
            return data
        except (RuntimeError_, json.JSONDecodeError) as e:
            _IDE_READY.discard(ready_key)
            last_err = e
            if attempt == 0 and vm_service.maybe_expand_disk(ws):
                continue
            if isinstance(e, json.JSONDecodeError):
                raise HttpError(502, f"ide helper returned invalid output: {raw_out[-300:] or 'empty'}")
            raise HttpError(502, str(e)[:600])
    raise HttpError(502, str(last_err)[:600])


@route("GET", r"/workspaces/(?P<ws>[^/]+)/ide/repos", "VIEWER")
def ide_repos(ctx, ws, body):
    return _ide(ws, op="repos")


@route("POST", r"/workspaces/(?P<ws>[^/]+)/ide/repos", "EDITOR")
def ide_init_repo(ctx, ws, body):
    repo = str((body or {}).get("repo") or "workspace").strip()
    return _ide(ws, op="init_repo", repo=repo)


@route("GET", r"/workspaces/(?P<ws>[^/]+)/ide/repos/(?P<repo>[^/]+)/tree", "VIEWER")
def ide_tree(ctx, ws, body, repo):
    return _ide(ws, op="tree", repo=repo)


@route("GET", r"/workspaces/(?P<ws>[^/]+)/ide/repos/(?P<repo>[^/]+)/diff", "VIEWER")
def ide_diff(ctx, ws, body, repo):
    return _ide(ws, op="diff", repo=repo)


@route("GET", r"/workspaces/(?P<ws>[^/]+)/ide/repos/(?P<repo>[^/]+)/commits/(?P<sha>[0-9a-f]{7,40})", "VIEWER")
def ide_commit(ctx, ws, body, repo, sha):
    return _ide(ws, op="commit", repo=repo, sha=sha)


@route("GET", r"/workspaces/(?P<ws>[^/]+)/ide/repos/(?P<repo>[^/]+)/files/(?P<path>.+)", "VIEWER")
def ide_file(ctx, ws, body, repo, path):
    return _ide(ws, op="file", repo=repo, path=urllib.parse.unquote(path))


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/ide/repos/(?P<repo>[^/]+)/files/(?P<path>.+)", "EDITOR")
def ide_write_file(ctx, ws, body, repo, path):
    content = str((body or {}).get("content", ""))
    if len(content.encode("utf-8")) > 512 * 1024:
        raise HttpError(413, "file too large (max 512 KiB)")
    return _ide(ws, op="write", repo=repo, path=urllib.parse.unquote(path), content=content)


@route("DELETE", r"/workspaces/(?P<ws>[^/]+)/ide/repos/(?P<repo>[^/]+)/files/(?P<path>.+)", "EDITOR")
def ide_delete_file(ctx, ws, body, repo, path):
    return _ide(ws, op="delete", repo=repo, path=urllib.parse.unquote(path))


@route("POST", r"/workspaces/(?P<ws>[^/]+)/ide/repos/(?P<repo>[^/]+)/rename", "EDITOR")
def ide_rename_file(ctx, ws, body, repo):
    return _ide(ws, op="rename", repo=repo, **{"from": str(body.get("from") or ""), "to": str(body.get("to") or "")})


@route("POST", r"/workspaces/(?P<ws>[^/]+)/ide/repos/(?P<repo>[^/]+)/mkdir", "EDITOR")
def ide_mkdir(ctx, ws, body, repo):
    return _ide(ws, op="mkdir", repo=repo, path=str(body.get("path") or ""))


@route("POST", r"/workspaces/(?P<ws>[^/]+)/ide/repos/(?P<repo>[^/]+)/run", "EDITOR")
def ide_run_file(ctx, ws, body, repo):
    return _ide(ws, op="run", repo=repo, path=str(body.get("path") or ""))


@route("POST", r"/workspaces/(?P<ws>[^/]+)/ide/repos/(?P<repo>[^/]+)/commit", "EDITOR")
def ide_commit_repo(ctx, ws, body, repo):
    msg = str((body or {}).get("message") or "").strip()[:200]
    if not msg:
        raise HttpError(422, "commit message required")
    return _ide(ws, op="git_commit", repo=repo, message=msg)


@route("POST", r"/workspaces/(?P<ws>[^/]+)/ide/repos/(?P<repo>[^/]+)/revert", "EDITOR")
def ide_revert_file(ctx, ws, body, repo):
    path = str((body or {}).get("path") or "").strip()
    if not path:
        raise HttpError(422, "path required")
    return _ide(ws, op="git_revert", repo=repo, path=path)


@route("POST", r"/workspaces/(?P<ws>[^/]+)/vm/cells/sync", "EDITOR")
def vm_cells_sync(ctx, ws, body):
    _require_vm()
    cells = (body or {}).get("cells") or []
    delete_missing = bool((body or {}).get("delete_missing", True))
    force = bool((body or {}).get("force", True))
    res = vm_sync.sync_cells(ws, cells, delete_missing=delete_missing, force=force)
    vm_sync.sync_workspace_context_async(ws)
    return res


@route("GET", r"/workspaces/(?P<ws>[^/]+)/vm/cells", "VIEWER")
def vm_cells_read(ctx, ws, body):
    _require_vm()
    return vm_sync.read_cells(ws)


@route("POST", r"/workspaces/(?P<ws>[^/]+)/vm/sync-spec-db", "EDITOR")
def vm_sync_spec_db(ctx, ws, body):
    _require_vm()
    return vm_sync.sync_workspace_context(ws, (body or {}).get("repo_key"))


DEMO_CALC = '''"""Tiny demo module for trying AGDD."""


def add(a, b):
    return a - b


def mean(values):
    return sum(values) / len(values)
'''
DEMO_TEST = '''import pytest

from calc import add, mean


def test_add():
    assert add(2, 3) == 5


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_mean_empty_raises_value_error():
    with pytest.raises(ValueError):
        mean([])
'''


# ---------------- bootstrap ----------------
def _ensure_user(c, user):
    c.execute("INSERT INTO users (user_id, email) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
              (user, f"{user}@agdd.local"))


@route("GET", r"/workspaces")
def my_workspaces(ctx, _, body):
    with infra.pg(readonly=True) as c:
        return c.execute("SELECT w.workspace_id, w.name, m.role FROM workspace_members m JOIN workspaces w "
                         "USING (workspace_id) WHERE m.user_id=%s ORDER BY w.created_at", (ctx["user"],)).fetchall()


@route("POST", r"/workspaces")
def create_workspace(ctx, _, body):
    name = str(body.get("name") or "").strip()[:100]
    if not name:
        raise HttpError(422, "name required")
    with infra.pg() as c:
        _ensure_user(c, ctx["user"])
        w = c.execute("INSERT INTO workspaces (name) VALUES (%s) RETURNING workspace_id", (name,)).fetchone()
        c.execute("INSERT INTO workspace_members VALUES (%s,%s,'OWNER')", (w["workspace_id"], ctx["user"]))
        c.commit()
    return {"workspace_id": w["workspace_id"], "name": name, "role": "OWNER"}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/members", "VIEWER")
def list_members(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        return {
            "members": c.execute(
                "SELECT m.user_id, u.email, m.role FROM workspace_members m "
                "LEFT JOIN users u USING (user_id) WHERE m.workspace_id=%s "
                "ORDER BY CASE m.role WHEN 'OWNER' THEN 0 WHEN 'APPROVER' THEN 1 WHEN 'EDITOR' THEN 2 ELSE 3 END, m.user_id",
                (ws,),
            ).fetchall()
        }


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/members", "OWNER")
def add_member(ctx, ws, body):
    if body.get("role") not in pgstore.ROLE_RANK or not re.fullmatch(r"[0-9a-f-]{36}", str(body.get("user_id"))):
        raise HttpError(422, "user_id(uuid) and role required")
    with infra.pg() as c:
        _ensure_user(c, body["user_id"])
        c.execute("INSERT INTO workspace_members VALUES (%s,%s,%s) ON CONFLICT (workspace_id, user_id) "
                  "DO UPDATE SET role=EXCLUDED.role", (ws, body["user_id"], body["role"]))
        c.commit()
    return {"ok": True}


@route("DELETE", r"/workspaces/(?P<ws>[^/]+)/members/(?P<uid>[0-9a-f-]{36})", "OWNER")
def remove_member(ctx, ws, body, uid):
    with infra.pg() as c:
        target = c.execute(
            "SELECT role FROM workspace_members WHERE workspace_id=%s AND user_id=%s",
            (ws, uid),
        ).fetchone()
        if not target:
            raise HttpError(404, "member not found")
        if target["role"] == "OWNER":
            owners = c.execute(
                "SELECT count(*) AS n FROM workspace_members WHERE workspace_id=%s AND role='OWNER'",
                (ws,),
            ).fetchone()["n"]
            if int(owners) <= 1:
                raise HttpError(409, "cannot remove the last OWNER")
        n = c.execute(
            "DELETE FROM workspace_members WHERE workspace_id=%s AND user_id=%s",
            (ws, uid),
        ).rowcount
        c.commit()
    return {"ok": True, "deleted": bool(n)}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/seed-example", "EDITOR")
def seed_example(ctx, ws, body):
    """Loads the platform's own spec tables (DOC.md §3.3) so the gate and runs can be tried immediately."""
    from .seed import PLATFORM_SNAPSHOT
    snap = {"srs": PLATFORM_SNAPSHOT["srs"], "arc": PLATFORM_SNAPSHOT["arc"],
            "sch": PLATFORM_SNAPSHOT["sch"], "trace": PLATFORM_SNAPSHOT["trace"]}
    with infra.pg() as c:
        sid = pgstore.save_snapshot(c, ws, snap, False)
        c.commit()
    spec_store.update(ws, sid, snap)
    vm_sync.sync_workspace_context_async(ws)
    return {"snapshot_id": sid}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/graphs", "VIEWER")
def list_graphs(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        return c.execute("SELECT DISTINCT ON (graph_id) graph_id, version, definition_json, created_at "
                         "FROM graph_definitions WHERE workspace_id=%s AND is_published "
                         "ORDER BY graph_id, version DESC", (ws,)).fetchall()


@route("GET", r"/workspaces/(?P<ws>[^/]+)/runs", "VIEWER")
def list_runs(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        return c.execute("SELECT r.run_id, r.route_mode, r.current_node, r.status, r.created_at, r.updated_at "
                         "FROM workflow_runs r JOIN graph_definitions g ON g.graph_id=r.graph_id "
                         "AND g.version=r.graph_version WHERE g.workspace_id=%s ORDER BY r.created_at DESC LIMIT 50",
                         (ws,)).fetchall()


@route("GET", r"/workspaces/(?P<ws>[^/]+)/conflicts", "VIEWER")
def last_gate(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        return c.execute("SELECT snapshot_id, version, conflict_passed, created_at FROM spec_snapshots "
                         "WHERE workspace_id=%s ORDER BY version DESC LIMIT 1", (ws,)).fetchone() or {}


# ---------------- specs ----------------
@route("GET", r"/workspaces/(?P<ws>[^/]+)/specs", "VIEWER")
def list_specs(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        eng, lock, _ = spec_store.get_engine(c, ws)
    with lock:
        return {t.lower(): eng.rows(t, accept_only=False) for t in TABLES} | {"trace": eng.snapshot()["trace"]}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/specs/query", "VIEWER")
def query_specs(ctx, ws, body):
    body = body or {}
    with infra.pg(readonly=True) as c:
        if body.get("sql"):
            try:
                return spec_store.query_readonly(c, ws, str(body["sql"]), body.get("params") or (), int(body.get("limit", 200)))
            except Exception as e:
                raise HttpError(400, str(e))
        if body.get("fts"):
            return {"matches": spec_store.search_fts(c, ws, str(body["fts"]), int(body.get("limit", 20)))}
        eng, lock, _ = spec_store.get_engine(c, ws)
        with lock:
            return {"scope": eng.executable_scope(body.get("domain")), "summary": spec_store.summary(c, ws)}


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/specs/(?P<typ>SRS|ARC|SCH)", "EDITOR")
def upsert_spec(ctx, ws, body, typ):
    """Write-through: every edit lands in the in-memory engine and is persisted as a new (unpassed) snapshot."""
    with infra.pg() as c:
        eng = _ws_engine(c, ws)
        try:
            rows = body if isinstance(body, list) else [body]
            for r in rows:
                eng.upsert(typ, r)
            for tr in (body.get("trace", []) if isinstance(body, dict) else []):
                eng.trace(*tr)
            snap = eng.snapshot()
            sid = pgstore.save_snapshot(c, ws, snap, False)
            c.commit()
        finally:
            eng.close()
    spec_store.update(ws, sid, snap)
    vm_sync.sync_workspace_context_async(ws)
    infra_safe(infra.cache_invalidate, f"agdd:dash:{ws}")
    return {"snapshot_id": sid}


@route("DELETE", r"/workspaces/(?P<ws>[^/]+)/specs/(?P<typ>SRS|ARC|SCH)/(?P<sid>[A-Za-z0-9_.-]{1,64})", "EDITOR")
def delete_spec(ctx, ws, body, typ, sid):
    with infra.pg() as c:
        eng = _ws_engine(c, ws)
        try:
            if not eng.delete(typ, sid):
                raise HttpError(404, f"{typ} {sid} not found")
            snap_obj = eng.snapshot()
            snap = pgstore.save_snapshot(c, ws, snap_obj, False)
            c.commit()
        finally:
            eng.close()
    spec_store.update(ws, snap, snap_obj)
    vm_sync.sync_workspace_context_async(ws)
    infra_safe(infra.cache_invalidate, f"agdd:dash:{ws}")
    return {"snapshot_id": snap}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/trace/delete", "EDITOR")
def delete_trace(ctx, ws, body):
    with infra.pg() as c:
        eng = _ws_engine(c, ws)
        try:
            eng.untrace(body["source_type"], body["source_id"], body["target_type"], body["target_id"], body["relation_type"])
            snap_obj = eng.snapshot()
            snap = pgstore.save_snapshot(c, ws, snap_obj, False)
            c.commit()
        finally:
            eng.close()
    spec_store.update(ws, snap, snap_obj)
    vm_sync.sync_workspace_context_async(ws)
    return {"snapshot_id": snap}


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/trace", "EDITOR")
def put_trace(ctx, ws, body):
    with infra.pg() as c:
        eng = _ws_engine(c, ws)
        try:
            for t in body:
                eng.trace(t["source_type"], t["source_id"], t["target_type"], t["target_id"], t["relation_type"])
            snap_obj = eng.snapshot()
            sid = pgstore.save_snapshot(c, ws, snap_obj, False)
            c.commit()
        finally:
            eng.close()
    spec_store.update(ws, sid, snap_obj)
    vm_sync.sync_workspace_context_async(ws)
    return {"snapshot_id": sid}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/preflight", "EDITOR")
def run_preflight(ctx, ws, body):
    """3-step: (1) hydrate in a short read -> (2) SQL + LLM checks with no DB connection held ->
    (3) commit the snapshot in a short write (DOC.md §4.1: LLM calls are never inside a transaction)."""
    from concurrent.futures import ThreadPoolExecutor

    with infra.pg(readonly=True) as c:
        eng = _ws_engine(c, ws)
    try:
        use_vm = _engine(body) == "vm-cli"
        llm_for = (lambda task: _vm_llm(ws, "agdd-gate")) if use_vm else (lambda task: infra.cheap_model(task, ws))
        llm = llm_for("conflict_check") if body.get("semantic", True) else None
        snaps = []
        with ThreadPoolExecutor(max_workers=1) as ex:  # ambiguity linting overlaps the conflict gate
            amb = ex.submit(router.ambiguity_gate, eng.rows("SRS"),
                            _vm_llm(ws, "agdd-amb") if use_vm else llm_for("ambiguity")) \
                if body.get("ambiguity", True) else None
            res = preflight.run_gate(eng, llm, lambda snap, ok: snaps.append((snap, ok)))
            questions = amb.result() if amb and res.passed else []
    finally:
        eng.close()
    with infra.pg() as c:
        sid = pgstore.save_snapshot(c, ws, *snaps[0])
        c.commit()
    spec_store.update(ws, sid, snaps[0][0])
    vm_sync.sync_workspace_context_async(ws)
    infra_safe(infra.cache_invalidate, f"agdd:dash:{ws}")
    return {"passed": res.passed, "snapshot_id": sid, "conflicts": res.conflicts,
            "ambiguity_questions": questions, "scope": res.scope}


# ---------------- graphs ----------------
@route("POST", r"/workspaces/(?P<ws>[^/]+)/graphs", "EDITOR")
def publish_graph(ctx, ws, body):
    try:
        orchestrator.validate_graph(body["definition"])
    except (orchestrator.GraphError, KeyError) as e:
        raise HttpError(422, str(e))
    with infra.pg() as c:
        gid = body.get("graph_id")
        r = c.execute(
            "INSERT INTO graph_definitions (graph_id, workspace_id, version, definition_json, is_published) "
            "SELECT coalesce(%s::uuid, gen_random_uuid()), %s, coalesce(max(version),0)+1, %s, true "
            "FROM graph_definitions WHERE graph_id = %s::uuid RETURNING graph_id, version",
            (gid, ws, Jsonb(body["definition"]), gid)).fetchone()
        c.commit()
    return r


# ---------------- runs ----------------
@route("POST", r"/workspaces/(?P<ws>[^/]+)/runs", "EDITOR")
def start_run(ctx, ws, body):
    with infra.pg() as c:
        snap = c.execute("SELECT snapshot_id FROM spec_snapshots WHERE workspace_id=%s AND conflict_passed "
                         "ORDER BY version DESC LIMIT 1", (ws,)).fetchone()
        latest = pgstore.latest_snapshot(c, ws)
        if not snap or latest["snapshot_id"] != snap["snapshot_id"]:
            raise HttpError(409, "latest spec snapshot has not passed the Pre-Flight Gate")
        g = c.execute("SELECT graph_id, version, definition_json FROM graph_definitions WHERE graph_id=%s "
                      "AND workspace_id=%s AND is_published ORDER BY version DESC LIMIT 1",
                      (body["graph_id"], ws)).fetchone()
        if not g:
            raise HttpError(404, "graph not found")
        engine = _engine(body)
        agent_override = body.get("agent") or {}
        if engine == "vm-cli":
            vm_service.load_settings(ws, agent_override)  # validates cli/model/effort/session before queueing
        route_llm = _vm_llm(ws, "agdd-route", agent_override) if engine == "vm-cli" else infra.cheap_model("route", ws)
        decision = router.route_request(body.get("request", ""), route_llm) \
            if body.get("request") else router.RouteDecision("FULL_AGDD", ["no request text"])
        entry = g["definition_json"].get("fast_track_entry") if decision.route_mode == "FAST_TRACK" else None
        entry = entry or g["definition_json"]["entry"]
        run = c.execute(
            "INSERT INTO workflow_runs (graph_id, graph_version, snapshot_id, route_mode, current_node, status, handoff_json) "
            "VALUES (%s,%s,%s,%s,%s,'PENDING',%s) RETURNING run_id",
            (g["graph_id"], g["version"], snap["snapshot_id"], decision.route_mode, entry,
             Jsonb({"request": body.get("request", ""), "repo_key": body.get("repo_key", ""), "engine": engine,
                    "agent": agent_override}))).fetchone()
        c.execute(orchestrator.ENQUEUE_SQL, {"run_id": run["run_id"], "node_name": entry, "priority": 5})
        c.commit()
    return {"run_id": run["run_id"], "route_mode": decision.route_mode, "reasons": decision.reasons}


@route("GET", r"/runs/(?P<run>[0-9a-f-]+)")
def get_run(ctx, run, body):
    def load():
        with infra.pg(readonly=True) as c:
            r = c.execute("SELECT * FROM workflow_runs WHERE run_id=%s", (run,)).fetchone()
            if not r:
                return None
            r["executions"] = c.execute(
                "SELECT node_name, model_name, prompt_tokens, cached_tokens, output_tokens, thinking_tokens, cost_usd, "
                "duration_ms, created_at FROM node_executions WHERE run_id=%s ORDER BY created_at", (run,)).fetchall()
            r["hitl"] = c.execute("SELECT * FROM hitl_decisions WHERE run_id=%s ORDER BY decision_id", (run,)).fetchall()
            return r
    r = infra_safe(infra.cache_get_or_set, f"agdd:run:{run}", 5, load, fallback=load)
    if not r:
        raise HttpError(404, "run not found")
    _check_run_role(ctx, run, "VIEWER")
    return r


@route("POST", r"/runs/(?P<run>[0-9a-f-]+)/cancel")
def cancel_run(ctx, run, body):
    ws = _check_run_role(ctx, run, "EDITOR")
    with infra.pg() as c:
        row = c.execute(
            "UPDATE workflow_runs SET status='HALTED', version=version+1, updated_at=now(), "
            "handoff_json = coalesce(handoff_json, '{}'::jsonb) || %s::jsonb "
            "WHERE run_id=%s AND status IN ('PENDING','RUNNING','WAITING_HITL') RETURNING run_id",
            (Jsonb({"cancelled_by": ctx["user"]}), run),
        ).fetchone()
        c.execute("UPDATE job_queue SET status='FAILED' WHERE run_id=%s AND status='PENDING'", (run,))
        c.execute(
            "UPDATE hitl_decisions SET status='REJECTED', decided_by=%s, decided_at=now() "
            "WHERE run_id=%s AND status='PENDING'",
            (ctx["user"], run),
        )
        c.commit()
    infra_safe(infra.cache_invalidate, f"agdd:run:{run}", f"agdd:dash:{ws}")
    return {"ok": True, "halted": bool(row)}


def _check_run_role(ctx, run, minimum):
    with infra.pg(readonly=True) as c:
        ws = c.execute("SELECT g.workspace_id FROM workflow_runs r JOIN graph_definitions g "
                       "ON g.graph_id=r.graph_id AND g.version=r.graph_version WHERE r.run_id=%s", (run,)).fetchone()
        if not ws:
            raise HttpError(404, "run not found")
        try:
            pgstore.require_role(c, ws["workspace_id"], ctx["user"], minimum)
        except PermissionError as e:
            raise HttpError(403, str(e))
    return ws["workspace_id"]


# ---------------- HITL ----------------
@route("GET", r"/workspaces/(?P<ws>[^/]+)/hitl", "VIEWER")
def list_hitl(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        return c.execute(
            "SELECT h.* FROM hitl_decisions h JOIN workflow_runs r USING (run_id) JOIN graph_definitions g "
            "ON g.graph_id=r.graph_id AND g.version=r.graph_version WHERE g.workspace_id=%s AND h.status='PENDING' "
            "ORDER BY h.reversibility DESC, h.decision_id", (ws,)).fetchall()


@route("POST", r"/hitl/(?P<did>\d+)/decide")
def decide(ctx, did, body):
    body = body or {}
    status = body.get("status")
    if status not in ("APPROVED", "REJECTED"):
        raise HttpError(422, "status must be APPROVED|REJECTED")
    chosen_raw = body.get("chosen_option")
    chosen_option = None
    if chosen_raw is not None and not isinstance(chosen_raw, bool):
        try:
            chosen_option = int(chosen_raw)
        except (TypeError, ValueError):
            chosen_option = None
    comment = str(body.get("comment") or "").strip()[:1000]
    resolution = {"status": status}
    if chosen_option is not None:
        resolution["chosen_option"] = chosen_option
    if comment:
        resolution["comment"] = comment
    with infra.pg() as c:
        d = c.execute("SELECT run_id FROM hitl_decisions WHERE decision_id=%s", (did,)).fetchone()
        if not d:
            raise HttpError(404, "decision not found")
        _check_run_role(ctx, d["run_id"], "APPROVER")
        r = c.execute("UPDATE hitl_decisions SET status=%s, decided_by=%s, decided_at=now(), "
                      "decision_card_json = coalesce(decision_card_json, '{}'::jsonb) || %s::jsonb "
                      "WHERE decision_id=%s AND status='PENDING' RETURNING run_id",
                      (status, ctx["user"], Jsonb({"resolution": resolution}), did)).fetchone()
        if not r:
            raise HttpError(409, "already decided")
        pending = c.execute("SELECT count(*) AS n FROM hitl_decisions WHERE run_id=%s AND status='PENDING' "
                            "AND reversibility='IRREVERSIBLE'", (r["run_id"],)).fetchone()["n"]
        if pending == 0:
            run = c.execute("SELECT current_node, status FROM workflow_runs WHERE run_id=%s FOR UPDATE",
                            (r["run_id"],)).fetchone()
            if run["status"] == "WAITING_HITL":
                new_status = "RUNNING" if status == "APPROVED" else "FAILED"
                c.execute("UPDATE workflow_runs SET status=%s, version=version+1, updated_at=now(), "
                          "handoff_json = coalesce(handoff_json, '{}'::jsonb) || %s::jsonb WHERE run_id=%s",
                          (new_status, Jsonb({"hitl_resolution": resolution}), r["run_id"]))
                if status == "APPROVED":
                    c.execute(orchestrator.ENQUEUE_SQL, {"run_id": r["run_id"], "node_name": run["current_node"],
                                                         "priority": 1})
        c.commit()
    infra_safe(infra.cache_invalidate, f"agdd:run:{r['run_id']}")
    return {"ok": True, "resolution": resolution}


# ---------------- collaborative CLI / AGDD chat ----------------
DEFAULT_COLLAB_GRAPH = {
    "entry": "ambiguity",
    "fast_track_entry": "dev",
    "nodes": [
        {"name": "ambiguity", "kind": "AMBIGUITY_GATE"},
        {"name": "dev", "kind": "DEV", "max_retries": 2, "command": ["python3", "-m", "pytest", "-q"]},
        {"name": "semantic", "kind": "SEMANTIC_VERIFY", "max_retries": 2},
        {"name": "verify", "kind": "VERIFY", "max_retries": 2, "command": ["python3", "-m", "pytest", "-q", "--junitxml=.agdd/junit.xml"], "reports": {"junit": ".agdd/junit.xml"}},
        {"name": "audit", "kind": "AUDIT", "max_retries": 1},
    ],
    "edges": [
        {"from": "ambiguity", "to": "dev"},
        {"from": "dev", "to": "semantic"},
        {"from": "dev", "to": "dev", "on": "failure"},
        {"from": "semantic", "to": "verify"},
        {"from": "semantic", "to": "dev", "on": "failure"},
        {"from": "verify", "to": "audit"},
        {"from": "verify", "to": "dev", "on": "failure"},
        {"from": "audit", "to": "dev", "on": "failure"},
    ],
}


def _pending_ux_requests(ws: str, check_vm: bool = True) -> list[dict]:
    with infra.pg(readonly=True) as c:
        rows = c.execute(
            "SELECT h.decision_id, h.run_id, h.decision_card_json, r.handoff_json "
            "FROM hitl_decisions h JOIN workflow_runs r USING (run_id) JOIN graph_definitions g "
            "ON g.graph_id=r.graph_id AND g.version=r.graph_version "
            "WHERE g.workspace_id=%s AND h.status='PENDING' AND h.reason_code='UX_VISUAL_REQUEST' "
            "ORDER BY h.decision_id DESC LIMIT 10",
            (ws,),
        ).fetchall()
    out = []
    for row in rows:
        card = row.get("decision_card_json") or {}
        req = card.get("ux_request") or (row.get("handoff_json") or {}).get("ux_request") or {"view_key": "home", "prompt": ""}
        out.append({"source": "hitl", "decision_id": row["decision_id"], "run_id": row["run_id"], "ux_request": req})
    if not out and check_vm and vm_service.enabled():
        try:
            vm_req = vm_sync.read_pending_vm_ux_request(ws)
            if vm_req:
                out.append({"source": "vm", "decision_id": None, "run_id": None, "ux_request": vm_req})
        except Exception:
            pass
    return out


def _start_collab_agdd_run(ws: str, request_text: str, repo_key: str, extra_handoff: dict | None = None,
                           agent_override: dict | None = None, user_id: str | None = None) -> dict:
    engine = "vm-cli" if vm_service.enabled() else "gateway"
    agent_override = agent_override or {}
    if engine == "vm-cli":
        vm_service.load_settings(ws, agent_override)

    with infra.pg() as c:
        latest = pgstore.latest_snapshot(c, ws)
        if not latest:
            empty_snap = {"srs": [], "arc": [], "sch": [], "trace": []}
            sid = pgstore.save_snapshot(c, ws, empty_snap, True)
            c.commit()
            spec_store.update(ws, sid, empty_snap)
            latest = pgstore.latest_snapshot(c, ws)

        g = c.execute(
            "SELECT DISTINCT ON (graph_id) graph_id, version, definition_json FROM graph_definitions "
            "WHERE workspace_id=%s AND is_published ORDER BY graph_id, version DESC",
            (ws,),
        ).fetchone()
        if not g:
            g = c.execute(
                "INSERT INTO graph_definitions (workspace_id, version, definition_json, is_published) "
                "VALUES (%s, 1, %s, true) RETURNING graph_id, version, definition_json",
                (ws, Jsonb(DEFAULT_COLLAB_GRAPH)),
            ).fetchone()
        c.commit()

    if not latest["conflict_passed"]:
        with infra.pg(readonly=True) as c:
            eng = _ws_engine(c, ws)
        try:
            snaps = []
            llm = _vm_llm(ws, "agdd-gate", agent_override) if engine == "vm-cli" else infra.cheap_model("conflict_check", ws)
            res = preflight.run_gate(eng, llm, lambda snap, ok: snaps.append((snap, ok)))
        finally:
            eng.close()
        with infra.pg() as c:
            sid = pgstore.save_snapshot(c, ws, *snaps[0])
            if not res.passed:
                pgstore.add_collab_message(
                    c, ws, "assistant", "agdd",
                    f"⚠ 사전 명세 검사에서 CRITICAL 충돌 {len(res.critical)}건이 발견되어 실행을 시작할 수 없습니다.",
                    meta={"event": "PREFLIGHT_BLOCKED", "conflicts": res.conflicts},
                    user_id=user_id,
                )
            c.commit()
        spec_store.update(ws, sid, snaps[0][0])
        if not res.passed:
            return {"blocked": True, "conflicts": res.conflicts, "snapshot_id": sid}
        latest = {"snapshot_id": sid}

    route_llm = _vm_llm(ws, "agdd-route", agent_override) if engine == "vm-cli" else infra.cheap_model("route", ws)
    if request_text:
        try:
            decision = router.route_request(request_text, route_llm)
        except Exception as exc:
            decision = router.RouteDecision("FULL_AGDD", [f"router fallback: {exc}"])
    else:
        decision = router.RouteDecision("FULL_AGDD", ["no request text"])
    entry = g["definition_json"].get("fast_track_entry") if decision.route_mode == "FAST_TRACK" else None
    entry = entry or g["definition_json"]["entry"]
    handoff = {
        "request": request_text,
        "repo_key": repo_key,
        "engine": engine,
        "agent": agent_override,
        "collab_mode": True,
        **(extra_handoff or {}),
    }
    with infra.pg() as c:
        run = c.execute(
            "INSERT INTO workflow_runs (graph_id, graph_version, snapshot_id, route_mode, current_node, status, handoff_json) "
            "VALUES (%s,%s,%s,%s,%s,'PENDING',%s) RETURNING run_id",
            (g["graph_id"], g["version"], latest["snapshot_id"], decision.route_mode, entry, Jsonb(handoff)),
        ).fetchone()
        c.execute(orchestrator.ENQUEUE_SQL, {"run_id": run["run_id"], "node_name": entry, "priority": 5})
        pgstore.add_collab_message(
            c, ws, "event", "agdd",
            f"자율 그래프 실행 시작 ({decision.route_mode} · 진입: {entry})",
            meta={"event": "RUN_STARTED", "route_mode": decision.route_mode, "entry": entry, "reasons": decision.reasons},
            run_id=run["run_id"],
            user_id=user_id,
        )
        c.commit()
    return {"blocked": False, "run_id": run["run_id"], "route_mode": decision.route_mode,
            "reasons": decision.reasons, "entry": entry}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/collab/state", "VIEWER")
def collab_state(ctx, ws, body):
    settings = vm_service.load_settings(ws)
    with infra.pg(readonly=True) as c:
        messages = pgstore.list_collab_messages(c, ws, limit=120)
        spec_sum = spec_store.summary(c, ws)
        runs = c.execute(
            "SELECT r.run_id, r.route_mode, r.current_node, r.status, r.handoff_json, r.created_at, r.updated_at "
            "FROM workflow_runs r JOIN graph_definitions g ON g.graph_id=r.graph_id AND g.version=r.graph_version "
            "WHERE g.workspace_id=%s ORDER BY r.created_at DESC LIMIT 15",
            (ws,),
        ).fetchall()
        ux_views = pgstore.PgOverrideStore().list_views(ws)
    pending_ux = _pending_ux_requests(ws, check_vm=True)
    return {
        "messages": messages,
        "spec_summary": spec_sum,
        "runs": runs,
        "settings": settings.__dict__,
        "ux_views": ux_views,
        "pending_ux": pending_ux,
        "vm_enabled": vm_service.enabled(),
    }


@route("POST", r"/workspaces/(?P<ws>[^/]+)/collab/send", "EDITOR")
def collab_send(ctx, ws, body):
    mode = str((body or {}).get("mode") or "agdd")
    if mode not in ("agdd", "cli_chat"):
        raise HttpError(422, "mode must be agdd or cli_chat")
    msg = str((body or {}).get("message") or "").strip()
    attachments = [a for a in ((body or {}).get("attachments") or []) if isinstance(a, dict) and a.get("path")][:10]
    if not msg and not attachments:
        raise HttpError(422, "message or attachments required")
    settings = vm_service.load_settings(ws, (body or {}).get("agent"))
    repo_key = str((body or {}).get("repo_key") or settings.default_repo or "workspace").strip() or "workspace"
    att_note = ""
    if attachments:
        att_note = "\n\n[첨부된 워크스페이스 파일]\n" + "\n".join(f"- {a['path']}" for a in attachments)
    full_prompt = (msg + att_note).strip()

    with infra.pg() as c:
        user_msg = pgstore.add_collab_message(
            c, ws, "user", mode, msg or "(파일 첨부)",
            meta={"attachments": attachments, "repo_key": repo_key},
            user_id=ctx["user"],
        )
        c.commit()

    if mode == "agdd":
        if vm_service.enabled():
            try:
                vm_sync.sync_workspace_context(ws, repo_key)
            except Exception:
                pass
        started = _start_collab_agdd_run(ws, full_prompt, repo_key, agent_override=(body or {}).get("agent"), user_id=ctx["user"])
        return {"mode": "agdd", "user_message": user_msg, **started}

    # Direct tuned CLI chat turn inside the VM.
    _require_vm()
    from .cli_agents import VmAgent
    from .vm import RuntimeClient, VmShell

    sh = VmShell(RuntimeClient(), vm_service.canvas_for(ws), "agdd-collab")
    try:
        sync_info = vm_sync.sync_workspace_context(ws, repo_key, shell=sh)
        vm_sync.clear_pending_vm_ux_request(ws, shell=sh)
        with infra.pg(readonly=True) as c:
            prev = c.execute(
                "SELECT meta_json FROM collab_messages WHERE workspace_id=%s AND mode='cli_chat' "
                "AND role='assistant' ORDER BY msg_id DESC LIMIT 1",
                (ws,),
            ).fetchone()
        prev_sid = ((prev or {}).get("meta_json") or {}).get("cli_session")
        session = None if settings.session == "new" else (prev_sid if settings.session == "per-run" else settings.session)
        vm_service.repo_path(repo_key)
        repo_dir = f"{sh.home}/repos/{repo_key}"
        inst_part = f"\n튜닝 지침:\n{settings.instructions}\n" if settings.instructions else ""
        prompt = (
            f"당신은 워크스페이스 VM({repo_dir})의 CLI 협업 에이전트입니다.\n"
            f"- 노트북 셀 파일: `{sync_info.get('cells_dir')}`\n"
            f"- 명세 인메모리 DB: `{sync_info.get('db_path')}` (`agdd-spec status | scope | sql | search | ux | files`)\n"
            f"- 2D UX 배치가 필요하면 `agdd-spec request-ux --view <view> --prompt \"<질문>\"`를 실행하세요.\n"
            f"{inst_part}\n사용자 요청:\n{full_prompt}"
        )
        t0 = time.time()
        res = VmAgent(sh, settings, vm_service.budget_gate(ws)).agent(prompt, repo_dir, session, timeout=600)
        vm_service.record_usage(ws, None, "collab-cli", settings, res, int((time.time() - t0) * 1000))
        ux_req = vm_sync.read_pending_vm_ux_request(ws, shell=sh) or ux_workbench.parse_ux_request(res.text or "")
        status_out = sh.run(f"cd {repo_dir} && git status --porcelain 2>/dev/null || true", timeout=30).output
        changed = sorted({l[3:].strip() for l in status_out.splitlines() if l[3:].strip() and not l[3:].startswith(".agdd")})
    finally:
        sh.close()

    reply_text = (res.text or res.raw_tail or "(응답 없음)")[:12000]
    with infra.pg() as c:
        asst_msg = pgstore.add_collab_message(
            c, ws, "assistant", "cli_chat", reply_text,
            meta={
                "event": "UX_VISUAL_REQUEST" if ux_req else "CLI_REPLY",
                "ok": res.ok,
                "cli_session": res.session_id or session,
                "changed_files": changed,
                "ux_request": ux_req,
            },
        )
        c.commit()
    return {
        "mode": "cli_chat",
        "ok": res.ok,
        "reply": reply_text,
        "session_id": res.session_id or session,
        "changed_files": changed,
        "ux_request": ux_req,
        "user_message": user_msg,
        "assistant_message": asst_msg,
    }


@route("POST", r"/workspaces/(?P<ws>[^/]+)/collab/cli-launch", "EDITOR")
def collab_cli_launch(ctx, ws, body):
    _require_vm()
    from .cli_agents import PROFILES
    settings = vm_service.load_settings(ws)
    repo_key = str((body or {}).get("repo_key") or settings.default_repo or "workspace").strip() or "workspace"
    sync_info = vm_sync.sync_workspace_context(ws, repo_key)
    repo_dir = vm_service.repo_path(repo_key)
    prof = PROFILES[settings.cli]
    cmd = prof.interactive_cmd(repo_dir, settings.model, settings.effort, settings.session, settings.permission)
    return {
        "command": cmd,
        "cli": settings.cli,
        "repo_dir": repo_dir,
        "db_path": sync_info.get("db_path"),
        "cells_dir": sync_info.get("cells_dir"),
        "context_file": "~/.agdd/WORKSPACE_CONTEXT.md",
    }


@route("DELETE", r"/workspaces/(?P<ws>[^/]+)/collab/messages", "EDITOR")
def collab_clear(ctx, ws, body):
    with infra.pg() as c:
        pgstore.clear_collab_messages(c, ws)
        c.commit()
    return {"ok": True}


# ---------------- Multi-agent teams (canvas orchestration) ----------------
_UUID_RX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _team_id(v):
    if v in (None, ""):
        return None
    if not isinstance(v, str) or not _UUID_RX.fullmatch(v):
        raise HttpError(422, "invalid team_id")
    return v


@route("GET", r"/workspaces/(?P<ws>[^/]+)/teams", "VIEWER")
def teams_list(ctx, ws, body):
    from . import multi_agent
    return {"teams": multi_agent_service.list_teams(ws), "presets": {p: multi_agent.preset(p) for p in multi_agent.PRESETS},
            "preset_info": multi_agent.PRESET_INFO, "attachments": multi_agent_service.list_attachments(ws),
            "limits": {"chunk_bytes": multi_agent_service.CHUNK_BYTES,
                       "max_attachment_bytes": multi_agent_service.MAX_ATTACHMENT_BYTES},
            "vm_enabled": vm_service.enabled()}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/teams/validate", "VIEWER")
def teams_validate(ctx, ws, body):
    try:
        return {"ok": True, "graph": multi_agent_service.validate_team_graph((body or {}).get("graph") or {})}
    except multi_agent_service.TeamGraphError as e:
        return {"ok": False, "error": str(e)}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/teams", "EDITOR")
def teams_save(ctx, ws, body):
    try:
        return multi_agent_service.save_team(ws, _team_id((body or {}).get("team_id")), (body or {}).get("name"),
                                             (body or {}).get("graph") or {}, ctx["user"])
    except multi_agent_service.TeamGraphError as e:
        raise HttpError(422, str(e))
    except LookupError as e:
        raise HttpError(404, str(e))


@route("DELETE", r"/workspaces/(?P<ws>[^/]+)/teams/(?P<team>[0-9a-f-]{36})", "EDITOR")
def teams_delete(ctx, ws, body, team):
    if not multi_agent_service.delete_team(ws, _team_id(team)):
        raise HttpError(404, "team not found")
    return {"ok": True}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/team-runs", "EDITOR")
def team_run_start(ctx, ws, body):
    body = body or {}
    request = str(body.get("request") or "").strip()[:12000]
    try:
        return multi_agent_service.start_run(ws, _team_id(body.get("team_id")), body.get("graph"), request, ctx["user"],
                                             body.get("attachments") or [])
    except multi_agent_service.TeamGraphError as e:
        raise HttpError(422, str(e))
    except LookupError as e:
        raise HttpError(404, str(e))
    except ValueError as e:  # invalid repo_key
        raise HttpError(422, str(e))
    except RuntimeError as e:
        raise HttpError(409, str(e))


@route("GET", r"/workspaces/(?P<ws>[^/]+)/team-runs", "VIEWER")
def team_runs_list(ctx, ws, body):
    team = ((ctx.get("query") or {}).get("team_id") or [None])[0]
    return {"runs": multi_agent_service.list_runs(ws, _team_id(team))}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/team-runs/(?P<rid>[0-9a-f-]{36})", "VIEWER")
def team_run_get(ctx, ws, body, rid):
    after = ((ctx.get("query") or {}).get("after") or ["0"])[0]
    run = multi_agent_service.get_run(ws, _team_id(rid), int(after) if str(after).isdigit() else 0)
    if run is None:
        raise HttpError(404, "team run not found")
    return run


@route("POST", r"/workspaces/(?P<ws>[^/]+)/team-runs/(?P<rid>[0-9a-f-]{36})/cancel", "EDITOR")
def team_run_cancel(ctx, ws, body, rid):
    return {"ok": multi_agent_service.cancel_run(ws, _team_id(rid))}


# Multimodal attachments (images, video, audio, PDF, text …): chunked upload keeps every request under the
# gateway body limit; the raw endpoint serves previews with a sandboxing CSP.
_INLINE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf", "text/plain"}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/team-attachments", "VIEWER")
def team_attachments_list(ctx, ws, body):
    return {"attachments": multi_agent_service.list_attachments(ws)}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/team-attachments", "EDITOR")
def team_attachment_create(ctx, ws, body):
    body = body or {}
    try:
        return multi_agent_service.create_attachment(ws, body.get("name"), body.get("mime"), body.get("size"), ctx["user"])
    except ValueError as e:
        raise HttpError(422, str(e))


@route("POST", r"/workspaces/(?P<ws>[^/]+)/team-attachments/(?P<aid>[0-9a-f-]{36})/chunks/(?P<idx>[0-9]{1,5})", "EDITOR")
def team_attachment_chunk(ctx, ws, body, aid, idx):
    try:
        return multi_agent_service.put_chunk(ws, _team_id(aid), int(idx), (body or {}).get("data"))
    except LookupError as e:
        raise HttpError(404, str(e))
    except ValueError as e:
        raise HttpError(422, str(e))


@route("POST", r"/workspaces/(?P<ws>[^/]+)/team-attachments/(?P<aid>[0-9a-f-]{36})/complete", "EDITOR")
def team_attachment_complete(ctx, ws, body, aid):
    try:
        return multi_agent_service.complete_attachment(ws, _team_id(aid))
    except LookupError as e:
        raise HttpError(404, str(e))
    except ValueError as e:
        raise HttpError(422, str(e))


@route("GET", r"/workspaces/(?P<ws>[^/]+)/team-attachments/(?P<aid>[0-9a-f-]{36})/raw", "VIEWER")
def team_attachment_raw(ctx, ws, body, aid):
    meta = multi_agent_service.attachment_for_download(ws, _team_id(aid))
    if meta is None:
        raise HttpError(404, "attachment not found")
    inline = meta["mime"] in _INLINE_MIMES or meta["mime"].split("/")[0] in ("video", "audio")
    return RawResponse(meta["mime"] if inline else "application/octet-stream",
                       multi_agent_service.iter_chunks(meta["id"]), meta["size"], meta["name"], inline)


@route("DELETE", r"/workspaces/(?P<ws>[^/]+)/team-attachments/(?P<aid>[0-9a-f-]{36})", "EDITOR")
def team_attachment_delete(ctx, ws, body, aid):
    if not multi_agent_service.delete_attachment(ws, _team_id(aid)):
        raise HttpError(404, "attachment not found")
    return {"ok": True}


# ---------------- UX workbench ----------------
@route("GET", r"/workspaces/(?P<ws>[^/]+)/ux", "VIEWER")
def ux_views(ctx, ws, body):
    return {"views": pgstore.PgOverrideStore().list_views(ws)}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/ux/pending-request", "VIEWER")
def ux_pending_request(ctx, ws, body):
    return {"pending": _pending_ux_requests(ws, check_vm=True)}


@route("GET", r"/workspaces/(?P<ws>[^/]+)/ux/(?P<view>[^/]+)", "VIEWER")
def ux_get(ctx, ws, body, view):
    store = pgstore.PgOverrideStore()
    wb = Workbench(store)
    bindings = wb.export_bindings(ws, view)
    if (ctx.get("query") or {}).get("full") == ["1"]:
        items = store.list(ws, view)
        locked = [r["component_selector"] for r in items if r.get("locked_by_user")]
        return {
            "view_key": view,
            "bindings": bindings,
            "items": items,
            "summary": ux_workbench.summarize_2d_layout(bindings, locked),
        }
    return bindings


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/ux/(?P<view>[^/]+)", "EDITOR")
def ux_put(ctx, ws, body, view):
    wb = Workbench(pgstore.PgOverrideStore())
    try:
        for sel, item in body.items():
            is_meta = str(sel) == ux_workbench.CANVAS_META_SELECTOR
            wb.user_set(ws, view, sel, item["config"], ctx["user"], lock=False if is_meta else item.get("locked", True))
    except ValueError as e:
        raise HttpError(422, str(e))
    vm_sync.sync_workspace_context_async(ws)
    return wb.export_bindings(ws, view)


@route("DELETE", r"/workspaces/(?P<ws>[^/]+)/ux/(?P<view>[^/]+)/(?P<selector>.+)", "EDITOR")
def ux_delete(ctx, ws, body, view, selector):
    wb = Workbench(pgstore.PgOverrideStore())
    deleted = wb.user_delete(ws, view, urllib.parse.unquote(selector))
    if deleted:
        vm_sync.sync_workspace_context_async(ws)
    return {"deleted": deleted}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/ux/(?P<view>[^/]+)/unlock", "EDITOR")
def ux_unlock(ctx, ws, body, view):
    wb = Workbench(pgstore.PgOverrideStore())
    for sel in body.get("selectors", []):
        wb.unlock(ws, view, sel, ctx["user"])
    vm_sync.sync_workspace_context_async(ws)
    return {"ok": True}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/ux/(?P<view>[^/]+)/submit-to-agent", "EDITOR")
def ux_submit_to_agent(ctx, ws, body, view):
    body = body or {}
    store = pgstore.PgOverrideStore()
    wb = Workbench(store)
    elements = body.get("elements") or {}
    try:
        for sel in body.get("removed") or []:
            wb.user_delete(ws, view, str(sel))
        for sel, item in elements.items():
            is_meta = str(sel) == ux_workbench.CANVAS_META_SELECTOR
            wb.user_set(ws, view, str(sel), item["config"], ctx["user"], lock=False if is_meta else item.get("locked", True))
    except ValueError as e:
        raise HttpError(422, str(e))

    bindings = wb.export_bindings(ws, view)
    locked = [r["component_selector"] for r in store.list(ws, view) if r.get("locked_by_user")]
    summary = ux_workbench.summarize_2d_layout(bindings, locked, body.get("viewport"))
    settings = vm_service.load_settings(ws)
    repo_key = str(body.get("repo_key") or settings.default_repo or "workspace").strip() or "workspace"
    if vm_service.enabled():
        try:
            vm_sync.sync_workspace_context(ws, repo_key)
            vm_sync.write_vm_ux_response(ws, view, summary, bindings, repo_key=repo_key)
        except Exception:
            pass

    note = str(body.get("note") or "").strip()[:1500]
    ux_response = {"view_key": view, "note": note, "summary": summary, "bindings": bindings}
    resumed_run_id = None
    decision_id = body.get("decision_id")

    with infra.pg() as c:
        if decision_id is None:
            hit = c.execute(
                "SELECT h.decision_id FROM hitl_decisions h JOIN workflow_runs r USING (run_id) JOIN graph_definitions g "
                "ON g.graph_id=r.graph_id AND g.version=r.graph_version "
                "WHERE g.workspace_id=%s AND h.status='PENDING' AND h.reason_code='UX_VISUAL_REQUEST' "
                "ORDER BY h.decision_id DESC LIMIT 1",
                (ws,),
            ).fetchone()
            if hit:
                decision_id = hit["decision_id"]

        if decision_id is not None:
            dec = c.execute(
                "UPDATE hitl_decisions SET status='APPROVED', decided_by=%s, decided_at=now() "
                "WHERE decision_id=%s AND status='PENDING' RETURNING run_id",
                (ctx["user"], int(decision_id)),
            ).fetchone()
            if dec:
                run = c.execute("SELECT run_id, current_node, status, handoff_json FROM workflow_runs WHERE run_id=%s FOR UPDATE",
                                (dec["run_id"],)).fetchone()
                if run and run["status"] == "WAITING_HITL":
                    next_handoff = dict(run["handoff_json"] or {})
                    next_handoff["ux_layout_response"] = ux_response
                    next_handoff["ux_request"] = None
                    c.execute(
                        "UPDATE workflow_runs SET status='RUNNING', handoff_json=%s, version=version+1, updated_at=now() WHERE run_id=%s",
                        (Jsonb(next_handoff), run["run_id"]),
                    )
                    c.execute(orchestrator.ENQUEUE_SQL, {"run_id": run["run_id"], "node_name": run["current_node"], "priority": 1})
                    resumed_run_id = run["run_id"]
                    pgstore.add_collab_message(
                        c, ws, "user", "ux_canvas",
                        f"🎨 2D UX 구성({view})을 에이전트에 전달하고 실행을 재개했습니다." + (f" ({note})" if note else ""),
                        meta={"event": "UX_RESPONSE_SENT", "view_key": view, "element_count": len(summary["elements"])},
                        run_id=run["run_id"],
                        user_id=ctx["user"],
                    )
        if resumed_run_id is None and not body.get("launch_run"):
            pgstore.add_collab_message(
                c, ws, "system", "ux_canvas",
                f"🎨 2D UX 구성({view})이 저장·동기화되었습니다 ({len(summary['elements'])}개 요소).",
                meta={"event": "UX_SAVED", "view_key": view, "element_count": len(summary["elements"])},
                user_id=ctx["user"],
            )
        c.commit()

    launched = None
    if resumed_run_id is None and body.get("launch_run"):
        req_text = note or f"`{view}` 뷰의 2D UX 캔버스 배치를 반영해 UI를 구현하세요."
        launched = _start_collab_agdd_run(ws, req_text, repo_key, extra_handoff={"ux_layout_response": ux_response}, user_id=ctx["user"])
        resumed_run_id = (launched or {}).get("run_id")

    return {"ok": True, "view_key": view, "bindings": bindings, "summary": summary,
            "resumed_run_id": resumed_run_id, "launched": launched}


# ---------------- research ----------------
@route("GET", r"/workspaces/(?P<ws>[^/]+)/research/history", "VIEWER")
def research_history(ctx, ws, body):
    with infra.pg(readonly=True) as c:
        rows = c.execute(
            "SELECT fact_key, fact_value FROM resolved_facts WHERE workspace_id=%s AND repo_key='_research' "
            "ORDER BY fact_id DESC LIMIT 20",
            (ws,),
        ).fetchall()
    items = []
    for row in rows:
        raw = row.get("fact_value") or ""
        parsed = None
        if isinstance(raw, str) and raw.lstrip().startswith("{"):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
        if isinstance(parsed, dict):
            items.append(parsed)
        else:
            items.append({
                "research_id": row.get("fact_key"),
                "problem": "",
                "query": row.get("fact_key"),
                "insight_brief_md": raw,
                "reused": False,
                "errors": [],
            })
    return {"items": items}


@route("POST", r"/workspaces/(?P<ws>[^/]+)/research", "EDITOR")
def research(ctx, ws, body):
    problem = str((body or {}).get("problem") or "").strip()
    if not problem:
        raise HttpError(422, "problem required")
    req = urllib.request.Request(os.environ.get("RESEARCH_URL", "http://research:8092") + "/research",
                                 data=json.dumps({"problem": problem, "trigger_source": "HUMAN",
                                                  "run_id": body.get("run_id"), "workspace_id": ws}).encode(),
                                 headers={"content-type": "application/json",
                                          "authorization": f"Bearer {os.environ.get('INTERNAL_TOKEN', '')}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HttpError(502 if e.code >= 500 else e.code, f"research service error ({e.code})")
    except urllib.error.URLError as e:
        raise HttpError(502, f"research service unavailable: {e.reason}")
    try:
        rid = out.get("research_id") or int(time.time() * 1000)
        summary_entry = json.dumps({
            "research_id": rid,
            "problem": problem[:500],
            "query": out.get("query") or "",
            "reused": bool(out.get("reused")),
            "insight_brief_md": str(out.get("insight_brief_md") or "")[:4000],
            "errors": out.get("errors") or [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False)
        with infra.pg() as c:
            c.execute(
                "INSERT INTO resolved_facts (workspace_id, repo_key, fact_key, fact_value) "
                "VALUES (%s,'_research',%s,%s) ON CONFLICT (workspace_id, repo_key, fact_key) "
                "DO UPDATE SET fact_value=EXCLUDED.fact_value",
                (ws, f"research:{rid}", summary_entry),
            )
            c.commit()
    except Exception:
        pass
    return out


# ---------------- dashboard ----------------
@route("GET", r"/workspaces/(?P<ws>[^/]+)/dashboard", "VIEWER")
def dashboard(ctx, ws, body):
    def load():
        with infra.pg(readonly=True) as c:
            return {
                "runs": c.execute(
                    "SELECT r.status, count(*) AS n FROM workflow_runs r JOIN graph_definitions g ON g.graph_id=r.graph_id "
                    "AND g.version=r.graph_version WHERE g.workspace_id=%s GROUP BY r.status", (ws,)).fetchall(),
                "budget": c.execute("SELECT * FROM workspace_budgets WHERE workspace_id=%s ORDER BY period_ym DESC LIMIT 1",
                                    (ws,)).fetchone(),
                "tokens": c.execute(
                    "SELECT e.model_name, sum(prompt_tokens) p, sum(cached_tokens) cached, sum(thinking_tokens) thinking, "
                    "sum(cost_usd) cost FROM node_executions e JOIN workflow_runs r USING (run_id) JOIN graph_definitions g "
                    "ON g.graph_id=r.graph_id AND g.version=r.graph_version WHERE g.workspace_id=%s "
                    "AND e.created_at > now() - interval '7 days' GROUP BY 1", (ws,)).fetchall(),
            }
    return infra_safe(infra.cache_get_or_set, f"agdd:dash:{ws}", 15, load, fallback=load)


@route("PUT", r"/workspaces/(?P<ws>[^/]+)/budget", "OWNER")
def set_budget(ctx, ws, body):
    body = body or {}
    raw_limit = body.get("limit_usd")
    if raw_limit is None or isinstance(raw_limit, bool) or (isinstance(raw_limit, str) and not raw_limit.strip()):
        raise HttpError(422, "limit_usd must be a non-negative number")
    try:
        limit = float(raw_limit)
    except (TypeError, ValueError):
        raise HttpError(422, "limit_usd must be a non-negative number")
    if not math.isfinite(limit) or limit < 0:
        raise HttpError(422, "limit_usd must be a non-negative number")
    period = body.get("period_ym") or datetime.now(timezone.utc).strftime("%Y-%m")
    with infra.pg() as c:
        c.execute("INSERT INTO workspace_budgets (workspace_id, period_ym, limit_usd) VALUES (%s,%s,%s) "
                  "ON CONFLICT (workspace_id, period_ym) DO UPDATE SET limit_usd=EXCLUDED.limit_usd",
                  (ws, period, limit))
        c.commit()
    infra_safe(infra.cache_invalidate, f"agdd:dash:{ws}")
    return {"ok": True}


def infra_safe(fn, *args, fallback=None):
    try:
        return fn(*args)
    except Exception:
        return fallback() if callable(fallback) else fallback


# ---------------- HTTP plumbing ----------------
class RawResponse:
    """A binary route result (attachment download) streamed chunk by chunk instead of JSON."""

    def __init__(self, content_type: str, chunks, length: int, filename: str = "", inline: bool = True):
        self.content_type, self.chunks, self.length = content_type, chunks, int(length)
        self.filename, self.inline = filename, inline


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send_raw(self, res: RawResponse):
        self.send_response(200)
        self.send_header("content-type", res.content_type)
        self.send_header("content-length", str(res.length))
        disp = "inline" if res.inline else "attachment"
        self.send_header("content-disposition", f"{disp}; filename*=UTF-8''{urllib.parse.quote(res.filename or 'file')}")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-security-policy", "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'")
        self.send_header("cache-control", "private, max-age=3600")
        self.end_headers()
        for chunk in res.chunks:
            self.wfile.write(chunk)

    def _send(self, code, obj):
        data = pgstore.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth(self):
        # The upstream gateway (Spring/nginx) authenticates the user and forwards X-User-Id with a shared token.
        if self.headers.get("authorization", "") != f"Bearer {os.environ.get('API_TOKEN', '')}" or \
                not os.environ.get("API_TOKEN"):
            raise HttpError(401, "unauthorized")
        user = self.headers.get("x-user-id", "")
        if not re.fullmatch(r"[0-9a-f-]{36}", user):
            raise HttpError(401, "x-user-id required")
        return {"user": user}

    def _link_irade(self, ws: str, ctx) -> str:
        """Resolve "irade-<id>" to the linked AGDD workspace. The gateway (nginx + Spring) has already verified
        Studio access and forwards the verified id and role; anything else is rejected."""
        m = re.fullmatch(r"irade-([1-9][0-9]{0,9})", ws)
        role = self.headers.get("x-agdd-role", "")
        if not m or self.headers.get("x-agdd-irade-ws", "") != m.group(1) or role not in pgstore.ROLE_RANK:
            raise HttpError(403, "IRADE workspace access not verified")
        n, key = int(m.group(1)), (ws, ctx["user"], role)
        hit = _LINKS.get(key)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        with infra.pg() as c:
            _ensure_user(c, ctx["user"])
            w = c.execute("INSERT INTO workspaces (name, irade_workspace_id) VALUES (%s, %s) "
                          "ON CONFLICT (irade_workspace_id) DO UPDATE SET irade_workspace_id = EXCLUDED.irade_workspace_id "
                          "RETURNING workspace_id", (f"IRADE #{n}", n)).fetchone()["workspace_id"]
            c.execute("INSERT INTO workspace_members VALUES (%s,%s,%s) ON CONFLICT (workspace_id, user_id) "
                      "DO UPDATE SET role = EXCLUDED.role", (w, ctx["user"], role))
            # Same runtime VM as the Studio workspace (Agora canvas id == Studio workspace id).
            c.execute("INSERT INTO workspace_vms (workspace_id, canvas_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (w, n))
            c.commit()
        _LINKS[key] = (time.monotonic(), str(w))
        return str(w)

    def _dispatch(self, method):
        path, _, query_str = self.path.partition("?")
        if path == "/healthz":
            return self._send(200, {"ok": True})
        if method == "GET" and path in ("/", "/console"):
            data = localize_vendor_urls((Path(__file__).parent / "static" / "console.html").read_bytes())
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.send_header("content-security-policy", "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                             "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; font-src 'self' data: https://cdn.jsdelivr.net; "
                             "worker-src 'self' blob:; frame-src 'self' blob: data:; connect-src 'self' ws: wss: https://cdn.jsdelivr.net")
            self.end_headers()
            self.wfile.write(data)
            return
        if method == "GET" and (mt_v := _VENDOR_RX.match(path)):
            fpath = _vendor_file(mt_v.group("rel"))
            if not fpath:
                return self._send(404, {"error": "not found"})
            data = fpath.read_bytes()
            self.send_response(200)
            self.send_header("content-type", _VENDOR_TYPES[fpath.suffix])
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "public, max-age=31536000, immutable")  # paths are version-pinned
            self.send_header("x-content-type-options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if method == "GET":
            mt_st = _STATIC_RX.match(path)
            if mt_st:
                fpath = Path(__file__).parent / "static" / mt_st.group("fname")
                if not fpath.is_file():
                    return self._send(404, {"error": "not found"})
                data = fpath.read_bytes()
                ctype = "application/javascript; charset=utf-8" if fpath.suffix == ".js" else "text/css; charset=utf-8"
                self.send_response(200)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.send_header("cache-control", "no-cache")
                self.send_header("x-content-type-options", "nosniff")
                self.end_headers()
                self.wfile.write(data)
                return
        try:
            if method == "GET" and path.startswith("/runs/") and path.endswith("/logs"):
                return self._sse(path.split("/")[2], self._auth())
            if method == "GET":
                mt_ws = _TERM_WS_RX.match(path)
                if mt_ws:
                    from .vm import RuntimeClient

                    def _check_access(ticket):
                        with infra.pg(readonly=True) as c:
                            pgstore.require_role(c, ticket.ws, ticket.user, "EDITOR")

                    return ws_terminal.handle_websocket(
                        self,
                        mt_ws.group("ws"),
                        mt_ws.group("name"),
                        _TERM_TICKETS,
                        RuntimeClient,
                        _TERM_WS_SLOTS,
                        check_access=_check_access,
                    )
            for m, rx, min_role, fn in ROUTES:
                mt = rx.match(path)
                if m == method and mt:
                    ctx = self._auth()
                    ctx["query"] = urllib.parse.parse_qs(query_str) if query_str else {}
                    n = int(self.headers.get("content-length", 0) or 0)
                    body = json.loads(self.rfile.read(n)) if n else {}
                    kw = mt.groupdict()
                    ctx["raw_ws"] = kw.get("ws")
                    if str(kw.get("ws") or "").startswith("irade-"):
                        kw["ws"] = self._link_irade(kw["ws"], ctx)
                    if min_role:
                        with infra.pg(readonly=True) as c:
                            try:
                                pgstore.require_role(c, kw["ws"], ctx["user"], min_role)
                            except PermissionError as e:
                                raise HttpError(403, str(e))
                    ws_or_first = kw.pop("ws", None) or (kw.pop(next(iter(kw))) if kw else None)
                    out = fn(ctx, ws_or_first, body, **kw)
                    return self._send_raw(out) if isinstance(out, RawResponse) else self._send(200, out)
            raise HttpError(404, "not found")
        except HttpError as e:
            self._send(e.code, {"error": str(e)})
        except LockedOverrideError as e:
            self._send(423, {"error": str(e)})
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": repr(e)[:300]})
        except Exception as e:  # CLI / VM failures must surface, never pass silently
            from .cli_agents import CliError
            from .vm import RuntimeError_
            if isinstance(e, (CliError, RuntimeError_, TimeoutError)):
                self._send(502, {"error": str(e)[:600]})
            else:
                raise

    def _sse(self, run, ctx):
        _check_run_role(ctx, run, "VIEWER")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        last = "0-0"
        try:
            while True:
                res = infra.valkey().xread({f"agdd:log:{run}": last}, block=15000, count=100) or []
                for _, entries in res:
                    for eid, fields in entries:
                        last = eid
                        self.wfile.write(f"id: {eid}\ndata: {json.dumps(fields, ensure_ascii=False)}\n\n".encode())
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


def main():
    infra.wait_for_pg()
    try:
        infra.ensure_bucket()
    except Exception as e:
        print(f"[agdd] object storage not ready: {e!r}", flush=True)
    if os.environ.get("AGDD_MIGRATE", "1") == "1":
        infra.migrate()
    port = int(os.environ.get("PORT", "8091"))
    print(f"[agdd-api] listening :{port}", flush=True)

    class _HTTPServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    _HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
