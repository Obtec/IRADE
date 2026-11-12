import { mkdtemp, mkdir, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, execFileSync } from 'node:child_process';
import { createServer } from 'node:https';
import { createInterface } from 'node:readline';
import { WebSocketServer } from 'ws';

export async function fixture() {
	const temporary = await mkdtemp(join(tmpdir(), 'agora-web-fixture-'));
	await mkdir(join(temporary, 'workspace'));
	execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', join(temporary, 'key.pem'),
		'-out', join(temporary, 'cert.pem'), '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'], { stdio: 'ignore' });
	const hypervisor = process.env.AGORA_HYPERVISOR_DIR || new URL('../../../hypervisor-for-agora/', import.meta.url).pathname;
	const python = spawn('python3', [new URL('web-fixture.py', import.meta.url).pathname,
		resolve(hypervisor, 'runtime/guest/workspace.py'), join(temporary, 'workspace'), join(temporary, 'state')], { stdio: ['pipe', 'pipe', 'inherit'] });
	const responses = createInterface({ input: python.stdout })[Symbol.asyncIterator]();
	const server = createServer({ key: await readFile(join(temporary, 'key.pem')), cert: await readFile(join(temporary, 'cert.pem')) }, (request, response) => {
		const origin = request.headers.origin || '';
		if (/^http:\/\/(?:v--[a-z0-9]+\.)?localhost:8096$/.test(origin)) { response.setHeader('Access-Control-Allow-Origin', origin); }
		response.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type');
		response.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
		if (request.method === 'OPTIONS') { response.writeHead(204).end(); return; }
		if (request.url !== '/api/canvases/42/access' || request.headers.authorization !== 'Bearer fixture-writer') { response.writeHead(403).end(); return; }
		response.setHeader('Content-Type', 'application/json');
		response.end(JSON.stringify({ ws_port: '8002', canvas_access_token: 'fixture-canvas' }));
	});
	const sockets = new WebSocketServer({ server });
	let queue = Promise.resolve();
	sockets.on('connection', socket => {
		socket.send(JSON.stringify({ type: 'init_items', is_admin: false, groups: ['default'], write_groups: ['default'] }));
		socket.on('message', data => {
			queue = queue.then(async () => {
				const event = JSON.parse(String(data));
				if (event.type !== 'workspace_files') { return; }
				const { type, request_id, ...request } = event;
				python.stdin.write(JSON.stringify({ ...request, requestId: `7:${request_id}` }) + '\n');
				const { value, done } = await responses.next();
				if (done) { throw new Error('Guest fixture exited'); }
				const result = JSON.parse(value);
				socket.send(JSON.stringify({ type: 'workspace_files_result', request_id, ok: !result.error, ...(result.error ? { code: result.error } : { result }) }));
				if (!result.error && !['stat', 'read', 'list'].includes(request.operation)) {
					for (const target of sockets.clients) {
						target.send(JSON.stringify({ type: 'workspace_files_changed', operation: request.operation, path: request.path, target: request.target }));
					}
				}
			}).catch(error => { console.error(error); socket.close(); });
		});
	});
	await new Promise(resolve => server.listen(9449, resolve));
	return async () => {
		for (const socket of sockets.clients) { socket.terminate(); }
		sockets.close(); server.close(); python.stdin.end();
		await new Promise(resolve => python.once('exit', resolve));
		await rm(temporary, { recursive: true, force: true });
	};
}
