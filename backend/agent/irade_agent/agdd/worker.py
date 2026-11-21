"""Isolated Agent Worker (DOC.md §2 sandbox, §4.1 3-step short transactions, §5.2-5.7).

Loop: (1) claim job with SKIP LOCKED + commit -> (2) execute node with no open DB
connection -> (3) optimistic-lock commit of run state, cost, history and next job.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from psycopg.types.json import Jsonb

from . import gateway, hitl, infra, orchestrator, pgstore, reporters, semantic_verify, watchdog
from .ux_workbench import Workbench, audit_patch
from .worker_db import WorkerDB

WORKER_ID = os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
REPO_ROOT = Path(os.environ.get("REPO_ROOT", "/workspace"))
SANDBOX_DB = os.environ.get("WORKER_SQLITE", "/tmp/agdd-worker.sqlite")
LEASE = os.environ.get("JOB_LEASE", "10 minutes")
GOD_FILE_LINES, MICRO_FILE_LINES = 1000, 60

def sh(args: list[str], cwd: Path, timeout: int = 900, env: dict | None = None) -> tuple[int, str]:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, **(env or {})})
    return p.returncode, (p.stdout + p.stderr)


def log(run_id, msg):
    print(f"[worker {WORKER_ID}] {run_id} {msg}", flush=True)
    try:
        infra.log_stream(str(run_id), msg)
    except Exception:
        pass


def chat(model_alias: str, task: str, messages: list[dict], schema: dict | None, run_id, node, ws):
    from openai import OpenAI

    client = OpenAI(base_url=infra.env("LLM_GATEWAY_URL", "http://llm-gateway:8090/v1"),
                    api_key=infra.env("LLM_GATEWAY_TOKEN", "internal"))
    kwargs = {"response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}}} if schema else {}
    r = client.chat.completions.create(model=model_alias, messages=messages,
                                       extra_headers={"x-agdd-task": task, "x-agdd-run": str(run_id),
                                                      "x-agdd-node": node, "x-agdd-workspace": str(ws)}, **kwargs)
    text = r.choices[0].message.content or ""
    return json.loads(text) if schema else text


class Ctx:
    def __init__(self, job, run, graph, ws, scope):
        self.job, self.run, self.graph, self.ws, self.scope = job, run, graph, ws, scope
        self.node = next(n for n in graph["nodes"] if n["name"] == job["node_name"])
        self.handoff = dict(run["handoff_json"] or {})
        self.repo = REPO_ROOT / (self.handoff.get("repo_key") or "")
        self.hitl_requests: list[dict] = []


# ---------------- node handlers: return (ok, handoff_patch) ----------------
def node_ambiguity(ctx: Ctx):
    from .router import ambiguity_gate

    qs = ambiguity_gate(ctx.scope["requirements"], infra.cheap_model("ambiguity", ctx.ws, ctx.run["run_id"], ctx.node["name"]))
    if qs:  # proceed with recommended defaults; decisions go to the digest (REVERSIBLE)
        ctx.hitl_requests.append({"id": "ambiguity", "reason_code": "AMBIGUITY_DEFAULTS", "reversibility": "REVERSIBLE",
                                  "files": [], "questions": qs})
    return True, {"assumed_defaults": [{"spec_id": q["spec_id"], "question": q["question"],
                                        "answer": q["options"][q["recommended"]]} for q in qs]}


def node_dev(ctx: Ctx, wdb: WorkerDB):
    """Tool-calling coding agent (dev_agent) running inside the sandbox against the repo working tree."""
    from openai import OpenAI

    from .dev_agent import DevTools, run_dev_agent

    if not (ctx.repo / ".git").exists():
        return False, {"feedback": f"repository not found at {ctx.repo} (set repo_key and clone it into the workspace)"}
    client = OpenAI(base_url=infra.env("LLM_GATEWAY_URL", "http://llm-gateway:8090/v1"),
                    api_key=infra.env("LLM_GATEWAY_TOKEN", "internal"), timeout=600)
    model = "advisor" if ctx.handoff.get("escalate_model") else "mid"
    headers = {"x-agdd-task": "implement", "x-agdd-run": str(ctx.run["run_id"]), "x-agdd-node": ctx.node["name"],
               "x-agdd-workspace": str(ctx.ws)}

    def call_model(messages, tools):
        r = client.chat.completions.create(model=model, messages=messages, tools=tools, extra_headers=headers)
        m = r.choices[0].message
        return {"content": m.content, "tool_calls": [
            {"id": t.id, "type": "function", "function": {"name": t.function.name, "arguments": t.function.arguments}}
            for t in (m.tool_calls or [])]}

    task = {"request": ctx.handoff.get("request"), "scope": ctx.scope,
            "assumed_defaults": ctx.handoff.get("assumed_defaults", []),
            "previous_failure_feedback": ctx.handoff.get("feedback", ""),
            "advisor_guidance": ctx.handoff.get("advisor", ""), "insight_brief": ctx.handoff.get("insight_brief", "")}
    rules = (ctx.repo / "AGENTS.md").read_text(encoding="utf-8")[:12000] if (ctx.repo / "AGENTS.md").exists() else ""
    tools = DevTools(ctx.repo, wdb, ctx.node.get("command") or ["python", "-m", "pytest", "-q"])
    res = run_dev_agent(call_model, tools, task, rules, int(ctx.node.get("max_steps", 24)),
                        log=lambda m: log(ctx.run["run_id"], m))
    rc, diff = sh(["git", "diff", "HEAD"], ctx.repo)
    _, untracked = sh(["git", "ls-files", "--others", "--exclude-standard"], ctx.repo)
    changed = sorted(set(res.changed_files) | {f for f in untracked.split() if not f.startswith(".agdd/")})
    if not res.ok:
        return False, {"feedback": f"DEV agent did not finish: {res.summary}", "last_patch": diff[-20000:],
                       "changed_files": changed}
    rev, code = hitl.classify("DEV_CHANGE", diff, bool(ctx.node.get("public_api")))
    if rev == "IRREVERSIBLE":
        ctx.hitl_requests.append({"id": "dev", "reason_code": code, "reversibility": rev, "files": changed,
                                  "diff_excerpt": diff[:3000]})
    wdb.set_state("last_dev_summary", res.summary)
    return True, {"last_patch": diff[-20000:], "changed_files": changed, "feedback": "", "dev_summary": res.summary}


def node_semantic_verify(ctx: Ctx, wdb: WorkerDB):
    files = [f for f in ctx.handoff.get("changed_files", []) if f.endswith(".py") and (ctx.repo / f).exists()]
    if not files:
        return True, {}
    wdb.root = ctx.repo
    repo_key = ctx.handoff.get("repo_key", "")
    with infra.pg(readonly=True) as c:
        contracts = pgstore.load_contracts(c, repo_key)
    verified = []
    rep = semantic_verify.verify_changed(wdb, files, contracts, infra.cheap_model("semantic_lift", ctx.ws, ctx.run["run_id"], ctx.node["name"]),
                                         on_verified=lambda q, h: verified.append((q, h)))
    if verified:
        with infra.pg() as c:
            for q, h in verified:
                pgstore.mark_verified(c, repo_key, q, h)
            c.commit()
    if not rep.ok:
        return False, {"feedback": "SEMANTIC_VERIFY drift:\n" + semantic_verify.dev_feedback(rep)}
    return True, {"semantic": {"lifted": rep.lifted, "cached": len(rep.cached)}}


def node_verify(ctx: Ctx, wdb: WorkerDB):
    """Runs the configured command against a throwaway DB branch (Ephemeral Test RDB)."""
    cmd = ctx.node.get("command") or ["pytest", "-q", "--junitxml=.agdd/junit.xml"]
    branch = f"verify_{ctx.job['job_id']}"
    admin = os.environ.get("TEST_DB_ADMIN_DSN")
    env = {}
    if admin:
        import psycopg
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{branch}" TEMPLATE {os.environ.get("TEST_DB_TEMPLATE", "template0")}')
        env["DATABASE_URL"] = admin.rsplit("/", 1)[0] + f"/{branch}"
    try:
        (ctx.repo / ".agdd").mkdir(exist_ok=True)
        for m in ctx.node.get("migrations", []):  # apply + rollback check
            for step in ("up", "down", "up"):
                rc, out = sh(m[step], ctx.repo, env=env)
                if rc != 0:
                    return False, {"feedback": gateway.parse_tool_output(f"migration {step}", rc, out, _offload(wdb))}
        rc, out = sh(cmd, ctx.repo, timeout=int(ctx.node.get("timeout", 1800)), env=env)
        reports = {}
        for kind, rel in (ctx.node.get("reports") or {"junit": ".agdd/junit.xml"}).items():
            p = ctx.repo / rel
            if p.exists():
                reports[kind] = p.read_text(encoding="utf-8")
        b = ctx.node.get("budgets", {})
        res = reporters.collect(reports, reporters.Budgets(b.get("p95_ms"), b.get("explain_cost"), b.get("bundle_bytes")))
    finally:
        if admin:
            import psycopg
            with psycopg.connect(admin, autocommit=True) as c:
                c.execute(f'DROP DATABASE IF EXISTS "{branch}" WITH (FORCE)')
    ok = rc == 0 and res.passed
    summary = gateway.parse_tool_output(" ".join(cmd), 0 if ok else (rc or 1), out + "\n" + res.summary(), _offload(wdb))
    return ok, {"feedback": "" if ok else summary, "verify_summary": summary}


def _offload(wdb):
    return lambda cmd, text: wdb.offload("shell", cmd, text)


def node_audit(ctx: Ctx, wdb: WorkerDB):
    patch = ctx.handoff.get("last_patch", "")
    problems, warnings = [], []
    for view in ctx.node.get("ux_views", []):
        res = audit_patch(Workbench(pgstore.PgOverrideStore()), ctx.ws, view, patch)
        if not res.ok:
            problems.append(f"locked UX selectors modified in {view}: {res.blocked}")
    for f in ctx.handoff.get("changed_files", []):
        p = ctx.repo / f
        if p.exists():
            n = sum(1 for _ in p.open(encoding="utf-8", errors="ignore"))
            if n >= GOD_FILE_LINES:
                warnings.append(f"{f}: {n} lines (God File; split to 150~350)")
            elif n <= MICRO_FILE_LINES:
                warnings.append(f"{f}: {n} lines (over-split)")
    if ctx.node.get("preview_url"):
        from . import vlm_screen
        defects = vlm_screen.screen(vlm_screen.capture(ctx.node["preview_url"]), None)
        if defects:
            problems.append("UI defects: " + json.dumps(defects[:10], ensure_ascii=False))
    if problems:
        return False, {"feedback": "AUDIT:\n" + "\n".join(problems), "audit_warnings": warnings}
    return True, {"audit_warnings": warnings}


def node_hitl(ctx: Ctx, wdb):
    ctx.hitl_requests.append({"id": "gate", "reason_code": ctx.node.get("reason_code", "MANUAL_GATE"),
                              "reversibility": ctx.node.get("reversibility", "IRREVERSIBLE"),
                              "files": ctx.handoff.get("changed_files", [])})
    return True, {}


HANDLERS = {"AMBIGUITY_GATE": lambda c, w: node_ambiguity(c), "DEV": node_dev,
            "SEMANTIC_VERIFY": node_semantic_verify, "VERIFY": node_verify, "AUDIT": node_audit,
            "HITL": node_hitl, "SPEC": lambda c, w: (True, {})}


# ---------------- escalation ----------------
def escalate(ctx: Ctx, verdict: watchdog.Verdict) -> dict:
    if verdict.escalate_to == "RESEARCH":
        import urllib.request
        req = urllib.request.Request(os.environ.get("RESEARCH_URL", "http://research:8092") + "/research",
                                     data=json.dumps({"problem": ctx.handoff.get("feedback", ""),
                                                      "trigger_source": "WATCHDOG", "run_id": str(ctx.run["run_id"]),
                                                      "workspace_id": str(ctx.ws)}).encode(),
                                     headers={"content-type": "application/json",
                                              "authorization": f"Bearer {os.environ.get('INTERNAL_TOKEN', '')}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return {"insight_brief": json.loads(r.read())["insight_brief_md"]}
        except Exception as e:
            log(ctx.run["run_id"], f"research failed: {e!r}")
            return {}
    if verdict.escalate_to == "ADVISOR" and getattr(ctx, "vmx", None):
        v = ctx.vmx  # advisor = same CLI at the highest effort, structured 400~700-token guidance
        v.agent.s.cheap_effort = "max"
        out = v.llm("advise")("상위 Advisor입니다. 400~700토큰 이내 구체적 수정 지침만 주세요.\n" + json.dumps(
            {"verdict": verdict.reason, "feedback": ctx.handoff.get("feedback", "")[-4000:],
             "patch": ctx.handoff.get("last_patch", "")[-6000:]}, ensure_ascii=False),
            {"type": "object", "properties": {"advice": {"type": "string"}}, "required": ["advice"]})
        return {"advisor": str(out.get("advice", ""))[:4000]}
    if verdict.escalate_to == "ADVISOR":
        advice = chat("advisor", "advise", [
            {"role": "system", "content": "상위 Advisor입니다. 400~700토큰 이내 구체적 수정 지침만 주세요."},
            {"role": "user", "content": json.dumps({"verdict": verdict.reason,
                                                    "feedback": ctx.handoff.get("feedback", "")[-4000:],
                                                    "patch": ctx.handoff.get("last_patch", "")[-6000:]},
                                                   ensure_ascii=False)}],
            None, ctx.run["run_id"], ctx.node["name"], ctx.ws)
        return {"advisor": advice[:4000]}
    ctx.hitl_requests.append({"id": "watchdog", "reason_code": f"WATCHDOG_{verdict.verdict}",
                              "reversibility": "IRREVERSIBLE", "files": ctx.handoff.get("changed_files", []),
                              "reason": verdict.reason})
    return {}


# ---------------- main loop ----------------
def process(job: dict, wdb: WorkerDB) -> None:
    with infra.pg(readonly=False) as c:
        run = c.execute("SELECT * FROM workflow_runs WHERE run_id=%s", (job["run_id"],)).fetchone()
        g = c.execute("SELECT workspace_id, definition_json FROM graph_definitions WHERE graph_id=%s AND version=%s",
                      (run["graph_id"], run["graph_version"])).fetchone()
        snap = c.execute("SELECT * FROM spec_snapshots WHERE snapshot_id=%s", (run["snapshot_id"],)).fetchone()
        c.commit()
    from .spec_engine import SpecEngine
    eng = SpecEngine.hydrate({"srs": snap["srs_json"], "arc": snap["arc_json"], "sch": snap["sch_json"],
                              "trace": snap["trace_json"]})
    graph = g["definition_json"]
    node_def = next(n for n in graph["nodes"] if n["name"] == job["node_name"])
    ctx = Ctx(job, run, graph, g["workspace_id"], eng.executable_scope(node_def.get("domain")))
    eng.close()
    log(run["run_id"], f"node {job['node_name']} attempt {job['attempt_no']}")

    vmx = None
    try:
        if ctx.handoff.get("engine") == "vm-cli":  # every step runs inside the workspace VM via the chosen CLI
            from . import worker_vm
            vmx = worker_vm.VmCtx(ctx)
            ok, patch = worker_vm.handlers(lambda m: log(run["run_id"], m))[ctx.node["kind"]](ctx, vmx)
        else:
            ok, patch = HANDLERS[ctx.node["kind"]](ctx, wdb)
    except Exception as e:
        ok, patch = False, {"feedback": f"node crashed: {e!r}"[:2000]}

    def cheap(task):
        return vmx.llm(task) if vmx else infra.cheap_model(task, ctx.ws, ctx.run["run_id"], ctx.node["name"])
    ctx.cheap = cheap
    ctx.vmx = vmx
    handoff = {**ctx.handoff, **patch}

    # Stateless watchdog on Dev -> Verify loops (history kept in handoff, never in the model prompt)
    wd_hist = handoff.get("wd", [])[-3:]
    if ctx.node["kind"] in ("VERIFY", "SEMANTIC_VERIFY"):
        wd = watchdog.Watchdog(cheap("watchdog"), history=list(wd_hist))
        try:
            verdict = wd.record(ok, handoff.get("last_patch", ""), handoff.get("feedback", ""))
        except Exception as e:  # model unavailable: deterministic heuristics only
            log(run["run_id"], f"watchdog model unavailable: {e!r:.200}")
            wd.llm = None
            verdict = wd.evaluate()
        handoff["wd"] = wd.history[-3:]
        if verdict.verdict != "CONTINUE":
            log(run["run_id"], f"watchdog {verdict.verdict}: {verdict.reason}")
            handoff.update(escalate(ctx, verdict))
            if verdict.escalate_to == "ADVISOR":
                handoff["escalate_model"] = True

    graph_run = orchestrator.GraphRun(graph, _NullSpec())
    graph_run.attempts = dict(handoff.get("attempts", {}))
    nxt = graph_run.next_node(ctx.node["name"], ok)
    handoff["attempts"] = graph_run.attempts

    try:
        cards = hitl.compress(ctx.hitl_requests, cheap("summarize")) if ctx.hitl_requests else []
    except Exception as e:  # uncompressed cards still stop the run safely
        log(run["run_id"], f"HITL card model unavailable: {e!r:.200}")
        cards = hitl.compress(ctx.hitl_requests, lambda p, sch: {})
    dispatch = hitl.dispatch(cards, datetime.now())
    if dispatch.push_now:
        status, current = "WAITING_HITL", nxt or ctx.node["name"]
    elif nxt is None:
        status, current = ("SUCCEEDED" if ok else "ESCALATED"), ctx.node["name"]
    else:
        status, current = "RUNNING", nxt

    # (3) optimistic-lock commit: run state + HITL + next job atomically
    with infra.pg() as c:
        r = c.execute(orchestrator.COMMIT_RUN_SQL, {"next_node": current, "status": status,
                                                    "handoff": orchestrator.handoff_json(handoff),
                                                    "run_id": run["run_id"], "version": run["version"]}).fetchone()
        if r is None:
            c.rollback()
            log(run["run_id"], "version conflict; job will be retried")
            c.execute(orchestrator.FINISH_JOB_SQL, {"status": "PENDING", "job_id": job["job_id"]})
            c.commit()
            return
        for card in dispatch.push_now + dispatch.digest:
            c.execute("INSERT INTO hitl_decisions (run_id, reversibility, reason_code, status, decision_card_json, "
                      "digest_batch_id) VALUES (%s,%s,%s,'PENDING',%s,%s)",
                      (run["run_id"], card["reversibility"], card["request_ids"][0] or "HITL", Jsonb(card),
                       hitl.digest_batch_id(dispatch.digest_at) if card["reversibility"] == "REVERSIBLE" else None))
        c.execute(orchestrator.FINISH_JOB_SQL, {"status": "DONE" if ok else "FAILED", "job_id": job["job_id"]})
        if status == "RUNNING":
            c.execute(orchestrator.ENQUEUE_SQL, {"run_id": run["run_id"], "node_name": current, "priority": 5})
        c.commit()
    try:
        infra.cache_invalidate(f"agdd:run:{run['run_id']}", f"agdd:dash:{ctx.ws}")
    except Exception:
        pass
    log(run["run_id"], f"-> {status} {current} ok={ok}")
    if vmx is not None:
        vmx.shell.close()  # free the guest PTY slot (max 8 per VM); per-run sessions live in the CLI, not the PTY
    if status in ("SUCCEEDED", "ESCALATED", "FAILED"):  # run finished: drop its local SQLite
        path = wdb.db.execute("PRAGMA database_list").fetchone()[2]
        wdb.db.close()
        if path:
            Path(path).unlink(missing_ok=True)


class _NullSpec:
    def executable_scope(self, domain=None):
        return {}


def _heartbeat_loop(stop: threading.Event):
    while not stop.is_set():
        try:
            infra.heartbeat(WORKER_ID)
        except Exception:
            pass
        stop.wait(5)


def main():
    infra.wait_for_schema()
    stop = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(stop,), daemon=True).start()
    print(f"[worker {WORKER_ID}] ready", flush=True)
    idle = 0.5
    while True:
        with infra.pg() as c:  # (1) claim + commit immediately
            job = c.execute(orchestrator.CLAIM_JOB_SQL, {"lease": LEASE}).fetchone()
            c.commit()
        if not job:
            time.sleep(idle)
            idle = min(idle * 2, 5)
            continue
        idle = 0.5
        # (2) runs with the connection returned to the pool; one local SQLite per run (no cross-repo leakage)
        run_db = Path(SANDBOX_DB).with_name(f"agdd-{job['run_id']}.sqlite")
        process(job, WorkerDB(str(run_db), REPO_ROOT))


if __name__ == "__main__":
    main()
