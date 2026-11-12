import { readFile, readdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

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
