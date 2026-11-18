export const PERF_SAMPLE_INTERVAL_MS = 200;
export const PERF_MAX_SAMPLES = 30;
const PROBE_STEP_MS = 25;
const PROBE_BASELINE_SLACK_MS = 2.0;

export interface ServerMetricsSnapshot {
	cpuPercent: number;
	processCpuPercent: number;
	cores: number;
	memUsedMb: number;
	memTotalMb: number;
	systemMemPercent: number;
	rssMb: number;
	heapUsedMb: number;
	loadAvg1: number;
	timestamp: number;
}

export interface PerfSnapshotExtras {
	timestampMs?: number;
	serverCpuPercent?: number | null;
	heapUsedMb?: number | null;
	heapTotalMb?: number | null;
}

export interface PerfSample {
	seq: number;
	timestampMs: number;
	elapsedMs: number;
	fps: number;
	frameTimeMs: number;
	clientCpuPercent: number;
	serverCpuPercent: number | null;
	heapUsedMb: number | null;
	heapTotalMb: number | null;
}

export interface PerfMonitorStats {
	count: number;
	maxSamples: number;
	intervalMs: number;
	latest: PerfSample | null;
	avgFps: number;
	minFps: number;
	maxFps: number;
	avgFrameTimeMs: number;
	maxFrameTimeMs: number;
	avgClientCpu: number;
	peakClientCpu: number;
	avgServerCpu: number | null;
	peakServerCpu: number | null;
}

export interface PerfMonitorHostContext {
	getCellCount: () => number;
	getEditorCount: () => number;
	getPreviewCount: () => number;
	getConnectionLabel: () => string;
}

interface BusySpan {
	startMs: number;
	endMs: number;
}

const round1 = (value: number): number => Math.round(value * 10) / 10;
const clamp = (value: number, min: number, max: number): number => Math.min(max, Math.max(min, value));
const escapeHtml = (value: string): string => value.replace(/[&<>"']/g, ch => ({
	'&': '&amp;',
	'<': '&lt;',
	'>': '&gt;',
	'"': '&quot;',
	"'": '&#39;',
})[ch]!);

function mergeBusyDuration(spans: BusySpan[], winStart: number, winEnd: number): { busyMs: number; carry: BusySpan[] } {
	const clipped: BusySpan[] = [];
	const carry: BusySpan[] = [];
	for (const span of spans) {
		if (span.endMs > winEnd) {
			carry.push({ startMs: Math.max(span.startMs, winEnd), endMs: span.endMs });
		}
		if (span.endMs <= winStart || span.startMs >= winEnd) { continue; }
		clipped.push({
			startMs: Math.max(span.startMs, winStart),
			endMs: Math.min(span.endMs, winEnd),
		});
	}
	if (!clipped.length) { return { busyMs: 0, carry }; }
	clipped.sort((a, b) => a.startMs - b.startMs);
	let busyMs = 0;
	let curStart = clipped[0].startMs;
	let curEnd = clipped[0].endMs;
	for (let i = 1; i < clipped.length; i += 1) {
		const item = clipped[i];
		if (item.startMs <= curEnd) {
			curEnd = Math.max(curEnd, item.endMs);
		} else {
			busyMs += curEnd - curStart;
			curStart = item.startMs;
			curEnd = item.endMs;
		}
	}
	busyMs += curEnd - curStart;
	return { busyMs, carry };
}

/**
 * Pure rolling-window telemetry aggregator.
 * Aggregates animation frames and main-thread busyness every 0.2s up to 30 samples.
 */
export class PerfSampler {
	readonly maxSamples: number;
	readonly samples: PerfSample[] = [];
	longTaskCount = 0;
	stutterCount = 0;

	private seq = 0;
	private windowStartMs = 0;
	private frameCount = 0;
	private frameDeltaSumMs = 0;
	private frameDeltaCount = 0;
	private maxFrameDeltaMs = 0;
	private lastFrameMs: number | null = null;
	private driftBusyMs = 0;
	private busySpans: BusySpan[] = [];

	constructor(maxSamples = PERF_MAX_SAMPLES) {
		this.maxSamples = Math.max(1, maxSamples);
	}

	reset(nowMs = 0): void {
		this.samples.length = 0;
		this.seq = 0;
		this.longTaskCount = 0;
		this.stutterCount = 0;
		this.windowStartMs = nowMs;
		this.frameCount = 0;
		this.frameDeltaSumMs = 0;
		this.frameDeltaCount = 0;
		this.maxFrameDeltaMs = 0;
		this.lastFrameMs = null;
		this.driftBusyMs = 0;
		this.busySpans.length = 0;
	}

	startWindow(nowMs: number): void {
		this.windowStartMs = nowMs;
		this.frameCount = 0;
		this.frameDeltaSumMs = 0;
		this.frameDeltaCount = 0;
		this.maxFrameDeltaMs = 0;
		this.lastFrameMs = null;
		this.driftBusyMs = 0;
		this.busySpans.length = 0;
	}

	recordFrame(nowMs: number): void {
		this.frameCount += 1;
		if (this.lastFrameMs !== null) {
			const dt = clamp(nowMs - this.lastFrameMs, 0, 1000);
			this.frameDeltaSumMs += dt;
			this.frameDeltaCount += 1;
			this.maxFrameDeltaMs = Math.max(this.maxFrameDeltaMs, dt);
			if (dt > 34) {
				this.stutterCount += 1;
			}
		}
		this.lastFrameMs = nowMs;
	}

	recordBusyInterval(startMs: number, durationMs: number): void {
		if (!Number.isFinite(startMs) || !Number.isFinite(durationMs) || durationMs <= 0) { return; }
		this.busySpans.push({ startMs, endMs: startMs + durationMs });
		if (durationMs >= 50) {
			this.longTaskCount += 1;
		}
	}

	recordTimerDrift(excessLagMs: number): void {
		if (!Number.isFinite(excessLagMs) || excessLagMs <= 0) { return; }
		this.driftBusyMs += excessLagMs;
	}

	commitWindow(nowMs: number, extras: PerfSnapshotExtras = {}): PerfSample {
		const elapsedMs = Math.max(1, nowMs - this.windowStartMs);
		let fps = 0;
		let frameTimeMs = round1(elapsedMs);

		if (this.frameCount === 1) {
			fps = Math.round(clamp(1000 / elapsedMs, 0, 240));
			frameTimeMs = round1(this.maxFrameDeltaMs > 0 ? this.maxFrameDeltaMs : elapsedMs);
		} else if (this.frameCount >= 2) {
			const meanDt = this.frameDeltaCount > 0
				? this.frameDeltaSumMs / this.frameDeltaCount
				: elapsedMs / this.frameCount;
			const countFps = (this.frameCount * 1000) / elapsedMs;
			const tailGap = this.lastFrameMs === null ? 0 : Math.max(0, nowMs - this.lastFrameMs);
			if (tailGap > Math.max(80, meanDt * 2.5) || meanDt <= 0.1) {
				fps = Math.round(clamp(countFps, 0, 240));
			} else if (Math.abs(this.frameCount - (elapsedMs / meanDt)) <= 1.25) {
				fps = Math.round(clamp(1000 / meanDt, 0, 240));
			} else {
				fps = Math.round(clamp(countFps, 0, 240));
			}
			frameTimeMs = round1(meanDt);
		}

		const { busyMs: observerBusyMs, carry } = mergeBusyDuration(this.busySpans, this.windowStartMs, nowMs);
		const combinedBusyMs = clamp(Math.max(observerBusyMs, this.driftBusyMs), 0, elapsedMs);
		const clientCpuPercent = round1(clamp((combinedBusyMs / elapsedMs) * 100, 0, 100));

		const sample: PerfSample = {
			seq: ++this.seq,
			timestampMs: extras.timestampMs ?? Date.now(),
			elapsedMs: Math.round(elapsedMs),
			fps,
			frameTimeMs,
			clientCpuPercent,
			serverCpuPercent: extras.serverCpuPercent ?? null,
			heapUsedMb: extras.heapUsedMb ?? null,
			heapTotalMb: extras.heapTotalMb ?? null,
		};

		this.samples.push(sample);
		while (this.samples.length > this.maxSamples) {
			this.samples.shift();
		}

		this.windowStartMs = nowMs;
		this.frameCount = 0;
		this.frameDeltaSumMs = 0;
		this.frameDeltaCount = 0;
		this.maxFrameDeltaMs = 0;
		this.driftBusyMs = 0;
		this.busySpans = carry;
		if (this.lastFrameMs !== null && (nowMs - this.lastFrameMs) > 500) {
			this.lastFrameMs = null;
		}
		return sample;
	}

	getStats(): PerfMonitorStats {
		const count = this.samples.length;
		if (!count) {
			return {
				count: 0,
				maxSamples: this.maxSamples,
				intervalMs: PERF_SAMPLE_INTERVAL_MS,
				latest: null,
				avgFps: 0,
				minFps: 0,
				maxFps: 0,
				avgFrameTimeMs: 0,
				maxFrameTimeMs: 0,
				avgClientCpu: 0,
				peakClientCpu: 0,
				avgServerCpu: null,
				peakServerCpu: null,
			};
		}

		let sumFps = 0;
		let minFps = Infinity;
		let maxFps = -Infinity;
		let sumFt = 0;
		let maxFt = 0;
		let sumClientCpu = 0;
		let peakClientCpu = 0;
		let sumServerCpu = 0;
		let peakServerCpu = 0;
		let serverCount = 0;

		for (const item of this.samples) {
			sumFps += item.fps;
			minFps = Math.min(minFps, item.fps);
			maxFps = Math.max(maxFps, item.fps);
			sumFt += item.frameTimeMs;
			maxFt = Math.max(maxFt, item.frameTimeMs);
			sumClientCpu += item.clientCpuPercent;
			peakClientCpu = Math.max(peakClientCpu, item.clientCpuPercent);
			if (item.serverCpuPercent !== null) {
				sumServerCpu += item.serverCpuPercent;
				peakServerCpu = Math.max(peakServerCpu, item.serverCpuPercent);
				serverCount += 1;
			}
		}

		return {
			count,
			maxSamples: this.maxSamples,
			intervalMs: PERF_SAMPLE_INTERVAL_MS,
			latest: this.samples[count - 1],
			avgFps: Math.round(sumFps / count),
			minFps,
			maxFps,
			avgFrameTimeMs: round1(sumFt / count),
			maxFrameTimeMs: round1(maxFt),
			avgClientCpu: round1(sumClientCpu / count),
			peakClientCpu: round1(peakClientCpu),
			avgServerCpu: serverCount > 0 ? round1(sumServerCpu / serverCount) : null,
			peakServerCpu: serverCount > 0 ? round1(peakServerCpu) : null,
		};
	}
}

interface ChartSeries {
	label: string;
	stroke: string;
	fillTop: string;
	fillBottom: string;
	dashed?: boolean;
	values: (number | null)[];
}

interface ChartConfig {
	maxSamples: number;
	minY: number;
	maxY: number;
	gridTicks: number[];
	formatTick: (value: number) => string;
	targetLine?: { value: number; label: string };
	series: ChartSeries[];
}

interface ThemePalette {
	bg: string;
	border: string;
	grid: string;
	textMuted: string;
	textPrimary: string;
	accent: string;
	accentSoftTop: string;
	accentSoftBottom: string;
	blue: string;
	blueSoftTop: string;
	blueSoftBottom: string;
	crosshair: string;
}

function readThemePalette(): ThemePalette {
	const styles = getComputedStyle(document.documentElement);
	const isLight = document.documentElement.getAttribute('data-theme') === 'light';
	const accent = styles.getPropertyValue('--accent').trim() || (isLight ? '#059669' : '#34d399');
	const blue = styles.getPropertyValue('--accent-blue').trim() || (isLight ? '#0284c7' : '#38bdf8');
	return {
		bg: styles.getPropertyValue('--bg-main').trim() || (isLight ? '#ffffff' : '#1f1f1f'),
		border: styles.getPropertyValue('--border').trim() || (isLight ? '#e5e7eb' : '#2b2b2b'),
		grid: isLight ? 'rgba(15, 23, 42, 0.08)' : 'rgba(255, 255, 255, 0.07)',
		textMuted: styles.getPropertyValue('--text-muted').trim() || (isLight ? '#6b7280' : '#8b949e'),
		textPrimary: styles.getPropertyValue('--text-primary').trim() || (isLight ? '#111827' : '#ffffff'),
		accent,
		accentSoftTop: isLight ? 'rgba(5, 150, 105, 0.24)' : 'rgba(52, 211, 153, 0.26)',
		accentSoftBottom: isLight ? 'rgba(5, 150, 105, 0.01)' : 'rgba(52, 211, 153, 0.01)',
		blue,
		blueSoftTop: isLight ? 'rgba(2, 132, 199, 0.22)' : 'rgba(56, 189, 248, 0.24)',
		blueSoftBottom: isLight ? 'rgba(2, 132, 199, 0.01)' : 'rgba(56, 189, 248, 0.01)',
		crosshair: isLight ? 'rgba(15, 23, 42, 0.35)' : 'rgba(255, 255, 255, 0.35)',
	};
}

function renderTimeSeriesChart(
	canvas: HTMLCanvasElement,
	config: ChartConfig,
	sampleCount: number,
	hoveredSampleIndex: number | null,
	palette: ThemePalette,
): void {
	const cssW = Math.max(200, Math.round(canvas.clientWidth || canvas.getBoundingClientRect().width || 280));
	const cssH = Math.max(110, Math.round(canvas.clientHeight || canvas.getBoundingClientRect().height || 144));
	const dpr = Math.min(2.5, Math.max(1, window.devicePixelRatio || 1));
	const targetW = Math.round(cssW * dpr);
	const targetH = Math.round(cssH * dpr);
	if (canvas.width !== targetW || canvas.height !== targetH) {
		canvas.width = targetW;
		canvas.height = targetH;
	}

	const ctx = canvas.getContext('2d');
	if (!ctx) { return; }
	ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

	ctx.fillStyle = palette.bg;
	ctx.fillRect(0, 0, cssW, cssH);

	const padL = 36;
	const padR = 10;
	const padT = 12;
	const padB = 22;
	const plotW = Math.max(40, cssW - padL - padR);
	const plotH = Math.max(40, cssH - padT - padB);
	const spanY = Math.max(1, config.maxY - config.minY);
	const slotOffset = Math.max(0, config.maxSamples - sampleCount);
	const slotX = (slot: number): number => padL + (slot / Math.max(1, config.maxSamples - 1)) * plotW;
	const valueY = (val: number): number => padT + (1 - clamp((val - config.minY) / spanY, 0, 1)) * plotH;

	ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace';
	ctx.textBaseline = 'middle';

	for (const tick of config.gridTicks) {
		const y = valueY(tick);
		ctx.strokeStyle = palette.grid;
		ctx.lineWidth = 1;
		ctx.setLineDash([3, 3]);
		ctx.beginPath();
		ctx.moveTo(padL, y);
		ctx.lineTo(padL + plotW, y);
		ctx.stroke();
		ctx.setLineDash([]);

		ctx.fillStyle = palette.textMuted;
		ctx.textAlign = 'right';
		ctx.fillText(config.formatTick(tick), padL - 5, y);
	}

	if (config.targetLine && config.targetLine.value <= config.maxY) {
		const y = valueY(config.targetLine.value);
		ctx.strokeStyle = palette.accent;
		ctx.globalAlpha = 0.45;
		ctx.lineWidth = 1;
		ctx.setLineDash([4, 4]);
		ctx.beginPath();
		ctx.moveTo(padL, y);
		ctx.lineTo(padL + plotW, y);
		ctx.stroke();
		ctx.setLineDash([]);
		ctx.globalAlpha = 0.8;
		ctx.fillStyle = palette.accent;
		ctx.textAlign = 'left';
		ctx.fillText(config.targetLine.label, padL + 4, Math.max(padT + 6, y - 7));
		ctx.globalAlpha = 1;
	}

	// X-axis bottom border & labels (-6.0s .. 0.0s)
	ctx.strokeStyle = palette.border;
	ctx.lineWidth = 1;
	ctx.beginPath();
	ctx.moveTo(padL, padT + plotH);
	ctx.lineTo(padL + plotW, padT + plotH);
	ctx.stroke();

	ctx.fillStyle = palette.textMuted;
	ctx.textBaseline = 'top';
	ctx.textAlign = 'left';
	ctx.fillText(`-${((config.maxSamples * PERF_SAMPLE_INTERVAL_MS) / 1000).toFixed(1)}s`, padL, padT + plotH + 5);
	ctx.textAlign = 'center';
	ctx.fillText(`-${((config.maxSamples * PERF_SAMPLE_INTERVAL_MS) / 2000).toFixed(1)}s`, padL + plotW / 2, padT + plotH + 5);
	ctx.textAlign = 'right';
	ctx.fillText('0.0s', padL + plotW, padT + plotH + 5);

	if (sampleCount === 0) {
		ctx.fillStyle = palette.textMuted;
		ctx.textAlign = 'center';
		ctx.textBaseline = 'middle';
		ctx.font = '11px Inter, -apple-system, BlinkMacSystemFont, sans-serif';
		ctx.fillText('0.2초 간격 실시간 데이터 수집 중…', padL + plotW / 2, padT + plotH / 2);
		return;
	}

	for (const series of config.series) {
		const pts: { x: number; y: number; index: number }[] = [];
		for (let i = 0; i < sampleCount; i += 1) {
			const v = series.values[i];
			if (v === null || !Number.isFinite(v)) { continue; }
			pts.push({ x: slotX(slotOffset + i), y: valueY(v), index: i });
		}
		if (!pts.length) { continue; }

		if (pts.length >= 2) {
			const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
			grad.addColorStop(0, series.fillTop);
			grad.addColorStop(1, series.fillBottom);
			ctx.fillStyle = grad;
			ctx.beginPath();
			ctx.moveTo(pts[0].x, padT + plotH);
			for (const pt of pts) {
				ctx.lineTo(pt.x, pt.y);
			}
			ctx.lineTo(pts[pts.length - 1].x, padT + plotH);
			ctx.closePath();
			ctx.fill();

			ctx.strokeStyle = series.stroke;
			ctx.lineWidth = 1.9;
			ctx.lineJoin = 'round';
			ctx.lineCap = 'round';
			if (series.dashed) { ctx.setLineDash([5, 3]); }
			ctx.beginPath();
			ctx.moveTo(pts[0].x, pts[0].y);
			for (let k = 1; k < pts.length; k += 1) {
				ctx.lineTo(pts[k].x, pts[k].y);
			}
			ctx.stroke();
			ctx.setLineDash([]);
		}

		for (let k = 0; k < pts.length; k += 1) {
			const pt = pts[k];
			const isHovered = hoveredSampleIndex !== null && pt.index === hoveredSampleIndex;
			const isLatest = k === pts.length - 1 && hoveredSampleIndex === null;
			ctx.fillStyle = series.stroke;
			ctx.beginPath();
			ctx.arc(pt.x, pt.y, isHovered || isLatest ? 3.2 : 1.9, 0, Math.PI * 2);
			ctx.fill();
		}
	}

	if (hoveredSampleIndex !== null && hoveredSampleIndex >= 0 && hoveredSampleIndex < sampleCount) {
		const hx = slotX(slotOffset + hoveredSampleIndex);
		ctx.strokeStyle = palette.crosshair;
		ctx.lineWidth = 1;
		ctx.setLineDash([2, 2]);
		ctx.beginPath();
		ctx.moveTo(hx, padT);
		ctx.lineTo(hx, padT + plotH);
		ctx.stroke();
		ctx.setLineDash([]);
	}
}

function readBrowserHeapMb(): { usedMb: number | null; totalMb: number | null } {
	const perfWithMem = performance as Performance & {
		memory?: { usedJSHeapSize: number; totalJSHeapSize: number; jsHeapSizeLimit: number };
	};
	const mem = perfWithMem.memory;
	if (!mem || !Number.isFinite(mem.usedJSHeapSize)) {
		return { usedMb: null, totalMb: null };
	}
	return {
		usedMb: Math.round((mem.usedJSHeapSize / (1024 * 1024)) * 10) / 10,
		totalMb: Math.round((mem.jsHeapSizeLimit / (1024 * 1024)) * 10) / 10,
	};
}

function resolveMetricsEndpoint(): string {
	const usesIradePrefix = location.pathname.startsWith('/irade')
		|| Boolean(document.querySelector('script[src*="/irade-assets/"]'));
	return usesIradePrefix ? '/irade/api/metrics' : '/api/metrics';
}

/**
 * Live Studio performance telemetry view & 0.2s x 30-sample collector for the Settings panel.
 */
export class PerfMonitorController {
	private root: HTMLElement;
	private host: PerfMonitorHostContext;
	private sampler = new PerfSampler(PERF_MAX_SAMPLES);
	private running = false;
	private paused = false;
	private rafId = 0;
	private bucketTimer: ReturnType<typeof setInterval> | null = null;
	private probeTimer: ReturnType<typeof setTimeout> | null = null;
	private expectedProbeAt = 0;
	private observer: PerformanceObserver | null = null;
	private resizeObserver: ResizeObserver | null = null;
	private abortCtrl: AbortController | null = null;
	private serverFetchInFlight = false;
	private latestServer: ServerMetricsSnapshot | null = null;
	private serverRttMs: number | null = null;
	private serverEndpoint = resolveMetricsEndpoint();
	private hoveredIndex: number | null = null;

	private fpsChart!: HTMLCanvasElement;
	private cpuChart!: HTMLCanvasElement;
	private countEl!: HTMLElement;
	private stateBadgeEl!: HTMLElement;
	private toggleBtn!: HTMLButtonElement;
	private fpsValueEl!: HTMLElement;
	private fpsDetailEl!: HTMLElement;
	private cpuValueEl!: HTMLElement;
	private cpuDetailEl!: HTMLElement;
	private memValueEl!: HTMLElement;
	private memDetailEl!: HTMLElement;
	private envValueEl!: HTMLElement;
	private envDetailEl!: HTMLElement;
	private fpsStatsEl!: HTMLElement;
	private cpuStatsEl!: HTMLElement;
	private hoverReadoutEl!: HTMLElement;
	private detailGridEl!: HTMLElement;

	private onVisibilityBound = (): void => {
		if (!this.running || this.paused) { return; }
		if (document.hidden) {
			this.stopLoops();
		} else {
			this.sampler.startWindow(performance.now());
			this.startLoops();
		}
	};

	constructor(root: HTMLElement, host: PerfMonitorHostContext) {
		this.root = root;
		this.host = host;
		this.renderSkeleton();
		this.bindEvents();
	}

	private renderSkeleton(): void {
		this.root.innerHTML = `
      <div class="monitor-toolbar">
        <div class="monitor-meta">
          <span class="monitor-state-badge" data-monitor-state>수집 중</span>
          <span class="monitor-window-label">0.2초당 1회 집계 · 최대 30개 (6.0초)</span>
          <span class="monitor-count-pill" data-monitor-count>0 / 30</span>
        </div>
        <div class="monitor-controls">
          <button type="button" class="button tiny" data-monitor-toggle>일시정지</button>
          <button type="button" class="button tiny" data-monitor-reset>초기화</button>
        </div>
      </div>

      <div class="monitor-kpi-grid">
        <div class="monitor-kpi-card">
          <span class="monitor-kpi-label">FPS</span>
          <strong class="monitor-kpi-value" data-monitor-fps>-- FPS</strong>
          <small class="monitor-kpi-sub" data-monitor-frame-time>프레임 타임 -- ms</small>
        </div>
        <div class="monitor-kpi-card">
          <span class="monitor-kpi-label">CPU 사용률</span>
          <strong class="monitor-kpi-value" data-monitor-cpu>-- %</strong>
          <small class="monitor-kpi-sub" data-monitor-cpu-detail>메인 스레드 측정 대기</small>
        </div>
        <div class="monitor-kpi-card">
          <span class="monitor-kpi-label">메모리</span>
          <strong class="monitor-kpi-value" data-monitor-mem>-- MB</strong>
          <small class="monitor-kpi-sub" data-monitor-mem-detail>JS 힙 · 서버 RAM</small>
        </div>
        <div class="monitor-kpi-card">
          <span class="monitor-kpi-label">런타임 · 상태</span>
          <strong class="monitor-kpi-value" data-monitor-env>--</strong>
          <small class="monitor-kpi-sub" data-monitor-env-detail>에디터 · 셀 · 코어</small>
        </div>
      </div>

      <div class="monitor-charts-grid">
        <section class="monitor-chart-card" aria-label="FPS 0.2초 집계 그래프">
          <header class="monitor-chart-head">
            <div>
              <strong>FPS 추이</strong>
              <small>프레임/초 (60 FPS 목표선)</small>
            </div>
            <span class="monitor-chart-stats" data-monitor-fps-stats>avg -- · min -- · max --</span>
          </header>
          <div class="monitor-canvas-wrap">
            <canvas class="monitor-canvas" data-monitor-chart="fps"></canvas>
          </div>
        </section>

        <section class="monitor-chart-card" aria-label="CPU 사용률 0.2초 집계 그래프">
          <header class="monitor-chart-head">
            <div>
              <strong>CPU 사용률 추이</strong>
              <small class="monitor-legend"><i class="leg-client"></i>브라우저 <i class="leg-server"></i>서버</small>
            </div>
            <span class="monitor-chart-stats" data-monitor-cpu-stats>브라우저 --% · 서버 --%</span>
          </header>
          <div class="monitor-canvas-wrap">
            <canvas class="monitor-canvas" data-monitor-chart="cpu"></canvas>
          </div>
        </section>
      </div>

      <div class="monitor-hover-bar" data-monitor-hover>
        그래프에 마우스를 올리면 0.2초 단위 상세 시점을 확인할 수 있습니다.
      </div>

      <div class="monitor-detail-grid" data-monitor-details></div>
    `;

		this.fpsChart = this.root.querySelector<HTMLCanvasElement>('[data-monitor-chart="fps"]')!;
		this.cpuChart = this.root.querySelector<HTMLCanvasElement>('[data-monitor-chart="cpu"]')!;
		this.countEl = this.root.querySelector<HTMLElement>('[data-monitor-count]')!;
		this.stateBadgeEl = this.root.querySelector<HTMLElement>('[data-monitor-state]')!;
		this.toggleBtn = this.root.querySelector<HTMLButtonElement>('[data-monitor-toggle]')!;
		this.fpsValueEl = this.root.querySelector<HTMLElement>('[data-monitor-fps]')!;
		this.fpsDetailEl = this.root.querySelector<HTMLElement>('[data-monitor-frame-time]')!;
		this.cpuValueEl = this.root.querySelector<HTMLElement>('[data-monitor-cpu]')!;
		this.cpuDetailEl = this.root.querySelector<HTMLElement>('[data-monitor-cpu-detail]')!;
		this.memValueEl = this.root.querySelector<HTMLElement>('[data-monitor-mem]')!;
		this.memDetailEl = this.root.querySelector<HTMLElement>('[data-monitor-mem-detail]')!;
		this.envValueEl = this.root.querySelector<HTMLElement>('[data-monitor-env]')!;
		this.envDetailEl = this.root.querySelector<HTMLElement>('[data-monitor-env-detail]')!;
		this.fpsStatsEl = this.root.querySelector<HTMLElement>('[data-monitor-fps-stats]')!;
		this.cpuStatsEl = this.root.querySelector<HTMLElement>('[data-monitor-cpu-stats]')!;
		this.hoverReadoutEl = this.root.querySelector<HTMLElement>('[data-monitor-hover]')!;
		this.detailGridEl = this.root.querySelector<HTMLElement>('[data-monitor-details]')!;
	}

	private bindEvents(): void {
		this.toggleBtn.addEventListener('click', () => {
			if (this.paused) {
				this.resume();
			} else {
				this.pause();
			}
		});
		this.root.querySelector<HTMLButtonElement>('[data-monitor-reset]')?.addEventListener('click', () => {
			this.reset();
		});

		const attachHover = (canvas: HTMLCanvasElement): void => {
			canvas.addEventListener('pointermove', event => {
				const count = this.sampler.samples.length;
				if (!count) { return; }
				const rect = canvas.getBoundingClientRect();
				const padL = 36;
				const padR = 10;
				const plotW = Math.max(40, rect.width - padL - padR);
				const relX = clamp((event.clientX - rect.left - padL) / plotW, 0, 1);
				const slot = Math.round(relX * (PERF_MAX_SAMPLES - 1));
				const slotOffset = PERF_MAX_SAMPLES - count;
				const idx = clamp(slot - slotOffset, 0, count - 1);
				if (this.hoveredIndex !== idx) {
					this.hoveredIndex = idx;
					this.renderUi();
				}
			});
			canvas.addEventListener('pointerleave', () => {
				if (this.hoveredIndex !== null) {
					this.hoveredIndex = null;
					this.renderUi();
				}
			});
		};
		attachHover(this.fpsChart);
		attachHover(this.cpuChart);

		if (typeof ResizeObserver !== 'undefined') {
			this.resizeObserver = new ResizeObserver(() => this.drawCharts());
			this.resizeObserver.observe(this.root);
		}
	}

	start(): void {
		if (this.running) { return; }
		this.running = true;
		this.paused = false;
		this.hoveredIndex = null;
		this.sampler.reset(performance.now());
		document.addEventListener('visibilitychange', this.onVisibilityBound);
		this.attachPerformanceObserver();
		void this.pollServerMetrics();
		if (!document.hidden) {
			this.startLoops();
		}
		this.renderUi();
		requestAnimationFrame(() => this.drawCharts());
	}

	stop(): void {
		if (!this.running) { return; }
		this.running = false;
		document.removeEventListener('visibilitychange', this.onVisibilityBound);
		this.stopLoops();
		this.detachPerformanceObserver();
		this.abortCtrl?.abort();
		this.abortCtrl = null;
		this.serverFetchInFlight = false;
	}

	pause(): void {
		if (!this.running || this.paused) { return; }
		this.paused = true;
		this.stopLoops();
		this.abortCtrl?.abort();
		this.abortCtrl = null;
		this.serverFetchInFlight = false;
		this.renderUi();
	}

	resume(): void {
		if (!this.running || !this.paused) { return; }
		this.paused = false;
		this.hoveredIndex = null;
		this.sampler.startWindow(performance.now());
		if (!document.hidden) {
			this.startLoops();
		}
		void this.pollServerMetrics();
		this.renderUi();
	}

	reset(): void {
		this.hoveredIndex = null;
		this.sampler.reset(performance.now());
		void this.pollServerMetrics();
		this.renderUi();
	}

	syncUi(): void {
		if (!this.running) { return; }
		this.renderUi();
	}

	dispose(): void {
		this.stop();
		this.resizeObserver?.disconnect();
		this.resizeObserver = null;
	}

	private attachPerformanceObserver(): void {
		if (typeof PerformanceObserver === 'undefined') { return; }
		try {
			const supported = PerformanceObserver.supportedEntryTypes || [];
			const entryType = supported.includes('longtask') ? 'longtask' : null;
			if (!entryType) { return; }
			this.observer = new PerformanceObserver(list => {
				for (const entry of list.getEntries()) {
					this.sampler.recordBusyInterval(entry.startTime, entry.duration);
				}
			});
			this.observer.observe({ type: entryType, buffered: false });
		} catch {
			this.observer = null;
		}
	}

	private detachPerformanceObserver(): void {
		this.observer?.disconnect();
		this.observer = null;
	}

	private startLoops(): void {
		this.stopLoops();
		const onFrame = (now: number): void => {
			if (!this.running || this.paused) { return; }
			this.sampler.recordFrame(now);
			this.rafId = requestAnimationFrame(onFrame);
		};
		this.rafId = requestAnimationFrame(onFrame);

		this.expectedProbeAt = performance.now() + PROBE_STEP_MS;
		const tickProbe = (): void => {
			if (!this.running || this.paused) { return; }
			const now = performance.now();
			const drift = Math.max(0, now - this.expectedProbeAt - PROBE_BASELINE_SLACK_MS);
			this.sampler.recordTimerDrift(drift);
			this.expectedProbeAt = now + PROBE_STEP_MS;
			this.probeTimer = setTimeout(tickProbe, PROBE_STEP_MS);
		};
		this.probeTimer = setTimeout(tickProbe, PROBE_STEP_MS);

		this.bucketTimer = setInterval(() => {
			if (!this.running || this.paused) { return; }
			const now = performance.now();
			const heap = readBrowserHeapMb();
			this.sampler.commitWindow(now, {
				serverCpuPercent: this.latestServer ? this.latestServer.cpuPercent : null,
				heapUsedMb: heap.usedMb,
				heapTotalMb: heap.totalMb,
			});
			void this.pollServerMetrics();
			this.renderUi();
		}, PERF_SAMPLE_INTERVAL_MS);
	}

	private stopLoops(): void {
		if (this.rafId) {
			cancelAnimationFrame(this.rafId);
			this.rafId = 0;
		}
		if (this.probeTimer !== null) {
			clearTimeout(this.probeTimer);
			this.probeTimer = null;
		}
		if (this.bucketTimer !== null) {
			clearInterval(this.bucketTimer);
			this.bucketTimer = null;
		}
	}

	private async pollServerMetrics(): Promise<void> {
		if (!this.running || this.paused || this.serverFetchInFlight) { return; }
		this.serverFetchInFlight = true;
		const controller = new AbortController();
		this.abortCtrl = controller;
		const startedAt = performance.now();
		try {
			const res = await fetch(this.serverEndpoint, {
				method: 'GET',
				cache: 'no-store',
				signal: controller.signal,
			});
			if (!res.ok) {
				if (this.serverEndpoint !== '/irade/api/metrics') {
					this.serverEndpoint = '/irade/api/metrics';
				}
				return;
			}
			const data = await res.json() as ServerMetricsSnapshot;
			if (data && typeof data.cpuPercent === 'number') {
				this.latestServer = data;
				this.serverRttMs = Math.round((performance.now() - startedAt) * 10) / 10;
				if (this.running && this.sampler.samples.length === 0) {
					this.renderUi();
				}
			}
		} catch {
			// Offline/aborted metrics fetch leaves client metrics running cleanly.
		} finally {
			this.serverFetchInFlight = false;
			if (this.abortCtrl === controller) {
				this.abortCtrl = null;
			}
		}
	}

	private renderUi(): void {
		const stats = this.sampler.getStats();
		const latest = stats.latest;

		this.stateBadgeEl.textContent = this.paused ? '일시정지' : '실시간 수집';
		this.stateBadgeEl.classList.toggle('is-paused', this.paused);
		this.toggleBtn.textContent = this.paused ? '재개' : '일시정지';
		this.countEl.textContent = `${stats.count} / ${stats.maxSamples}`;
		this.root.dataset.sampleCount = String(stats.count);

		if (latest) {
			this.fpsValueEl.textContent = `${latest.fps} FPS`;
			this.fpsDetailEl.textContent = `${latest.frameTimeMs.toFixed(1)} ms · 평균 ${stats.avgFps} FPS`;
			this.root.dataset.latestFps = String(latest.fps);

			const primaryCpu = latest.serverCpuPercent ?? latest.clientCpuPercent;
			this.cpuValueEl.textContent = `${primaryCpu.toFixed(1)}%`;
			this.cpuDetailEl.textContent = latest.serverCpuPercent !== null
				? `서버 ${latest.serverCpuPercent.toFixed(1)}% · 탭 ${latest.clientCpuPercent.toFixed(1)}%`
				: `브라우저 · 평균 ${stats.avgClientCpu.toFixed(1)}%`;
			this.root.dataset.latestCpu = primaryCpu.toFixed(1);
		} else {
			this.fpsValueEl.textContent = '-- FPS';
			this.fpsDetailEl.textContent = '프레임 타임 -- ms';
			this.cpuValueEl.textContent = this.latestServer ? `${this.latestServer.cpuPercent.toFixed(1)}%` : '-- %';
			this.cpuDetailEl.textContent = this.latestServer
				? `서버 ${this.latestServer.cpuPercent.toFixed(1)}% · 측정 중`
				: '0.2초 첫 집계 대기 중';
		}

		const heap = readBrowserHeapMb();
		if (heap.usedMb !== null) {
			this.memValueEl.textContent = `${heap.usedMb.toFixed(1)} MB`;
			this.memDetailEl.textContent = this.latestServer
				? `JS 힙 · 서버 RAM ${this.latestServer.systemMemPercent.toFixed(0)}%`
				: `JS 힙 (상한 ${Math.round(heap.totalMb || 0)} MB)`;
		} else if (this.latestServer) {
			this.memValueEl.textContent = `${this.latestServer.systemMemPercent.toFixed(1)}%`;
			this.memDetailEl.textContent = `서버 ${this.latestServer.memUsedMb.toLocaleString()} / ${this.latestServer.memTotalMb.toLocaleString()} MB`;
		} else {
			this.memValueEl.textContent = '지원 안 됨';
			this.memDetailEl.textContent = '브라우저 메모리 API 제한됨';
		}

		const cores = navigator.hardwareConcurrency || this.latestServer?.cores || 0;
		const domNodes = document.getElementsByTagName('*').length;
		const editors = this.host.getEditorCount();
		const cellCount = this.host.getCellCount();
		this.envValueEl.textContent = `${cellCount} cells · ${editors} ed`;
		this.envDetailEl.textContent = `${cores ? `${cores} 코어` : '코어 미상'} · DOM ${domNodes.toLocaleString()}`;

		this.fpsStatsEl.textContent = stats.count
			? `avg ${stats.avgFps} · min ${stats.minFps} · max ${stats.maxFps}`
			: 'avg -- · min -- · max --';
		this.cpuStatsEl.textContent = stats.count
			? `브라우저 ${stats.latest!.clientCpuPercent.toFixed(1)}%${stats.latest!.serverCpuPercent !== null ? ` · 서버 ${stats.latest!.serverCpuPercent.toFixed(1)}%` : ''}`
			: '브라우저 --% · 서버 --%';

		if (this.hoveredIndex !== null && this.sampler.samples[this.hoveredIndex]) {
			const s = this.sampler.samples[this.hoveredIndex];
			const offsetSec = -((stats.count - 1 - this.hoveredIndex) * PERF_SAMPLE_INTERVAL_MS) / 1000;
			const serverPart = s.serverCpuPercent !== null ? ` · 서버 CPU ${s.serverCpuPercent.toFixed(1)}%` : '';
			this.hoverReadoutEl.textContent = `시점 ${offsetSec.toFixed(1)}s (#${s.seq}) — ${s.fps} FPS (${s.frameTimeMs.toFixed(1)} ms) · 브라우저 CPU ${s.clientCpuPercent.toFixed(1)}%${serverPart}`;
		} else {
			this.hoverReadoutEl.textContent = '그래프에 마우스를 올리면 0.2초 단위 상세 시점을 확인할 수 있습니다.';
		}

		const dpr = window.devicePixelRatio || 1;
		const detailRows: [string, string][] = [
			['논리 CPU 코어', cores ? `${cores} threads${this.latestServer ? ` (서버 ${this.latestServer.cores})` : ''}` : '미확인'],
			['프레임 지연 / Long Task', `스터터 ${this.sampler.stutterCount}회 · LongTask ${this.sampler.longTaskCount}회`],
			['평균 / 최대 프레임 타임', stats.count ? `${stats.avgFrameTimeMs.toFixed(1)} / ${stats.maxFrameTimeMs.toFixed(1)} ms` : '--'],
			['서버 워커 · Load Avg', this.latestServer ? `Worker ${this.latestServer.processCpuPercent.toFixed(1)}% · Load ${this.latestServer.loadAvg1.toFixed(2)}` : '로컬 오프라인 / 대기'],
			['워크스페이스 동기화 · 응답', `${this.host.getConnectionLabel()}${this.serverRttMs !== null ? ` · API ${this.serverRttMs} ms` : ''}`],
			['뷰포트 · 프리뷰 프레임', `${window.innerWidth}×${window.innerHeight} @${dpr.toFixed(dpr % 1 === 0 ? 0 : 1)}x · HTML/MD ${this.host.getPreviewCount()}개`],
		];
		this.detailGridEl.innerHTML = detailRows
			.map(([label, value]) => `<div class="monitor-detail-item"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`)
			.join('');

		this.drawCharts();
	}

	private drawCharts(): void {
		if (!this.running) { return; }
		const samples = this.sampler.samples;
		const palette = readThemePalette();
		const stats = this.sampler.getStats();
		const fpsCeiling = Math.max(75, Math.min(240, Math.ceil((stats.maxFps + 10) / 30) * 30));

		renderTimeSeriesChart(
			this.fpsChart,
			{
				maxSamples: PERF_MAX_SAMPLES,
				minY: 0,
				maxY: fpsCeiling,
				gridTicks: fpsCeiling <= 90 ? [0, 30, 60] : [0, 60, 120],
				formatTick: v => `${v}`,
				targetLine: { value: 60, label: '60 FPS' },
				series: [{
					label: 'FPS',
					stroke: palette.accent,
					fillTop: palette.accentSoftTop,
					fillBottom: palette.accentSoftBottom,
					values: samples.map(s => s.fps),
				}],
			},
			samples.length,
			this.hoveredIndex,
			palette,
		);

		const cpuSeries: ChartSeries[] = [
			{
				label: '브라우저',
				stroke: palette.accent,
				fillTop: palette.accentSoftTop,
				fillBottom: palette.accentSoftBottom,
				values: samples.map(s => s.clientCpuPercent),
			},
		];
		if (samples.some(s => s.serverCpuPercent !== null)) {
			cpuSeries.unshift({
				label: '서버',
				stroke: palette.blue,
				fillTop: palette.blueSoftTop,
				fillBottom: palette.blueSoftBottom,
				dashed: true,
				values: samples.map(s => s.serverCpuPercent),
			});
		}

		renderTimeSeriesChart(
			this.cpuChart,
			{
				maxSamples: PERF_MAX_SAMPLES,
				minY: 0,
				maxY: 100,
				gridTicks: [0, 25, 50, 75, 100],
				formatTick: v => `${v}%`,
				series: cpuSeries,
			},
			samples.length,
			this.hoveredIndex,
			palette,
		);
	}
}
