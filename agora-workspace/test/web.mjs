import { build } from 'esbuild';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { mkdir } from 'node:fs/promises';
import { createServer } from 'node:http';
import assert from 'node:assert/strict';
import { fixture } from './web-fixture.mjs';
import { createEditorApp } from '../scripts/web-app.mjs';
const require = createRequire(import.meta.url);
const { chromium } = require(require.resolve('playwright', { paths: [dirname(require.resolve('@vscode/test-web'))] }));
await build({ entryPoints: ['test/webSuite.ts'], outfile: 'dist/webSuite.cjs', bundle: true, platform: 'browser', format: 'cjs', external: ['vscode'],
	alias: { '@automerge/automerge': resolve(dirname(require.resolve('@automerge/automerge')), 'fullfat_base64.cjs') } });
const cleanup = await fixture();
let server, browser;
try {
	const app = await createEditorApp({ extensionTestsPath: resolve('dist/webSuite.cjs') });
	server = createServer(app.callback());
	await new Promise(resolve => server.listen(8096, 'localhost', resolve));
	browser = await chromium.launch({ channel: 'chrome', headless: true, args: ['--no-sandbox', '--ignore-certificate-errors'] });
	const page = await browser.newPage({ ignoreHTTPSErrors: true });
	let finish;
	const result = new Promise(resolve => { finish = resolve; });
	await page.exposeFunction('codeAutomationExit', code => finish(code));
	await page.exposeFunction('codeAutomationLog', (type, args) => console[type](...args));
	page.on('pageerror', error => console.error('Workbench:', error.message));
	page.on('console', message => { if (message.type() === 'error' || message.text().includes('AGORA_')) { console.log(message.text()); } });
	await page.goto('http://localhost:8096/');
	assert.equal(await page.locator('link[rel="icon"]').getAttribute('href'), '/irade-assets/irade-favicon.ico');
	assert.equal(await page.locator('link[rel="icon"]').count(), 1);
	assert.equal(await page.locator('link[rel="apple-touch-icon"]').getAttribute('href'), '/irade-assets/irade-icon-180.png');
	const manifestLink = await page.locator('link[rel="manifest"]').getAttribute('href');
	const manifestResponse = await page.request.get(`http://localhost:8096${manifestLink}`);
	assert.equal(manifestResponse.status(), 200);
	const manifest = await manifestResponse.json();
	assert.equal(manifest.name, 'IRADE Web Editor');
	for (const icon of manifest.icons) {
		const response = await page.request.get(`http://localhost:8096/irade-assets/${icon.src}`);
		assert.equal(response.status(), 200);
		assert.equal(response.headers()['content-type'], 'image/png');
		const bytes = await response.body();
		assert.equal(`${bytes.readUInt32BE(16)}x${bytes.readUInt32BE(20)}`, icon.sizes);
	}
	let timeout;
	const code = await Promise.race([result, new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error('Web suite timed out')), 60000); })]);
	clearTimeout(timeout);
	await mkdir('.artifacts', { recursive: true });
	await page.screenshot({ path: '.artifacts/web-test.png' });
	if (code !== 0) { throw new Error(`Web suite failed: ${code}`); }
} finally { await browser?.close(); server?.close(); await cleanup(); }
