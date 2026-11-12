import { readFile } from 'node:fs/promises';

const prefix = '/irade-assets/';
const types = new Map([
	['irade-favicon.ico', 'image/x-icon'],
	['irade-icon-180.png', 'image/png'],
	['irade-icon-192.png', 'image/png'],
	['irade-icon-512.png', 'image/png'],
	['manifest.webmanifest', 'application/manifest+json'],
]);

/** Apply the same icons to downloaded VS Code builds and source builds. */
export async function applyWebBranding(app) {
	const assets = new Map(await Promise.all([...types.keys()].map(async name => [name, await readFile(new URL(`../assets/${name}`, import.meta.url))])));
	app.middleware.unshift(async (context, next) => {
		if (context.path.startsWith(prefix)) {
			const name = context.path.slice(prefix.length);
			if (assets.has(name) && ['GET', 'HEAD'].includes(context.method)) {
				context.type = types.get(name);
				context.set('Cache-Control', name === 'manifest.webmanifest' ? 'no-cache' : 'public, max-age=86400');
				context.body = assets.get(name);
				return;
			}
		}
		await next();
		if (context.path === '/' && typeof context.body === 'string' && context.status === 200) {
			const links = `<meta name="theme-color" content="#020925">\n<link rel="icon" href="${prefix}irade-favicon.ico" type="image/x-icon" sizes="16x16 32x32 48x48">\n<link rel="apple-touch-icon" href="${prefix}irade-icon-180.png" sizes="180x180">\n<link rel="manifest" href="${prefix}manifest.webmanifest">`;
			context.body = context.body
				.replace(/<link\b[^>]*\brel\s*=\s*["'](?:icon|shortcut icon|apple-touch-icon|manifest)["'][^>]*>/gi, '')
				.replace(/<meta\b[^>]*\bname\s*=\s*["'](?:theme-color|apple-mobile-web-app-title)["'][^>]*>/gi, '')
				.replace(/<\/head>/i, `<meta name="apple-mobile-web-app-title" content="IRADE">\n${links}\n</head>`);
		}
	});
}
