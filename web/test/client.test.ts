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

test('AgoraClient executeAgent streams progress, preserves telemetry errors, and agent prefs/keys isolate per API', async () => {
	const { clampAgentOptions, loadAgentPreferences, saveAgentPreferences } = await import('../src/studio/agentPrefs.ts');
	const { clearPersonalApiKey, getPersonalApiKey, setPersonalApiKey } = await import('../src/studio/agentCredentials.ts');

	const mem = new Map<string, string>();
	const storage = {
		getItem: (k: string) => mem.get(k) ?? null,
		setItem: (k: string, v: string) => { mem.set(k, v); },
		removeItem: (k: string) => { mem.delete(k); },
	};
	assert.equal(setPersonalApiKey('k-exaone_v2.5', '  exaone-secret-1  ', storage), true);
	assert.equal(setPersonalApiKey('exaone-deep-test', 'deep-secret-2', storage), true);
	assert.equal(setPersonalApiKey('k-exaone_v2.5', 'has space', storage), false);
	assert.equal(getPersonalApiKey('k-exaone_v2.5', storage), 'exaone-secret-1');
	assert.equal(getPersonalApiKey('exaone-deep-test', storage), 'deep-secret-2');
	clearPersonalApiKey('k-exaone_v2.5', storage);
	assert.equal(getPersonalApiKey('k-exaone_v2.5', storage), undefined);
	assert.equal(getPersonalApiKey('exaone-deep-test', storage), 'deep-secret-2');

	const clamped = clampAgentOptions({ reasoning_effort: 'max', max_tokens: 999999, max_steps: 0, temperature: 5, top_p: 0 });
	assert.deepEqual(clamped, { reasoning_effort: 'max', max_tokens: 32768, max_steps: 1, temperature: 2, top_p: 0.05 });
	saveAgentPreferences({ api: 'exaone-deep-test', options: clamped, attachSelection: false, includeHistory: true, allowDelete: true }, storage);
	const loaded = loadAgentPreferences(storage);
	assert.equal(loaded.api, 'exaone-deep-test');
	assert.equal(loaded.attachSelection, false);
	assert.equal(loaded.allowDelete, true);

	const sent: Array<Record<string, unknown>> = [];
	const sock: {
		send: (d: string) => void;
		close: () => void;
		onmessage: ((e: MessageEvent) => void) | null;
		onerror: ((e: Event) => void) | null;
		onclose: ((e: CloseEvent) => void) | null;
	} = {
		send: d => {
			const msg = JSON.parse(d) as Record<string, unknown>;
			sent.push(msg);
			if (msg.type === 'runtime_execute' && msg.action === 'agent') {
				if (msg.operation === 'status') {
					sock.onmessage?.({
						data: JSON.stringify({
							type: 'runtime_result',
							request_id: msg.request_id,
							ok: true,
							result: {
								ready: true,
								api: msg.api || 'k-exaone_v2.5',
								model: msg.api || 'k-exaone_v2.5',
								telemetry: { api: msg.api || 'k-exaone_v2.5', limits: { second: { remaining: 9, limit: 10 } } },
							},
						}),
					} as MessageEvent);
				} else if (msg.api === 'rate-limited') {
					sock.onmessage?.({
						data: JSON.stringify({
							type: 'runtime_result',
							request_id: msg.request_id,
							ok: false,
							code: 'AGENT_PROVIDER_BUSY',
							message: '한도 초과',
							telemetry: { api: 'rate-limited', limits: { minute: { remaining: 0, limit: 60 } }, retry_at: Date.now() / 1000 + 30 },
						}),
					} as MessageEvent);
				} else {
					sock.onmessage?.({ data: JSON.stringify({ type: 'runtime_output', request_id: msg.request_id, output: 'step 1\n' }) } as MessageEvent);
					sock.onmessage?.({ data: JSON.stringify({ type: 'runtime_result', request_id: msg.request_id, ok: true, result: { answer: 'done', api: 'k-exaone_v2.5' } }) } as MessageEvent);
				}
			}
		},
		close: () => {},
		onmessage: null,
		onerror: null,
		onclose: null,
	};

	const client = new AgoraClient(
		{ origin: 'https://agora.example', canvasId: 7 },
		async () => 'jwt',
		async () => undefined,
		{
			fetch: async () => new Response(JSON.stringify({ ws_port: '8002', canvas_access_token: 'tok' }), { status: 200 }),
			socket: () => sock,
		},
	);
	try {
		const connectPromise = client.ensureConnected();
		await new Promise(r => setTimeout(r, 10));
		sock.onmessage?.({ data: JSON.stringify({ type: 'init_items', items: {}, write_groups: ['g'], groups: ['g'] }) } as MessageEvent);
		await connectPromise;

		const status = await client.executeAgent({ operation: 'status', api: 'exaone-deep-test' });
		assert.equal(status.api, 'exaone-deep-test');
		assert.equal(status.telemetry?.limits?.second?.remaining, 9);

		const chunks: string[] = [];
		const run = await client.executeAgent({ operation: 'run', message: 'hello', options: { reasoning_effort: 'high', max_tokens: 4096 } }, c => chunks.push(c));
		assert.equal(run.answer, 'done');
		assert.deepEqual(chunks, ['step 1\n']);

		await assert.rejects(
			() => client.executeAgent({ operation: 'run', api: 'rate-limited', message: 'hello' }),
			(err: unknown) => {
				assert.ok(err instanceof AgoraError);
				assert.equal(err.code, 'AGENT_PROVIDER_BUSY');
				assert.equal(err.serverMessage, '한도 초과');
				assert.equal((err.payload?.telemetry as { limits?: { minute?: { remaining?: number } } })?.limits?.minute?.remaining, 0);
				return true;
			},
		);
		assert.equal(sent.some(f => f.action === 'agent' && f.operation === 'run'), true);
	} finally {
		client.dispose();
	}
});
