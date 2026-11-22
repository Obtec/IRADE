"""Multi-agent teams: persistence (agent_teams / agent_team_runs / agent_team_events / attachments) and execution.

A team run executes in a background thread of agdd-api. Each agent node gets its own VM PTY
(``agdd-ma-<node>``) so independent agents really run in parallel inside the workspace VM; code-mode agents
share one repo working tree and are therefore serialized per workspace. Without a VM, think-mode agents
fall back to the LLM gateway and code-mode agents fail with a clear error.

Multimodal inputs: files are uploaded in chunks (``agent_team_attachments`` + ``_chunks``). At run start
:class:`VmMedia` streams every attachment that reaches an agent into ``~/.agdd/attachments/<sha256>/`` (cached
across runs) and runs ``agdd_media.py`` in the VM: PDF -> text + page images, video -> key frames + audio
transcript, audio -> transcript (whisper, when installed). Each CLI then gets the files it can consume natively:
Claude Code reads images/PDFs with its Read tool, Codex receives images with ``--image``, Gemini-family CLIs get
``@path`` references (video/audio included). Without a VM, images go to the gateway as image parts and text
files as inline excerpts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import threading
import time

from psycopg.types.json import Jsonb

from . import infra, multi_agent, pgstore, ux_workbench, vm_service
from .multi_agent import TeamGraphError, TeamRun, validate_team_graph

MAX_ACTIVE_PER_WORKSPACE = 2
CHUNK_BYTES = 4 * 1024 * 1024               # raw bytes per upload request (base64 JSON stays < nginx 8 MiB)
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024     # per file: the VM copy streams through the terminal bridge
MAX_WORKSPACE_ATTACHMENT_BYTES = 2 * 1024 ** 3
MAX_EXCERPT_CHARS = 200_000
GATEWAY_IMAGE_BYTES = 5 * 1024 * 1024
_MIME_RX = re.compile(r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,99}")
_ACTIVE: dict[str, threading.Event] = {}          # team_run_id -> cancel flag
_ACTIVE_WS: dict[str, set[str]] = {}
_CODE_LOCKS: dict[str, threading.Lock] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- teams CRUD
def list_teams(ws) -> list[dict]:
    with infra.pg(readonly=True) as c:
        return c.execute("SELECT team_id, name, graph_json, updated_at FROM agent_teams WHERE workspace_id=%s "
                         "ORDER BY updated_at DESC", (ws,)).fetchall()


def save_team(ws, team_id, name: str, graph: dict, user) -> dict:
    name = str(name or "").strip()[:80] or "새 팀"
    clean = validate_team_graph(graph)
    uid = user if user and len(str(user)) == 36 else None
    with infra.pg() as c:
        if team_id:
            row = c.execute("UPDATE agent_teams SET name=%s, graph_json=%s, updated_by=%s, updated_at=now() "
                            "WHERE team_id=%s AND workspace_id=%s RETURNING team_id, name, graph_json, updated_at",
                            (name, Jsonb(clean), uid, team_id, ws)).fetchone()
            if row is None:
                raise LookupError("team not found")
        else:
            row = c.execute("INSERT INTO agent_teams (workspace_id, name, graph_json, updated_by) VALUES (%s,%s,%s,%s) "
                            "RETURNING team_id, name, graph_json, updated_at", (ws, name, Jsonb(clean), uid)).fetchone()
        c.commit()
    return row


def delete_team(ws, team_id) -> bool:
    with infra.pg() as c:
        n = c.execute("DELETE FROM agent_teams WHERE team_id=%s AND workspace_id=%s", (team_id, ws)).rowcount
        c.commit()
    return bool(n)


# ---------------------------------------------------------------- attachments
def _safe_name(name: str) -> str:
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[^\w.\- ()\[\]+]", "_", name, flags=re.UNICODE).strip(" .")[:120]
    return name or "file"


def _uid(user):
    return user if user and len(str(user)) == 36 else None


_META_COLS = "attachment_id, name, mime, modality, size_bytes, sha256, status, created_at"


def _meta(row: dict, excerpt: bool = False) -> dict:
    m = {"id": str(row["attachment_id"]), "name": row["name"], "mime": row["mime"], "modality": row["modality"],
         "size": int(row["size_bytes"]), "sha256": row.get("sha256"), "status": row.get("status", "READY"),
         "created_at": row.get("created_at")}
    if excerpt and row.get("excerpt"):
        m["excerpt"] = row["excerpt"][:20000]
    return m


def create_attachment(ws, name: str, mime: str, size, user) -> dict:
    """Start a chunked upload; returns the attachment id, chunk size and number of chunks to send."""
    name = _safe_name(name)
    mime = str(mime or "").lower().split(";")[0].strip() or "application/octet-stream"
    if not _MIME_RX.fullmatch(mime):
        mime = "application/octet-stream"
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise ValueError("size must be an integer")
    if size < 0 or size > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"파일은 최대 {MAX_ATTACHMENT_BYTES // 1024 // 1024}MB까지 올릴 수 있습니다")
    modality = multi_agent.modality_of(mime, name)
    with infra.pg() as c:
        used = c.execute("SELECT COALESCE(sum(size_bytes), 0) AS n FROM agent_team_attachments WHERE workspace_id=%s",
                         (ws,)).fetchone()
        if int((used or {}).get("n") or 0) + size > MAX_WORKSPACE_ATTACHMENT_BYTES:
            raise ValueError("워크스페이스 첨부 파일 용량(2GB)을 초과합니다. 사용하지 않는 첨부를 삭제하세요")
        row = c.execute("INSERT INTO agent_team_attachments (workspace_id, name, mime, modality, size_bytes, chunk_size, "
                        "created_by) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING " + _META_COLS,
                        (ws, name, mime, modality, size, CHUNK_BYTES, _uid(user))).fetchone()
        c.commit()
    return {**_meta(row), "chunk_size": CHUNK_BYTES, "chunks": max(1, math.ceil(size / CHUNK_BYTES))}


def _upload_row(c, ws, aid):
    row = c.execute("SELECT attachment_id, size_bytes, chunk_size, status FROM agent_team_attachments "
                    "WHERE attachment_id=%s AND workspace_id=%s", (aid, ws)).fetchone()
    if row is None:
        raise LookupError("attachment not found")
    return row


def put_chunk(ws, aid, idx, data_b64: str) -> dict:
    try:
        raw = base64.b64decode(str(data_b64 or ""), validate=True)
    except ValueError:
        raise ValueError("data must be base64")
    with infra.pg() as c:
        row = _upload_row(c, ws, aid)
        if row["status"] != "UPLOADING":
            raise ValueError("upload already completed")
        n = max(1, math.ceil(int(row["size_bytes"]) / int(row["chunk_size"])))
        idx = int(idx)
        expect = min(int(row["chunk_size"]), int(row["size_bytes"]) - idx * int(row["chunk_size"]))
        if not 0 <= idx < n or len(raw) != max(0, expect):
            raise ValueError(f"chunk {idx}: expected {max(0, expect)} bytes, got {len(raw)}")
        c.execute("INSERT INTO agent_team_attachment_chunks (attachment_id, idx, data) VALUES (%s,%s,%s) "
                  "ON CONFLICT (attachment_id, idx) DO UPDATE SET data=EXCLUDED.data", (aid, idx, raw))
        c.commit()
    return {"ok": True, "idx": idx, "chunks": n}


def iter_chunks(aid):
    """Attachment bytes chunk by chunk (one row in memory at a time)."""
    with infra.pg(readonly=True) as c:
        idxs = [r["idx"] for r in c.execute("SELECT idx FROM agent_team_attachment_chunks WHERE attachment_id=%s "
                                            "ORDER BY idx", (aid,)).fetchall()]
    for i in idxs:
        with infra.pg(readonly=True) as c:
            row = c.execute("SELECT data FROM agent_team_attachment_chunks WHERE attachment_id=%s AND idx=%s",
                            (aid, i)).fetchone()
        yield bytes(row["data"]) if row else b""


def complete_attachment(ws, aid) -> dict:
    with infra.pg() as c:
        row = _upload_row(c, ws, aid)
        n = max(1, math.ceil(int(row["size_bytes"]) / int(row["chunk_size"])))
        have = c.execute("SELECT count(*) AS n, COALESCE(sum(length(data)), 0) AS bytes "
                         "FROM agent_team_attachment_chunks WHERE attachment_id=%s", (aid,)).fetchone()
    if int(row["size_bytes"]) == 0:
        n = int(have["n"])
    if int(have["n"]) != n or int(have["bytes"]) != int(row["size_bytes"]):
        raise ValueError(f"upload incomplete: {have['n']}/{n} chunks, {have['bytes']}/{row['size_bytes']} bytes")
    digest, text = hashlib.sha256(), bytearray()
    full = None
    with infra.pg(readonly=True) as c:
        kind = c.execute("SELECT modality FROM agent_team_attachments WHERE attachment_id=%s", (aid,)).fetchone()
    keep_text = (kind or {}).get("modality") == "text" and int(row["size_bytes"]) <= 2 * 1024 * 1024
    for chunk in iter_chunks(aid):
        digest.update(chunk)
        if keep_text:
            text += chunk
    if keep_text:
        full = bytes(text).decode("utf-8", errors="replace")[:MAX_EXCERPT_CHARS]
    with infra.pg() as c:
        out = c.execute("UPDATE agent_team_attachments SET status='READY', sha256=%s, excerpt=%s "
                        "WHERE attachment_id=%s AND workspace_id=%s RETURNING " + _META_COLS,
                        (digest.hexdigest(), full, aid, ws)).fetchone()
        c.commit()
    return _meta(out)


def get_attachments(ws, ids, excerpt: bool = True) -> list[dict]:
    """READY attachments in the order of ``ids``; unknown ids raise LookupError (never run with missing media)."""
    ids = [str(i) for i in ids or []]
    if not ids:
        return []
    with infra.pg(readonly=True) as c:
        rows = c.execute("SELECT " + _META_COLS + ", excerpt FROM agent_team_attachments WHERE workspace_id=%s "
                         "AND status='READY' AND attachment_id = ANY(%s::uuid[])", (ws, ids)).fetchall()
    by_id = {str(r["attachment_id"]): _meta(r, excerpt) for r in rows}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise LookupError(f"attachment not found or not uploaded: {', '.join(missing)}")
    return [by_id[i] for i in ids]


def list_attachments(ws, limit: int = 200) -> list[dict]:
    with infra.pg(readonly=True) as c:
        rows = c.execute("SELECT " + _META_COLS + " FROM agent_team_attachments WHERE workspace_id=%s AND status='READY' "
                         "ORDER BY created_at DESC LIMIT %s", (ws, int(limit))).fetchall()
    return [_meta(r) for r in rows]


def attachment_for_download(ws, aid) -> dict | None:
    with infra.pg(readonly=True) as c:
        row = c.execute("SELECT " + _META_COLS + " FROM agent_team_attachments WHERE attachment_id=%s AND workspace_id=%s "
                        "AND status='READY'", (aid, ws)).fetchone()
    return _meta(row) if row else None


def delete_attachment(ws, aid) -> bool:
    with infra.pg() as c:
        n = c.execute("DELETE FROM agent_team_attachments WHERE attachment_id=%s AND workspace_id=%s", (aid, ws)).rowcount
        c.commit()
    return bool(n)


# ---------------------------------------------------------------- media in the VM
MEDIA_SCRIPT = r'''#!/usr/bin/env python3
"""agdd_media.py <file> <modality>: best-effort derivatives for CLI agents; prints a JSON manifest (cached)."""
import json, os, shutil, subprocess, sys

path, modality = sys.argv[1], sys.argv[2]
base = path + ".agdd"
manifest_path = os.path.join(base, "manifest.json")
if os.path.exists(manifest_path):
    print(open(manifest_path).read().strip()); sys.exit(0)
os.makedirs(base, exist_ok=True)
out = {"derived": [], "notes": [], "meta": "", "preview": ""}


def run(cmd, timeout=900):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def ensure(binary, package):
    if shutil.which(binary):
        return True
    if os.environ.get("AGDD_MEDIA_INSTALL", "1") == "0":
        out["notes"].append(f"{binary} 없음 — 원본 파일만 제공됩니다")
        return False
    run(["bash", "-lc", f"apt install -y -q {package} >/dev/null 2>&1 || (apt update -q >/dev/null 2>&1 && "
                        f"apt install -y -q {package} >/dev/null 2>&1)"], timeout=1200)
    if not shutil.which(binary):
        out["notes"].append(f"{binary} 설치 실패 — 원본 파일만 제공됩니다")
    return bool(shutil.which(binary))


def text_file(p, label):
    if os.path.exists(p) and os.path.getsize(p) > 0:
        out["derived"].append({"kind": "text", "label": label, "path": p})
        if not out["preview"]:
            out["preview"] = open(p, errors="replace").read(4000)


def probe(p):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
             "-of", "json", p], timeout=120)
    try:
        info = json.loads(r.stdout or "{}")
    except ValueError:
        return {}, False
    streams = info.get("streams") or []
    dur = float((info.get("format") or {}).get("duration") or 0)
    parts = [f"길이 {dur:.1f}초"] if dur else []
    for st in streams:
        if st.get("codec_type") == "video" and st.get("width"):
            parts.append(f"영상 {st.get('codec_name')} {st['width']}x{st.get('height')}")
        elif st.get("codec_type") == "audio":
            parts.append(f"오디오 {st.get('codec_name')}")
    out["meta"] = ", ".join(parts)
    return {"duration": dur}, any(st.get("codec_type") == "audio" for st in streams)


def transcribe(audio):
    if shutil.which("whisper"):
        r = run(["whisper", audio, "--model", os.environ.get("AGDD_WHISPER_MODEL", "base"), "--output_format", "txt",
                 "--output_dir", base], timeout=3600)
        txt = os.path.join(base, os.path.splitext(os.path.basename(audio))[0] + ".txt")
        if r.returncode == 0 and os.path.exists(txt):
            text_file(txt, "음성 전사")
            return
    out["notes"].append("음성 전사 도구(whisper)가 VM에 없어 전사본이 없습니다 — Gemini 계열(agy) 에이전트는 원본 오디오를 "
                        "직접 들을 수 있고, 다른 에이전트는 메타데이터만 참고합니다")


if modality == "pdf":
    if ensure("pdftotext", "poppler-utils"):
        txt = os.path.join(base, "text.txt")
        run(["pdftotext", "-layout", path, txt], timeout=600)
        text_file(txt, "추출 텍스트")
        run(["pdftoppm", "-r", "80", "-png", "-l", "6", path, os.path.join(base, "page")], timeout=600)
        for i, f in enumerate(sorted(x for x in os.listdir(base) if x.startswith("page") and x.endswith(".png"))):
            out["derived"].append({"kind": "image", "label": f"페이지 {i + 1} 이미지", "path": os.path.join(base, f)})
elif modality in ("video", "audio"):
    if ensure("ffmpeg", "ffmpeg"):
        info, has_audio = probe(path)
        if modality == "video":
            dur = info.get("duration") or 0
            n = 8 if dur >= 8 else max(1, int(dur) or 1)
            fps = f"fps={n}/{max(dur, 1):.3f}" if dur else "fps=1"
            run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", f"{fps},scale='min(768,iw)':-2", "-frames:v",
                 str(n), os.path.join(base, "frame_%02d.jpg")], timeout=900)
            for f in sorted(x for x in os.listdir(base) if x.startswith("frame_")):
                out["derived"].append({"kind": "image", "label": f"키프레임 {f[6:8]}", "path": os.path.join(base, f)})
        if modality == "audio" or has_audio:
            wav = os.path.join(base, "audio.wav")
            run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", wav], timeout=900)
            if os.path.exists(wav):
                transcribe(wav)
elif modality == "text":
    text_file(path, "텍스트")
    out["derived"] = []
json.dump(out, open(manifest_path, "w"), ensure_ascii=False)
print(json.dumps(out, ensure_ascii=False))
'''

_MODAL_KO = {"text": "텍스트", "image": "이미지", "video": "동영상", "audio": "오디오", "pdf": "PDF", "file": "파일"}


class VmMedia(multi_agent.MediaHooks):
    """Copies attachments into the workspace VM once per run and tells each CLI how to read them."""

    def __init__(self, ws, use_vm: bool | None = None):
        self.ws = ws
        self.use_vm = vm_service.enabled() if use_vm is None else use_vm
        self.info: dict[str, dict] = {}
        self.errors: dict[str, str] = {}

    def lookup(self, ids):
        return get_attachments(self.ws, ids)

    def prepare(self, metas):
        if not self.use_vm:
            return
        from .vm import RuntimeClient, VmShell, sh_quote
        sh = VmShell(RuntimeClient(), vm_service.canvas_for(self.ws), "agdd-ma-media")
        try:
            script = f"{sh.home}/.agdd/bin/agdd_media.py"
            sh.put_file(script, MEDIA_SCRIPT)
            for m in metas:
                path = f"{sh.home}/.agdd/attachments/{m['sha256']}/{_safe_name(m['name'])}"
                try:
                    r = sh.run(f"stat -c %s {sh_quote(path)} 2>/dev/null || echo -1", timeout=30)
                    if (r.output.strip().splitlines() or ["-1"])[-1] != str(m["size"]):
                        sh.put_stream(path, iter_chunks(m["id"]), m["size"], m["sha256"])
                    r = sh.run(f"python3 {sh_quote(script)} {sh_quote(path)} {m['modality']}", timeout=3600)
                    line = next((ln for ln in reversed(r.output.splitlines()) if ln.startswith("{")), "{}")
                    self.info[m["id"]] = {"path": path, **json.loads(line)}
                except Exception as exc:  # one broken file must not hide the others
                    self.errors[m["id"]] = str(exc)[:500]
        finally:
            sh.close()
        if self.errors and not self.info:
            raise RuntimeError("; ".join(self.errors.values())[:2000])

    def describe(self, metas):
        rows = []
        for m in metas:
            rows.append(f"- **{m['name']}** ({_MODAL_KO.get(m['modality'], m['modality'])}, {m['mime']}, "
                        f"{multi_agent._size(m['size'])})")
            inf = self.info.get(m["id"])
            if inf:
                rows.append(f"  - 원본: `{inf['path']}`")
                for d in inf.get("derived") or []:
                    rows.append(f"  - {d['label']}: `{d['path']}`")
                if inf.get("meta"):
                    rows.append(f"  - 정보: {inf['meta']}")
                for note in inf.get("notes") or []:
                    rows.append(f"  - 참고: {note}")
            elif m["id"] in self.errors:
                rows.append(f"  - VM 복사 실패: {self.errors[m['id']]}")
            elif not self.use_vm:
                rows.append("  - (VM 없음) " + ("이미지는 모델 입력에 직접 첨부됩니다" if m["modality"] == "image"
                                              else "텍스트 파일은 아래 내용이 전달됩니다" if m.get("excerpt")
                                              else "이 형식은 VM 없이 내용을 전달할 수 없어 이름·형식만 제공됩니다"))
            preview = (inf or {}).get("preview") or (m.get("excerpt") if m["modality"] == "text" else "")
            if preview:
                rows.append(f"  - 내용 미리보기:\n```\n{multi_agent._trim(preview, 6000)}\n```")
        return "\n".join(rows)

    def files_for(self, metas) -> list:
        from .cli_agents import MediaFile
        files = []
        for m in metas:
            inf = self.info.get(m["id"])
            if not inf:
                continue
            kind = m["modality"] if m["modality"] in ("image", "pdf", "video", "audio", "text") else "file"
            files.append(MediaFile(inf["path"], kind, True))
            for d in inf.get("derived") or []:
                if d["path"] != inf["path"]:
                    files.append(MediaFile(d["path"], d["kind"], False))
        return files


def _gateway_multimodal(ws, node: dict, prompt: str, schema: dict, metas: list[dict]) -> dict:
    """No VM: send images as OpenAI-compatible image parts through the LLM gateway."""
    from openai import OpenAI
    parts = [{"type": "text", "text": prompt + "\n\n반드시 다음 JSON Schema를 따르는 JSON 객체 하나만 출력하세요:\n"
                                     + json.dumps(schema, ensure_ascii=False)}]
    for m in metas:
        if m["modality"] != "image" or m["size"] > GATEWAY_IMAGE_BYTES or \
                m["mime"] not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            continue
        data = base64.b64encode(b"".join(iter_chunks(m["id"]))).decode()
        parts.append({"type": "image_url", "image_url": {"url": f"data:{m['mime']};base64,{data}"}})
    client = OpenAI(base_url=infra.env("LLM_GATEWAY_URL", "http://llm-gateway:8090/v1"),
                    api_key=infra.env("LLM_GATEWAY_TOKEN", "internal"))
    r = client.chat.completions.create(
        model=infra.env("AGDD_VISION_MODEL", infra.env("AGDD_CHEAP_MODEL", "cheap")),
        messages=[{"role": "user", "content": parts}], response_format={"type": "json_object"},
        extra_headers={"x-agdd-task": "multi_agent", "x-agdd-workspace": str(ws), "x-agdd-node": node["id"]})
    return infra.parse_json(r.choices[0].message.content or "")


# ---------------------------------------------------------------- data sources
def resolver(ws, repo_key: str):
    def resolve(node: dict) -> str:
        src, p = node["source"], node.get("params") or {}
        if src == "text":
            return p.get("text") or "(빈 텍스트)"
        if src == "spec_scope":
            with infra.pg(readonly=True) as c:
                eng = pgstore.hydrate_engine(c, ws)
            try:
                scope = eng.executable_scope(p.get("domain") or None)
            finally:
                eng.close()
            return json.dumps(scope, ensure_ascii=False, indent=1, default=str)
        if src == "ux_layout":
            store = pgstore.PgOverrideStore()
            view = p.get("view_key") or "home"
            bindings = ux_workbench.Workbench(store).export_bindings(ws, view)
            locked = [r["component_selector"] for r in store.list(ws, view) if r.get("locked_by_user")]
            return json.dumps(ux_workbench.summarize_2d_layout(bindings, locked), ensure_ascii=False, indent=1)
        if src == "collab_history":
            with infra.pg(readonly=True) as c:
                rows = pgstore.list_collab_messages(c, ws, limit=p.get("limit", 20))
            return "\n".join(f"[{r['role']}/{r['mode']}] {str(r['content'])[:800]}" for r in rows) or "(대화 없음)"
        if not vm_service.enabled():
            raise RuntimeError(f"{src} 데이터 소스는 워크스페이스 VM이 필요합니다")
        if src == "cells":
            from . import vm_sync
            return json.dumps(vm_sync.read_cells(ws), ensure_ascii=False, indent=1)[:multi_agent.MAX_INPUT_CHARS]
        if src == "repo_files":
            from .vm import RuntimeClient, VmShell, sh_quote
            repo = p.get("repo_key") or repo_key
            vm_service.repo_path(repo)
            sh = VmShell(RuntimeClient(), vm_service.canvas_for(ws), "agdd-ma-data")
            try:
                parts = []
                for path in p.get("paths") or []:
                    r = sh.run(f"head -c 8000 {sh_quote(f'{sh.home}/repos/{repo}/{path}')} 2>&1", timeout=60)
                    parts.append(f"### {path}\n```\n{r.output}\n```")
                return "\n\n".join(parts) or "(경로 없음)"
            finally:
                sh.close()
        raise RuntimeError(f"unknown data source {src}")
    return resolve


# ---------------------------------------------------------------- agent invocation
def invoker(ws, repo_key: str, run_id: str, media: VmMedia | None = None):
    use_vm = vm_service.enabled()
    base = vm_service.load_settings(ws) if use_vm else None
    media = media or VmMedia(ws, use_vm)

    def invoke(node: dict, prompt: str, ctx: dict):
        schema = ctx.get("schema") or multi_agent.THINK_SCHEMA
        metas = ctx.get("attachments") or []
        if not use_vm:
            if node["mode"] == "code":
                raise RuntimeError("code 모드 에이전트는 워크스페이스 VM이 필요합니다 (AGORA_RUNTIME_SOCKET)")
            if any(m["modality"] == "image" for m in metas):
                return _gateway_multimodal(ws, node, prompt, schema, metas)
            llm = infra.cheap_model("multi_agent", ws, None, node["id"])
            return llm(prompt, schema)
        from .cli_agents import VmAgent
        from .vm import RuntimeClient, VmShell
        prompt += f"\n\n(team run {run_id})"  # keep the semantic cache from replaying an earlier run's answer
        override = {k: node[k] for k in ("cli", "model", "effort") if node.get(k)}
        if node["mode"] == "think":  # structured() uses the cheap slot: point it at this node's model/effort
            override["cheap_model"] = node.get("model") or base.model
            override["cheap_effort"] = node.get("effort") or base.effort
        settings = vm_service.load_settings(ws, override)
        term = f"agdd-ma-{node['id']}" + (f"-t{ctx['subtask']['index']}" if ctx.get("subtask") else "")
        sh = VmShell(RuntimeClient(), vm_service.canvas_for(ws), term[:64])  # parallel sub-tasks: one PTY each
        sh.mirror = True  # echo CLI output into the PTY: the console's agent monitor watches it live
        agent = VmAgent(sh, settings, vm_service.budget_gate(ws))
        files = media.files_for(metas)
        t0 = time.time()
        try:
            if node["mode"] == "think":
                data, res = agent.structured(prompt, schema, timeout=node["timeout"], files=files)
                vm_service.record_usage(ws, None, f"team:{node['id']}", settings, res,
                                        int((time.time() - t0) * 1000), cheap=True)
                return data
            with _code_lock(ws):
                repo_dir = f"{sh.home}/repos/{repo_key}"
                res = agent.agent(prompt, repo_dir, None, timeout=node["timeout"], files=files)
                status = sh.run(f"cd {repo_dir} && git status --porcelain 2>/dev/null || true", timeout=30).output
            vm_service.record_usage(ws, None, f"team:{node['id']}", settings, res, int((time.time() - t0) * 1000))
            if not res.ok:
                raise RuntimeError((res.text or res.raw_tail or "CLI agent failed")[:3000])
            out = multi_agent.coerce_output(res.text)
            out.meta = {"changed_files": sorted({l[3:].strip() for l in status.splitlines()
                                                 if l[3:].strip() and not l[3:].startswith(".agdd")}),
                        "cli": settings.cli, "model": res.model}
            return out
        finally:
            sh.close()
    return invoke


def _code_lock(ws) -> threading.Lock:
    with _lock:
        return _CODE_LOCKS.setdefault(str(ws), threading.Lock())


# ---------------------------------------------------------------- runs
def start_run(ws, team_id, graph: dict | None, request: str, user, attachments=None) -> dict:
    """Start a team run from a saved team (``team_id``) or an unsaved canvas graph, with optional run-level
    attachments (ids of uploaded files) delivered alongside the request."""
    name = "임시 팀"
    if team_id:
        with infra.pg(readonly=True) as c:
            row = c.execute("SELECT name, graph_json FROM agent_teams WHERE team_id=%s AND workspace_id=%s",
                            (team_id, ws)).fetchone()
        if row is None:
            raise LookupError("team not found")
        graph = graph or row["graph_json"]
        name = row["name"]
    clean = validate_team_graph(graph or {})
    att_ids = multi_agent._norm_attachments(attachments or [], "run")
    metas = get_attachments(ws, att_ids)
    with _lock:
        if len(_ACTIVE_WS.get(str(ws), set())) >= MAX_ACTIVE_PER_WORKSPACE:
            raise RuntimeError(f"이 워크스페이스에서 이미 {MAX_ACTIVE_PER_WORKSPACE}개의 팀 실행이 진행 중입니다")
    settings = vm_service.load_settings(ws) if vm_service.enabled() else None
    repo_key = clean["settings"]["repo_key"] or (settings.default_repo if settings else "workspace") or "workspace"
    if any(n["kind"] == "agent" and n["mode"] == "code" for n in clean["nodes"]):
        vm_service.repo_path(repo_key)
    uid = user if user and len(str(user)) == 36 else None
    with infra.pg() as c:
        run = c.execute("INSERT INTO agent_team_runs (team_id, workspace_id, graph_json, request, status, created_by, "
                        "attachments) VALUES (%s,%s,%s,%s,'RUNNING',%s,%s) RETURNING team_run_id, created_at",
                        (team_id or None, ws, Jsonb(clean), request or "", uid,
                         Jsonb([{k: v for k, v in m.items() if k not in ("excerpt", "created_at")} for m in metas]))
                        ).fetchone()
        c.commit()
    rid = str(run["team_run_id"])
    cancel = threading.Event()
    with _lock:
        _ACTIVE[rid] = cancel
        _ACTIVE_WS.setdefault(str(ws), set()).add(rid)
    threading.Thread(target=_execute, args=(ws, rid, name, clean, request, repo_key, cancel, uid, metas),
                     name=f"agdd-team-{rid[:8]}", daemon=True).start()
    return {"team_run_id": rid, "status": "RUNNING", "repo_key": repo_key}


def _execute(ws, rid, name, graph, request, repo_key, cancel, uid, metas=None):
    holder: dict = {}
    last_state = [0.0]

    def on_event(ev):
        tr = holder.get("run")
        try:
            with infra.pg() as c:
                c.execute("INSERT INTO agent_team_events (team_run_id, seq, type, node, target, content, meta_json) "
                          "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                          (rid, ev["seq"], ev["type"], ev["node"], ev["target"], ev["content"],
                           Jsonb(json.loads(json.dumps(ev["meta"], default=str)))))
                if tr is not None and (ev["type"] in ("node_started", "node_finished", "node_failed")
                                       or time.time() - last_state[0] > 2):
                    c.execute("UPDATE agent_team_runs SET node_state=%s, updated_at=now() WHERE team_run_id=%s",
                              (Jsonb(tr.snapshot()), rid))
                    last_state[0] = time.time()
                c.commit()
        except Exception as exc:
            print(f"[agdd-team] event write failed: {exc}", flush=True)

    try:
        if vm_service.enabled() and any(n["kind"] == "agent" and n["mode"] == "code" for n in graph["nodes"]):
            try:
                from . import vm_sync
                vm_sync.sync_workspace_context(ws, repo_key)
            except Exception as exc:
                print(f"[agdd-team] context sync failed: {exc}", flush=True)
        has_live_agents = any(n["kind"] == "agent" and n["mode"] != "mock" for n in graph["nodes"])
        media = VmMedia(ws, use_vm=None if has_live_agents else False)
        invoke = invoker(ws, repo_key, rid, media) if has_live_agents else None
        tr = TeamRun(graph, invoke, resolver(ws, repo_key), request, on_event, cancel,
                     attachments=metas or [], media=media)
        holder["run"] = tr
        out = tr.run()
        status, result, state = out["status"], out["result"], out["node_state"]
    except Exception as exc:
        status, result, state = "FAILED", {"error": str(exc)[:4000]}, {}
    finally:
        with _lock:
            _ACTIVE.pop(rid, None)
            _ACTIVE_WS.get(str(ws), set()).discard(rid)
    try:
        with infra.pg() as c:
            c.execute("UPDATE agent_team_runs SET status=%s, result_json=%s, node_state=%s, updated_at=now() "
                      "WHERE team_run_id=%s",
                      (status, Jsonb(json.loads(json.dumps(result, default=str))), Jsonb(state), rid))
            outputs = (result or {}).get("outputs") or {}
            first = next((v[-1] for v in outputs.values() if v), "") if isinstance(outputs, dict) else ""
            pgstore.add_collab_message(
                c, ws, "event", "agdd",
                f"🤝 멀티 에이전트 팀 '{name}' 실행 종료: {status}" + (f"\n\n{first[:1500]}" if first else ""),
                meta={"event": "TEAM_RUN_FINISHED", "team_run_id": rid, "status": status}, user_id=uid)
            c.commit()
    except Exception as exc:
        print(f"[agdd-team] finish write failed: {exc}", flush=True)


def cancel_run(ws, rid) -> bool:
    with _lock:
        ev = _ACTIVE.get(str(rid))
    if ev is None:
        return False
    ev.set()
    return True


def get_run(ws, rid, after: int = 0) -> dict | None:
    with infra.pg() as c:
        run = c.execute("SELECT team_run_id, team_id, graph_json, request, status, node_state, result_json, attachments, "
                        "created_at, updated_at, (updated_at < now() - interval '3 minutes') AS stale "
                        "FROM agent_team_runs WHERE team_run_id=%s AND workspace_id=%s", (rid, ws)).fetchone()
        if run is None:
            return None
        with _lock:
            alive = str(rid) in _ACTIVE
        if run["status"] == "RUNNING" and not alive and run["stale"]:
            # the API process restarted mid-run: the thread is gone, so the run can never finish
            c.execute("UPDATE agent_team_runs SET status='FAILED', result_json=%s, updated_at=now() "
                      "WHERE team_run_id=%s", (Jsonb({"error": "agdd-api restarted during the run"}), rid))
            c.commit()
            run["status"] = "FAILED"
        events = c.execute("SELECT seq, type, node, target, content, meta_json, created_at FROM agent_team_events "
                           "WHERE team_run_id=%s AND seq > %s ORDER BY seq LIMIT 500", (rid, int(after))).fetchall()
    run.pop("stale", None)
    run["events"] = events
    run["active"] = alive
    return run


def list_runs(ws, team_id=None, limit: int = 20) -> list[dict]:
    q = ("SELECT team_run_id, team_id, request, status, created_at, updated_at FROM agent_team_runs "
         "WHERE workspace_id=%s" + (" AND team_id=%s" if team_id else "") + " ORDER BY created_at DESC LIMIT %s")
    args = (ws, team_id, limit) if team_id else (ws, limit)
    with infra.pg(readonly=True) as c:
        return c.execute(q, args).fetchall()


__all__ = ["TeamGraphError", "list_teams", "save_team", "delete_team", "start_run", "get_run", "list_runs",
           "cancel_run", "create_attachment", "put_chunk", "complete_attachment", "get_attachments",
           "list_attachments", "attachment_for_download", "delete_attachment", "iter_chunks", "VmMedia"]
