"""Semantic cache with schema_hash cross-check (DOC.md §4.1 semantic_cache, §6.1 #6).

A vector hit is only accepted if the task_type and schema_hash match exactly,
which prevents returning a cached answer computed against a different schema.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable

from .research import cosine

Embed = Callable[[str], list[float]]

PG_LOOKUP_SQL = """
SELECT response_payload, 1 - (embedding <=> %(emb)s::vector) AS sim
  FROM semantic_cache
 WHERE workspace_id = %(ws)s AND task_type = %(task)s AND schema_hash = %(schema_hash)s
 ORDER BY embedding <=> %(emb)s::vector
 LIMIT 1;
"""


def schema_hash(schema: dict | str) -> str:
    text = schema if isinstance(schema, str) else json.dumps(schema, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


class SemanticCache:
    def __init__(self, embed: Embed, threshold: float = 0.95):
        self.embed = embed
        self.threshold = threshold
        self.rows: list[dict] = []

    def get(self, workspace_id: str, task_type: str, prompt: str, sh: str):
        emb = self.embed(prompt)
        best, best_sim = None, -1.0
        for r in self.rows:
            if (r["workspace_id"], r["task_type"], r["schema_hash"]) != (workspace_id, task_type, sh):
                continue
            s = cosine(emb, r["embedding"])
            if s > best_sim:
                best, best_sim = r, s
        return best["response_payload"] if best and best_sim >= self.threshold else None

    def put(self, workspace_id: str, task_type: str, prompt: str, sh: str, payload) -> None:
        self.rows.append({"workspace_id": workspace_id, "task_type": task_type, "schema_hash": sh,
                          "embedding": self.embed(prompt), "response_payload": payload})

    def wrap(self, workspace_id: str, task_type: str, llm: Callable[[str, dict], dict]):
        """Decorate a cheap-model callable so identical-schema near-duplicate prompts skip the model."""
        def call(prompt, schema):
            sh = schema_hash(schema)
            hit = self.get(workspace_id, task_type, prompt, sh)
            if hit is not None:
                return hit
            out = llm(prompt, schema)
            self.put(workspace_id, task_type, prompt, sh, out)
            return out
        return call
