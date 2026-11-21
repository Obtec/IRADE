"""Sync workspace In-Memory relational spec DB, UX overrides, agent tuning, and real-file cells into the VM."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading

from . import infra, pgstore, ux_workbench, vm_service
from .spec_engine import DDL

_VM_SCRIPTS_READY: set[tuple] = set()
_SYNC_LOCK = threading.Lock()
_SYNC_PENDING: dict[str, threading.Timer] = {}

AGDD_SPEC_CLI = r'''#!/usr/bin/env python3
"""Read-only SQL/FTS interface to the workspace's RAM-backed In-Memory Spec DB + UX request emitter."""
import json
import os
import sqlite3
import sys

HOME_DIR = os.environ.get("HOME", "/workspace")


def find_db():
    for cand in (
        os.environ.get("AGDD_SPEC_DB", ""),
        os.path.realpath(os.path.join(HOME_DIR, ".agdd", "spec.db")),
        "/dev/shm/agdd-spec.db",
        "/tmp/agdd-spec.db",
    ):
        if cand and os.path.exists(cand):
            return cand
    return None


def connect_ro(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def rows_to_dicts(cur):
    cols = [d[0] for d in (cur.description or [])]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(
            "Usage: agdd-spec <status|scope [domain]|sql <SELECT...>|search <fts>|ux [view]|files|request-ux ...>",
            file=sys.stderr,
        )
        raise SystemExit(0)
    cmd = args[0]
    if cmd == "request-ux":
        import argparse
        p = argparse.ArgumentParser(prog="agdd-spec request-ux")
        p.add_argument("--view", default="home")
        p.add_argument("--prompt", required=True)
        p.add_argument("--elements", default="[]")
        p.add_argument("--width", type=int, default=1200)
        p.add_argument("--height", type=int, default=720)
        ns = p.parse_args(args[1:])
        try:
            elems = json.loads(ns.elements)
            if not isinstance(elems, list):
                elems = []
        except json.JSONDecodeError:
            elems = []
        req = {
            "view_key": ns.view,
            "prompt": ns.prompt,
            "viewport": {"width": ns.width, "height": ns.height},
            "elements": elems,
        }
        agdd_dir = os.path.join(HOME_DIR, ".agdd")
        os.makedirs(agdd_dir, exist_ok=True)
        out_path = os.path.join(agdd_dir, "ux-request.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(req, f, ensure_ascii=False, indent=2)
        print("<<AGDD_UX_REQUEST:" + json.dumps(req, ensure_ascii=False, separators=(",", ":")) + ">>")
        return

    db = find_db()
    if not db:
        print(json.dumps({"error": "spec db not synced yet"}))
        raise SystemExit(1)

    with connect_ro(db) as c:
        c.row_factory = sqlite3.Row
        if cmd == "status":
            meta = {r["k"]: r["v"] for r in c.execute("SELECT k, v FROM spec_meta")}
            counts = {
                "srs_accept": c.execute("SELECT count(*) FROM srs_requirements WHERE adoption_status='ACCEPT_NOW'").fetchone()[0],
                "srs_total": c.execute("SELECT count(*) FROM srs_requirements").fetchone()[0],
                "arc_accept": c.execute("SELECT count(*) FROM arch_definitions WHERE adoption_status='ACCEPT_NOW'").fetchone()[0],
                "sch_accept": c.execute("SELECT count(*) FROM schema_definitions WHERE adoption_status='ACCEPT_NOW'").fetchone()[0],
                "ux_overrides": c.execute("SELECT count(*) FROM ux_overrides").fetchone()[0],
                "cells": c.execute("SELECT count(*) FROM workspace_cells").fetchone()[0],
            }
            print(json.dumps({"db_path": db, "in_memory_ram": db.startswith("/dev/shm"), "meta": meta, "counts": counts}, ensure_ascii=False, indent=2))
        elif cmd == "scope":
            dom = args[1] if len(args) > 1 else None
            sql = "SELECT * FROM srs_requirements WHERE adoption_status='ACCEPT_NOW'"
            params = ()
            if dom:
                sql += " AND domain=?"
                params = (dom,)
            reqs = [dict(r) for r in c.execute(sql + " ORDER BY priority, req_id", params)]
            arc, sch = {}, {}
            for req in reqs:
                links = c.execute(
                    "SELECT target_type, target_id FROM spec_traceability WHERE source_type='SRS' AND source_id=? "
                    "UNION SELECT source_type, source_id FROM spec_traceability WHERE target_type='SRS' AND target_id=?",
                    (req["req_id"], req["req_id"]),
                ).fetchall()
                for typ, sid in links:
                    if typ == "ARC":
                        r = c.execute("SELECT * FROM arch_definitions WHERE arch_id=? AND adoption_status='ACCEPT_NOW'", (sid,)).fetchone()
                        if r:
                            arc[sid] = dict(r)
                    elif typ == "SCH":
                        r = c.execute("SELECT * FROM schema_definitions WHERE schema_id=? AND adoption_status='ACCEPT_NOW'", (sid,)).fetchone()
                        if r:
                            sch[sid] = dict(r)
            print(json.dumps({"requirements": reqs, "architecture": list(arc.values()), "schemas": list(sch.values())}, ensure_ascii=False, indent=2))
        elif cmd == "sql":
            if len(args) < 2:
                raise SystemExit("usage: agdd-spec sql <SELECT ...>")
            cur = c.execute(args[1])
            cols = [d[0] for d in (cur.description or [])]
            rows = [dict(zip(cols, tuple(r))) for r in cur.fetchmany(500)]
            print(json.dumps({"columns": cols, "rows": rows}, ensure_ascii=False, indent=2))
        elif cmd == "search":
            if len(args) < 2:
                raise SystemExit("usage: agdd-spec search <query>")
            rows = [dict(r) for r in c.execute(
                "SELECT spec_type, spec_id, domain_or_layer, content_text FROM spec_fts WHERE spec_fts MATCH ? ORDER BY bm25(spec_fts) LIMIT 25",
                (args[1],),
            )]
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif cmd == "ux":
            view = args[1] if len(args) > 1 else None
            if view:
                rows = [dict(r) for r in c.execute("SELECT * FROM ux_overrides WHERE view_key=? ORDER BY component_selector", (view,))]
            else:
                rows = [dict(r) for r in c.execute("SELECT * FROM ux_overrides ORDER BY view_key, component_selector")]
            for r in rows:
                try:
                    r["config_json"] = json.loads(r["config_json"])
                except Exception:
                    pass
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif cmd == "files":
            rows = [dict(r) for r in c.execute("SELECT * FROM workspace_cells ORDER BY frame_order, cell_id")]
            for r in rows:
                r["exists"] = os.path.exists(os.path.join(HOME_DIR, "repos", "workspace", r["file_path"]))
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            raise SystemExit(f"unknown subcommand: {cmd}")


if __name__ == "__main__":
    main()
'''

VM_SYNC_SCRIPT = r'''
import base64, hashlib, json, os, shutil, sqlite3, subprocess, sys
from datetime import datetime, timezone

a = json.loads(base64.b64decode(sys.argv[1]))
home = a.get("home") or os.environ.get("HOME", "/workspace")
agdd_dir = os.path.join(home, ".agdd")
repos_dir = os.path.join(home, "repos")
repo_key = a.get("repo_key") or "workspace"
repo_dir = os.path.join(repos_dir, repo_key)
ws_repo = os.path.join(repos_dir, "workspace")
cells_dir = os.path.join(ws_repo, "cells")

os.makedirs(agdd_dir, exist_ok=True)
os.makedirs(repos_dir, exist_ok=True)
os.makedirs(cells_dir, exist_ok=True)

def ensure_git_repo(d):
    os.makedirs(d, exist_ok=True)
    if not os.path.isdir(os.path.join(d, ".git")):
        subprocess.run(["git", "-C", d, "init", "-q"], check=False)
        gi = os.path.join(d, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as f:
                f.write(".agdd/\n__pycache__/\n.pytest_cache/\nnode_modules/\n")
        readme = os.path.join(d, "README.md")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as f:
                f.write("# IRADE Workspace\n\nWorkspace cells live under `cells/` and `/workspace/cells`.\n")
        subprocess.run(["git", "-C", d, "add", "."], check=False)
        subprocess.run(
            ["git", "-C", d, "-c", "user.name=agdd", "-c", "user.email=agdd@localhost", "commit", "-qm", "init workspace"],
            check=False,
        )

if repo_key == "workspace":
    ensure_git_repo(ws_repo)

cells_link = os.path.join(home, "cells")
if not os.path.exists(cells_link) and not os.path.islink(cells_link):
    try:
        os.symlink(cells_dir, cells_link)
    except OSError:
        pass

if home == "/workspace":
    db_path = "/dev/shm/agdd-spec.db" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else "/tmp/agdd-spec.db"
else:
    hid = hashlib.sha1(home.encode()).hexdigest()[:12]
    db_path = f"/dev/shm/agdd-spec-{hid}.db" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else os.path.join(agdd_dir, "spec-ram.db")

tmp_db = f"{db_path}.tmp.{os.getpid()}"
if os.path.exists(tmp_db):
    os.unlink(tmp_db)

ddl = a["ddl"]
extra_ddl = """
CREATE TABLE IF NOT EXISTS spec_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ux_overrides (
    view_key TEXT NOT NULL,
    component_selector TEXT NOT NULL,
    config_json TEXT NOT NULL,
    locked_by_user INTEGER NOT NULL DEFAULT 0,
    updated_by TEXT,
    PRIMARY KEY (view_key, component_selector)
);
CREATE TABLE IF NOT EXISTS workspace_cells (
    cell_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    frame_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_tuning (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

con = sqlite3.connect(tmp_db)
con.executescript(ddl + "\n" + extra_ddl)
now_iso = datetime.now(timezone.utc).isoformat()
snap = a.get("snapshot") or {}
for row in snap.get("srs", []):
    row = dict(row)
    row.setdefault("updated_at", now_iso)
    cols = list(row.keys())
    con.execute(f"INSERT OR REPLACE INTO srs_requirements ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(row.values()))
    con.execute("INSERT INTO spec_fts (spec_type, spec_id, domain_or_layer, content_text) VALUES (?,?,?,?)",
                ("SRS", row["req_id"], row.get("domain", ""), " ".join(str(row.get(k, "")) for k in ("title", "requirement_content", "acceptance_criteria"))))
for row in snap.get("arc", []):
    row = dict(row)
    row.setdefault("updated_at", now_iso)
    cols = list(row.keys())
    con.execute(f"INSERT OR REPLACE INTO arch_definitions ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(row.values()))
    con.execute("INSERT INTO spec_fts (spec_type, spec_id, domain_or_layer, content_text) VALUES (?,?,?,?)",
                ("ARC", row["arch_id"], row.get("layer", ""), " ".join(str(row.get(k, "")) for k in ("component_name", "role_content", "tech_stack", "interface_contract"))))
for row in snap.get("sch", []):
    row = dict(row)
    row.setdefault("updated_at", now_iso)
    if not isinstance(row.get("columns_and_ddl_json", ""), str):
        row["columns_and_ddl_json"] = json.dumps(row["columns_and_ddl_json"], ensure_ascii=False, sort_keys=True)
    cols = list(row.keys())
    con.execute(f"INSERT OR REPLACE INTO schema_definitions ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(row.values()))
    con.execute("INSERT INTO spec_fts (spec_type, spec_id, domain_or_layer, content_text) VALUES (?,?,?,?)",
                ("SCH", row["schema_id"], row.get("storage_engine", ""), " ".join(str(row.get(k, "")) for k in ("table_or_key_name", "role_content", "columns_and_ddl_json"))))
for tr in snap.get("trace", []):
    con.execute("INSERT OR IGNORE INTO spec_traceability (source_type, source_id, target_type, target_id, relation_type) VALUES (?,?,?,?,?)",
                (tr["source_type"], tr["source_id"], tr["target_type"], tr["target_id"], tr["relation_type"]))

for k, v in (a.get("meta") or {}).items():
    con.execute("INSERT OR REPLACE INTO spec_meta (k, v) VALUES (?, ?)", (str(k), json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v))

ux_by_view = {}
for u in (a.get("ux_rows") or []):
    cfg = u.get("config_json") or {}
    con.execute("INSERT OR REPLACE INTO ux_overrides (view_key, component_selector, config_json, locked_by_user, updated_by) VALUES (?,?,?,?,?)",
                (u["view_key"], u["component_selector"], json.dumps(cfg, ensure_ascii=False, sort_keys=True), 1 if u.get("locked_by_user") else 0, str(u.get("updated_by") or "")))
    ux_by_view.setdefault(u["view_key"], {})[u["component_selector"]] = cfg

manifest_path = os.path.join(cells_dir, ".irade-manifest.json")
if os.path.isfile(manifest_path):
    try:
        man = json.load(open(manifest_path, encoding="utf-8"))
        for cid, info in (man or {}).items():
            con.execute("INSERT OR REPLACE INTO workspace_cells (cell_id, kind, title, file_path, sha256, frame_order) VALUES (?,?,?,?,?,?)",
                        (cid, info.get("kind", "markdown"), info.get("title", ""), info.get("path", ""), info.get("sha256", ""), int(info.get("frameOrder", 0))))
    except Exception:
        pass

for k, v in (a.get("tuning") or {}).items():
    con.execute("INSERT OR REPLACE INTO agent_tuning (k, v) VALUES (?, ?)", (str(k), json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v))

con.commit()
con.close()
os.replace(tmp_db, db_path)

spec_link = os.path.join(agdd_dir, "spec.db")
if os.path.islink(spec_link) or os.path.exists(spec_link):
    try:
        os.unlink(spec_link)
    except OSError:
        pass
try:
    os.symlink(db_path, spec_link)
except OSError:
    shutil.copy2(db_path, spec_link)

with open(os.path.join(agdd_dir, "ux-overrides.json"), "w", encoding="utf-8") as f:
    json.dump(ux_by_view, f, ensure_ascii=False, indent=2, sort_keys=True)
if os.path.isdir(repo_dir):
    r_agdd = os.path.join(repo_dir, ".agdd")
    os.makedirs(r_agdd, exist_ok=True)
    with open(os.path.join(r_agdd, "ux-overrides.json"), "w", encoding="utf-8") as f:
        json.dump(ux_by_view, f, ensure_ascii=False, indent=2, sort_keys=True)

tuning = a.get("tuning") or {}
inst = (tuning.get("instructions") or "").strip()
lines = [
    "# IRADE Workspace Context",
    f"- Spec In-Memory DB: `{db_path}` (alias `{spec_link}`)",
    "- CLI: `agdd-spec status | scope [domain] | sql \"SELECT ...\" | search \"<kw>\" | ux [view] | files`",
    f"- Workspace Files: `{repo_dir}` · Notebook Cell Files: `{cells_dir}` (symlink `{cells_link}`)",
    f"- UX Overrides: `{os.path.join(agdd_dir, 'ux-overrides.json')}` (never modify `locked_by_user=1` selectors)",
    "- Visual Request: if you need 2D spatial layout from the user, run `agdd-spec request-ux --view <view> --prompt \"...\" --elements '[...]'`",
]
if inst:
    lines += ["", "## User Agent Tuning Instructions", inst[:2000]]
with open(os.path.join(agdd_dir, "WORKSPACE_CONTEXT.md"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")

print(json.dumps({"ok": True, "db_path": db_path, "repo_dir": repo_dir, "cells_dir": cells_dir}))
'''

VM_CELLS_SCRIPT = r'''
import base64, hashlib, json, os, re, sqlite3, subprocess, sys

a = json.loads(base64.b64decode(sys.argv[1]))
home = a.get("home") or os.environ.get("HOME", "/workspace")
ws_repo = os.path.join(home, "repos", "workspace")
cells_dir = os.path.join(ws_repo, "cells")
os.makedirs(cells_dir, exist_ok=True)

if not os.path.isdir(os.path.join(ws_repo, ".git")):
    subprocess.run(["git", "-C", ws_repo, "init", "-q"], check=False)
    gi = os.path.join(ws_repo, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8") as f:
            f.write(".agdd/\n__pycache__/\n.pytest_cache/\nnode_modules/\n")
    subprocess.run(["git", "-C", ws_repo, "add", "."], check=False)
    subprocess.run(["git", "-C", ws_repo, "-c", "user.name=agdd", "-c", "user.email=agdd@localhost", "commit", "-qm", "init workspace"], check=False)

cells_link = os.path.join(home, "cells")
if not os.path.exists(cells_link) and not os.path.islink(cells_link):
    try:
        os.symlink(cells_dir, cells_link)
    except OSError:
        pass

EXT = {"python": "py", "lean": "lean", "rocq": "v", "html": "html", "markdown": "md", "agent": "md"}
KIND_BY_EXT = {".py": "python", ".lean": "lean", ".v": "rocq", ".html": "html", ".md": "markdown"}
manifest_path = os.path.join(cells_dir, ".irade-manifest.json")

def load_manifest():
    if os.path.isfile(manifest_path):
        try:
            return json.load(open(manifest_path, encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}

def sha(txt):
    return hashlib.sha256((txt or "").encode("utf-8")).hexdigest()

def slug_for(cell, used_paths):
    ext = EXT.get(cell.get("kind", "markdown"), "md")
    title = re.sub(r"[^A-Za-z0-9._-]+", "-", str(cell.get("title") or "")).strip("-_.")[:28]
    if not title:
        title = cell.get("kind", "cell")
    tail = re.sub(r"[^A-Za-z0-9]+", "", str(cell.get("id") or ""))[-8:] or "item"
    cand = f"cells/{title}_{tail}.{ext}"
    n = 1
    while cand in used_paths:
        cand = f"cells/{title}_{tail}_{n}.{ext}"
        n += 1
    return cand

def refresh_db_cells(man):
    db_link = os.path.realpath(os.path.join(home, ".agdd", "spec.db"))
    if not os.path.isfile(db_link):
        return
    try:
        con = sqlite3.connect(db_link)
        con.execute("CREATE TABLE IF NOT EXISTS workspace_cells (cell_id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, file_path TEXT NOT NULL, sha256 TEXT NOT NULL, frame_order INTEGER NOT NULL DEFAULT 0)")
        con.execute("DELETE FROM workspace_cells")
        for cid, info in man.items():
            con.execute("INSERT OR REPLACE INTO workspace_cells (cell_id, kind, title, file_path, sha256, frame_order) VALUES (?,?,?,?,?,?)",
                        (cid, info.get("kind", "markdown"), info.get("title", ""), info.get("path", ""), info.get("sha256", ""), int(info.get("frameOrder", 0))))
        con.commit()
        con.close()
    except Exception:
        pass

op = a.get("op")
man = load_manifest()

if op == "sync":
    incoming = a.get("cells") or []
    delete_missing = bool(a.get("delete_missing", True))
    force = bool(a.get("force", True))
    used = {v["path"] for cid, v in man.items() if cid in {c["id"] for c in incoming}}
    incoming_ids = {c["id"] for c in incoming}
    if delete_missing:
        for cid in list(man.keys()):
            if cid not in incoming_ids:
                old_full = os.path.join(ws_repo, man[cid]["path"])
                if os.path.isfile(old_full):
                    try:
                        os.unlink(old_full)
                    except OSError:
                        pass
                man.pop(cid, None)

    written = []
    external_changes = []
    next_man = {}
    for idx, c in enumerate(incoming):
        cid = c["id"]
        kind = c.get("kind", "markdown")
        ext = EXT.get(kind, "md")
        prev = man.get(cid)
        rel = prev["path"] if (prev and prev.get("path", "").endswith("." + ext)) else slug_for(c, used)
        used.add(rel)
        full = os.path.join(ws_repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        inc_text = c.get("text") or ""
        inc_hash = sha(inc_text)
        disk_text = None
        if os.path.isfile(full):
            disk_text = open(full, "r", encoding="utf-8", errors="replace").read()
        disk_hash = sha(disk_text) if disk_text is not None else None
        prev_hash = prev.get("sha256") if prev else None

        if not force and prev_hash and inc_hash == prev_hash and disk_hash is not None and disk_hash != prev_hash:
            # VM/IDE/CLI edited this cell on disk while incoming cell stayed at old hash.
            final_text = disk_text
            final_hash = disk_hash
            external_changes.append({"id": cid, "kind": kind, "path": rel, "text": disk_text, "sha256": disk_hash})
        else:
            if disk_hash != inc_hash:
                with open(full, "w", encoding="utf-8") as f:
                    f.write(inc_text)
                written.append(rel)
            final_text = inc_text
            final_hash = inc_hash
        next_man[cid] = {
            "path": rel,
            "kind": kind,
            "title": c.get("title", ""),
            "sha256": final_hash,
            "frameOrder": int(c.get("frameOrder", idx * 10)),
        }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(next_man, f, ensure_ascii=False, indent=2, sort_keys=True)
    refresh_db_cells(next_man)
    print(json.dumps({"ok": True, "count": len(next_man), "written": written, "external_changes": external_changes, "manifest": next_man}, ensure_ascii=False))

elif op == "read":
    cells_out = []
    tracked_paths = set()
    for cid, info in sorted(man.items(), key=lambda kv: (kv[1].get("frameOrder", 0), kv[0])):
        rel = info["path"]
        tracked_paths.add(rel)
        full = os.path.join(ws_repo, rel)
        if not os.path.isfile(full):
            continue
        text = open(full, "r", encoding="utf-8", errors="replace").read()
        cur_sha = sha(text)
        changed = cur_sha != info.get("sha256")
        if changed:
            info["sha256"] = cur_sha
        cells_out.append({
            "id": cid,
            "kind": info.get("kind", "markdown"),
            "title": info.get("title", ""),
            "path": rel,
            "text": text,
            "sha256": cur_sha,
            "changed": changed,
            "frameOrder": info.get("frameOrder", 0),
        })
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2, sort_keys=True)
    refresh_db_cells(man)
    untracked = []
    for root_dir, _, files in os.walk(cells_dir):
        for fn in sorted(files):
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in KIND_BY_EXT:
                continue
            full = os.path.join(root_dir, fn)
            rel = os.path.relpath(full, ws_repo).replace(os.sep, "/")
            if rel in tracked_paths:
                continue
            text = open(full, "r", encoding="utf-8", errors="replace").read()
            untracked.append({"path": rel, "kind": KIND_BY_EXT[ext], "text": text[:65536], "sha256": sha(text)})
    print(json.dumps({"cells": cells_out, "untracked": untracked[:100]}, ensure_ascii=False))
'''

_SYNC_HASH = hashlib.sha256(VM_SYNC_SCRIPT.encode()).hexdigest()[:12]
_CELLS_HASH = hashlib.sha256(VM_CELLS_SCRIPT.encode()).hexdigest()[:12]
_CLI_HASH = hashlib.sha256(AGDD_SPEC_CLI.encode()).hexdigest()[:12]


def _ensure_script(shell, kind: str, path: str, content: str, h: str) -> None:
    key = (shell.canvas_id, shell.home, kind, h)
    if key in _VM_SCRIPTS_READY:
        return
    shell.run(f"mkdir -p {shell.home}/.agdd {shell.home}/.local/bin", timeout=30)
    shell.put_file(path, content)
    if kind == "cli":
        shell.run(f"chmod 0755 {path}", timeout=30)
    _VM_SCRIPTS_READY.add(key)


def _run_vm_script(shell, kind: str, script_body: str, h: str, payload: dict, timeout: int = 120) -> dict:
    script_path = f"{shell.home}/.agdd/{kind}-{h}.py"
    _ensure_script(shell, kind, script_path, script_body, h)
    _ensure_script(shell, "cli", f"{shell.home}/.local/bin/agdd-spec", AGDD_SPEC_CLI, _CLI_HASH)
    arg = base64.b64encode(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")).decode("ascii")
    r = shell.run_inline(f"python3 {script_path} {arg}", timeout=timeout)
    if r.exit_code != 0 and "No such file" in r.output:
        _VM_SCRIPTS_READY.discard((shell.canvas_id, shell.home, kind, h))
        _ensure_script(shell, kind, script_path, script_body, h)
        r = shell.run_inline(f"python3 {script_path} {arg}", timeout=timeout)
    if r.exit_code != 0:
        raise RuntimeError(r.output.strip()[-800:] or f"vm_sync {kind} failed")
    lines = [ln for ln in r.output.strip().splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else {"ok": True}


def sync_workspace_context(workspace_id, repo_key: str | None = None, shell=None) -> dict:
    """Push latest Postgres spec snapshot, UX overrides, and agent tuning into the VM's RAM SQLite DB."""
    if not vm_service.enabled() and shell is None:
        return {"skipped": True}
    settings = vm_service.load_settings(workspace_id)
    rk = (repo_key or settings.default_repo or "workspace").strip() or "workspace"
    with infra.pg(readonly=True) as c:
        snap_row = pgstore.latest_snapshot(c, workspace_id)
        ux_rows = pgstore.PgOverrideStore().list(workspace_id)
    snapshot = {
        "srs": (snap_row or {}).get("srs_json") or [],
        "arc": (snap_row or {}).get("arc_json") or [],
        "sch": (snap_row or {}).get("sch_json") or [],
        "trace": (snap_row or {}).get("trace_json") or [],
    }
    meta = {
        "workspace_id": str(workspace_id),
        "snapshot_id": (snap_row or {}).get("snapshot_id"),
        "version": (snap_row or {}).get("version", 0),
        "conflict_passed": bool((snap_row or {}).get("conflict_passed")),
    }
    tuning = {
        "cli": settings.cli,
        "model": settings.model,
        "effort": settings.effort,
        "permission": settings.permission,
        "default_repo": rk,
        "instructions": settings.instructions,
        "ux_auto_canvas": settings.ux_auto_canvas,
    }
    sh = shell or vm_service.shell(workspace_id, "agdd-ide")
    payload = {
        "home": sh.home,
        "ddl": DDL,
        "snapshot": snapshot,
        "meta": meta,
        "ux_rows": ux_rows,
        "tuning": tuning,
        "repo_key": rk,
    }
    return _run_vm_script(sh, "vm-sync", VM_SYNC_SCRIPT, _SYNC_HASH, payload, timeout=120)


def sync_workspace_context_async(workspace_id, delay: float = 0.35) -> None:
    if not vm_service.enabled():
        return
    key = str(workspace_id)

    def _fire():
        with _SYNC_LOCK:
            _SYNC_PENDING.pop(key, None)
        try:
            sync_workspace_context(workspace_id)
        except Exception:
            pass

    with _SYNC_LOCK:
        prev = _SYNC_PENDING.pop(key, None)
        if prev is not None:
            prev.cancel()
        t = threading.Timer(delay, _fire)
        t.daemon = True
        _SYNC_PENDING[key] = t
        t.start()


def sync_cells(workspace_id, cells: list[dict], delete_missing: bool = True, force: bool = True, shell=None) -> dict:
    sh = shell or vm_service.shell(workspace_id, "agdd-ide")
    sanitized = []
    for i, c in enumerate(cells or []):
        cid = str(c.get("id") or "").strip()
        if not cid or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", cid):
            continue
        sanitized.append({
            "id": cid,
            "kind": str(c.get("kind") or "markdown"),
            "title": str(c.get("title") or "")[:120],
            "text": str(c.get("text") or ""),
            "frameOrder": int(c.get("frameOrder", i * 10)),
        })
    return _run_vm_script(
        sh,
        "vm-cells",
        VM_CELLS_SCRIPT,
        _CELLS_HASH,
        {"home": sh.home, "op": "sync", "cells": sanitized, "delete_missing": delete_missing, "force": force},
        timeout=120,
    )


def read_cells(workspace_id, shell=None) -> dict:
    sh = shell or vm_service.shell(workspace_id, "agdd-ide")
    return _run_vm_script(
        sh,
        "vm-cells",
        VM_CELLS_SCRIPT,
        _CELLS_HASH,
        {"home": sh.home, "op": "read"},
        timeout=120,
    )


def read_pending_vm_ux_request(workspace_id, shell=None) -> dict | None:
    sh = shell or vm_service.shell(workspace_id, "agdd-ide")
    path = f"{sh.home}/.agdd/ux-request.json"
    try:
        raw = sh.read_file(path, max_bytes=65536).decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None
    try:
        return ux_workbench.normalize_ux_request(json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        return None


def clear_pending_vm_ux_request(workspace_id, shell=None) -> None:
    sh = shell or vm_service.shell(workspace_id, "agdd-ide")
    sh.run(f"rm -f {sh.home}/.agdd/ux-request.json", timeout=30)


def write_vm_ux_response(workspace_id, view_key: str, summary: dict, bindings: dict,
                         repo_key: str | None = None, shell=None) -> None:
    sh = shell or vm_service.shell(workspace_id, "agdd-ide")
    rk = (repo_key or "workspace").strip() or "workspace"
    sh.run(f"mkdir -p {sh.home}/.agdd {sh.home}/repos/{rk}/.agdd", timeout=30)
    resp = json.dumps({"view_key": view_key, "summary": summary, "bindings": bindings}, ensure_ascii=False, indent=2)
    sh.put_file(f"{sh.home}/.agdd/ux-response.json", resp)
    sh.put_file(f"{sh.home}/repos/{rk}/.agdd/ux-response.json", resp)
    clear_pending_vm_ux_request(workspace_id, shell=sh)
