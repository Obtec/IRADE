import { mkdtemp, mkdir, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, execFileSync } from 'node:child_process';
import { createServer } from 'node:https';
import { createInterface } from 'node:readline';
import { WebSocketServer } from 'ws';

export async function fixture({ studio } = {}) {
	let writable = true;
	const temporary = await mkdtemp(join(tmpdir(), 'agora-web-fixture-'));
	await mkdir(join(temporary, 'workspace'));
	execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', join(temporary, 'key.pem'),
		'-out', join(temporary, 'cert.pem'), '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'], { stdio: 'ignore' });
	const hypervisor = process.env.AGORA_HYPERVISOR_DIR || new URL('../../../hypervisor-for-agora/', import.meta.url).pathname;
	const python = spawn('python3', [new URL('web-fixture.py', import.meta.url).pathname,
		resolve(hypervisor, 'runtime/guest/workspace.py'), join(temporary, 'workspace'), join(temporary, 'state')], { stdio: ['pipe', 'pipe', 'inherit'] });
	const responses = createInterface({ input: python.stdout })[Symbol.asyncIterator]();
	const server = createServer({ key: await readFile(join(temporary, 'key.pem')), cert: await readFile(join(temporary, 'cert.pem')) }, (request, response) => {
		if (studio && !request.url.startsWith('/api/') && !request.url.startsWith('/wss/')) { request.url=request.url.replace(/^\/irade(?=\/|\?|$)/,'') || '/'; studio(request, response); return; }
		const origin = request.headers.origin || '';
		if (/^http:\/\/(?:v--[a-z0-9]+\.)?localhost:8096$/.test(origin)) { response.setHeader('Access-Control-Allow-Origin', origin); }
		response.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type');
		response.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
		if (request.method === 'OPTIONS') { response.writeHead(204).end(); return; }
		if (studio && request.url === '/api/irade/auth/orcid/status') { response.setHeader('Content-Type','application/json'); response.end(JSON.stringify({ available:true, issuer:'https://orcid.org' })); return; }
        if (studio && request.url.startsWith('/api/irade/auth/orcid/start')) { const url=new URL(request.url,'https://localhost:9449'); response.setHeader('Set-Cookie','fixture_handoff=1; HttpOnly; Secure; SameSite=Lax; Path=/'); response.writeHead(303,{Location:'/irade/login?orcid=success'+(url.searchParams.has('workspace')?'&workspace='+url.searchParams.get('workspace'):'')}).end(); return; }
        if (studio && request.url === '/api/irade/auth/session' && request.method === 'POST') { response.setHeader('Content-Type','application/json'); if(!(request.headers.cookie || '').includes('fixture_handoff=1')) { response.writeHead(401).end(JSON.stringify({message:'Expired fixture handoff'})); return; } response.setHeader('Set-Cookie','fixture_handoff=; Max-Age=0; Path=/'); response.end(JSON.stringify({login:(request.headers.cookie || '').includes('fixture_mfa=1')?{mfaRequired:true,challengeToken:'fixture-challenge'}:{accessToken:'fixture-writer'},orcid:'0000-0002-1825-0097',issuer:'https://orcid.org'})); return; }
        if (studio && request.url === '/api/irade/auth/me' && request.headers.authorization === 'Bearer fixture-writer') { response.setHeader('Content-Type','application/json'); response.end(JSON.stringify({userId:7,name:'Fixture researcher',orcid:'0000-0002-1825-0097',issuer:'https://orcid.org',profileUrl:'https://orcid.org/0000-0002-1825-0097'})); return; }
        if (studio && request.url.startsWith('/api/irade/workspaces') && !request.url.endsWith('/access') && request.method === 'GET' && request.headers.authorization === 'Bearer fixture-writer') { const url=new URL(request.url,'https://localhost:9449'); response.setHeader('Content-Type','application/json'); const workspace={workspaceId:42,name:'공동 연구 workspace',description:'Shared research'}; response.end(JSON.stringify(workspace.name.includes(url.searchParams.get('q') || '')?[workspace]:[])); return; }
		if (studio && request.url === '/api/auth/login' && request.method === 'POST') {
			let body = ''; request.on('data', chunk => body += chunk); request.on('end', () => {
				const data = JSON.parse(body);
				response.setHeader('Content-Type', 'application/json');
				if (data.password === 'fixture-password') { response.end(JSON.stringify(data.email === 'mfa@example.invalid' ? { mfaRequired: true, challengeToken: 'fixture-challenge' } : { accessToken: 'fixture-writer' })); }
				else { response.writeHead(401).end(JSON.stringify({ message: 'Wrong fixture password' })); }
			}); return;
		}
		if (studio && request.url === '/api/auth/mfa/verify' && request.method === 'POST') {
			let body = ''; request.on('data', chunk => body += chunk); request.on('end', () => { const data = JSON.parse(body); response.setHeader('Content-Type', 'application/json'); if (data.code === '123456' && data.challengeToken === 'fixture-challenge') { response.end(JSON.stringify({ accessToken: 'fixture-writer' })); } else { response.writeHead(400).end(JSON.stringify({ message: '잘못된 인증 코드' })); } }); return;
		}
		if (studio && request.url === '/api/canvases' && request.method === 'GET' && request.headers.authorization === 'Bearer fixture-writer') { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify([{ canvas_id: 42, canvas_name: '공동 연구 workspace' }])); return; }
		if (!['/api/canvases/42/access','/api/irade/workspaces/42/access'].includes(request.url) || request.headers.authorization !== 'Bearer fixture-writer') { response.writeHead(403).end(); return; }
		response.setHeader('Content-Type', 'application/json');
		response.end(JSON.stringify({ ws_port: '8002', canvas_access_token: 'fixture-canvas' }));
	});
	const sockets = new WebSocketServer({ server });
	let queue = Promise.resolve();
	sockets.on('connection', socket => {
		socket.send(JSON.stringify({ type: 'init_items', is_admin: false, groups: ['default'], write_groups: writable ? ['default'] : [] }));
		socket.on('message', data => {
			queue = queue.then(async () => {
				const event = JSON.parse(String(data));
				if (event.type !== 'workspace_files') { return; }
				const { type, request_id, ...request } = event;
				if (!writable && !['read','stat','list'].includes(request.operation)) { socket.send(JSON.stringify({ type: 'workspace_files_result', request_id, ok: false, code: 'WORKSPACE_ACCESS_DENIED' })); return; }
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
	const cleanup = async () => {
		for (const socket of sockets.clients) { socket.terminate(); }
		sockets.close(); server.close(); python.stdin.end();
		await new Promise(resolve => python.once('exit', resolve));
		await rm(temporary, { recursive: true, force: true });
	};
	cleanup.setWritable = value => { writable = value; for (const socket of sockets.clients) { socket.send(JSON.stringify({ type: 'canvas_access_changed', is_admin: false, write_groups: value ? ['default'] : [] })); } };
	return cleanup;
}
