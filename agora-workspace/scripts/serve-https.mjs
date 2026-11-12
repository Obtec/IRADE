import { readFile } from 'node:fs/promises';
import { createServer } from 'node:https';
import { parseArgs } from 'node:util';
import { createEditorApp } from './web-app.mjs';

const { values } = parseArgs({ options: {
	cert: { type: 'string' }, key: { type: 'string' }, host: { type: 'string', default: 'localhost' },
	port: { type: 'string', default: '8080' }, 'folder-uri': { type: 'string' }, sourcesPath: { type: 'string' },
} });
if (!values.cert || !values.key) { throw new Error('Provide --cert and --key for the HTTPS IDE host and its *.host extension subdomains.'); }
const port = Number(values.port);
if (!Number.isInteger(port) || port < 1 || port > 65535) { throw new Error('Invalid port'); }
const app = await createEditorApp({ sourcesPath: values.sourcesPath, folderUri: values['folder-uri'] });
const server = createServer({ key: await readFile(values.key), cert: await readFile(values.cert), minVersion: 'TLSv1.3' }, app.callback());
server.listen(port, values.host, () => console.log(`IRADE Web Editor: https://${values.host}:${port}`));
for (const signal of ['SIGINT', 'SIGTERM']) { process.on(signal, () => server.close()); }
