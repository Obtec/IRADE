import { build } from 'esbuild';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';
const require = createRequire(import.meta.url);
// Embed WASM, so the web extension worker does not fetch an extra cross-origin asset.
const automerge = resolve(dirname(require.resolve('@automerge/automerge')), 'fullfat_base64.cjs');
await build({ entryPoints: ['src/extension.ts'], outfile: 'dist/extension.cjs', bundle: true,
	platform: 'browser', format: 'cjs', target: 'es2022', external: ['vscode'],
	alias: { '@automerge/automerge': automerge }, sourcemap: true,
	banner: { js: `/* Bundled @automerge/automerge 3.5.0\n${readFileSync(resolve(dirname(automerge), '../../LICENSE'), 'utf8')} */` } });

// Standalone workspace shell: no extension gallery, vendor welcome screens or remote assets.
const { mkdir, copyFile } = await import('node:fs/promises');
await mkdir('dist/studio', { recursive: true });
await build({ entryPoints: ['src/studio/main.ts'], outfile: 'dist/studio/studio.js', bundle: true,
  platform: 'browser', format: 'esm', target: 'es2022', alias: { '@automerge/automerge': automerge },
  loader: { '.ttf': 'file' }, assetNames: '[name]-[hash]', minify: true });
await build({ entryPoints: ['node_modules/monaco-editor/esm/vs/editor/editor.worker.js'], outfile: 'dist/studio/editor.worker.js', bundle: true, platform: 'browser', format: 'esm', target: 'es2022', minify: true });
await copyFile('dist/studio/studio.css', 'dist/studio/monaco.css');
const css = readFileSync('studio/studio.css', 'utf8');
const { writeFile } = await import('node:fs/promises');
await writeFile('dist/studio/studio.css', readFileSync('dist/studio/monaco.css', 'utf8') + '\n' + css);
await copyFile('studio/index.html', 'dist/studio/index.html');
