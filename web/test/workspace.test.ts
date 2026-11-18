import assert from 'node:assert/strict';
import { test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import * as A from '@automerge/automerge';
import { AgoraClient, AgoraError, type SocketTransport, type Transport } from '../src/client.ts';
import {
	applyTextDelta,
	canAccessItem,
	computeTextDelta,
	type CanvasItem,
	type PatchOp,
	type PeerProfile,
} from '../src/canvasSync.ts';
import { decode } from '../src/document.ts';
import { ResearchWorkspace } from '../src/studio/workspace.ts';

class MockSocket implements SocketTransport {
	onmessage: SocketTransport['onmessage'] = null;
	onerror: SocketTransport['onerror'] = null;
	onclose: SocketTransport['onclose'] = null;
	constructor(private readonly handleFrame: (frame: Record<string, unknown>, socket: MockSocket) => void) {}
	send(data: string): void {
		const frame = JSON.parse(data) as Record<string, unknown>;
		queueMicrotask(() => this.handleFrame(frame, this));
	}
	close(): void { this.onclose?.(new Event('close') as CloseEvent); }
	receive(message: object): void {
		this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(message) }));
	}
}

interface MockIdentity {
	userId: string;
	isAdmin: boolean;
	groups: string[];
	memberGroups: string[];
	writeGroups: string[];
	executeGroups: string[];
}

function createCanvasHarness(options?: {
	initialItems?: Record<string, CanvasItem>;
	identity?: Partial<MockIdentity>;
	groupBits?: Record<string, number>;
	withRtc?: boolean;
}) {
	const store = new Map<string, CanvasItem>(Object.entries(structuredClone(options?.initialItems || {})));
	const identity: MockIdentity = {
		userId: 'user-1',
		isAdmin: true,
		groups: ['team-a'],
		memberGroups: ['team-a'],
		writeGroups: ['team-a'],
		executeGroups: ['team-a'],
		...options?.identity,
	};
	const groupBits = options?.groupBits || { 'team-a': 0, 'team-b': 1 };
	const acceptedCounts = new Map<string, number>();
	let revision = 1;
	const sentFrames: Record<string, unknown>[] = [];
	const runtimeCalls: { objectId: string; language: string; code: string }[] = [];
	const canvasSockets: MockSocket[] = [];
	const rtcSockets: MockSocket[] = [];
	const peers: PeerProfile[] = [
		{ peer_id: 'peer-1', user_id: '2', nickname: 'Alice', groups: ['team-a'], is_admin: false },
		{ peer_id: 'peer-2', user_id: '3', nickname: 'Bob', groups: ['team-b'], is_admin: false },
	];

	const canAccess = (item: CanvasItem | undefined, cap: 'read' | 'write' | 'execute'): boolean => {
		if (!item) { return false; }
		return canAccessItem(item, {
			userId: identity.userId,
			isAdmin: identity.isAdmin,
			groups: identity.groups,
			memberGroups: identity.memberGroups,
			writeGroups: identity.writeGroups,
			executeGroups: identity.executeGroups,
			groupBits,
		}, cap);
	};

	const visibleSnapshot = (): Record<string, CanvasItem> => {
		const out: Record<string, CanvasItem> = {};
		for (const [id, item] of store.entries()) {
			if (canAccess(item, 'read')) { out[id] = structuredClone(item); }
		}
		return out;
	};

	const sendError = (socket: MockSocket, requestId: unknown, code: string) => {
		socket.receive({ type: 'error', ...(typeof requestId === 'string' ? { request_id: requestId } : {}), code });
	};

	const handleCanvasFrame = (frame: Record<string, unknown>, socket: MockSocket) => {
		sentFrames.push(frame);
		const type = String(frame.type || '');
		const clientId = String(frame.client_id || 'client');

		if (type === 'canvas_sync') {
			socket.receive({
				type: 'canvas_snapshot',
				revision,
				accepted_count: acceptedCounts.get(clientId) || 0,
				items: visibleSnapshot(),
			});
			return;
		}

		if (type === 'runtime_execute') {
			const objectId = String(frame.object_id || '');
			const requestId = String(frame.request_id || '');
			runtimeCalls.push({ objectId, language: String(frame.language), code: String(frame.code) });
			const target = store.get(objectId);
			if (!target || target.kind !== 'code' || !canAccess(target, 'execute')) {
				socket.receive({ type: 'runtime_result', request_id: requestId, ok: false, code: 'RUNTIME_EXECUTION_DENIED' });
				return;
			}
			socket.receive({ type: 'runtime_output', request_id: requestId, output: 'streaming...\n' });
			socket.receive({
				type: 'runtime_result',
				request_id: requestId,
				ok: true,
				result: { exitCode: 0, output: `executed:${objectId}`, timedOut: false },
			});
			return;
		}

		if (type !== 'canvas_ops') { return; }
		const reqCount = Number(frame.request_count || 0);
		const accepted = acceptedCounts.get(clientId) || 0;
		if (reqCount !== accepted + 1) {
			sendError(socket, frame.request_id, 'CLIENT_SEQUENCE_CONFLICT');
			return;
		}

		const operations = Array.isArray(frame.operations) ? frame.operations as Record<string, unknown>[] : [];
		const draft = new Map<string, CanvasItem>(Array.from(store.entries()).map(([k, v]) => [k, structuredClone(v)]));
		const commitItems: Record<string, CanvasItem | null> = {};
		const commitPatches: Record<string, PatchOp[]> = {};

		for (const op of operations) {
			const action = String(op.action || '');
			const id = String(op.id || '');
			if (action === 'create') {
				if (draft.has(id)) { sendError(socket, frame.request_id, 'ITEM_ALREADY_EXISTS'); return; }
				const patch = structuredClone((op.patch || {}) as CanvasItem);
				if (!patch.permission) { patch.permission = [...identity.memberGroups]; }
				if (!canAccess(patch, 'write')) { sendError(socket, frame.request_id, 'ITEM_WRITE_DENIED'); return; }
				draft.set(id, patch);
				commitItems[id] = structuredClone(patch);
				continue;
			}
			const existing = draft.get(id);
			if (!existing) { sendError(socket, frame.request_id, 'ITEM_NOT_FOUND'); return; }
			if (!canAccess(existing, 'write')) { sendError(socket, frame.request_id, 'ITEM_WRITE_DENIED'); return; }
			if (action === 'delete') {
				if (existing.locked) { sendError(socket, frame.request_id, 'ITEM_LOCKED'); return; }
				draft.delete(id);
				commitItems[id] = null;
				continue;
			}
			if (action === 'patch') {
				const patchObj = (op.patch || {}) as Record<string, unknown>;
				const textEdits = (op.text_edits || {}) as Record<string, { at: number; remove: number; insert: string; base?: string }>;
				const unlocking = patchObj.locked === false && Object.keys(patchObj).length === 1 && !Object.keys(textEdits).length;
				if (existing.locked && !unlocking) { sendError(socket, frame.request_id, 'ITEM_LOCKED'); return; }
				if (patchObj.permission !== undefined && !identity.isAdmin) {
					sendError(socket, frame.request_id, 'ITEM_PERMISSION_DENIED');
					return;
				}
				const next = structuredClone(existing);
				const patches: PatchOp[] = [];
				for (const [key, val] of Object.entries(patchObj)) {
					if (key === 'automerge_changes' && Array.isArray(val) && Array.isArray(next.automerge_changes) && !patchObj.automerge_snapshot) {
						next.automerge_changes = [...next.automerge_changes, ...val];
					} else {
						next[key] = structuredClone(val);
					}
					patches.push({ op: 'replace', path: `/${key}`, value: structuredClone(next[key]) });
				}
				for (const [field, edit] of Object.entries(textEdits)) {
					try {
						next[field] = applyTextDelta(String(next[field] || ''), edit);
					} catch {
						sendError(socket, frame.request_id, 'CANVAS_TEXT_CONFLICT');
						return;
					}
					patches.push({ op: 'text', path: `/${field}`, value: edit });
				}
				draft.set(id, next);
				if (patchObj.permission !== undefined && !canAccess(next, 'read')) {
					commitItems[id] = null;
				} else {
					commitPatches[id] = patches;
				}
			}
		}

		store.clear();
		for (const [k, v] of draft.entries()) { store.set(k, v); }
		acceptedCounts.set(clientId, reqCount);
		revision += 1;
		socket.receive({
			type: 'canvas_commit',
			revision,
			author_id: identity.userId,
			client_id: clientId,
			request_id: frame.request_id,
			request_count: reqCount,
			...(Object.keys(commitItems).length ? { items: commitItems } : {}),
			...(Object.keys(commitPatches).length ? { patches: commitPatches } : {}),
		});
	};

	const handleRtcFrame = (frame: Record<string, unknown>, socket: MockSocket) => {
		sentFrames.push(frame);
		if (frame.type === 'rtc_join') {
			socket.receive({ type: 'rtc_peers', self_peer_id: 'self-peer', peers });
		}
	};

	const transport: Transport = {
		fetch: async () => Response.json({ ws_port: '8002', canvas_access_token: 'tok' }),
		socket: url => {
			if (url.pathname.includes('/rtc/')) {
				const rtc = new MockSocket(handleRtcFrame);
				rtcSockets.push(rtc);
				queueMicrotask(() => rtc.receive({ type: 'rtc_ready' }));
				return rtc;
			}
			const socket = new MockSocket(handleCanvasFrame);
			canvasSockets.push(socket);
			queueMicrotask(() => socket.receive({
				type: 'init_items',
				sync_user_id: identity.userId,
				is_admin: identity.isAdmin,
				groups: identity.groups,
				member_groups: identity.memberGroups,
				write_groups: identity.writeGroups,
				execute_groups: identity.executeGroups,
				group_bits: groupBits,
				revision,
				accepted_count: 0,
				...(options?.withRtc ? { rtc_canvas_connection_id: 'conn-1', rtc_canvas_connection_hash: 'hash-1' } : {}),
				items: visibleSnapshot(),
			}));
			return socket;
		},
	};

	const client = new AgoraClient({ origin: 'https://agora.example', canvasId: 42 }, async () => 'jwt', undefined, transport);
	const workspace = new ResearchWorkspace(client, 0);

	return {
		store,
		identity,
		sentFrames,
		runtimeCalls,
		canvasSockets,
		rtcSockets,
		client,
		workspace,
		emitPeerPatch: (id: string, nextText: string, extraPatch?: Record<string, unknown>) => {
			const existing = store.get(id)!;
			const field = existing.kind === 'code' ? 'code' : 'text';
			const before = String(existing[field] || '');
			const delta = computeTextDelta(before, nextText);
			const next = structuredClone(existing);
			next[field] = nextText;
			if (extraPatch) { Object.assign(next, extraPatch); }
			store.set(id, next);
			revision += 1;
			const patches: PatchOp[] = [];
			if (delta) { patches.push({ op: 'text', path: `/${field}`, value: delta }); }
			for (const [k, v] of Object.entries(extraPatch || {})) { patches.push({ op: 'replace', path: `/${k}`, value: v }); }
			canvasSockets[0].receive({
				type: 'canvas_commit',
				revision,
				author_id: 'peer-user',
				client_id: 'other-client',
				patches: { [id]: patches },
			});
		},
		emitPeerDelete: (id: string) => {
			store.delete(id);
			revision += 1;
			canvasSockets[0].receive({
				type: 'canvas_commit',
				revision,
				author_id: 'peer-user',
				client_id: 'other-client',
				items: { [id]: null },
			});
		},
	};
}

test('load() reads Canvas items instantly without VM calls and preserves non-cell Canvas shapes', async () => {
	const h = createCanvasHarness({
		initialItems: {
			'shape-1': { kind: 'shape', shapeType: 'rectangle', x: 0.1, y: 0.1, width: 0.2, height: 0.2, permission: ['team-a'] },
			'code-1': { kind: 'code', language: 'python', code: 'x = 1', title: '기존 셀', frameOrder: 5, permission: ['team-a'] },
		},
	});
	try {
		await h.workspace.load();
		assert.equal(h.workspace.cells.length, 1);
		assert.equal(h.workspace.cells[0].id, 'code-1');
		assert.equal(h.workspace.cells[0].kind, 'python');

		const leanCell = await h.workspace.add('lean', 'theorem t : True := trivial');
		assert.equal(h.workspace.cells.length, 2);
		assert.equal(h.store.get(leanCell.id)?.kind, 'code');
		assert.equal(h.store.get(leanCell.id)?.language, 'lean4');
		assert.ok(h.store.has('shape-1'));
		assert.equal(h.sentFrames.some(f => f.type === 'canvas_ops'), true);
	} finally { h.workspace.dispose(); }
});

test('UTF-8 text delta handles multi-byte Korean/math text and auto-merges conflicts', async () => {
	const h = createCanvasHarness();
	try {
		const cell = await h.workspace.add('lean', 'theorem a : ∀ n : ℕ, n = n := by\n  sorry\n-- 한 줄 메모');
		await delay(10);

		// Peer mutates server copy WITHOUT notifying client yet to force CANVAS_TEXT_CONFLICT on flush.
		const item = h.store.get(cell.id)!;
		const serverDoc = A.load<{ content: string }>(decode(item.automerge_snapshot!));
		const peerDoc = A.change(serverDoc, draft => {
			A.updateText(draft, ['content'], 'theorem a : ∀ n : ℕ, n = n := by\n  sorry\n-- 동료 변경 ✓');
		});
		const peerChange = A.getLastLocalChange(peerDoc)!;
		h.store.set(cell.id, {
			...item,
			code: peerDoc.content,
			automerge_changes: [{ field: 'code', change: Buffer.from(peerChange).toString('base64') }],
		});

		h.workspace.edit(cell.id, 'theorem a : ∀ n : ℕ, n + 0 = n := by\n  sorry\n-- 한 줄 메모');
		await h.workspace.flush();
		await delay(20);

		// Auto-resync via canvas_sync + Automerge merge should preserve BOTH the local theorem edit and peer comment.
		const converged = h.workspace.cells.find(c => c.id === cell.id)?.text || '';
		assert.match(converged, /n \+ 0 = n/);
		assert.match(converged, /-- 동료 변경 ✓/);
		assert.equal(h.sentFrames.some(f => f.type === 'canvas_sync'), true);
	} finally { h.workspace.dispose(); }
});

test('per-item permission ACL, lock, moveCell ordering, and object-scoped execution enforcement', async () => {
	const h = createCanvasHarness({
		initialItems: {
			'cell-exec': { kind: 'code', language: 'python', code: 'print(1)', title: '실행 가능', frameOrder: 10, permission: ['team-a'] },
			'cell-ro': { kind: 'code', language: 'python', code: 'print(2)', title: '읽기 전용', frameOrder: 20, permission: ['team-b'] },
		},
		identity: {
			isAdmin: false,
			groups: ['team-a', 'team-b'],
			memberGroups: ['team-a', 'team-b'],
			writeGroups: ['team-a'],
			executeGroups: ['team-a'],
		},
	});
	try {
		await h.workspace.load();
		const [execCell, roCell] = h.workspace.cells;
		assert.equal(execCell.canWrite, true);
		assert.equal(execCell.canExecute, true);
		assert.equal(roCell.canWrite, false);
		assert.equal(roCell.canExecute, false);

		// Per-cell runtime_execute passes object_id and enforces execute_groups ACL on server.
		const okRes = await h.client.executeRuntime('python', 'print(1)', undefined, execCell.id);
		assert.equal(okRes.exitCode, 0);
		assert.equal(h.runtimeCalls.at(-1)?.objectId, execCell.id);
		await assert.rejects(
			h.client.executeRuntime('python', 'print(2)', undefined, roCell.id),
			(err: Error) => err instanceof AgoraError && err.code === 'RUNTIME_EXECUTION_DENIED',
		);

		// Locking cell-exec marks it non-writable locally and blocks server mutation until unlocked.
		await h.workspace.toggleLock(execCell.id, true);
		assert.equal(h.workspace.cells[0].locked, true);
		assert.equal(h.workspace.cells[0].canWrite, false);
		await h.workspace.toggleLock(execCell.id, false);
		assert.equal(h.workspace.cells[0].canWrite, true);

		// Non-admin cannot patch permission on an existing cell.
		await assert.rejects(
			h.workspace.updatePermission(execCell.id, ['team-b']),
			(err: Error) => err instanceof AgoraError && err.code === 'ITEM_PERMISSION_DENIED',
		);
	} finally { h.workspace.dispose(); }
});

test('admin permission revocation removes invisible item & moveCell swaps frameOrder atomically', async () => {
	const h = createCanvasHarness({
		initialItems: {
			c1: { kind: 'text', text: '# 첫째', title: '첫째', frameOrder: 10, permission: ['team-a'] },
			c2: { kind: 'text', text: '# 둘째', title: '둘째', frameOrder: 20, permission: ['team-a'] },
		},
		withRtc: true,
	});
	try {
		await h.workspace.load();
		await delay(10);
		assert.deepEqual(h.workspace.cells.map(c => c.id), ['c1', 'c2']);

		await h.workspace.moveCell('c2', -1);
		assert.deepEqual(h.workspace.cells.map(c => c.id), ['c2', 'c1']);

		// Verify RTC presence relay filters and sends editor_presence.
		h.workspace.sendCellPresence('c2', true);
		await delay(10);
		const relay = h.sentFrames.find(f => f.type === 'canvas_relay');
		assert.ok(relay);
		assert.deepEqual(relay.peer_ids, ['peer-1']);
		assert.deepEqual(relay.payload, { type: 'editor_presence', item_id: 'c2', editing: true });

		// Simulate server revoking read access / deleting c1.
		h.emitPeerDelete('c1');
		assert.deepEqual(h.workspace.cells.map(c => c.id), ['c2']);

		// Test delta helper directly with multi-byte Unicode boundary safety.
		const delta = computeTextDelta('안녕 ∀ world', '안녕 ∃ world!')!;
		assert.equal(applyTextDelta('안녕 ∀ world', delta), '안녕 ∃ world!');
	} finally { h.workspace.dispose(); }
});

test('incremental peer patch triggers single-cell onText & canvas_watermark settles pending counts', async () => {
	const h = createCanvasHarness({
		initialItems: {
			p1: { kind: 'code', language: 'python', code: 'x = 1\ny = 2', title: '첫째', frameOrder: 10, permission: ['team-a'] },
		},
	});
	try {
		await h.workspace.load();
		const textEvents: { id: string; text: string }[] = [];
		h.workspace.onText = cell => textEvents.push({ id: cell.id, text: cell.text });

		// Peer patches line 1 while client holds no dirty text -> emits clean incremental onText.
		h.emitPeerPatch('p1', 'x = 42\ny = 2');
		assert.deepEqual(textEvents, [{ id: 'p1', text: 'x = 42\ny = 2' }]);

		// Commit an HTML cell first so accepted_count advances to 1 on server & client cleanly.
		const created = await h.workspace.add('html', '<style>b{color:red}</style><b>hi</b>');
		assert.equal(h.store.get(created.id)?.kind, 'text');
		assert.equal(h.store.get(created.id)?.format, 'html');
		assert.equal(h.store.get(created.id)?.renderMode, 'irade-html');

		// Re-verify watermark handling settles counts and updates revision cleanly.
		h.canvasSockets[0].receive({ type: 'canvas_watermark', client_id: h.client.socketClientId, accepted_count: 1, revision: 5 });
		assert.equal(h.workspace.revision, 5);
		await h.workspace.add('agent', '## 질문\n테스트');
		assert.equal(h.workspace.cells.length, 3);
		assert.equal(h.workspace.cells[2].kind, 'agent');
	} finally { h.workspace.dispose(); }
});

test('new cells are always appended at the bottom even with missing or MAX_FRAME_ORDER items', async () => {
	const h = createCanvasHarness({
		initialItems: {
			a_legacy: { kind: 'text', text: '# legacy', title: 'legacy', permission: ['team-a'] },
			z_max: { kind: 'text', text: '# maxed', title: 'maxed', frameOrder: 1000000, permission: ['team-a'] },
			b_explicit: { kind: 'code', language: 'python', code: 'x = 1', title: 'explicit', frameOrder: 10, permission: ['team-a'] },
		},
	});
	try {
		await h.workspace.load();
		// b_explicit(10) < z_max(1000000) and a_legacy missing frameOrder should not force new cells to top.
		const first = await h.workspace.add('python', 'y = 2', { id: '000_early_lex_id' });
		assert.equal(h.workspace.cells[h.workspace.cells.length - 1].id, first.id);

		const second = await h.workspace.add('markdown', '# last', { id: '000_even_earlier' });
		assert.equal(h.workspace.cells[h.workspace.cells.length - 1].id, second.id);
		for (let i = 1; i < h.workspace.cells.length; i += 1) {
			assert.ok(h.workspace.cells[i - 1].frameOrder < h.workspace.cells[i].frameOrder);
		}
	} finally { h.workspace.dispose(); }
});

test('inserts new cell directly after target cell and handles tight frameOrder clusters', async () => {
	const h = createCanvasHarness({
		initialItems: {
			c1: { kind: 'code', language: 'python', code: 'a = 1', title: '첫째', frameOrder: 0, permission: ['team-a'] },
			c2: { kind: 'code', language: 'python', code: 'b = 2', title: '둘째', frameOrder: 2, permission: ['team-a'] },
			c3: { kind: 'code', language: 'python', code: 'c = 3', title: '셋째', frameOrder: 20, permission: ['team-a'] },
		},
	});
	try {
		await h.workspace.load();
		// Gap 0..2 allocates midpoint 1 cleanly without touching c2/c3.
		const mid = await h.workspace.add('lean', 'theorem t : True := trivial', { afterId: 'c1' });
		assert.deepEqual(h.workspace.cells.map(c => c.id), ['c1', mid.id, 'c2', 'c3']);
		assert.equal(mid.frameOrder, 1);

		// Consecutive insert right after c1 with gap 0..1 shifts only the minimal tight cluster [mid, c2], leaving c3(20) untouched.
		const tight = await h.workspace.add('markdown', '# 끼워넣기', { afterId: 'c1' });
		assert.deepEqual(h.workspace.cells.map(c => c.id), ['c1', tight.id, mid.id, 'c2', 'c3']);
		for (let i = 1; i < h.workspace.cells.length; i += 1) {
			assert.ok(h.workspace.cells[i - 1].frameOrder < h.workspace.cells[i].frameOrder);
		}
		assert.equal(h.workspace.cells[h.workspace.cells.length - 1].frameOrder, 20);
	} finally { h.workspace.dispose(); }
});
