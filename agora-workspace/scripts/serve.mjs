import { createServer } from 'node:http';
import { parseArgs } from 'node:util';
import { createEditorApp } from './web-app.mjs';

const { values } = parseArgs({ options: {
	host: { type: 'string', default: 'localhost' }, port: { type: 'string', default: '8080' },
	quality: { type: 'string', default: 'stable' }, 'folder-uri': { type: 'string' }, sourcesPath: { type: 'string' },
} });
const port = Number(values.port);
if (!Number.isInteger(port) || port < 1 || port > 65535) { throw new Error('Invalid port'); }
if (!['stable', 'insider'].includes(values.quality)) { throw new Error('Invalid VS Code quality'); }
const app = await createEditorApp({ sourcesPath: values.sourcesPath, quality: values.quality, folderUri: values['folder-uri'] });
const server = createServer(app.callback());
server.listen(port, values.host, () => console.log(`IRADE Web Editor: http://${values.host}:${port}`));
for (const signal of ['SIGINT', 'SIGTERM']) { process.on(signal, () => server.close()); }
