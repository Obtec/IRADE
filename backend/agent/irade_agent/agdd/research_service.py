"""On-Demand Research Engine HTTP service (DOC.md §5.6). Internal only (behind INTERNAL_TOKEN)."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import infra, pgstore, research


def build_engine(workspace_id: str | None) -> research.ResearchEngine:
    providers = [research.ArxivProvider(), research.SemanticScholarProvider(os.environ.get("S2_API_KEY") or None)]
    if os.environ.get("DOCS_SEARCH_URL"):
        providers.append(research.DocSiteProvider(os.environ["DOCS_SEARCH_URL"]))
    names = [n for n in os.environ.get("INTERNAL_NAMES", "").split(",") if n.strip()]
    if workspace_id:
        with infra.pg(readonly=True) as c:
            names += [r["repo_key"] for r in c.execute(
                "SELECT DISTINCT repo_key FROM semantic_contracts WHERE workspace_id=%s", (workspace_id,))]
    return research.ResearchEngine(providers, infra.cheap_model("summarize", workspace_id), infra.embedder(),
                                   pgstore.PgResearchStore(), infra.put_object, names)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200 if self.path == "/healthz" else 404, {"ok": self.path == "/healthz"})

    def do_POST(self):
        if self.headers.get("authorization") != f"Bearer {os.environ.get('INTERNAL_TOKEN', '')}" or \
                not os.environ.get("INTERNAL_TOKEN"):
            return self._send(401, {"error": "unauthorized"})
        if self.path != "/research":
            return self._send(404, {"error": "not found"})
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        try:
            out = build_engine(body.get("workspace_id")).run(body["problem"], body.get("trigger_source", "HUMAN"),
                                                             body.get("run_id"))
        except ValueError as e:
            return self._send(422, {"error": str(e)})
        if body.get("run_id") and not out.get("reused"):
            with infra.pg() as c:  # feed back into resolved_facts for the session-reset memory
                ws = body.get("workspace_id")
                if ws:
                    c.execute("INSERT INTO resolved_facts (workspace_id, repo_key, fact_key, fact_value) "
                              "VALUES (%s,'_research',%s,%s) ON CONFLICT (workspace_id, repo_key, fact_key) "
                              "DO UPDATE SET fact_value=EXCLUDED.fact_value",
                              (ws, f"research:{out['research_id']}", out["insight_brief_md"][:4000]))
                    c.commit()
        self._send(200, out)


def main():
    infra.wait_for_pg()
    try:
        infra.ensure_bucket()
    except Exception as e:
        print(f"[agdd] object storage not ready: {e!r}", flush=True)
    port = int(os.environ.get("PORT", "8092"))
    print(f"[research] listening :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
