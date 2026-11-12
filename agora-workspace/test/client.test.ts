import assert from 'node:assert/strict';
import { test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { AgoraClient, connection, type SocketTransport, type Transport } from '../src/client.ts';

class Socket implements SocketTransport {
	onmessage: SocketTransport['onmessage'] = null;
	onerror: SocketTransport['onerror'] = null;
	onclose: SocketTransport['onclose'] = null;
	sent: Record<string, unknown>[] = [];
	send(data: string): void { this.sent.push(JSON.parse(data)); }
	close(): void { this.onclose?.(new Event('close') as CloseEvent); }
	receive(message: object): void { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(message) })); }
}

function fixture() {
	const sockets: Socket[] = [];
	const requests: { url: string; init?: RequestInit }[] = [];
	const transport: Transport = {
		fetch: async (url, init) => {
			requests.push({ url: String(url), init });
			return Response.json({ ws_port: '8002', canvas_access_token: 'canvas-token' });
		},
		socket: url => {
			assert.equal(url.searchParams.get('token'), 'canvas-token');
			const socket = new Socket(); sockets.push(socket); return socket;
		},
	};
	const client = new AgoraClient({ origin: 'https://agora.example', canvasId: 42 }, async () => 'login-token', undefined, transport);
	return { client, requests, sockets };
}

test('uses access-token exchange and correlates out-of-order file responses', async () => {
	const { client, sockets, requests } = fixture();
	try {
		const a = client.request({ operation: 'read', path: '/a' });
		const b = client.request({ operation: 'stat', path: '/b' });
		await delay(0);
		const socket = sockets[0]; socket.receive({ type: 'init_items', write_groups: ['writers'] });
		const [first, second] = socket.sent;
		socket.receive({ type: 'workspace_files_result', request_id: second.request_id, ok: true, result: { path: '/b' } });
		socket.receive({ type: 'workspace_files_result', request_id: first.request_id, ok: true, result: { path: '/a' } });
		assert.deepEqual(await Promise.all([a, b]), [{ path: '/a' }, { path: '/b' }]);
		assert.deepEqual([requests[0].url, requests[0].init?.headers, client.canWrite], [
			'https://agora.example/api/canvases/42/access', { Authorization: 'Bearer login-token', 'Content-Type': 'application/json' }, true,
		]);
	} finally { client.dispose(); }
});

test('reconnection retains the exact mutation ID and payload after a lost acknowledgement', async () => {
	const { client, sockets } = fixture();
	try {
		const result = client.request({ operation: 'write', path: '/a', document: 'snapshot' });
		await delay(0);
		sockets[0].receive({ type: 'init_items' });
		const original = sockets[0].sent[0];
		sockets[0].close();
		await delay(1100);
		assert.equal(sockets.length, 2);
		sockets[1].receive({ type: 'init_items' });
		assert.deepEqual(sockets[1].sent[0], original);
		sockets[1].receive({ type: 'workspace_files_result', request_id: original.request_id, ok: true, result: { duplicate: true } });
		assert.deepEqual(await result, { duplicate: true });
	} finally { client.dispose(); }
});

test('revocation rejects pending requests and disables the connection', async () => {
	const { client, sockets } = fixture();
	try {
		const result = client.request({ operation: 'read', path: '/a' });
		const failure = assert.rejects(result, /CANVAS_ACCESS_REVOKED/);
		await delay(0);
		sockets[0].receive({ type: 'init_items' });
		sockets[0].receive({ type: 'canvas_access_revoked' });
		await failure;
		await assert.rejects(client.request({ operation: 'read', path: '/a' }), /DISCONNECTED/);
	} finally { client.dispose(); }
});

test('workspace origins reject credentials, insecure URLs and ambiguous paths', () => {
	for (const origin of ['http://agora.example', 'https://user:secret@agora.example', 'https://agora.example/elsewhere', 'https://agora.example/?token=secret']) {
		assert.throws(() => connection({ origin, canvasId: 1 }));
	}
});
