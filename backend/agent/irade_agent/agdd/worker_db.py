"""Worker-local SQLite (DOC.md §4.2, §6.1 #2,#3,#5).

Symbol indexing uses Python's ``ast`` for .py files (Tree-sitter can be plugged in
via ``WorkerDB.add_symbols`` for other languages). ``ast_hash`` ignores whitespace
and comments because it hashes the normalized AST dump.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS tool_outputs_fts USING fts5(
    output_id UNINDEXED, tool_name, command_or_target, chunk_text, tokenize = 'unicode61');
CREATE TABLE IF NOT EXISTS code_symbols (
    symbol_id INTEGER PRIMARY KEY, file_path TEXT NOT NULL, symbol_name TEXT NOT NULL,
    qualified_name TEXT NOT NULL, symbol_kind TEXT NOT NULL, ast_hash TEXT NOT NULL,
    start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, signature TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_symbols_qn ON code_symbols (qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON code_symbols (file_path);
CREATE TABLE IF NOT EXISTS code_edges (
    src_symbol_id INTEGER NOT NULL, dst_symbol_id INTEGER NOT NULL, edge_kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS semantic_projections (
    qualified_name TEXT NOT NULL, ast_hash TEXT NOT NULL, declarative_json TEXT NOT NULL,
    contract_diff_status TEXT NOT NULL CHECK (contract_diff_status IN ('MATCHED','DRIFT','UNCONTRACTED')),
    PRIMARY KEY (qualified_name, ast_hash));
CREATE TABLE IF NOT EXISTS file_read_watermarks (
    file_path TEXT PRIMARY KEY, last_read_hash TEXT NOT NULL, last_read_lines TEXT NOT NULL, snapshot_text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_state (
    state_key TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL);
"""
READ_LINE_CAP = 150
CHUNK_CHARS = 2000


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ast_hash(node: ast.AST) -> str:
    body = node.body if hasattr(node, "body") else []
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        node = type(node)(**{**{f: getattr(node, f) for f in node._fields}, "body": body[1:] or [ast.Pass()]})
    return hashlib.sha256(ast.dump(node, annotate_fields=False).encode()).hexdigest()[:16]


def extract_python_symbols(source: str, file_path: str) -> tuple[list[dict], list[tuple[str, str, str]]]:
    tree = ast.parse(source)
    symbols, edges = [], []

    def visit(node, prefix, in_class):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qn = f"{prefix}.{child.name}" if prefix else child.name
                kind = "class" if isinstance(child, ast.ClassDef) else ("method" if in_class else "function")
                if isinstance(child, ast.ClassDef):
                    sig = f"class {child.name}({', '.join(ast.unparse(b) for b in child.bases)})"
                else:
                    sig = f"def {child.name}({ast.unparse(child.args)})"
                symbols.append({"file_path": file_path, "symbol_name": child.name, "qualified_name": qn,
                                "symbol_kind": kind, "ast_hash": ast_hash(child), "start_line": child.lineno,
                                "end_line": child.end_lineno or child.lineno, "signature": sig})
                if not isinstance(child, ast.ClassDef):
                    for sub in ast.walk(child):
                        if isinstance(sub, ast.Call):
                            f = sub.func
                            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
                            if name:
                                edges.append((qn, name, "calls"))
                visit(child, qn, isinstance(child, ast.ClassDef))

    visit(tree, "", False)
    return symbols, edges


class WorkerDB:
    def __init__(self, path: str = ":memory:", root: str | Path = "."):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DDL)
        self.root = Path(root)

    # ---- §6.1 #2 tool output offloading ----
    def offload(self, tool_name: str, target: str, text: str) -> str:
        oid = uuid.uuid4().hex[:12]
        with self.db:
            for i in range(0, max(len(text), 1), CHUNK_CHARS):
                self.db.execute("INSERT INTO tool_outputs_fts VALUES (?,?,?,?)",
                                (oid, tool_name, target, text[i:i + CHUNK_CHARS]))
        return oid

    def search_outputs(self, query: str, limit: int = 5, output_id: str | None = None) -> list[str]:
        sql = "SELECT chunk_text FROM tool_outputs_fts WHERE tool_outputs_fts MATCH ?"
        args: list = [query]
        if output_id:
            sql += " AND output_id = ?"
            args.append(output_id)
        try:
            return [r[0] for r in self.db.execute(sql + " ORDER BY bm25(tool_outputs_fts) LIMIT ?", (*args, limit))]
        except sqlite3.OperationalError:
            return []

    # ---- §6.1 #3 symbol index ----
    def add_symbols(self, file_path: str, symbols: list[dict], edges: list[tuple[str, str, str]] = ()) -> None:
        with self.db:
            old = [r[0] for r in self.db.execute("SELECT symbol_id FROM code_symbols WHERE file_path=?", (file_path,))]
            if old:
                marks = ",".join("?" * len(old))
                self.db.execute(f"DELETE FROM code_edges WHERE src_symbol_id IN ({marks})", old)
                self.db.execute("DELETE FROM code_symbols WHERE file_path=?", (file_path,))
            for s in symbols:
                self.db.execute(
                    "INSERT INTO code_symbols (file_path, symbol_name, qualified_name, symbol_kind, ast_hash, "
                    "start_line, end_line, signature) VALUES (:file_path,:symbol_name,:qualified_name,:symbol_kind,"
                    ":ast_hash,:start_line,:end_line,:signature)", s)
            for src, dst, kind in edges:
                s = self.db.execute("SELECT symbol_id FROM code_symbols WHERE file_path=? AND qualified_name=?",
                                    (file_path, src)).fetchone()
                d = self.db.execute("SELECT symbol_id FROM code_symbols WHERE symbol_name=? LIMIT 1", (dst,)).fetchone()
                if s and d:
                    self.db.execute("INSERT INTO code_edges VALUES (?,?,?)", (s[0], d[0], kind))

    def index_file(self, rel_path: str) -> dict[str, str]:
        """(Re)index one file; returns {qualified_name: ast_hash} of symbols whose hash changed or are new."""
        before = {r["qualified_name"]: r["ast_hash"] for r in
                  self.db.execute("SELECT qualified_name, ast_hash FROM code_symbols WHERE file_path=?", (rel_path,))}
        src = (self.root / rel_path).read_text(encoding="utf-8")
        symbols, edges = extract_python_symbols(src, rel_path)
        self.add_symbols(rel_path, symbols, edges)
        return {s["qualified_name"]: s["ast_hash"] for s in symbols if before.get(s["qualified_name"]) != s["ast_hash"]}

    def index_tree(self, pattern: str = "**/*.py") -> int:
        n = 0
        for p in sorted(self.root.glob(pattern)):
            try:
                self.index_file(str(p.relative_to(self.root)))
                n += 1
            except (SyntaxError, UnicodeDecodeError):
                continue
        return n

    def find_symbol(self, name: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM code_symbols WHERE qualified_name=? OR symbol_name=? ORDER BY file_path, start_line",
            (name, name))]

    def read_symbol(self, qualified_name: str, cap: int = READ_LINE_CAP) -> str:
        """Partial read of one symbol's line range instead of the whole file."""
        rows = self.find_symbol(qualified_name)
        if not rows:
            raise KeyError(qualified_name)
        s = rows[0]
        lines = (self.root / s["file_path"]).read_text(encoding="utf-8").splitlines()
        end = min(s["end_line"], s["start_line"] + cap - 1)
        body = "\n".join(lines[s["start_line"] - 1:end])
        more = f"\n[... {s['end_line'] - end} more lines]" if end < s["end_line"] else ""
        return f"# {s['file_path']}:{s['start_line']}-{end}\n{body}{more}"

    def read_file(self, rel_path: str, start: int = 1, cap: int = READ_LINE_CAP) -> str:
        """Watermarked read: first read returns <=cap lines; later reads return only a unified diff."""
        text = (self.root / rel_path).read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode()).hexdigest()
        wm = self.db.execute("SELECT * FROM file_read_watermarks WHERE file_path=?", (rel_path,)).fetchone()
        lines = text.splitlines()
        end = min(len(lines), start + cap - 1)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO file_read_watermarks VALUES (?,?,?,?)",
                            (rel_path, digest, f"{start}-{end}", text))
        if wm is not None:
            if wm["last_read_hash"] == digest:
                return f"[UNCHANGED since last read: {rel_path} {wm['last_read_lines']}]"
            diff = difflib.unified_diff(wm["snapshot_text"].splitlines(), lines, rel_path, rel_path, lineterm="", n=1)
            return "\n".join(diff)
        return "\n".join(f"{i}: {l}" for i, l in enumerate(lines[start - 1:end], start)) + \
            (f"\n[... {len(lines) - end} more lines]" if end < len(lines) else "")

    # ---- §6.1 #5 explicit session state ----
    def set_state(self, key: str, value) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO session_state VALUES (?,?,?)",
                            (key, json.dumps(value, ensure_ascii=False), _now()))

    def get_state(self, key: str, default=None):
        r = self.db.execute("SELECT state_json FROM session_state WHERE state_key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def state_prompt(self) -> str:
        """O(1)-size state block injected in place of accumulated conversation history."""
        return "\n".join(f"{r[0]}: {r[1]}" for r in
                         self.db.execute("SELECT state_key, state_json FROM session_state ORDER BY state_key"))

    # ---- projection cache ----
    def cached_projection(self, qualified_name: str, h: str) -> dict | None:
        r = self.db.execute("SELECT declarative_json, contract_diff_status FROM semantic_projections "
                            "WHERE qualified_name=? AND ast_hash=?", (qualified_name, h)).fetchone()
        return {"declarative": json.loads(r[0]), "status": r[1]} if r else None

    def store_projection(self, qualified_name: str, h: str, declarative: dict, status: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO semantic_projections VALUES (?,?,?,?)",
                            (qualified_name, h, json.dumps(declarative, ensure_ascii=False), status))
