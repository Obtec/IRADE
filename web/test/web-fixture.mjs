import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { createServer } from 'node:https';
import { WebSocketServer } from 'ws';

const utf8Encoder = new TextEncoder();
const utf8Decoder = new TextDecoder('utf-8', { fatal: true });
const FNV_OFFSET = 14695981039346656037n;
const FNV_PRIME = 1099511628211n;
const FNV_MASK = 0xffffffffffffffffn;

function fnv1a64(text) {
	let hash = FNV_OFFSET;
	for (const byte of utf8Encoder.encode(text)) {
		hash ^= BigInt(byte);
		hash = (hash * FNV_PRIME) & FNV_MASK;
	}
	return hash.toString(10);
}

function applyTextDelta(before, edit) {
	if (edit.base && fnv1a64(before) !== edit.base) {
		throw new Error('CANVAS_TEXT_CONFLICT');
	}
	const bytes = utf8Encoder.encode(before);
	if (!Number.isInteger(edit.at) || !Number.isInteger(edit.remove)
		|| edit.at < 0 || edit.remove < 0 || edit.at + edit.remove > bytes.length) {
		throw new Error('CANVAS_TEXT_CONFLICT');
	}
	const head = utf8Decoder.decode(bytes.subarray(0, edit.at));
	const tail = utf8Decoder.decode(bytes.subarray(edit.at + edit.remove));
	return head + String(edit.insert ?? '') + tail;
}

/** Local HTTPS + ORCID + real-time item-based Workspace WebSocket fixture for Studio tests. */
export async function fixture({ studio } = {}) {
	let writable = true;
	let revision = 1;
	const items = new Map();
	const acceptedCounts = new Map();
	const temporary = await mkdtemp(join(tmpdir(), 'irade-web-fixture-'));
	execFileSync('openssl', [
		'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
		'-keyout', join(temporary, 'key.pem'),
		'-out', join(temporary, 'cert.pem'),
		'-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost',
	], { stdio: 'ignore' });

	const server = createServer({
		key: await readFile(join(temporary, 'key.pem')),
		cert: await readFile(join(temporary, 'cert.pem')),
	}, (request, response) => {
		if (studio && !request.url.startsWith('/api/') && !request.url.startsWith('/wss/')) {
			request.url = request.url.replace(/^\/irade(?=\/|\?|$)/, '') || '/';
			studio(request, response);
			return;
		}
		response.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type');
		response.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
		if (request.method === 'OPTIONS') { response.writeHead(204).end(); return; }

		if (studio && request.url === '/api/irade/auth/orcid/status') {
			response.setHeader('Content-Type', 'application/json');
			response.end(JSON.stringify({ available: true, issuer: 'https://orcid.org' }));
			return;
		}
		if (studio && request.url.startsWith('/api/irade/auth/orcid/start')) {
			const url = new URL(request.url, 'https://localhost:9449');
			response.setHeader('Set-Cookie', 'fixture_handoff=1; HttpOnly; Secure; SameSite=Lax; Path=/');
			response.writeHead(303, {
				Location: '/irade/login?orcid=success' + (url.searchParams.has('workspace') ? '&workspace=' + url.searchParams.get('workspace') : ''),
			}).end();
			return;
		}
		if (studio && request.url === '/api/irade/auth/session' && request.method === 'POST') {
			response.setHeader('Content-Type', 'application/json');
			if (!(request.headers.cookie || '').includes('fixture_handoff=1')) {
				response.writeHead(401).end(JSON.stringify({ message: 'Expired fixture handoff' }));
				return;
			}
			response.setHeader('Set-Cookie', 'fixture_handoff=; Max-Age=0; Path=/');
			response.end(JSON.stringify({
				login: (request.headers.cookie || '').includes('fixture_mfa=1')
					? { mfaRequired: true, challengeToken: 'fixture-challenge' }
					: { accessToken: 'fixture-writer' },
				orcid: '0000-0002-1825-0097',
				issuer: 'https://orcid.org',
			}));
			return;
		}
		if (studio && request.url === '/api/irade/auth/me' && request.headers.authorization === 'Bearer fixture-writer') {
			response.setHeader('Content-Type', 'application/json');
			response.end(JSON.stringify({
				userId: 7,
				name: 'Fixture researcher',
				orcid: '0000-0002-1825-0097',
				issuer: 'https://orcid.org',
				profileUrl: 'https://orcid.org/0000-0002-1825-0097',
			}));
			return;
		}
		if (studio && request.url.startsWith('/api/irade/workspaces') && !request.url.endsWith('/access') && request.method === 'GET' && request.headers.authorization === 'Bearer fixture-writer') {
			const url = new URL(request.url, 'https://localhost:9449');
			response.setHeader('Content-Type', 'application/json');
			const workspace = { workspaceId: 42, name: '공동 연구 workspace', description: 'Shared research' };
			response.end(JSON.stringify(workspace.name.includes(url.searchParams.get('q') || '') ? [workspace] : []));
			return;
		}
		if (studio && request.url === '/api/auth/login' && request.method === 'POST') {
			let body = '';
			request.on('data', chunk => { body += chunk; });
			request.on('end', () => {
				const data = JSON.parse(body);
				response.setHeader('Content-Type', 'application/json');
				if (data.password === 'fixture-password') {
					response.end(JSON.stringify(data.email === 'mfa@example.invalid'
						? { mfaRequired: true, challengeToken: 'fixture-challenge' }
						: { accessToken: 'fixture-writer' }));
				} else {
					response.writeHead(401).end(JSON.stringify({ message: 'Wrong fixture password' }));
				}
			});
			return;
		}
		if (studio && request.url === '/api/auth/mfa/verify' && request.method === 'POST') {
			let body = '';
			request.on('data', chunk => { body += chunk; });
			request.on('end', () => {
				const data = JSON.parse(body);
				response.setHeader('Content-Type', 'application/json');
				if (data.code === '123456' && data.challengeToken === 'fixture-challenge') {
					response.end(JSON.stringify({ accessToken: 'fixture-writer' }));
				} else {
					response.writeHead(400).end(JSON.stringify({ message: '잘못된 인증 코드' }));
				}
			});
			return;
		}
		if (request.url !== '/api/irade/workspaces/42/access' || request.headers.authorization !== 'Bearer fixture-writer') {
			response.writeHead(403).end();
			return;
		}
		response.setHeader('Content-Type', 'application/json');
		response.end(JSON.stringify({ ws_port: '8002', canvas_access_token: 'fixture-workspace' }));
	});

	const sockets = new WebSocketServer({ server });
	const workspaceClients = new Set();
	const agentQuotas = new Map([
		['k-exaone_v2.5::server', { second: { remaining: 9, limit: 10 }, minute: { remaining: 98, limit: 100 }, day: { remaining: 4990, limit: 5000 } }],
		['exaone-deep-test::server', { second: { remaining: 3, limit: 5 }, minute: { remaining: 35, limit: 40 }, day: { remaining: 900, limit: 1000 } }],
	]);
	const getQuota = (api, key) => {
		const scope = `${api}::${key ? `personal:${key}` : 'server'}`;
		if (!agentQuotas.has(scope)) {
			agentQuotas.set(scope, { second: { remaining: 5, limit: 5 }, minute: { remaining: 50, limit: 50 }, day: { remaining: 500, limit: 500 } });
		}
		return agentQuotas.get(scope);
	};

	const snapshotObject = () => {
		const out = {};
		for (const [id, item] of items.entries()) {
			out[id] = structuredClone(item);
		}
		return out;
	};

	const sendError = (socket, requestId, code) => {
		socket.send(JSON.stringify({ type: 'error', ...(requestId ? { request_id: requestId } : {}), code }));
	};

	sockets.on('connection', (socket, req) => {
		const url = new URL(req.url || '/', 'wss://localhost:9449');
		const clientId = url.searchParams.get('client_id') || '';
		if (url.pathname.includes('/rtc/')) {
			socket.send(JSON.stringify({ type: 'rtc_ready' }));
			socket.on('message', raw => {
				const frame = JSON.parse(String(raw));
				if (frame.type === 'rtc_join') {
					socket.send(JSON.stringify({ type: 'rtc_peers', self_peer_id: 'self', peers: [] }));
				}
			});
			return;
		}

		workspaceClients.add(socket);
		socket.on('close', () => workspaceClients.delete(socket));
		socket.send(JSON.stringify({
			type: 'init_items',
			sync_user_id: '7',
			is_admin: false,
			groups: ['default'],
			member_groups: ['default'],
			write_groups: writable ? ['default'] : [],
			execute_groups: ['default'],
			revision,
			accepted_count: acceptedCounts.get(clientId) || 0,
			items: snapshotObject(),
		}));

		socket.on('message', raw => {
			const frame = JSON.parse(String(raw));
			if (frame.type === 'canvas_sync') {
				socket.send(JSON.stringify({
					type: 'canvas_snapshot',
					revision,
					accepted_count: acceptedCounts.get(clientId) || 0,
					items: snapshotObject(),
				}));
				return;
			}
			if (frame.type === 'runtime_execute' && frame.action === 'agent') {
				const api = typeof frame.api === 'string' && frame.api ? frame.api : 'k-exaone_v2.5';
				const personal = typeof frame.api_key === 'string' && frame.api_key.length > 0;
				const apis = [
					{ id: 'k-exaone_v2.5', label: 'EXAONE 2.5 (k-exaone_v2.5)', model: 'k-exaone_v2.5', default_configured: true },
					{ id: 'exaone-deep-test', label: 'EXAONE Deep Test', model: 'exaone-deep-test', default_configured: true },
				];
				const limits = structuredClone(getQuota(api, personal ? frame.api_key : ''));
				if (frame.operation === 'status') {
					socket.send(JSON.stringify({
						type: 'runtime_result',
						request_id: frame.request_id,
						ok: true,
						result: {
							ready: true,
							configured: true,
							credential_source: personal ? 'personal' : 'server',
							default_configured: true,
							api,
							model: api,
							apis,
							defaults: { reasoning_effort: 'low', max_tokens: 8192, max_steps: 8, temperature: 1, top_p: 0.95 },
							telemetry: { api, limits, observed_at: Math.floor(Date.now() / 1000), limits_observed_at: Math.floor(Date.now() / 1000), status: 200 },
						},
					}));
					return;
				}
				if (!writable) {
					socket.send(JSON.stringify({ type: 'runtime_result', request_id: frame.request_id, ok: false, code: 'RUNTIME_EXECUTION_DENIED', message: '쓰기 권한 필요' }));
					return;
				}
				const quota = getQuota(api, personal ? frame.api_key : '');
				quota.second.remaining = Math.max(0, quota.second.remaining - 1);
				quota.minute.remaining = Math.max(0, quota.minute.remaining - 1);
				quota.day.remaining = Math.max(0, quota.day.remaining - 1);
				socket.send(JSON.stringify({ type: 'runtime_output', request_id: frame.request_id, output: `모델 계획 (${api})\n` }));
				const nextOrder = Math.max(-10, ...Array.from(items.values()).map(i => Number(i.frameOrder ?? 0))) + 10;
				const newId = `agent-cell-${items.size + 1}`;
				const newItem = {
					kind: 'text',
					renderMode: 'irade-markdown',
					format: 'markdown',
					frameOrder: nextOrder,
					title: '에이전트 생성 메모',
					text: `# 에이전트 생성 메모\nAPI=${api}, effort=${frame.options?.reasoning_effort || 'low'}, tokens=${frame.options?.max_tokens || 8192}, steps=${frame.options?.max_steps || 8}\n\n${frame.message}`,
					permission: ['default'],
				};
				items.set(newId, newItem);
				revision += 1;
				const commitMsg = JSON.stringify({
					type: 'canvas_commit',
					revision,
					author_id: '7',
					items: { [newId]: structuredClone(newItem) },
				});
				for (const target of workspaceClients) { target.send(commitMsg); }
				socket.send(JSON.stringify({
					type: 'runtime_result',
					request_id: frame.request_id,
					ok: true,
					result: {
						answer: `셀을 하단에 생성했습니다 (#${newId}).`,
						api,
						model: api,
						telemetry: { api, limits: structuredClone(quota), observed_at: Math.floor(Date.now() / 1000), limits_observed_at: Math.floor(Date.now() / 1000), status: 200 },
					},
				}));
				return;
			}
			if (frame.type !== 'canvas_ops') { return; }
			const cid = String(frame.client_id || clientId);
			const reqCount = Number(frame.request_count || 0);
			const accepted = acceptedCounts.get(cid) || 0;
			if (reqCount !== accepted + 1) {
				sendError(socket, frame.request_id, 'CLIENT_SEQUENCE_CONFLICT');
				return;
			}
			if (!writable) {
				sendError(socket, frame.request_id, 'ITEM_WRITE_DENIED');
				return;
			}

			const draft = new Map(Array.from(items.entries()).map(([k, v]) => [k, structuredClone(v)]));
			const commitItems = {};
			const commitPatches = {};

			for (const op of Array.isArray(frame.operations) ? frame.operations : []) {
				const id = String(op.id || '');
				if (op.action === 'create') {
					if (draft.has(id)) { sendError(socket, frame.request_id, 'ITEM_ALREADY_EXISTS'); return; }
					const item = structuredClone(op.patch || {});
					draft.set(id, item);
					commitItems[id] = structuredClone(item);
				} else if (op.action === 'delete') {
					const existing = draft.get(id);
					if (!existing) { sendError(socket, frame.request_id, 'ITEM_NOT_FOUND'); return; }
					if (existing.locked) { sendError(socket, frame.request_id, 'ITEM_LOCKED'); return; }
					draft.delete(id);
					commitItems[id] = null;
				} else if (op.action === 'patch') {
					const existing = draft.get(id);
					if (!existing) { sendError(socket, frame.request_id, 'ITEM_NOT_FOUND'); return; }
					const next = structuredClone(existing);
					const patches = [];
					for (const [key, val] of Object.entries(op.patch || {})) {
						if (key === 'automerge_changes' && Array.isArray(val) && Array.isArray(next.automerge_changes) && !(op.patch || {}).automerge_snapshot) {
							next.automerge_changes = [...next.automerge_changes, ...val];
						} else {
							next[key] = structuredClone(val);
						}
						patches.push({ op: 'replace', path: `/${key}`, value: structuredClone(next[key]) });
					}
					for (const [field, edit] of Object.entries(op.text_edits || {})) {
						try {
							next[field] = applyTextDelta(String(next[field] || ''), edit);
						} catch {
							sendError(socket, frame.request_id, 'CANVAS_TEXT_CONFLICT');
							return;
						}
						patches.push({ op: 'text', path: `/${field}`, value: edit });
					}
					draft.set(id, next);
					commitPatches[id] = patches;
				}
			}

			items.clear();
			for (const [k, v] of draft.entries()) { items.set(k, v); }
			acceptedCounts.set(cid, reqCount);
			revision += 1;
			const commitMsg = JSON.stringify({
				type: 'canvas_commit',
				revision,
				author_id: '7',
				client_id: cid,
				request_id: frame.request_id,
				request_count: reqCount,
				...(Object.keys(commitItems).length ? { items: commitItems } : {}),
				...(Object.keys(commitPatches).length ? { patches: commitPatches } : {}),
			});
			for (const target of workspaceClients) {
				target.send(commitMsg);
			}
		});
	});

	await new Promise(resolve => server.listen(9449, resolve));
	const cleanup = async () => {
		for (const socket of sockets.clients) { socket.terminate(); }
		sockets.close();
		server.close();
		await rm(temporary, { recursive: true, force: true });
	};
	cleanup.setWritable = value => {
		writable = value;
		const msg = JSON.stringify({
			type: 'canvas_access_changed',
			is_admin: false,
			groups: ['default'],
			member_groups: ['default'],
			write_groups: value ? ['default'] : [],
			execute_groups: ['default'],
		});
		for (const target of workspaceClients) { target.send(msg); }
	};
	return cleanup;
}
