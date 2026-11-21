import { build } from 'esbuild';
import { createRequire } from 'node:module';
import { copyFileSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

const require = createRequire(import.meta.url);
// Embed WASM, so the browser bundle does not fetch an extra cross-origin Automerge asset.
const automerge = resolve(dirname(require.resolve('@automerge/automerge')), 'fullfat_base64.cjs');

await build({
	entryPoints: ['src/studio/main.ts'],
	outfile: 'dist/studio/studio.js',
	bundle: true,
	platform: 'browser',
	format: 'esm',
	target: 'es2022',
	alias: { '@automerge/automerge': automerge },
	loader: { '.ttf': 'dataurl' },
	sourcemap: true,
});

await build({
	entryPoints: ['node_modules/monaco-editor/esm/vs/editor/editor.worker.js'],
	outfile: 'dist/studio/editor.worker.js',
	bundle: true,
	platform: 'browser',
	format: 'esm',
	target: 'es2022',
});

await build({
	entryPoints: ['src/studio/login.ts'],
	outfile: 'dist/studio/login.js',
	bundle: true,
	platform: 'browser',
	format: 'esm',
	target: 'es2022',
	sourcemap: true,
});

mkdirSync('dist/studio', { recursive: true });
rmSync('dist/studio/monaco.css', { force: true });
const monacoCss = readFileSync('dist/studio/studio.css', 'utf8');
const studioCss = readFileSync('studio/studio.css', 'utf8');
writeFileSync('dist/studio/studio.css', `${monacoCss}\n${studioCss}`);
for (const name of ['index.html', 'home.html', 'home.css', 'home.js', 'login.css', 'login.html']) {
	copyFileSync(`studio/${name}`, `dist/studio/${name}`);
}
