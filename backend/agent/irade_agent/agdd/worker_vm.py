"""Graph node handlers that execute inside the workspace VM through coding-agent CLIs.

The worker container only orchestrates (queue, optimistic commits, HITL); repositories, tests and
every model call (claude / codex / agy) run in the workspace's Agora VM.
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from . import gateway, hitl, infra, pgstore, reporters, semantic_verify, vm_service, watchdog
from .ux_workbench import Workbench, audit_patch
from .worker_db import WorkerDB

GOD_FILE_LINES, MICRO_FILE_LINES = 1000, 60

DEV_PROMPT = """당신은 AGDD(자율 그래프 주도 개발) 파이프라인의 Dev 노드입니다. 저장소: {repo}
아래 작업 지시서(JSON)의 ACCEPT_NOW 명세 범위만 구현하세요.
규칙:
- 테스트 파일(tests/**, *.spec.ts, test_*.py 등)은 읽기 전용입니다. 절대 수정·삭제하지 마세요.
- 파일을 통째로 다시 쓰지 말고 필요한 부분만 수정하세요. 파일은 책임 단위 150~350줄을 유지하세요.
- 수정 후 테스트를 실행해 통과를 확인하세요: {test_cmd}
- 커밋하지 마세요. 작업이 끝나면 무엇을 바꿨는지와 테스트 결과를 3~6줄로 요약하세요.
- 범위 밖 작업이 필요하면 하지 말고 요약에 이유를 적으세요.

작업 지시서:
{task}
"""


class VmCtx:
    """Per-job VM facilities attached to the worker Ctx."""

    def __init__(self, ctx):
        self.ctx = ctx
        override = ctx.handoff.get("agent") or {}
        self.settings = vm_service.load_settings(ctx.ws, override)
        from .cli_agents import VmAgent
        from .vm import RuntimeClient, VmShell
        # a fresh PTY per job (not cached): closed after the job so guest PTY slots are not leaked
        self.agent = VmAgent(VmShell(RuntimeClient(), vm_service.canvas_for(ctx.ws), f"agdd-{str(ctx.run['run_id'])[:8]}"),
                             self.settings)
        self.shell = self.agent.shell
        self.repo = vm_service.repo_path(ctx.handoff.get("repo_key") or "")

    def llm(self, task_kind: str):
        def on_result(res, t0=[time.time()]):
            vm_service.record_usage(self.ctx.ws, self.ctx.run["run_id"], self.ctx.node["name"], self.settings, res,
                                    int((time.time() - t0[0]) * 1000), cheap=True)
            t0[0] = time.time()
        return self.agent.cheap_llm(on_result)

    def sh(self, cmd, timeout=600):
        return self.shell.run(f"cd {self.repo} && {cmd}", timeout=timeout)


def _require_repo(v: VmCtx):
    if v.shell.run(f"test -d {v.repo}/.git", timeout=30).exit_code != 0:
        return f"VM에 저장소가 없습니다: {v.repo} (VM 터미널에서 git clone 하세요)"
    return None


def node_ambiguity(ctx, v: VmCtx):
    from .router import ambiguity_gate
    qs = ambiguity_gate(ctx.scope["requirements"], v.llm("ambiguity"))
    if qs:
        ctx.hitl_requests.append({"id": "ambiguity", "reason_code": "AMBIGUITY_DEFAULTS", "reversibility": "REVERSIBLE",
                                  "files": [], "questions": qs})
    return True, {"assumed_defaults": [{"spec_id": q["spec_id"], "question": q["question"],
                                        "answer": q["options"][q["recommended"]]} for q in qs]}


def node_dev(ctx, v: VmCtx, log):
    err = _require_repo(v)
    if err:
        return False, {"feedback": err}
    s = v.settings
    # session policy: new = fresh context every attempt (DOC node reset), per-run = resume across retries,
    # explicit id = continue an existing CLI session chosen by the user
    session = None if s.session == "new" else (ctx.handoff.get("cli_session") if s.session == "per-run" else s.session)
    task = {"request": ctx.handoff.get("request"), "scope": ctx.scope,
            "assumed_defaults": ctx.handoff.get("assumed_defaults", []),
            "previous_failure_feedback": ctx.handoff.get("feedback", ""),
            "advisor_guidance": ctx.handoff.get("advisor", ""), "insight_brief": ctx.handoff.get("insight_brief", "")}
    test_cmd = " ".join(ctx.node.get("command") or ["python3", "-m", "pytest", "-q"])
    prompt = DEV_PROMPT.format(repo=v.repo, test_cmd=test_cmd, task=json.dumps(task, ensure_ascii=False, indent=1))
    if session and ctx.handoff.get("feedback"):
        prompt = "이전 시도가 실패했습니다. 피드백을 반영해 이어서 수정하세요.\n" + prompt
    log(f"DEV via {s.cli} model={s.model or 'default'} effort={s.effort} session={session or 'new'}")
    t0 = time.time()
    res = v.agent.agent(prompt, v.repo, session, timeout=int(ctx.node.get("timeout", 3600)),
                        on_progress=lambda: infra.heartbeat(f"run-{ctx.run['run_id']}"))
    vm_service.record_usage(ctx.ws, ctx.run["run_id"], ctx.node["name"], s, res, int((time.time() - t0) * 1000))
    patch = {"cli_session": res.session_id or session, "dev_summary": (res.text or "")[:3000]}
    diff = v.sh("git diff HEAD", 120).output
    status = v.sh("git status --porcelain", 60).output
    changed = sorted({l[3:].strip() for l in status.splitlines() if l[3:].strip() and not l[3:].startswith(".agdd")})
    protected = [f for f in changed if watchdog.is_protected(f)]
    if protected:  # restore read-only tests the agent touched (physical lock equivalent inside the VM)
        v.sh("git checkout -- " + " ".join(protected) + " 2>/dev/null; true", 60)
        return False, {**patch, "feedback": f"테스트 파일 수정은 금지입니다 (원복함): {protected}",
                       "last_patch": diff[-20000:], "changed_files": changed}
    if not res.ok:
        return False, {**patch, "feedback": f"{s.cli} 실행 실패: {(res.text or res.raw_tail)[-2000:]}",
                       "last_patch": diff[-20000:], "changed_files": changed}
    if not changed:
        return False, {**patch, "feedback": "변경 사항이 없습니다: " + (res.text or "")[:1000]}
    rev, code = hitl.classify("DEV_CHANGE", diff, bool(ctx.node.get("public_api")))
    if rev == "IRREVERSIBLE":
        ctx.hitl_requests.append({"id": "dev", "reason_code": code, "reversibility": rev, "files": changed,
                                  "diff_excerpt": diff[:3000]})
    return True, {**patch, "last_patch": diff[-20000:], "changed_files": changed, "feedback": ""}


def node_semantic_verify(ctx, v: VmCtx):
    files = [f for f in ctx.handoff.get("changed_files", []) if f.endswith(".py")]
    if not files:
        return True, {}
    with tempfile.TemporaryDirectory() as d:
        for f in files:  # mirror only the changed files; AST lifting runs locally, the model call runs in the VM
            try:
                data = v.shell.read_file(f"{v.repo}/{f}")
            except FileNotFoundError:
                continue
            (Path(d) / f).parent.mkdir(parents=True, exist_ok=True)
            (Path(d) / f).write_bytes(data)
        wdb = WorkerDB(root=d)
        repo_key = ctx.handoff.get("repo_key", "")
        with infra.pg(readonly=True) as c:
            contracts = pgstore.load_contracts(c, repo_key)
        verified = []
        present = [f for f in files if (Path(d) / f).exists()]
        rep = semantic_verify.verify_changed(wdb, present, contracts, v.llm("semantic_lift"),
                                             on_verified=lambda q, h: verified.append((q, h)))
    if verified:
        with infra.pg() as c:
            for q, h in verified:
                pgstore.mark_verified(c, repo_key, q, h)
            c.commit()
    if not rep.ok:
        return False, {"feedback": "SEMANTIC_VERIFY drift:\n" + semantic_verify.dev_feedback(rep)}
    return True, {"semantic": {"lifted": rep.lifted, "cached": len(rep.cached)}}


def node_verify(ctx, v: VmCtx):
    err = _require_repo(v)
    if err:
        return False, {"feedback": err}
    cmd = " ".join(ctx.node.get("command") or ["python3", "-m", "pytest", "-q", "--junitxml=.agdd/junit.xml"])
    v.sh("mkdir -p .agdd", 30)
    for m in ctx.node.get("migrations", []):
        for step in ("up", "down", "up"):
            r = v.sh(" ".join(m[step]), 900)
            if r.exit_code != 0:
                return False, {"feedback": gateway.parse_tool_output(f"migration {step}", r.exit_code, r.output)}
    r = v.sh(cmd, int(ctx.node.get("timeout", 1800)))
    reports = {}
    for kind, rel in (ctx.node.get("reports") or {"junit": ".agdd/junit.xml"}).items():
        try:
            reports[kind] = v.shell.read_file(f"{v.repo}/{rel}").decode(errors="replace")
        except FileNotFoundError:
            pass
    b = ctx.node.get("budgets", {})
    res = reporters.collect(reports, reporters.Budgets(b.get("p95_ms"), b.get("explain_cost"), b.get("bundle_bytes")))
    ok = r.exit_code == 0 and res.passed
    summary = gateway.parse_tool_output(cmd, 0 if ok else (r.exit_code or 1), r.output + "\n" + res.summary())
    return ok, {"feedback": "" if ok else summary, "verify_summary": summary}


def node_audit(ctx, v: VmCtx):
    patch = ctx.handoff.get("last_patch", "")
    problems, warnings = [], []
    for view in ctx.node.get("ux_views", []):
        res = audit_patch(Workbench(pgstore.PgOverrideStore()), ctx.ws, view, patch)
        if not res.ok:
            problems.append(f"locked UX selectors modified in {view}: {res.blocked}")
    files = ctx.handoff.get("changed_files", [])
    if files:
        out = v.sh("wc -l " + " ".join(f"'{f}'" for f in files) + " 2>/dev/null", 60).output
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1] != "total":
                n = int(parts[0])
                if n >= GOD_FILE_LINES:
                    warnings.append(f"{parts[1]}: {n} lines (God File; split to 150~350)")
                elif n <= MICRO_FILE_LINES:
                    warnings.append(f"{parts[1]}: {n} lines (over-split)")
    if problems:
        return False, {"feedback": "AUDIT:\n" + "\n".join(problems), "audit_warnings": warnings}
    return True, {"audit_warnings": warnings}


def node_hitl(ctx, v):
    ctx.hitl_requests.append({"id": "gate", "reason_code": ctx.node.get("reason_code", "MANUAL_GATE"),
                              "reversibility": ctx.node.get("reversibility", "IRREVERSIBLE"),
                              "files": ctx.handoff.get("changed_files", [])})
    return True, {}


def handlers(log):
    return {"AMBIGUITY_GATE": node_ambiguity, "DEV": lambda c, v: node_dev(c, v, log),
            "SEMANTIC_VERIFY": node_semantic_verify, "VERIFY": node_verify, "AUDIT": node_audit,
            "HITL": node_hitl, "SPEC": lambda c, v: (True, {})}
