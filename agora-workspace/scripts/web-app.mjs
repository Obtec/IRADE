import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { applyWebBranding } from './web-branding.mjs';

const require = createRequire(import.meta.url);
const serverModules = dirname(require.resolve('@vscode/test-web'));
const createApp = require(resolve(serverModules, 'app.js')).default;
const { downloadAndUnzipVSCode } = require(resolve(serverModules, 'download.js'));
const extension = fileURLToPath(new URL('..', import.meta.url));

export async function createEditorApp({ sourcesPath, buildPath, extensionPath, quality = 'stable', folderUri, extensionTestsPath, basePath = '/', workerOrigin } = {}) {
	const build = sourcesPath ? { type: 'sources', location: resolve(sourcesPath) }
		: buildPath ? { type: 'static', location: resolve(buildPath) }
			: await downloadAndUnzipVSCode(resolve(extension, '.vscode-test-web'), quality);
	const app = await createApp({ build, folderUri,
		...(extensionTestsPath ? { extensionDevelopmentPath: extension, extensionTestsPath } : { extensionPaths: [extensionPath ? resolve(extensionPath) : extension] }) });
	await applyWebBranding(app, { basePath, workerOrigin, sourceBuild: !!sourcesPath });
	return app;
}
