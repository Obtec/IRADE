import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { createEditorApp } from '../scripts/web-app.mjs';
import { fixture } from './web-fixture.mjs';

function playwrightLaunchOptions() {
	const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;
	const channel = process.env.IRADE_PLAYWRIGHT_CHANNEL;
	const args = ['--no-sandbox', '--disable-dev-shm-usage', '--ignore-certificate-errors'];
	if (executablePath) { return { headless: true, executablePath, args }; }
	if (channel === 'bundled') { return { headless: true, args }; }
	return { headless: true, channel: channel || 'chrome', args };
}

const origin = 'https://localhost:9449';
const app = await createEditorApp({ basePath: '/' });
const cleanup = await fixture({ studio: app.callback() });
const browser = await chromium.launch(playwrightLaunchOptions());

try {
	const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 950 } });
	await context.addInitScript(baseOrigin => {
		if (window === window.top && location.origin === baseOrigin) {
			sessionStorage.setItem(`irade.orcid.token:${baseOrigin}`, 'fixture-writer');
		}
	}, origin);
	const page = await context.newPage();
	page.setDefaultTimeout(20000);

	await page.goto(`${origin}/agdd/notebook?workspace=42`, { waitUntil: 'networkidle' });
	const logoLoaded = await page.locator('.wordmark img').evaluate(img => img.complete && img.naturalWidth > 0);
	assert.equal(logoLoaded, true, 'IRADE header logo must load');
	await page.waitForFunction(() => document.querySelector('#connection-label')?.textContent === 'WORKSPACE LIVE SYNC');

	// Desktop right panel opens on the Antigravity AI Agent drawer by default, and #agent-toggle cleanly toggles it closed/open.
	await page.waitForSelector('#agent-drawer:not([hidden])');
	await page.click('#agent-toggle');
	await page.waitForSelector('#agent-drawer', { state: 'hidden' });
	await page.click('#agent-toggle');
	await page.waitForSelector('#agent-drawer:not([hidden])');
	await page.waitForFunction(() =>
		document.querySelector('#agent-quota-scope')?.textContent?.includes('k-exaone_v2.5 · 서버 기본 키')
		&& document.querySelector('[data-quota-window="second"] b')?.textContent === '9 / 10',
	);
	assert.equal(await page.textContent('[data-quota-window="minute"] b'), '98 / 100');
	assert.equal(await page.textContent('[data-quota-window="day"] b'), '4,990 / 5,000');

	// Switch API and confirm quota updates per API independently.
	await page.selectOption('#agent-api-select', 'exaone-deep-test');
	await page.waitForFunction(() =>
		document.querySelector('#agent-quota-scope')?.textContent?.includes('exaone-deep-test · 서버 기본 키')
		&& document.querySelector('[data-quota-window="second"] b')?.textContent === '3 / 5',
	);
	assert.equal(await page.textContent('[data-quota-window="minute"] b'), '35 / 40');
	assert.equal(await page.textContent('[data-quota-window="day"] b'), '900 / 1,000');

	// Apply personal API key for exaone-deep-test -> quota switches to key-isolated counter (5/5).
	await page.fill('#agent-api-key-input', 'personal-secret-key-fixture');
	await page.click('#agent-api-key-apply');
	await page.waitForFunction(() =>
		document.querySelector('#agent-quota-scope')?.textContent?.includes('exaone-deep-test · 개인 키')
		&& document.querySelector('[data-quota-window="second"] b')?.textContent === '5 / 5',
	);
	assert.equal(await page.textContent('[data-quota-window="minute"] b'), '50 / 50');
	assert.equal(await page.textContent('[data-quota-window="day"] b'), '500 / 500');
	assert.equal(await page.textContent('#agent-cred-badge'), '개인 키 적용됨 (세션 전용)');
	const drawerText = await page.textContent('#agent-drawer');
	assert.equal(drawerText?.includes('personal-secret-key-fixture'), false);

	// Configure reasoning/tokens/steps parameters and verify persistence across reload.
	await page.click('#agent-params-panel summary');
	await page.selectOption('#agent-reasoning', 'max');
	await page.fill('#agent-max-tokens', '16384');
	await page.dispatchEvent('#agent-max-tokens', 'change');
	await page.fill('#agent-max-steps', '6');
	await page.dispatchEvent('#agent-max-steps', 'change');
	await page.fill('#agent-temperature', '0.45');
	await page.dispatchEvent('#agent-temperature', 'change');
	await page.fill('#agent-top-p', '0.85');
	await page.dispatchEvent('#agent-top-p', 'change');

	await page.reload({ waitUntil: 'networkidle' });
	await page.waitForFunction(() => document.querySelector('#connection-label')?.textContent === 'WORKSPACE LIVE SYNC');
	await page.waitForSelector('#agent-drawer:not([hidden])');
	await page.waitForFunction(() =>
		document.querySelector('#agent-quota-scope')?.textContent?.includes('exaone-deep-test · 개인 키')
		&& document.querySelector('[data-quota-window="second"] b')?.textContent === '5 / 5',
	);
	assert.equal(await page.inputValue('#agent-reasoning'), 'max');
	assert.equal(await page.inputValue('#agent-max-tokens'), '16384');
	assert.equal(await page.inputValue('#agent-max-steps'), '6');

	// Run agent prompt -> streams output, updates quota for personal key (4/5), and commits bottom cell live.
	await page.fill('#agent-prompt', '새 마크다운 요약 셀 추가');
	await page.click('#agent-submit');
	await page.waitForFunction(() =>
		document.querySelector('#agent-thread')?.textContent?.includes('셀을 하단에 생성했습니다')
		&& document.querySelector('[data-quota-window="second"] b')?.textContent === '4 / 5',
	);
	await page.waitForFunction(() =>
		Array.from(document.querySelectorAll('.nb-cell-title')).some(el => el.textContent?.includes('에이전트 생성 메모')),
	);

	// Clear personal key -> reverts cleanly to exaone-deep-test server key quota (3/5, isolated from personal key usage!).
	await page.click('#agent-api-key-clear');
	await page.waitForFunction(() =>
		document.querySelector('#agent-quota-scope')?.textContent?.includes('exaone-deep-test · 서버 기본 키')
		&& document.querySelector('[data-quota-window="second"] b')?.textContent === '3 / 5',
	);

	console.log('IRADE_AGENT_STUDIO_PASSED: per-API + per-key quota cards, parameter persistence, prompt execution, live workspace cell commit');
} finally {
	await browser.close();
	await cleanup();
}
