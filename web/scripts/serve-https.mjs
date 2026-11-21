import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:https';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { parseArgs } from 'node:util';
import { createEditorApp } from './web-app.mjs';

const { values } = parseArgs({ options: {
	cert: { type: 'string' }, key: { type: 'string' }, 'tls-dir': { type: 'string' },
	host: { type: 'string', default: 'localhost' },
	port: { type: 'string', default: '8080' }, 'folder-uri': { type: 'string' }, sourcesPath: { type: 'string' },
	'build-path': { type: 'string' }, 'base-path': { type: 'string', default: '/' }, 'worker-origin': { type: 'string' },
	'extension-path': { type: 'string' }, 'studio-path': { type: 'string' },
} });

function resolveTlsFiles() {
	let cert = values.cert || process.env.IRADE_WEB_TLS_CERT;
	let key = values.key || process.env.IRADE_WEB_TLS_KEY;
	const tlsDir = values['tls-dir'] || process.env.IRADE_WEB_TLS_DIR;
	if ((!cert || !key) && tlsDir) {
		cert ||= join(tlsDir, 'fullchain.pem');
		key ||= join(tlsDir, 'privkey.pem');
	}
	if (!cert || !key) {
		const domain = process.env.IRADE_PUBLIC_HOSTNAME || 'irade.p-e.kr';
		const candidates = [
			join(homedir(), '.local/share/irade/letsencrypt', domain),
			join(homedir(), '.local/share/irade/tls-dev/internal/frontend'),
			join(homedir(), '.local/share/irade/tls-dev'),
		];
		for (const candidate of candidates) {
			const c = join(candidate, 'fullchain.pem');
			const k = join(candidate, 'privkey.pem');
			if (existsSync(c) && existsSync(k)) {
				cert ||= c;
				key ||= k;
				break;
			}
		}
	}
	if (!cert || !key || !existsSync(cert) || !existsSync(key)) {
		throw new Error(
			'HTTPS-only server requires valid TLS credentials. Pass --cert/--key or --tls-dir, set IRADE_WEB_TLS_CERT/KEY, or provision ~/.local/share/irade/letsencrypt/<domain>.',
		);
	}
	return { cert, key };
}

const tls = resolveTlsFiles();
const port = Number(values.port);
if (!Number.isInteger(port) || port < 1 || port > 65535) { throw new Error('Invalid port'); }
const basePath = values['base-path'];
if (basePath !== '/' && !/^\/[a-zA-Z0-9_-]+\/$/.test(basePath)) { throw new Error('Invalid public base path'); }
const workerOrigin = values['worker-origin'];
if (workerOrigin && (new URL(workerOrigin).protocol !== 'https:' || new URL(workerOrigin).origin !== workerOrigin)) { throw new Error('Worker origin must be an HTTPS origin'); }
const app = await createEditorApp({ studioPath: values['studio-path'], sourcesPath: values.sourcesPath, buildPath: values['build-path'], extensionPath: values['extension-path'], folderUri: values['folder-uri'], basePath, workerOrigin });
const server = createServer({ key: await readFile(tls.key), cert: await readFile(tls.cert), minVersion: 'TLSv1.3' }, app.callback());
server.listen(port, values.host, () => console.log(`IRADE Web Editor: https://${values.host}:${port}`));
for (const signal of ['SIGINT', 'SIGTERM']) { process.on(signal, () => server.close()); }
