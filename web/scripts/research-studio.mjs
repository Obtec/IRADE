import { readFile, readdir, mkdtemp, writeFile, rm, access } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { cpus, freemem, homedir, loadavg, tmpdir, totalmem } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const MAX_BODY_BYTES = 256 * 1024;
const MAX_OUTPUT_BYTES = 64 * 1024;

let prevCpuSnapshot = null;
let lastComputedMetrics = null;
let metricsBaselinePromise = null;

function elanHome() {
	return process.env.ELAN_HOME || join(homedir(), '.elan');
}

function readCpuTotals() {
  let idle = 0;
  let total = 0;
  const list = cpus();
  for (const cpu of list) {
    const t = cpu.times;
    idle += t.idle;
    total += t.user + t.nice + t.sys + t.idle + t.irq;
  }
  return {
    idle,
    total,
    cores: Math.max(1, list.length),
    proc: process.cpuUsage(),
    hrMs: Number(process.hrtime.bigint()) / 1e6,
  };
}

function computeCpuDelta(prev, curr) {
  const totalDiff = Math.max(1, curr.total - prev.total);
  const idleDiff = Math.max(0, curr.idle - prev.idle);
  const cpuPercent = Math.min(100, Math.max(0, ((totalDiff - idleDiff) / totalDiff) * 100));
  const wallUs = Math.max(1000, (curr.hrMs - prev.hrMs) * 1000);
  const procUsedUs = Math.max(0, (curr.proc.user - prev.proc.user) + (curr.proc.system - prev.proc.system));
  const processCpuPercent = Math.min(100, Math.max(0, (procUsedUs / (wallUs * curr.cores)) * 100));
  const totalBytes = Math.max(1, totalmem());
  const freeBytes = Math.max(0, freemem());
  const usedBytes = Math.max(0, totalBytes - freeBytes);
  const mem = process.memoryUsage();
  return {
    cpuPercent: Math.round(cpuPercent * 10) / 10,
    processCpuPercent: Math.round(processCpuPercent * 10) / 10,
    cores: curr.cores,
    memUsedMb: Math.round(usedBytes / (1024 * 1024)),
    memTotalMb: Math.round(totalBytes / (1024 * 1024)),
    systemMemPercent: Math.round((usedBytes / totalBytes) * 1000) / 10,
    rssMb: Math.round((mem.rss / (1024 * 1024)) * 10) / 10,
    heapUsedMb: Math.round((mem.heapUsed / (1024 * 1024)) * 10) / 10,
    loadAvg1: Math.round((loadavg()[0] || 0) * 100) / 100,
    timestamp: Date.now(),
  };
}

async function sampleRuntimeMetrics() {
  const nowSnapshot = readCpuTotals();
  if (!prevCpuSnapshot || (nowSnapshot.hrMs - prevCpuSnapshot.hrMs) > 15000) {
    if (!metricsBaselinePromise) {
      const startSnap = nowSnapshot;
      metricsBaselinePromise = new Promise(resolveBaseline => {
        setTimeout(() => {
          const endSnap = readCpuTotals();
          prevCpuSnapshot = endSnap;
          lastComputedMetrics = computeCpuDelta(startSnap, endSnap);
          metricsBaselinePromise = null;
          resolveBaseline(lastComputedMetrics);
        }, 50);
      });
    }
    return await metricsBaselinePromise;
  }
  if ((nowSnapshot.hrMs - prevCpuSnapshot.hrMs) < 120 && lastComputedMetrics) {
    return { ...lastComputedMetrics, timestamp: Date.now() };
  }
  const metrics = computeCpuDelta(prevCpuSnapshot, nowSnapshot);
  prevCpuSnapshot = nowSnapshot;
  lastComputedMetrics = metrics;
  return metrics;
}

async function readJsonBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) {
      throw new Error('실행할 코드 크기가 너무 큽니다 (최대 256 KiB).');
    }
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
}

async function resolveExecutable(candidates) {
  for (const candidate of candidates) {
    if (!candidate) { continue; }
    if (!candidate.includes('/')) { return candidate; }
    try {
      await access(candidate);
      return candidate;
    } catch { /* try next */ }
  }
  return undefined;
}

async function discoverToolchainLeans() {
  const candidates = [];
  for (const root of ['/opt/elan/toolchains', join(elanHome(), 'toolchains')]) {
    try {
      for (const entry of await readdir(root)) {
        candidates.push(join(root, entry, 'bin', 'lean'));
      }
    } catch { /* ignore missing elan root */ }
  }
  return candidates;
}

function runProcess(command, args, { cwd, stdin, timeoutMs = 15000, env }) {
  return new Promise(resolvePromise => {
    const started = Date.now();
    const child = spawn(command, args, {
      cwd,
      env: { ...process.env, ...env },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    let truncated = false;
    let timedOut = false;

    const append = (current, chunk) => {
      const next = current + chunk.toString('utf8');
      if (Buffer.byteLength(next, 'utf8') > MAX_OUTPUT_BYTES) {
        truncated = true;
        return next.slice(0, MAX_OUTPUT_BYTES);
      }
      return next;
    };

    child.stdout.on('data', chunk => { stdout = append(stdout, chunk); });
    child.stderr.on('data', chunk => { stderr = append(stderr, chunk); });

    const timer = setTimeout(() => {
      timedOut = true;
      child.kill('SIGKILL');
    }, timeoutMs);

    child.on('error', error => {
      clearTimeout(timer);
      resolvePromise({
        ok: false,
        exitCode: null,
        stdout,
        stderr: error.message,
        durationMs: Date.now() - started,
        status: 'error',
      });
    });

    child.on('close', code => {
      clearTimeout(timer);
      if (truncated) { stdout += '\n… (출력이 64 KiB를 초과하여 잘렸습니다)'; }
      resolvePromise({
        ok: !timedOut && code === 0,
        exitCode: code,
        stdout,
        stderr: timedOut ? `${stderr}\n실행 시간(${Math.round(timeoutMs / 1000)}초)을 초과하여 중단되었습니다.`.trim() : stderr,
        durationMs: Date.now() - started,
        status: timedOut ? 'timeout' : code === 0 ? 'success' : 'error',
      });
    });

    if (stdin !== undefined) {
      child.stdin.write(stdin);
    }
    child.stdin.end();
  });
}

async function executeCode(kind, code) {
  if (typeof code !== 'string') {
    return { ok: false, exitCode: 1, stdout: '', stderr: '코드 내용이 문자열이어야 합니다.', durationMs: 0, status: 'error' };
  }
  const dir = await mkdtemp(join(tmpdir(), 'irade-exec-'));
  try {
    if (kind === 'python') {
      const py = await resolveExecutable(['/usr/bin/python3', '/usr/local/bin/python3', 'python3']);
      if (!py) {
        return { ok: false, exitCode: 127, stdout: '', stderr: 'Python 3 실행기를 찾을 수 없습니다.', durationMs: 0, status: 'error' };
      }
      const scriptPath = join(dir, 'cell.py');
      await writeFile(scriptPath, code, 'utf8');
      return await runProcess(py, ['-I', '-u', scriptPath], {
        cwd: dir,
        timeoutMs: 10000,
        env: { PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' },
      });
    }

    if (kind === 'lean') {
      const toolchainLeans = await discoverToolchainLeans();
      const lean = await resolveExecutable([
        process.env.IRADE_LEAN_BIN,
        ...toolchainLeans,
        '/opt/elan/bin/lean',
        join(elanHome(), 'bin', 'lean'),
        '/usr/local/bin/lean',
        'lean',
      ]);
      if (!lean) {
        return { ok: false, exitCode: 127, stdout: '', stderr: 'Lean 4 실행기를 찾을 수 없습니다.', durationMs: 0, status: 'error' };
      }
      const selectedElanHome = lean.startsWith('/opt/elan/') ? '/opt/elan' : elanHome();
      const leanEnv = lean.includes('/')
        ? { ELAN_HOME: selectedElanHome, PATH: `${resolve(lean, '..')}:${process.env.PATH || ''}` }
        : { ELAN_HOME: selectedElanHome };
      const normalizedCode = code.replace(
        /^(\s*(?:@\[[^\]]*\]\s*)*(?:(?:private|protected|noncomputable|unsafe|partial)\s+)*)lemma(?=\s)/gm,
        '$1theorem',
      );
      const result = await runProcess(lean, ['--stdin'], {
        cwd: dir,
        stdin: normalizedCode,
        timeoutMs: 25000,
        env: leanEnv,
      });
      if (result.ok) {
        result.status = 'verified';
        if (!result.stdout.trim()) {
          result.stdout = '✓ Proof verified (0 errors)';
        }
      } else if (!result.stdout.trim() && !result.stderr.trim()) {
        result.stderr = `Lean 4 프로세스가 비정상 종료되었습니다 (exit ${result.exitCode ?? '?'}).`;
      }
      return result;
    }

    if (kind === 'rocq') {
      const coqc = await resolveExecutable([process.env.IRADE_COQC_BIN, '/usr/bin/coqc', '/usr/local/bin/coqc', 'coqc']);
      if (!coqc) {
        return { ok: false, exitCode: 127, stdout: '', stderr: 'Rocq (coqc) 실행기가 서버에 설치되어 있지 않습니다.', durationMs: 0, status: 'error' };
      }
      const scriptPath = join(dir, 'Cell.v');
      await writeFile(scriptPath, code, 'utf8');
      const result = await runProcess(coqc, [scriptPath], {
        cwd: dir,
        timeoutMs: 25000,
      });
      if (result.status === 'error' && /ENOENT|not found/i.test(result.stderr)) {
        result.stderr = 'Rocq (coqc) 실행기가 서버에 설치되어 있지 않습니다.';
      } else if (result.ok) {
        result.status = 'verified';
        if (!result.stdout.trim()) {
          result.stdout = '✓ Rocq proof verified (Qed / 0 errors)';
        }
      }
      return result;
    }

    return { ok: false, exitCode: 1, stdout: '', stderr: `지원하지 않는 셀 종류입니다: ${kind}`, durationMs: 0, status: 'error' };
  } finally {
    await rm(dir, { recursive: true, force: true }).catch(() => { });
  }
}

/** Serve only built, allowlisted UI assets; never expose the source tree or test artifacts. */
export async function applyResearchStudio(app, { studioPath } = {}) {
  const root = studioPath ? pathToFileURL(resolve(studioPath) + '/') : new URL('../dist/studio/', import.meta.url);
  const types = { '.js': 'application/javascript', '.css': 'text/css', '.ttf': 'font/ttf', '.html': 'text/html' };
  const files = new Map();
  for (const name of await readdir(root)) {
    const extension = name.slice(name.lastIndexOf('.'));
    if (types[extension] && /^[a-zA-Z0-9_.-]+$/.test(name)) { files.set(name, { content: await readFile(new URL(name, root)), type: types[extension] }); }
  }
  const studioHtml = files.get('index.html').content.toString();
  const homeHtml = (files.get('home.html') || files.get('index.html')).content.toString();
  const previewHtml = `<!doctype html><html lang="ko"><head><meta charset="UTF-8"><script>window.addEventListener('message',function(e){if(e.source!==window.parent||!e.data||e.data.type!=='irade:render-html')return;document.open();document.write(String(e.data.html||''));document.close();});</script></head><body></body></html>`;
  app.middleware.unshift(async (context, next) => {
    if (context.path === '/api/metrics' && ['GET', 'HEAD'].includes(context.method)) {
      context.set('X-Content-Type-Options', 'nosniff');
      context.set('Cache-Control', 'no-store');
      context.status = 200;
      context.type = 'application/json';
      context.body = context.method === 'HEAD' ? '' : await sampleRuntimeMetrics();
      return;
    }

    if (context.path === '/api/execute' && context.method === 'POST') {
      context.set('X-Content-Type-Options', 'nosniff');
      context.set('Cache-Control', 'no-store');
      try {
        const body = await readJsonBody(context.req);
        const result = await executeCode(String(body.kind || ''), body.code);
        context.status = 200;
        context.type = 'application/json';
        context.body = result;
      } catch (error) {
        context.status = 400;
        context.type = 'application/json';
        context.body = { ok: false, exitCode: 1, stdout: '', stderr: error instanceof Error ? error.message : String(error), durationMs: 0, status: 'error' };
      }
      return;
    }

    if (['/preview', '/preview/'].includes(context.path) && ['GET', 'HEAD'].includes(context.method)) {
      context.set('X-Content-Type-Options', 'nosniff');
      context.set('Referrer-Policy', 'no-referrer');
      context.set('Cache-Control', 'no-store');
      context.set('Content-Security-Policy', "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; img-src data: blob: https:; font-src data: https:; connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'");
      context.type = 'text/html';
      context.body = previewHtml;
      return;
    }

    const login = ['/login', '/login/'].includes(context.path);
    const studioRoute = ['/studio', '/studio/', '/workspace', '/workspace/'].includes(context.path);
    const hasStudioQuery = 'workspace' in context.query || 'workspaces' in context.query || 'demo' in context.query;
    const homeRoute = ['/', '/home', '/home/'].includes(context.path);
    const page = login || studioRoute || homeRoute;
    const asset = context.path.startsWith('/assets/studio/') ? files.get(context.path.slice('/assets/studio/'.length)) : undefined;
    if ((!page && !asset) || !['GET', 'HEAD'].includes(context.method)) { return next(); }
    context.set('X-Content-Type-Options', 'nosniff');
    context.set('Referrer-Policy', 'same-origin');
    context.set('Cache-Control', 'no-cache');
    if (page) {
      context.type = 'text/html';
      context.set('Content-Security-Policy', "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data: blob:; connect-src 'self' wss:; worker-src 'self' blob:; frame-src 'self' about:; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'");
      if (login) {
        context.body = files.get('login.html').content.toString();
      } else if (studioRoute || hasStudioQuery) {
        context.body = studioHtml;
      } else {
        context.body = homeHtml;
      }
    } else { context.type = asset.type; context.body = asset.content; }
  });
}
