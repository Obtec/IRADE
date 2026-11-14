import { readFile, readdir, mkdtemp, writeFile, rm, access } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const MAX_BODY_BYTES = 256 * 1024;
const MAX_OUTPUT_BYTES = 64 * 1024;

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
    if (!candidate.includes('/')) { return candidate; }
    try {
      await access(candidate);
      return candidate;
    } catch { /* try next */ }
  }
  return undefined;
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
      const lean = await resolveExecutable(['/home/steam/.elan/bin/lean', '/usr/local/bin/lean', 'lean']);
      if (!lean) {
        return { ok: false, exitCode: 127, stdout: '', stderr: 'Lean 4 실행기를 찾을 수 없습니다.', durationMs: 0, status: 'error' };
      }
      const result = await runProcess(lean, ['--stdin'], {
        cwd: dir,
        stdin: code,
        timeoutMs: 20000,
        env: { PATH: `/home/steam/.elan/bin:${process.env.PATH || ''}` },
      });
      if (result.ok) {
        result.status = 'verified';
        if (!result.stdout.trim()) {
          result.stdout = '✓ Proof verified (0 errors)';
        }
      }
      return result;
    }

    if (kind === 'rocq') {
      const coqc = await resolveExecutable(['/usr/bin/coqc', '/usr/local/bin/coqc', 'coqc']);
      const scriptPath = join(dir, 'Cell.v');
      await writeFile(scriptPath, code, 'utf8');
      const result = await runProcess(coqc, [scriptPath], {
        cwd: dir,
        timeoutMs: 20000,
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
    await rm(dir, { recursive: true, force: true }).catch(() => {});
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
  const html = files.get('index.html').content.toString();
  app.middleware.unshift(async (context, next) => {
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

    const login = ['/login','/login/'].includes(context.path);
    const home = context.path === '/' || login;
    const asset = context.path.startsWith('/irade-assets/studio/') ? files.get(context.path.slice('/irade-assets/studio/'.length)) : undefined;
    if ((!home && !asset) || !['GET','HEAD'].includes(context.method)) { return next(); }
    context.set('X-Content-Type-Options', 'nosniff');
    context.set('Referrer-Policy', 'same-origin');
    context.set('Cache-Control', 'no-cache');
    if (home) {
      context.type = 'text/html';
      context.set('Content-Security-Policy', "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data: blob:; connect-src 'self' wss:; worker-src 'self' blob:; frame-src 'self' about:; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'");
      context.body = login ? files.get('login.html').content.toString() : html;
    } else { context.type = asset.type; context.body = asset.content; }
  });
}
