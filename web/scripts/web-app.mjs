import Koa from 'koa';
import { applyWebBranding } from './web-branding.mjs';
import { applyResearchStudio } from './research-studio.mjs';

export async function createEditorApp({ basePath = '/irade/', studioPath } = {}) {
	const app = new Koa();
	await applyWebBranding(app, { basePath });
	await applyResearchStudio(app, { studioPath });
	return app;
}
