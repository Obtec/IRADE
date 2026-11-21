import { readFile } from 'node:fs/promises';

const prefix = '/assets/';
const types = new Map([
	['irade-favicon.ico', 'image/x-icon'],
	['irade-icon-16.png', 'image/png'],
	['irade-icon-32.png', 'image/png'],
	['irade-icon-48.png', 'image/png'],
	['irade-icon-128.png', 'image/png'],
	['irade-icon-180.png', 'image/png'],
	['irade-icon-192.png', 'image/png'],
	['irade-icon-512.png', 'image/png'],
	['irade-icon-source.png', 'image/png'],
	['orcid-id.svg', 'image/svg+xml'],
	['manifest.webmanifest', 'application/manifest+json'],
]);

export async function applyWebBranding(app, { basePath = '/' } = {}) {
	const assets = new Map(await Promise.all([...types.keys()].map(async name => [
		name,
		await readFile(new URL(`../assets/${name}`, import.meta.url)),
	])));
	app.middleware.unshift(async (context, next) => {
		if (context.path.startsWith(prefix)) {
			const name = context.path.slice(prefix.length);
			if (assets.has(name) && ['GET', 'HEAD'].includes(context.method)) {
				context.type = types.get(name);
				context.set('Cache-Control', 'public, max-age=86400');
				if (name === 'manifest.webmanifest') {
					const manifest = JSON.parse(assets.get(name).toString());
					Object.assign(manifest, { id: basePath, start_url: basePath, scope: basePath });
					context.body = JSON.stringify(manifest);
				} else {
					context.body = assets.get(name);
				}
				return;
			}
		}
		await next();
	});
}
