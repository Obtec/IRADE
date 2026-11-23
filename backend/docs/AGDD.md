# AGDD platform (Docker)

Implements [DOC.md](../../DOC.md) v2.0. Code is in `agent/irade_agent/agdd/`. The stack is defined in `docker-compose.agdd.yml`.

## Containers

| Service | DOC component | Image / module |
| :--- | :--- | :--- |
| `postgres` | Control Plane DB (PostgreSQL + pgvector) | `pgvector/pgvector:pg17`, schema `sql/control_plane.sql` (applied by `agdd-api`) |
| `postgres-replica` | Read Replica (streaming, slot `agdd_replica`) | same image, `pg_basebackup -R` on first start |
| `pgbouncer` | PgBouncer, transaction pooling (`agdd` = primary, `agdd_ro` = replica) | `docker/agdd/pgbouncer.Dockerfile` |
| `valkey` | heartbeats (TTL), log Streams, Cache-Aside, rate limits | `valkey/valkey:8.1` |
| `object-storage` | S3 object storage (SHA-256 keys) | `chrislusf/seaweedfs:3.97` (MinIO's public images are no longer published) |
| `test-db` | Ephemeral Test RDB (tmpfs, one DB branch per VERIFY job) | `pgvector/pgvector:pg17` |
| `agdd-api` | API/Auth/RBAC, spec sync, Pre-Flight Gate, Entry Router, runs, HITL, UX overrides, SSE | `agdd.api` |
| `llm-gateway` | LLM Gateway Proxy: model aliases, budget 80%/100%, reasoning control, prefix normalization, batch clearing, accounting | `agdd.llm_gateway` |
| `research` | On-Demand Research Engine (sanitize → pgvector reuse → arXiv/Semantic Scholar/docs → Insight Brief) | `agdd.research_service` |
| `scheduler` | lease reaper, HITL digests 10:00/16:00, monthly partitions, detach + Parquet archive | `agdd.scheduler` |
| `worker` (scalable) | Isolated Agent Worker: SKIP LOCKED queue, local SQLite FTS5, semantic verify, watchdog, reporters, Playwright/VLM | `agdd.worker` |
| `analytics` (profile `phase3`) | OLAP: DuckDB over Parquet in object storage | `agdd.analytics` |

### Networks

- `agdd-data` and `agdd-sandbox` are `internal`: they have no route to the internet.
- Workers can reach the outside only through `llm-gateway` and `research`.
- To run workers under gVisor, set `AGDD_WORKER_RUNTIME=runsc`.

## Run

```bash
cd backend
cp agdd.env.example agdd.env   # fill in the secrets
docker compose -f docker-compose.agdd.yml --env-file agdd.env up -d --build
docker compose -f docker-compose.agdd.yml --env-file agdd.env up -d --scale worker=4
docker compose -f docker-compose.agdd.yml --env-file agdd.env --profile phase3 run --rm analytics
```

- The API listens on `127.0.0.1:${AGDD_API_PORT:-18091}`.
- Each request must carry `Authorization: Bearer $AGDD_API_TOKEN` and `X-User-Id: <uuid>`. The upstream auth gateway sets these.
- Roles are read from `workspace_members` (`VIEWER < EDITOR < APPROVER < OWNER`).

## Try it (web console)

1. Open `http://127.0.0.1:18091/`. In VS Code Remote, forward port 18091 first.
2. Paste `AGDD_API_TOKEN` from `backend/agdd.env` into the token field and click 저장. The browser generates a user id and keeps it in localStorage.
3. Create a workspace. Then, in this order:
   - **명세 테이블:** load the example specs.
   - **사전 검사:** run the gate.
   - **그래프:** publish the default template.
   - **실행:** start a run with repo_key `demo`.
4. Each repository must be a git checkout under `backend/agdd-workspace/<repo_key>`.
   - `demo` is a small buggy module whose tests fail until `add` is fixed and `mean([])` raises.
   - Reset it with `git -C backend/agdd-workspace/demo checkout -- .`.

### Offline browser libraries

The console's terminal (xterm) and editor (Monaco) normally load from jsDelivr. The `agdd-api` image bundles pinned copies at build time, verified against SHA-512 integrity values. For a local checkout, fetch them once:

```bash
cd backend/agent && python -m irade_agent.agdd.fetch_vendor   # -> irade_agent/agdd/static/vendor/ (gitignored)
```

When a package is present, the API rewrites its CDN URLs in `console.html` to `static/vendor/<pkg>@<ver>/`; missing packages keep using the CDN. `AGDD_VENDOR_DIR` overrides the location. Build with `--build-arg AGDD_BUNDLE_VENDOR=0` to skip bundling.

## Workspace VMs and API agents

Each workspace gets its own Cloud Hypervisor VM from the Project Agora runtime ([hypervisor-for-agora](../../../hypervisor-for-agora/README.md)). With the `vm` engine (the pre-API name `vm-cli` is still accepted), repositories, tests and the coding agent's tools run inside that VM, and every model call goes through the LLM Gateway with the server's API key.

### How AGDD drives the VM

- AGDD talks to the root-owned runtime manager only through its existing socket protocol. The manager itself is not modified.
- VM ids come from a reserved range, `AGDD_VM_ID_BASE` (default 1,900,000,000) plus a sequence. This keeps them apart from Agora canvas ids and the 2147483xxx test ids.
- Automation runs commands in PTY sessions owned by owner 1 and named `agdd-<run>`. Results come back through files, start/end markers and base64.
- Members can watch the `agdd-*` sessions but cannot type into them. Agent PTYs mirror the agent's tool calls and command output (lines clipped to 400 columns) so the console's VM terminals and the multi-agent monitor show them live.

### What runs in the VM

- Repositories live at `/workspace/repos/<repo_key>`. Create the demo repo from the console, or `git clone` one in the terminal.
- Tests run in the VM.
- The coding agent (`agdd/api_agents.py`, `ApiAgent`) is a tool-calling loop over the LLM Gateway. Its tools — `list_files`, `read_file`, `grep`, `edit_file`, `write_file`, `run_command` (sandbox permission only) and `finish` — execute in the VM. Test files stay read-only.
- Structured steps (pre-flight, ambiguity, routing, semantic lifting, watchdog/advisor, HITL cards, think-mode team agents) are JSON-schema calls through the gateway; they need no PTY.
- Budget gating (80 % → cheap model, 100 % → HITL), per-workspace rate limits and cost accounting happen in the gateway (`x-agdd-workspace` / `x-agdd-run` / `x-agdd-node` headers).

### Guest environment (from Agora)

- The shell runs as UID 1000 with `HOME=/workspace`.
- Each shell is limited to 512 MiB, 128 processes and 1 CPU.
- A VM holds at most 8 PTY sessions.
- Outbound traffic is NAT; there are no inbound connections.
- `apt install` works for the user through the guest's package bridge; there is no sudo.
- Host-wide limits: at most 4 VMs and 8 GiB of VM RAM, shared with Agora canvases.

### Model access (API key)

There are no per-user logins. The operator configures the upstream once in `agdd.env`:

| Variable | Purpose |
| :--- | :--- |
| `AGDD_UPSTREAM_BASE_URL` | OpenAI-compatible upstream used by the LLM Gateway |
| `AGDD_UPSTREAM_API_KEY` | API key for that upstream (kept in the gateway; never sent to browsers or VMs) |
| `AGDD_MODEL_CHEAP` / `AGDD_MODEL_MID` / `AGDD_MODEL_ADVISOR` | Upstream models behind the `cheap` / `mid` / `advisor` aliases |
| `AGDD_MODEL_PRICES` | Optional per-model USD prices for cost accounting |

Subscription coding CLIs (consumer-plan logins) are not used: their consumer terms do not cover automated use inside a multi-user hosted service, so all agents use the API key above.

### Per-workspace settings

These are set in the console's **에이전트 · 모델 API** tab and can be overridden for a single run:

- **Model:** a gateway alias (`cheap`, `mid`, `advisor`) or an upstream model name (default `mid`)
- **Effort:** low / medium / high / xhigh / max (sent as `reasoning_effort`; xhigh/max map to `high`)
- **Session:**
  - `new`: a fresh context on every attempt
  - `per-run`: retries resume the same agent conversation
  - an explicit session id: continue a saved conversation
  - Conversations are stored as transcripts in the VM (`~/.agdd/sessions/<id>.json`), so any worker can resume them; old tool output is cleared above 120k tokens.
- **Permission:**
  - `sandbox`: file edits and shell commands inside the VM
  - `ask-edits`: file edits only (no `run_command` tool)
- **Cheap-step model and effort:** used for the structured steps (default `cheap` / low)

### Usage

The **사용량 (7일)** button shows the gateway-recorded 7-day tally per model (`node_executions`) and this month's workspace budget (`GET /workspaces/{ws}/model-usage`). `GET /workspaces/{ws}/agent-settings` also reports the gateway's health and alias map (`gateway.api_key_configured` tells whether an upstream key is set; the key itself is never returned).

### Verified on a real VM (2026-11-21)

- **Guest image:**
  - Debian 13 with Node 20 and Python 3.12.
  - git is not installed. The demo repo adds it first through `apt`.
  - The 6 GiB default disk had about 100 MB free. Set the VM disk to 12 GiB before cloning larger repositories.
- **Host disk:**
  - About 23 GB was free (95% used).
  - Agora refuses to create or grow a disk unless 8 GiB stays free on the host, so only one or two more 12 GiB VMs fit.
- **Failures:** a failed model API call raises `AgentError` (HTTP 502 from the console API; a gateway budget halt is HTTP 402). Structured steps never treat a failed call as an empty answer.

### Enabling it

```bash
docker compose -f docker-compose.agdd.yml -f docker-compose.agdd-vm.yml --env-file agdd.env up -d
```

The override grants `agdd-api` and `worker` the same manager-socket access the Spring backend has: a bind mount of `/run/agora-runtime` and supplementary group 10001. Without it, the stack uses the gateway engine.

To test without real VMs, `tests/fake_runtime.py` emulates the manager's terminal protocol with local PTYs.

## Multi-agent teams (canvas)

Console tab **멀티 에이전트**: design a team as a node graph and run it. Engine: `agdd/multi_agent.py`
(pure, unit tested); persistence + VM wiring: `agdd/multi_agent_service.py`; UI: `static/multi_agent.js`.
The canvas is resizable (drag bar under it, height remembered) and has a full-screen mode (⛶, Esc closes).

- **Nodes**: `input` (the request + optional default attachments), `data` (attached files, spec scope, UX 2D
  layout, repo files, notebook cells, collab history, fixed text; resolved once at run start), `agent`, `output`.
- **Agent settings**: role, default system prompt, input template (`{{auto}}`, `{{request}}`, `{{attachments}}`,
  `{{data}}`, `{{agents}}`, `{{subtasks}}`, `{{feedback}}`, `{{messages}}`, `{{blackboard}}`, `{{state}}`,
  `{{state:<key>}}`, `{{input:<id>}}`), accepted input kinds (request/data/agent/feedback/message), accepted
  media `modalities` (text/image/video/audio/pdf/file; `[]` = no media), wait-for-all vs. react-to-any,
  `think` (JSON answer) or `code` (autonomous run in the repo), model/effort overrides, `max_runs`, who it
  may message, `output_key` (shared state), `guardrail`.
- **Edges** (`when`):
  - `always`, `approve`, `revise` — a `revise` edge back to an earlier agent is a review / evaluator-optimizer
    loop; the target gets the reviewer's output as feedback and runs again.
  - `handoff` — fires only when the agent picks that target in `handoff` (code mode: `<<HANDOFF:id>>`); the
    conversation and its attachments move to the target and the agent's other edges do not fire.
  - `spawn` — orchestrator → worker. The orchestrator returns `tasks: [{agent, task}]` (code mode:
    `<<TASK:id>>…<</TASK>>`, max 8 per turn); each task runs on the worker in parallel (own VM PTY) and the batch
    comes back to the orchestrator as one `subtask` input. A turn with tasks is intermediate: the orchestrator's
    edges fire with its final (task-free) answer. Workers only take spawn/input/data edges and have no outgoing
    edges (their result always returns to the orchestrator).
- **Shared state**: an agent with `output_key` writes its answer to `state[key]`; prompts, templates and system
  prompts read it with `{{state:key}}`. The final state is in `result_json.state`.
- **Guardrails**: a `guardrail` agent answers `tripwire` / `tripwire_reason` (code mode: `<<TRIPWIRE:reason>>`).
  A tripped guardrail halts the run with status `BLOCKED` (`result_json.blocked`).
- **Pattern templates** (toolbar select, `PRESET_INFO`): Anthropic orchestrator-workers (lead + sub-agents +
  citation), evaluator-optimizer, voting, sectioning; OpenAI Agents SDK triage with handoffs and input/output
  guardrails; Google ADK sequential pipeline with `output_key` state, Parallel/Loop equivalents; plus review loop,
  debate and a multimodal analysis team.
- **Collaboration**: agents send direct messages to teammates (`messages` in think mode,
  `<<TO:id>>…<</TO>>` in code mode). A message re-activates the recipient, so agents ask and answer each other.
  Runs are bounded by per-agent `max_runs` and the team's `max_steps` (worker tasks count as steps).
- **Multimodal inputs**: files (images, video, audio, PDF, text/code, other) are uploaded in 4 MiB chunks
  (stays under the gateway's 8 MiB body limit; max 50 MB per file, 2 GB per workspace) and can be attached to a
  run, to an input node (default attachments) or to a `files` data node. Items keep their attachments as they
  flow through edges and handoffs; each agent only receives its `modalities`. At run start every attachment is
  streamed into the VM once (`~/.agdd/attachments/<sha256>/`, cached) and `agdd_media.py` derives PDF text +
  page images (poppler), video key frames + audio track (ffmpeg) and transcripts (only if `whisper` is installed);
  missing tools are apt-installed best effort (`AGDD_MEDIA_INSTALL=0` disables that). Delivery to the model:
  images (originals, PDF pages, video frames; ≤ 5 MB each, max 12) go in as image parts, text files inline, and
  the VM paths of everything else are listed so the agent's tools can inspect them. Without a VM, images are sent
  to the gateway as image parts (`AGDD_VISION_MODEL`) and text files as inline excerpts.
- **Execution**: a background thread in agdd-api; every model call goes through the LLM Gateway. Each agent gets
  its own VM PTY for its tools (`agdd-ma-<id>`, workers `agdd-ma-<id>-t<n>`; watch them live in the 에이전트 모니터
  panel), so independent agents run in parallel (`parallelism`); code-mode agents share the repo
  and run one at a time. Without a VM, think agents use the LLM gateway and code agents fail with a clear error.
  A run left `RUNNING` by an API restart is marked `FAILED` when it is next read.
- **API**: `GET/POST /workspaces/{ws}/teams`, `POST …/teams/validate`, `DELETE …/teams/{id}`,
  `POST/GET …/team-runs` (start body: `team_id`, `graph`, `request`, `attachments: [ids]`),
  `GET …/team-runs/{id}?after=<seq>` (events), `POST …/team-runs/{id}/cancel`;
  attachments: `GET/POST …/team-attachments` (init: `name`, `mime`, `size`), `POST …/team-attachments/{id}/chunks/{n}`
  (`data`: base64), `POST …/team-attachments/{id}/complete`, `GET …/team-attachments/{id}/raw`, `DELETE …/team-attachments/{id}`.
- **Tables**: `agent_teams`, `agent_team_runs` (+ `attachments`), `agent_team_events`, `agent_team_attachments`,
  `agent_team_attachment_chunks`.

## Provider notes (EXAONE)

- The provider allows 1 request per second. The gateway serializes upstream calls and retries 429 responses (`UPSTREAM_MIN_INTERVAL`, `UPSTREAM_MAX_RETRIES`).
- With `json_schema` constrained decoding, EXAONE's output degenerates into whitespace. Instead, the gateway sends `json_object` with the schema in the prompt (`STRUCTURED_OUTPUT_MODE=json_object`).
- `reasoning_effort=minimal` is rejected, so the gateway uses `low`. Even at `low` the model reasons a lot: expect 20–100 s per cheap call and 1–2 min for a pre-flight gate.
- Costs use the fallback prices ($1 input / $0.10 cached / $4 output per 1M tokens) until `AGDD_MODEL_PRICES` is set. Budget gates are only as accurate as those prices.

## DEV agent

The DEV node runs a tool-calling loop (`dev_agent.py`, up to `max_steps`, default 24) through the gateway.

Tools: `search_symbols`, `read_symbol`, `read_file`, `list_files`, `grep`, `apply_patch`, `run_tests`, `search_outputs`, `note`, `finish`.

Each restriction is enforced by the tools themselves:
- whole-file reads are capped at 150 lines; a re-read within the same attempt returns only the diff
- every tool output passes through the output parser and the 2,500-token cap; raw output is kept in FTS5
- edits are accepted only as unified diffs
- patches to protected test files are rejected

Each retry starts with a fresh model context. It carries over only the handoff (feedback, advisor guidance, Insight Brief) and the `note` facts.

## API

| Method | Path | Role |
| :--- | :--- | :--- |
| GET | `/workspaces/{ws}/specs` | VIEWER |
| PUT | `/workspaces/{ws}/specs/{SRS\|ARC\|SCH}`, `/workspaces/{ws}/trace` | EDITOR (each write stores a new snapshot) |
| POST | `/workspaces/{ws}/preflight` `{semantic?, ambiguity?}` | EDITOR |
| POST | `/workspaces/{ws}/graphs` `{definition, graph_id?}` | EDITOR |
| POST | `/workspaces/{ws}/runs` `{graph_id, request?, repo_key?}` | EDITOR (returns 409 unless the latest snapshot passed the gate) |
| GET | `/runs/{id}`, `/runs/{id}/logs` (SSE) | VIEWER |
| GET | `/workspaces/{ws}/hitl` | VIEWER |
| POST | `/hitl/{id}/decide` `{status}` | APPROVER |
| GET/PUT | `/workspaces/{ws}/ux/{view}` | EDITOR to write |
| POST | `/workspaces/{ws}/ux/{view}/unlock` | EDITOR |
| POST | `/workspaces/{ws}/research` `{problem}` | EDITOR |
| GET | `/workspaces/{ws}/dashboard` | VIEWER |
| PUT | `/workspaces/{ws}/budget` | OWNER |

## Graph nodes

Node kinds: `SPEC`, `AMBIGUITY_GATE`, `DEV`, `SEMANTIC_VERIFY`, `VERIFY`, `AUDIT`, `HITL`.

Optional fields on a node:

| Field | Meaning |
| :--- | :--- |
| `domain` | narrows the spec scope the node receives |
| `allowed_mcp` | MCP tool allowlist for the node |
| `max_retries` | retry limit before escalation |
| `command`, `reports`, `budgets`, `migrations` | VERIFY: test command, standard reporter files, numeric budgets, migrations to apply and roll back |
| `ux_views`, `preview_url` | AUDIT: views with locked UX overrides, page to screenshot |
| `public_api` | DEV: changes to this node's code count as API contract changes |
| `reversibility` | HITL: `REVERSIBLE` or `IRREVERSIBLE` |

On the graph itself, `fast_track_entry` names the node that FAST_TRACK runs start from.

The worker expects repositories at `${AGDD_WORKSPACE_DIR}/{repo_key}`.
