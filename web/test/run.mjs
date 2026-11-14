import { build } from 'esbuild';
import { createRequire } from 'node:module';
import { mkdtemp, readdir, rm } from 'node:fs/promises';
import { dirname, resolve, join } from 'node:path';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
const require = createRequire(import.meta.url);
const temporary = await mkdtemp(join(tmpdir(), 'agora-workspace-tests-'));
try {
	const tests = (await readdir(new URL('.', import.meta.url))).filter(name => name.endsWith('.test.ts'));
	await build({ entryPoints: tests.map(name => new URL(name, import.meta.url).pathname), outdir: temporary,
		bundle: true, platform: 'node', format: 'cjs', outExtension: { '.js': '.cjs' },
		alias: { '@automerge/automerge': resolve(dirname(require.resolve('@automerge/automerge')), 'fullfat_base64.cjs') } });
	const result = spawnSync(process.execPath, ['--test', ...tests.map(name => join(temporary, name.replace('.ts', '.cjs')))], { stdio: 'inherit' });
	process.exitCode = result.status ?? 1;
} finally { await rm(temporary, { recursive: true, force: true }); }
