import test from 'node:test';
import assert from 'node:assert/strict';
import { AgoraClient, AgoraError, connection, type SocketTransport, type Transport } from '../src/client.ts';
import { decode, encode, replacement } from '../src/document.ts';

class FakeSocket implements SocketTransport {
	sent: Record<string, unknown>[] = [];
	closed = false;
	onmessage: ((event: MessageEvent) => void) | null = null;
	onerror: ((event: Event) => void) | null = null;
	onclose: ((event: CloseEvent) => void) | null = null;
	send(data: string): void { this.sent.push(JSON.parse(data) as Record<string, unknown>); }
	close(): void { this.closed = true; }
	receive(data: unknown): void { this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent); }
	drop(): void { this.onclose?.({} as CloseEvent); }
}

function fixture() {
	const requests: { url: string; init?: RequestInit }[] = [];
	const sockets: FakeSocket[] = [];
	const socketUrls: URL[] = [];
	const transport: Transport = {
		fetch: async (url, init) => {
			requests.push({ url: String(url), init });
			return new Response(JSON.stringify({ ws_port: '8002', canvas_access_token: 'access-token' }), { status: 200 });
		},
		socket: url => {
			socketUrls.push(url);
			const socket = new FakeSocket();
			sockets.push(socket);
			return socket;
		},
	};
	const client = new AgoraClient(
		{ origin: 'https://agora.example', canvasId: 42 },
		async () => 'login-token',
		undefined,
		transport,
	);
	return { client, requests, sockets, socketUrls };
}

test('validates HTTPS connection configuration', () => {
	assert.deepEqual(connection({ origin: 'https://irade.example/', canvasId: 7 }), { origin: 'https://irade.example', canvasId: 7 });
	assert.throws(() => connection({ origin: 'http://irade.example', canvasId: 7 }), /HTTPS/);
	assert.throws(() => connection({ origin: 'https://irade.example', canvasId: 0 }), /Invalid workspace ID/);
});

test('document helpers preserve UTF-8 base64 encoding and minimal replacement span', () => {
	const raw = new Uint8Array([0, 42, 128, 255]);
	assert.deepEqual(decode(encode(raw)), raw);
	assert.deepEqual(replacement('alpha beta gamma', 'alpha DELTA gamma'), { start: 6, end: 10, text: 'DELTA' });
	assert.deepEqual(replacement('한글 테스트', '한글 새 테스트'), { start: 3, end: 3, text: '새 ' });
});

test('connects via standalone workspace access, WebSocket, RTC presence, and streams runtime execution', async () => {
	const { client, requests, sockets, socketUrls } = fixture();
	try {
		const states: string[] = [];
		client.onState = state => states.push(state);
		const ready = client.ensureConnected();
		await new Promise(resolve => setTimeout(resolve, 10));

		assert.equal(requests[0].url, 'https://agora.example/api/irade/workspaces/42/access');
		assert.equal(socketUrls[0].pathname, '/wss/port/8002/workspace/42');

		sockets[0].receive({
			type: 'init_items',
			is_admin: false,
			groups: ['default'],
			write_groups: ['default'],
			execute_groups: ['default'],
			rtc_canvas_connection_id: 'conn-1',
			rtc_canvas_connection_hash: 'hash-1',
			items: {},
		});
		await ready;
		assert.equal(socketUrls.length, 2);
		assert.equal(socketUrls[1].pathname, '/wss/port/8002/rtc/workspace/42');
		assert.equal(client.canWrite, true);
		assert.equal(client.canExecute, true);

		await assert.rejects(client.executeRuntime('python', 'print(1)'), /RUNTIME_REQUEST_INVALID/);

		const chunks: string[] = [];
		const execPromise = client.executeRuntime('python', 'print(42)', chunk => chunks.push(chunk), 'cell-1');
		const frame = sockets[0].sent.find(msg => msg.type === 'runtime_execute');
		assert.ok(frame);
		assert.equal(frame.object_id, 'cell-1');
		sockets[0].receive({ type: 'runtime_output', request_id: frame.request_id, output: '42\n' });
		sockets[0].receive({ type: 'runtime_result', request_id: frame.request_id, ok: true, result: { exitCode: 0, output: '42\n' } });
		const result = await execPromise;
		assert.deepEqual(chunks, ['42\n']);
		assert.equal(result.exitCode, 0);

		const aborted = client.executeRuntime('python', 'print(1)', undefined, 'cell-1');
		sockets[0].receive({ type: 'canvas_access_revoked' });
		await assert.rejects(aborted, (err: AgoraError) => err.code === 'CANVAS_ACCESS_REVOKED');
		assert.equal(states.at(-1), 'CANVAS_ACCESS_REVOKED');
	} finally {
		client.dispose();
	}
});
