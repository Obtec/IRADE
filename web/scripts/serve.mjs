import { createServer } from 'node:http';
import { parseArgs } from 'node:util';
import { createEditorApp } from './web-app.mjs';

const { values } = parseArgs({
	options: {
		host: { type: 'string', default: 'localhost' },
		port: { type: 'string', default: '8080' },
		'base-path': { type: 'string', default: '/irade/' },
		'studio-path': { type: 'string' },
	},
});
const port = Number(values.port);
if (!Number.isInteger(port) || port < 1 || port > 65535) { throw new Error('Invalid port'); }
const app = await createEditorApp({ basePath: values['base-path'], studioPath: values['studio-path'] });
const server = createServer(app.callback());
server.listen(port, values.host, () => console.log(`IRADE Research Studio: http://${values.host}:${port}`));
for (const signal of ['SIGINT', 'SIGTERM']) { process.on(signal, () => server.close()); }
