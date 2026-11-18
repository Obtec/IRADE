import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import {
	PERF_MAX_SAMPLES,
	PERF_SAMPLE_INTERVAL_MS,
	PerfSampler,
} from '../src/studio/perfMonitor.ts';

test('PerfSampler aggregates 0.2s FPS, frame time, and non-overlapping CPU utilization', () => {
	assert.equal(PERF_SAMPLE_INTERVAL_MS, 200);
	assert.equal(PERF_MAX_SAMPLES, 30);

	const sampler = new PerfSampler();
	sampler.reset(1000);

	// 12 evenly spaced 60Hz frames across [1000..1200] ms
	for (let i = 0; i < 12; i += 1) {
		sampler.recordFrame(1000 + i * (1000 / 60));
	}
	// Overlapping long-task intervals covering 50ms total ([1020..1060] U [1040..1070])
	sampler.recordBusyInterval(1020, 40);
	sampler.recordBusyInterval(1040, 30);
	sampler.recordTimerDrift(10);

	const s1 = sampler.commitWindow(1200, { serverCpuPercent: 24.5, heapUsedMb: 42.1, heapTotalMb: 2048 });
	assert.equal(s1.seq, 1);
	assert.equal(s1.elapsedMs, 200);
	assert.equal(s1.fps, 60);
	assert.equal(s1.frameTimeMs, 16.7);
	assert.equal(s1.clientCpuPercent, 25); // 50ms busy / 200ms = 25%
	assert.equal(s1.serverCpuPercent, 24.5);
	assert.equal(s1.heapUsedMb, 42.1);

	// Zero-frame stalled window -> 0 FPS
	const s2 = sampler.commitWindow(1400, { serverCpuPercent: null });
	assert.equal(s2.seq, 2);
	assert.equal(s2.fps, 0);
	assert.equal(s2.clientCpuPercent, 0);
	assert.equal(s2.serverCpuPercent, null);
});

test('PerfSampler enforces a strict maximum of 30 rolling samples and computes summary stats', () => {
	const sampler = new PerfSampler();
	sampler.reset(0);

	for (let step = 1; step <= 45; step += 1) {
		const winStart = (step - 1) * 200;
		for (let f = 0; f < 6; f += 1) {
			sampler.recordFrame(winStart + f * (200 / 6));
		}
		sampler.recordTimerDrift(step % 2 === 0 ? 40 : 20);
		sampler.commitWindow(winStart + 200, { serverCpuPercent: 10 + step });
	}

	assert.equal(sampler.samples.length, 30);
	assert.equal(sampler.samples[0].seq, 16);
	assert.equal(sampler.samples[29].seq, 45);

	const stats = sampler.getStats();
	assert.equal(stats.count, 30);
	assert.equal(stats.maxSamples, 30);
	assert.equal(stats.intervalMs, 200);
	assert.equal(stats.avgFps, 30);
	assert.equal(stats.peakClientCpu, 20);
	assert.equal(stats.peakServerCpu, 55);

	sampler.reset(9000);
	assert.equal(sampler.samples.length, 0);
	assert.equal(sampler.getStats().latest, null);
});

test('/api/metrics endpoint serves real host CPU, memory, and no-store cache headers', async () => {
	const webAppUrl = pathToFileURL(resolve('scripts/web-app.mjs')).href;
	const { createEditorApp } = await import(webAppUrl) as {
		createEditorApp: (opts?: { basePath?: string; studioPath?: string }) => Promise<{ callback: () => (req: unknown, res: unknown) => void }>;
	};
	const app = await createEditorApp({ basePath: '/irade/' });
	const server = createServer(app.callback());
	await new Promise<void>(resolve => server.listen(0, '127.0.0.1', () => resolve()));
	try {
		const addr = server.address();
		assert.ok(addr && typeof addr === 'object');
		const res = await fetch(`http://127.0.0.1:${addr.port}/api/metrics`);
		assert.equal(res.status, 200);
		assert.equal(res.headers.get('cache-control'), 'no-store');
		assert.equal(res.headers.get('x-content-type-options'), 'nosniff');
		const payload = await res.json() as Record<string, unknown>;
		assert.equal(typeof payload.cpuPercent, 'number');
		assert.equal(typeof payload.processCpuPercent, 'number');
		assert.equal(typeof payload.cores, 'number');
		assert.equal(typeof payload.systemMemPercent, 'number');
		assert.equal(typeof payload.timestamp, 'number');
		assert.ok((payload.cores as number) >= 1);
	} finally {
		await new Promise<void>(resolve => server.close(() => resolve()));
	}
});
