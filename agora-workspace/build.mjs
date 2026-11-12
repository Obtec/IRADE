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
