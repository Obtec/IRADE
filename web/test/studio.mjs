import { mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { createEditorApp } from '../scripts/web-app.mjs';
import { fixture } from './web-fixture.mjs';

const app = await createEditorApp({ basePath: '/irade/' });
const cleanup = await fixture({ studio: app.callback() });
let browser;
const origin = 'https://localhost:9449';
const errors = [];

try {
  browser = await chromium.launch({ channel: 'chrome', headless: true, args: ['--no-sandbox', '--ignore-certificate-errors'] });
  const first = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 950 } });
  const page = await first.newPage();
  page.setDefaultTimeout(20000);
  page.on('pageerror', error => errors.push(error.message));
  const requests = [];
  page.on('request', request => requests.push(request.url()));

  await page.goto(origin, { waitUntil: 'networkidle' });
  assert.equal(await page.title(), 'IRADE · Research Workspace');
  assert.equal(await page.locator('.type-card').count(), 4);
  await mkdir('.artifacts', { recursive: true });
  await page.screenshot({ path: '.artifacts/studio-overview.png' });

  await page.getByRole('button', { name: '로컬 체험', exact: false }).click();
  await page.getByRole('button', { name: /새로운 정리/ }).first().click();
  await page.locator('.nb-cell.active .monaco-editor .view-lines').click();
  await page.keyboard.press('Control+End');
  await page.keyboard.insertText('\n-- 로컬 편집 확인');
  await page.screenshot({ path: '.artifacts/studio-proof.png' });
  assert.ok((await page.locator('.nb-cell.active .nb-cell-title').textContent()).includes('새로운 정리'));

  await page.getByRole('button', { name: /연구 노트/ }).first().click();
  await page.locator('.nb-cell.active [data-cell-preview]').click();
  await page.frameLocator('.nb-cell.active iframe.nb-preview-frame').getByRole('heading', { name: '연구 노트', exact: true }).waitFor();

  await page.locator('#account').click();
  await page.waitForURL('**/irade/login');
  await page.screenshot({ path: '.artifacts/orcid-login.png' });
  await page.locator('#orcid-login:not([disabled])').click();
  await page.getByRole('button', { name: /공동 연구 workspace/ }).click();
  await page.waitForFunction(() => document.querySelector('#connection-label')?.textContent === 'WORKSPACE LIVE SYNC');

  await page.getByRole('button', { name: '새 셀 만들기', exact: false }).click();
  await page.getByRole('button', { name: 'Python 코드', exact: false }).last().click();
  await page.locator('.nb-cell.active .monaco-editor .view-lines').waitFor();
  await page.locator('.nb-cell.active .monaco-editor .view-lines').click();
  await page.keyboard.press('Control+a');
  await page.keyboard.insertText('# 공동 연구\nowner = 0\npeer = 0\n');
  await page.keyboard.press('Control+s');
  await page.waitForFunction(() => document.querySelector('#cell-save')?.textContent === 'Workspace 동기화됨');

  const second = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 950 } });
  await second.addInitScript(origin => {
    if (window === window.top && location.origin === origin) {
      sessionStorage.setItem(`irade.orcid.token:${origin}`, 'fixture-writer');
    }
  }, origin);
  const peer = await second.newPage();
  peer.setDefaultTimeout(20000);
  peer.on('pageerror', error => errors.push(error.message));
  await peer.goto(origin + '/?workspace=42', { waitUntil: 'networkidle' });
  await peer.getByRole('button', { name: /공동 연구/ }).first().click();

  // Two real editor replicas concurrently modify disjoint lines over WebSocket delta + Automerge.
  await page.locator('.nb-cell.active .monaco-editor .view-lines').click();
  await page.keyboard.press('Control+Home');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Home');
  await page.keyboard.press('Shift+End');

  await peer.locator('.nb-cell.active .monaco-editor .view-lines').click();
  await peer.keyboard.press('Control+Home');
  await peer.keyboard.press('ArrowDown');
  await peer.keyboard.press('ArrowDown');
  await peer.keyboard.press('Home');
  await peer.keyboard.press('Shift+End');

  await Promise.all([
    page.keyboard.insertText('owner = 42'),
    peer.keyboard.insertText('peer = "함께 🧪"'),
  ]);
  const converged = async target => target.waitForFunction(() => {
    const text = Array.from(document.querySelectorAll('.nb-cell.active .view-line'))
      .map(line => line.textContent.replaceAll('\u00a0', ' ')).join('\n');
    return text.includes('owner = 42') && text.includes('peer = "함께 🧪"');
  });
  await Promise.all([converged(page), converged(peer)]);

  await page.keyboard.press('Control+s');
  await page.waitForFunction(() => document.querySelector('#cell-save')?.textContent === 'Workspace 동기화됨');
  await page.reload({ waitUntil: 'networkidle' });
  await page.getByRole('button', { name: /공동 연구/ }).first().click();
  await converged(page);

  // Concurrent cell creation converges without losing either cell.
  await page.getByRole('button', { name: '새 셀 만들기', exact: false }).click();
  await peer.getByRole('button', { name: '새 셀 만들기', exact: false }).click();
  await Promise.all([
    page.getByRole('button', { name: /Lean 4 증명/ }).last().click(),
    peer.getByRole('button', { name: /Rocq 증명/ }).last().click(),
  ]);
  await Promise.all([
    page.waitForFunction(() => document.querySelector('#cell-count')?.textContent === '3'),
    peer.waitForFunction(() => document.querySelector('#cell-count')?.textContent === '3'),
  ]);

  await page.getByRole('button', { name: '새 셀 만들기', exact: false }).click();
  await page.getByRole('button', { name: /HTML 인터랙티브/ }).last().click();
  await page.waitForFunction(() =>
    document.querySelector('#cell-count')?.textContent === '4'
    && document.querySelector('.nb-cell.active .nb-cell-kind')?.textContent?.includes('HTML'));
  await page.locator('.nb-cell.active .monaco-editor .view-lines').waitFor();
  await page.locator('.nb-cell.active .monaco-editor .view-lines').click();
  await page.keyboard.press('Control+a');
  await page.keyboard.insertText('<h1>격리된 문서</h1><script>parent.document.body.dataset.injected="yes"</script><img src=x onerror="alert(1)">');
  await page.locator('.nb-cell.active iframe.nb-preview-frame').waitFor();
  await page.frameLocator('.nb-cell.active iframe.nb-preview-frame').getByRole('heading', { name: '격리된 문서' }).waitFor();
  assert.equal(await page.locator('body').getAttribute('data-injected'), null);

  // Live permission revocation disables both the editor and mutation requests.
  cleanup.setWritable(false);
  await page.waitForFunction(() => !(document.querySelector('#permission-label')?.textContent || '').includes('쓰기'));
  await page.getByRole('button', { name: '새 셀 만들기', exact: false }).click();
  await page.getByRole('status').getByText('이 workspace는 읽기 전용입니다.').waitFor();

  cleanup.setWritable(true);
  await page.waitForFunction(() => (document.querySelector('#permission-label')?.textContent || '').includes('쓰기'));
  await page.locator('.nb-cell.active [data-cell-delete]').click();
  await page.locator('#confirm-delete').click();
  await page.waitForFunction(() => document.querySelector('#cell-count')?.textContent === '3');
  await page.getByRole('button', { name: /새로운 정리/ }).first().click();
  await page.screenshot({ path: '.artifacts/studio-shared-workspace.png' });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('tab', { name: 'Overview', exact: false }).click();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: '.artifacts/studio-mobile.png' });

  const mfaContext = await browser.newContext({ ignoreHTTPSErrors: true });
  const mfaPage = await mfaContext.newPage();
  await mfaPage.goto(origin);
  await mfaPage.locator('#account').click();
  await mfaContext.addCookies([{ name: 'fixture_mfa', value: '1', url: origin }]);
  await mfaPage.locator('#orcid-login:not([disabled])').click();
  await mfaPage.getByLabel('2단계 인증 코드', { exact: true }).fill('000000');
  await mfaPage.locator('#mfa-form button').click();
  await mfaPage.getByText('잘못된 인증 코드', { exact: true }).waitFor();
  await mfaPage.getByLabel('2단계 인증 코드', { exact: true }).fill('123456');
  await mfaPage.locator('#mfa-form button').click();
  await mfaPage.getByRole('heading', { name: 'Workspace 선택', exact: true }).waitFor();
  await mfaContext.close();

  assert.equal(requests.some(url => /marketplace|vscode-cdn|visualstudio\.com/.test(url)), false, 'No marketplace or vendor CDN requests');
  assert.deepEqual(errors, [], 'No browser errors');
  console.log('IRADE_STUDIO_PASSED: login, MFA retry, picker, cell CRUD, real guest CRDT merge, concurrent additions, session reload, preview isolation, live permissions, responsive layout, no marketplace requests');
} catch (error) {
  for (const [index, context] of (browser?.contexts() || []).entries()) {
    for (const page of context.pages()) {
      await page.screenshot({ path: `.artifacts/studio-failure-${index}.png` }).catch(() => {});
      console.error((await page.locator('body').innerText()).slice(-6000));
    }
  }
  throw error;
} finally {
  await browser?.close();
  await cleanup();
}
