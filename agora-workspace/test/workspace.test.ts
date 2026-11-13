import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import * as A from '@automerge/automerge';
import { AgoraClient, type SocketTransport, type Transport } from '../src/client.ts';
import { decode, encode } from '../src/document.ts';
import { ResearchWorkspace, cellDirectory } from '../src/studio/workspace.ts';

type StoredDoc = { fileId: string; text: string; [key: string]: unknown };

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
	receive(message: object): void { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(message) })); }
}

interface StoredFile { fileId: string; doc: A.Doc<StoredDoc>; resourceId: string; }

function createWorkspaceHarness() {
	const files = new Map<string, StoredFile>();
	const dirs = new Set<string>(['/']);
	const ops: { operation: string; path: string }[] = [];
	const sockets: MockSocket[] = [];
	let seq = 1;
	let deferNextList: (() => void) | undefined;

	const snapshot = (doc: A.Doc<StoredDoc>) => encode(A.save(doc));
	const etagOf = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex');

	const handleFrame = (frame: Record<string, unknown>, socket: MockSocket) => {
		if (frame.type !== 'workspace_files') { return; }
		const op = String(frame.operation);
		const path = String(frame.path);
		const requestId = String(frame.request_id);
		ops.push({ operation: op, path });

		const sendResult = (ok: boolean, payload: object, code?: string) => {
			socket.receive({ type: 'workspace_files_result', request_id: requestId, ok, ...(ok ? { result: payload } : { code }) });
		};

		if (op === 'mkdir') {
			if (dirs.has(path)) { sendResult(false, {}, 'FILE_EXISTS'); return; }
			dirs.add(path);
			sendResult(true, { path, operation: 'mkdir' });
			socket.receive({ type: 'workspace_files_changed', path, operation: 'mkdir', request_id: requestId, created: true });
			return;
		}
		if (op === 'list') {
			const respond = () => {
				if (!dirs.has(path)) { sendResult(false, {}, 'FILE_NOT_FOUND'); return; }
				const prefix = path === '/' ? '/' : `${path}/`;
				const entries: [string, string][] = [];
				for (const key of files.keys()) {
					if (key.startsWith(prefix)) {
						const rest = key.slice(prefix.length);
						if (!rest.includes('/')) { entries.push([rest, 'file']); }
					}
				}
				entries.sort();
				sendResult(true, { entries, cursor: null });
			};
			if (deferNextList) {
				const trigger = respond;
				deferNextList = () => trigger();
				return;
			}
			respond();
			return;
		}
		if (op === 'read') {
			const file = files.get(path);
			if (!file) { sendResult(false, {}, 'FILE_NOT_FOUND'); return; }
			sendResult(true, {
				path, type: 'file', size: file.doc.text.length, mtime: Date.now(), ctime: Date.now(),
				fileId: file.fileId, etag: etagOf(file.doc.text), document: snapshot(file.doc),
				content: encode(new TextEncoder().encode(file.doc.text)), resourceId: file.resourceId,
			});
			return;
		}
		if (op === 'write') {
			const existing = files.get(path);
			if (!existing && frame.create) {
				const text = new TextDecoder().decode(decode(String(frame.content ?? '')));
				const fileId = `file-${seq++}`;
				const doc = A.from<StoredDoc>({ fileId, text });
				files.set(path, { fileId, doc, resourceId: `1:${seq}:1` });
				sendResult(true, { path, operation: 'write', fileId, etag: etagOf(text), created: true });
				socket.receive({ type: 'workspace_files_changed', path, operation: 'write', request_id: requestId, created: true });
				return;
			}
			if (!existing) { sendResult(false, {}, 'FILE_NOT_FOUND'); return; }
			const incoming = A.load<StoredDoc>(decode(String(frame.document)));
			existing.doc = A.merge(existing.doc, incoming);
			existing.resourceId = `1:${seq++}:1`;
			sendResult(true, { path, operation: 'write', fileId: existing.fileId, etag: etagOf(existing.doc.text), created: false });
			socket.receive({ type: 'workspace_files_changed', path, operation: 'write', request_id: requestId, created: false });
			return;
		}
	};

	const transport: Transport = {
		fetch: async () => Response.json({ ws_port: '8002', canvas_access_token: 'tok' }),
		socket: () => {
			const socket = new MockSocket(handleFrame);
			sockets.push(socket);
			queueMicrotask(() => socket.receive({ type: 'init_items', is_admin: true, groups: ['g'], write_groups: ['g'], execute_groups: ['g'] }));
			return socket;
		},
	};
	const client = new AgoraClient({ origin: 'https://agora.example', canvasId: 42 }, async () => 'jwt', undefined, transport);
	const workspace = new ResearchWorkspace(client, 0);
	return {
		files, dirs, ops, sockets, client, workspace,
		holdNextList: () => { deferNextList = () => {}; return () => { const fn = deferNextList; deferNextList = undefined; fn?.(); }; },
		emitPeerWrite: (path: string, text: string) => {
			let existing = files.get(path);
			if (!existing) {
				const fileId = `peer-${seq++}`;
				existing = { fileId, doc: A.from<StoredDoc>({ fileId, text }), resourceId: `1:${seq}:1` };
				files.set(path, existing);
			} else {
				existing.doc = A.change(existing.doc, draft => A.updateText(draft, ['text'], text));
				existing.resourceId = `1:${seq++}:1`;
			}
			sockets[0].receive({ type: 'workspace_files_changed', path, operation: 'write', request_id: `peer-${seq}` });
		},
		emitPeerDelete: (path: string) => {
			files.delete(path);
			sockets[0].receive({ type: 'workspace_files_changed', path, operation: 'delete', request_id: `peer-del-${seq++}` });
		},
	};
}

test('add() caches mkdir state and only reads the newly created cell once', async () => {
	const h = createWorkspaceHarness();
	try {
		await h.workspace.load();
		h.ops.length = 0;

		const first = await h.workspace.add('python', 'x = 1');
		assert.equal(h.workspace.cells.length, 1);
		assert.deepEqual(h.ops, [
			{ operation: 'mkdir', path: '/.irade' },
			{ operation: 'mkdir', path: cellDirectory },
			{ operation: 'write', path: first.path },
			{ operation: 'read', path: first.path },
		]);

		h.ops.length = 0;
		const second = await h.workspace.add('lean', 'theorem p : True := trivial');
		assert.equal(h.workspace.cells.length, 2);
		assert.deepEqual(h.ops, [
			{ operation: 'write', path: second.path },
			{ operation: 'read', path: second.path },
		]);
	} finally { h.workspace.dispose(); }
});

test('stale concurrent list() does not prune a locally added cell', async () => {
	const h = createWorkspaceHarness();
	try {
		await h.workspace.load();
		await h.workspace.add('python', 'base = 0');
		const releaseList = h.holdNextList();
		const staleLoad = h.workspace.load();
		await delay(0);

		const created = await h.workspace.add('python', 'fresh = 1');
		releaseList();
		await staleLoad;

		assert.equal(h.workspace.cells.some(c => c.id === created.id), true);
		assert.equal(h.workspace.cells.length, 2);
	} finally { h.workspace.dispose(); }
});

test('incremental onChange skips clean self echoes and merges concurrent peer edits', async () => {
	const h = createWorkspaceHarness();
	try {
		const a = await h.workspace.add('python', 'a = 1\nb = 1');
		const b = await h.workspace.add('python', 'other = 1');
		await delay(10);
		h.ops.length = 0;

		// Clean own edit flush: write once, zero redundant list/read calls on own echo.
		h.workspace.edit(a.id, 'a = 2\nb = 1');
		await h.workspace.flush();
		await delay(10);
		assert.deepEqual(h.ops, [{ operation: 'write', path: a.path }]);

		// Simulate peer updating cell b while client holds local state: reads ONLY cell b, not cell a.
		h.ops.length = 0;
		h.emitPeerWrite(b.path, 'other = 99');
		await delay(10);
		assert.deepEqual(h.ops, [{ operation: 'read', path: b.path }]);
		assert.equal(h.workspace.cells.find(c => c.id === b.id)?.text, 'other = 99');

		// Simulate peer modifying cell a before this tab flushes another disjoint change:
		// etag mismatch triggers single-cell read and converges both edits.
		const entry = h.files.get(a.path)!;
		entry.doc = A.change(entry.doc, draft => A.updateText(draft, ['text'], 'a = 2\nb = 99'));
		h.ops.length = 0;
		h.workspace.edit(a.id, 'a = 42\nb = 1');
		await h.workspace.flush();
		await delay(10);
		assert.deepEqual(h.ops, [
			{ operation: 'write', path: a.path },
			{ operation: 'read', path: a.path },
		]);
		assert.equal(h.workspace.cells.find(c => c.id === a.id)?.text, 'a = 42\nb = 99');

		// Peer deletion removes replica immediately without any read or list request.
		h.ops.length = 0;
		h.emitPeerDelete(b.path);
		assert.deepEqual(h.ops, []);
		assert.equal(h.workspace.cells.length, 1);
	} finally { h.workspace.dispose(); }
});
