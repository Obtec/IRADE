import { mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { createEditorApp } from '../scripts/web-app.mjs';
import { fixture } from './web-fixture.mjs';

const app = await createEditorApp({ basePath: '/' });
const cleanup = await fixture({ studio: app.callback() });
let browser;
const origin = 'https://localhost:9449';
const errors = [];

try {
  const launchOptions = { headless: true, args: ['--no-sandbox', '--ignore-certificate-errors'] };
  if (process.env.IRADE_PLAYWRIGHT_CHANNEL !== 'bundled') {
    launchOptions.channel = process.env.IRADE_PLAYWRIGHT_CHANNEL || 'chrome';
  }
  browser = await chromium.launch(launchOptions);
  const first = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 950 } });
  const page = await first.newPage();
  page.setDefaultTimeout(20000);
  page.on('pageerror', error => errors.push(error.message));
  const requests = [];
  page.on('request', request => requests.push(request.url()));

  await page.goto(origin, { waitUntil: 'networkidle' });
  assert.ok((await page.title()).includes('IRADE'));
  await page.locator('#particle-canvas').waitFor();
  const homeCanvasBox = await page.locator('#particle-canvas').boundingBox();
  assert.ok(homeCanvasBox && homeCanvasBox.width > 400 && homeCanvasBox.height > 400, 'Home particle landing canvas must fill viewport');
  assert.equal(await page.locator('.chapter-panel').count(), 6);
  await page.locator('.hud-ch-btn[data-jump="1"]').click();
  await page.waitForFunction(() => document.querySelector('.hud-ch-btn[data-jump="1"]')?.classList.contains('active'));
  await mkdir('.artifacts', { recursive: true });
  await page.screenshot({ path: '.artifacts/studio-home.png' });

  // Verify the get-started section at the end of the scroll story.
  await page.locator('#interactive-lab').scrollIntoViewIfNeeded();
  assert.equal(await page.locator('#interactive-lab a.home-card').count(), 3);
  await page.locator('#interactive-lab').screenshot({ path: '.artifacts/studio-get-started.png' });

  assert.equal(await page.locator('#interactive-lab a.home-card[href="/agdd/"]').count(), 1);
  // The standalone /studio route is gone: legacy links redirect into the AGDD console / notebook route.
  const legacy = await page.request.get(`${origin}/studio?workspace=42`, { maxRedirects: 0 });
  assert.equal(legacy.status(), 308);
  assert.equal(legacy.headers().location, '/agdd/?tab=ide&pane=studio&workspace=42');
  await page.goto(`${origin}/agdd/notebook`);
  await page.waitForURL('**/agdd/notebook');
  assert.equal(await page.title(), 'IRADE · Research Workspace');
  assert.equal(await page.locator('.type-card').count(), 4);
  await page.screenshot({ path: '.artifacts/studio-overview.png' });

  await page.getByRole('button', { name: '로컬 체험', exact: false }).click();
  await page.getByRole('button', { name: /새로운 정리/ }).first().click();
  await page.locator('.nb-cell.active .monaco-editor .view-lines').click();
  await page.keyboard.press('Control+End');
  await page.keyboard.insertText('\n-- 로컬 편집 확인');
  const demoLeanCard = page.locator('.nb-cell', { hasText: '새로운 정리' }).first();
  await demoLeanCard.locator('[data-cell-run]').click();
  await page.waitForFunction(() => {
    const cards = Array.from(document.querySelectorAll('.nb-cell'));
    const lean = cards.find(el => el.textContent?.includes('새로운 정리'));
    return lean?.querySelector('.nb-cell-output')?.textContent?.includes('add_zero_identity')
      && Boolean(lean?.querySelector('.nb-proof-db-chip')?.textContent?.includes('DB 저장됨'))
      && lean?.querySelector('[data-exec-order]')?.textContent?.trim() === '실행 #1'
      && document.querySelector('.nb-cell.active .nb-cell-title')?.textContent?.includes('새로운 증명');
  });
  await page.screenshot({ path: '.artifacts/studio-proof.png' });
  assert.ok((await demoLeanCard.locator('.nb-cell-title').textContent()).includes('새로운 정리'));

  await demoLeanCard.locator('[data-open-proof-db]').click();
  await page.locator('#inspector-content .proof-db-inspector').waitFor();
  const dbHtml = await page.locator('#inspector-content').innerText();
  assert.ok(dbHtml.includes('add_zero_identity'), 'Verified theorem must appear in relational DB table');
  assert.ok(dbHtml.includes('add_zero_lemma'), 'Supporting lemma must appear in lemma relation table');
  assert.ok(dbHtml.includes('idNat'), 'Used definition must appear in definitions table');
  await page.screenshot({ path: '.artifacts/studio-proof-db.png' });
  await page.locator('[data-open-proof-db-modal]').click();
  await page.locator('#modal .proof-db-modal-wrap').waitFor();
  await page.locator('#modal-close').click();
  await page.locator('.inspector .inspector-tabs [data-action="agent"]').click();

  await page.getByRole('button', { name: /연구 노트/ }).first().click();
  await page.locator('.nb-cell.active [data-cell-preview]').click();
  await page.frameLocator('.nb-cell.active iframe.nb-preview-frame').getByRole('heading', { name: '연구 노트', exact: true }).waitFor();

  // Verify AI conversation cell renders as Messenger UI (not Monaco) with bespoke agent profile & fixed-size bubbles.
  await page.getByRole('button', { name: /연구 대화/ }).first().click();
  await page.locator('.nb-cell.active.nb-cell-messenger .nb-chat-shell').waitFor();
  assert.equal(await page.locator('.nb-cell.active .monaco-editor').count(), 0, 'Agent cell must render as messenger, not Monaco');
  assert.equal(await page.locator('.nb-cell.active .chat-avatar-agent svg').count() >= 1, true, 'Agent SVG profile must be visible');
  await page.locator('.nb-cell.active [data-chat-input]').fill('로컬 메신저 첫 질문입니다.');
  await page.locator('.nb-cell.active [data-chat-input]').press('Enter');
  await page.locator('.nb-cell.active .chat-row.is-self').getByText('로컬 메신저 첫 질문입니다.').waitFor();
  await page.locator('.nb-cell.active [data-chat-speaker]').selectOption('peer');
  await page.locator('.nb-cell.active [data-chat-input]').fill('상대방 테스트 응답입니다.');
  await page.locator('.nb-cell.active [data-chat-send-only]').click();
  await page.locator('.nb-cell.active .chat-row.is-peer').getByText('상대방 테스트 응답입니다.').waitFor();
  const demoBubbleWidths = await page.locator('.nb-cell.active .chat-bubble-card').evaluateAll(els => els.map(el => Math.round(el.getBoundingClientRect().width)));
  assert.ok(demoBubbleWidths.length >= 3 && demoBubbleWidths.every(w => Math.abs(w - demoBubbleWidths[0]) <= 1), `Fixed-width bubbles expected, got ${demoBubbleWidths.join(',')}`);

  // Execute Python cell: verify sequential execution order (#2), output, bottom dock, and automatic advance to next cell below.
  await page.getByRole('button', { name: /새로운 실험/ }).first().click();
  const demoPyCard = page.locator('.nb-cell', { hasText: '새로운 실험' }).first();
  await demoPyCard.locator('[data-cell-run]').click();
  await page.waitForFunction(() => {
    const cards = Array.from(document.querySelectorAll('.nb-cell'));
    const py = cards.find(el => el.textContent?.includes('새로운 실험'));
    return py?.querySelector('.nb-cell-output')?.textContent?.includes('Squares: [0, 1, 4, 9, 16, 25]')
      && py?.querySelector('[data-exec-order]')?.textContent?.trim() === '실행 #2'
      && document.querySelector('#bottom-dock-output')?.textContent?.includes('Squares: [0, 1, 4, 9, 16, 25]')
      && document.querySelector('.nb-cell.active .nb-cell-title')?.textContent?.includes('인터랙티브 HTML');
  });

  // Verify interactive HTML cell renders (#3) inside isolated /preview sandbox without parent CSP blocking.
  await page.locator('.nb-cell.active [data-cell-render]').click();
  await page.waitForFunction(() =>
    document.querySelector('.nb-cell.active [data-exec-order]')?.textContent?.trim() === '실행 #3',
  );
  const htmlFrame = page.frameLocator('.nb-cell.active iframe.nb-preview-frame');
  const pingBtn = htmlFrame.locator('#ping');
  await pingBtn.waitFor();
  await pingBtn.click();
  await htmlFrame.getByText('실행 확인 (1)', { exact: true }).waitFor();
  await htmlFrame.getByText('CSS와 JavaScript가 정상 반영되었습니다.', { exact: true }).waitFor();

  // Verify Colab-style bottom-border cell insertion bar fades by mouse proximity and inserts directly below target cell.
  const firstNoteCard = page.locator('.nb-cell', { hasText: '연구 노트' }).first();
  await firstNoteCard.scrollIntoViewIfNeeded();
  const firstInsertZone = firstNoteCard.locator('.nb-cell-insert-zone');
  const firstInsertBar = firstInsertZone.locator('.nb-cell-insert-bar');
  const firstNoteBox = await firstNoteCard.boundingBox();
  const zoneBox = await firstInsertZone.boundingBox();
  assert.ok(firstNoteBox && zoneBox);
  await page.mouse.move(firstNoteBox.x + 40, firstNoteBox.y + 12);
  await page.waitForFunction(el => Number.parseFloat(getComputedStyle(el).opacity) < 0.25, await firstInsertBar.elementHandle());
  const farOpacity = await firstInsertBar.evaluate(el => Number.parseFloat(getComputedStyle(el).opacity));
  await page.mouse.move(zoneBox.x + zoneBox.width / 2, zoneBox.y + zoneBox.height / 2);
  await page.waitForFunction(el => Number.parseFloat(getComputedStyle(el).opacity) > 0.85, await firstInsertBar.elementHandle());
  const nearOpacity = await firstInsertBar.evaluate(el => Number.parseFloat(getComputedStyle(el).opacity));
  assert.ok(nearOpacity > farOpacity + 0.6, `Expected bottom insert bar opacity to respond to pointer proximity (${farOpacity} -> ${nearOpacity})`);
  await firstInsertZone.locator('[data-insert-kind="python"]').click();
  await page.waitForFunction(() => document.querySelector('#cell-count')?.textContent === '7');
  const secondKindAfterInsert = await page.locator('.nb-cell').nth(1).locator('.nb-cell-kind').textContent();
  assert.ok(secondKindAfterInsert?.includes('Python'), `Expected inserted cell right below first cell, got ${secondKindAfterInsert}`);
  await page.getByRole('button', { name: /새로운 실험/ }).first().click();

  // Drag cell resize handle and sidebar splitter to verify resizable editor geometry.
  const editorHost = page.locator('.nb-cell.active .nb-editor-host');
  const cellGrip = page.locator('.nb-cell.active .nb-cell-resize-handle');
  await cellGrip.scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);
  const beforeCellBox = await editorHost.boundingBox();
  const gripBox = await cellGrip.boundingBox();
  if (beforeCellBox && gripBox) {
    await page.mouse.move(gripBox.x + gripBox.width / 2, gripBox.y + gripBox.height / 2);
    await page.mouse.down();
    await page.mouse.move(gripBox.x + gripBox.width / 2, gripBox.y + gripBox.height / 2 + 80);
    await page.mouse.up();
    const afterCellBox = await editorHost.boundingBox();
    assert.ok(afterCellBox && afterCellBox.height > beforeCellBox.height + 50, 'Cell height must increase on drag');
  }
  const sidebarBefore = await page.locator('.sidebar').boundingBox();
  const sidebarResizer = await page.locator('#resizer-sidebar').boundingBox();
  if (sidebarBefore && sidebarResizer) {
    await page.mouse.move(sidebarResizer.x + 2, sidebarResizer.y + 100);
    await page.mouse.down();
    await page.mouse.move(sidebarResizer.x + 62, sidebarResizer.y + 100);
    await page.mouse.up();
    const sidebarAfter = await page.locator('.sidebar').boundingBox();
    assert.ok(sidebarAfter && sidebarAfter.width > sidebarBefore.width + 40, 'Sidebar width must increase on drag');
  }
  await page.screenshot({ path: '.artifacts/studio-proof.png' });

  // Verify mini-arcade games (Snake & Block Drop) inside the bottom output dock.
  await page.locator('[data-dock-tab="games"]').click();
  await page.locator('[data-arcade-stage]').waitFor();
  await page.locator('[data-arcade-toggle]').click();
  await page.waitForFunction(() => document.querySelector('[data-arcade-state]')?.textContent === '플레이 중');
  await page.locator('[data-pad="down"]').click();
  await page.screenshot({ path: '.artifacts/studio-dock-snake.png' });

  await page.locator('[data-arcade-game="blocks"]').click();
  await page.locator('[data-arcade-next]').waitFor();
  await page.locator('[data-arcade-toggle]').click();
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('Space');
  await page.waitForFunction(() => Number(document.querySelector('[data-arcade-score]')?.textContent || '0') > 0);
  assert.ok((await page.locator('#bottom-dock-target').textContent())?.includes('블록 낙하'));
  await page.screenshot({ path: '.artifacts/studio-dock-blocks.png' });
  await page.locator('[data-dock-tab="output"]').click();
  await page.locator('#bottom-dock-output').waitFor();

  // Verify Settings panel starts on general settings, and shows live FPS / CPU 0.2s x 30 charts when top Monitoring tab or topbar button is clicked.
  await page.locator('[data-action="settings"]').click();
  await page.locator('[data-settings-tab="general"][aria-selected="true"]').waitFor();
  assert.equal(await page.locator('#settings-monitor-pane').isHidden(), true, 'Monitoring pane stays hidden until Monitoring tab is clicked');
  await page.locator('[data-settings-tab="monitoring"]').click();
  await page.locator('#settings-monitor-pane:not([hidden])').waitFor();
  await page.waitForFunction(() => {
    const pane = document.querySelector('#settings-monitor-pane');
    const count = Number(pane?.getAttribute('data-sample-count') || '0');
    const fpsText = document.querySelector('[data-monitor-fps]')?.textContent || '';
    const cpuText = document.querySelector('[data-monitor-cpu]')?.textContent || '';
    return count >= 5 && count <= 30 && fpsText.includes('FPS') && cpuText.includes('%');
  });
  await page.locator('[data-monitor-chart="fps"]').waitFor();
  await page.locator('[data-monitor-chart="cpu"]').waitFor();
  await page.screenshot({ path: '.artifacts/studio-monitor.png' });
  await page.locator('[data-monitor-toggle]').click();
  assert.equal(await page.locator('[data-monitor-state]').textContent(), '일시정지');
  await page.locator('[data-monitor-toggle]').click();
  assert.equal(await page.locator('[data-monitor-state]').textContent(), '실시간 수집');
  await page.locator('#modal-close').click();
  await page.waitForFunction(() => !document.querySelector('#modal')?.hasAttribute('open'));

  // Clicking topbar "모니터링" button opens Settings directly on the Monitoring tab.
  await page.locator('#monitor-toggle').click();
  await page.locator('[data-settings-tab="monitoring"][aria-selected="true"]').waitFor();
  await page.waitForFunction(() => Number(document.querySelector('#settings-monitor-pane')?.getAttribute('data-sample-count') || '0') >= 2);
  await page.locator('#modal-close').click();
  await page.waitForFunction(() => !document.querySelector('#modal')?.hasAttribute('open'));

  // Verify left, bottom, right panel toggles show filled active pane when open and outline-only when closed.
  const fillOpacity = async selector => page.locator(`${selector} .layout-pane-fill`).evaluate(el => Number.parseFloat(getComputedStyle(el).opacity));
  assert.ok((await fillOpacity('#sidebar-toggle')) >= 0.99, 'Left panel toggle fill must be solid when open');
  assert.ok((await fillOpacity('#bottom-panel-toggle')) >= 0.99, 'Bottom panel toggle fill must be solid when open');
  assert.ok((await fillOpacity('#inspector-toggle')) >= 0.99, 'Right panel toggle fill must be solid when open');
  await page.locator('#inspector-toggle').click();
  await page.waitForFunction(() => document.body.classList.contains('inspector-hidden'));
  await page.waitForFunction(() => Number.parseFloat(getComputedStyle(document.querySelector('#inspector-toggle .layout-pane-fill')).opacity) <= 0.05);
  await page.screenshot({ path: '.artifacts/studio-panels-toggled.png' });
  await page.locator('#inspector-toggle').click();
  await page.waitForFunction(() => !document.body.classList.contains('inspector-hidden'));
  await page.waitForFunction(() => Number.parseFloat(getComputedStyle(document.querySelector('#inspector-toggle .layout-pane-fill')).opacity) >= 0.99);
  await page.locator('#sidebar-toggle').click();
  await page.waitForFunction(() => Number.parseFloat(getComputedStyle(document.querySelector('#sidebar-toggle .layout-pane-fill')).opacity) <= 0.05);
  await page.locator('#sidebar-toggle').click();
  await page.waitForFunction(() => Number.parseFloat(getComputedStyle(document.querySelector('#sidebar-toggle .layout-pane-fill')).opacity) >= 0.99);
  await page.locator('#bottom-panel-toggle').click();
  await page.waitForFunction(() => Number.parseFloat(getComputedStyle(document.querySelector('#bottom-panel-toggle .layout-pane-fill')).opacity) <= 0.05);
  await page.locator('#bottom-panel-toggle').click();
  await page.waitForFunction(() => Number.parseFloat(getComputedStyle(document.querySelector('#bottom-panel-toggle .layout-pane-fill')).opacity) >= 0.99);

  // Verify right-panel agent chat & Context/Activity tab switching on desktop.
  await page.locator('#agent-drawer:not([hidden])').waitFor();
  await page.locator('.agent-dock-tabs [data-action="context"]').click();
  await page.locator('.inspector:not([hidden]) #inspector-content').waitFor();
  await page.locator('.inspector .inspector-tabs [data-action="agent"]').click();
  await page.locator('#agent-drawer:not([hidden]) #agent-prompt').waitFor();
  await page.locator('#agent-prompt').fill('선택한 셀의 내용을 설명해 줘');
  await page.locator('#agent-prompt').press('Enter');
  await page.locator('#agent-thread .agent-msg.assistant .agent-markdown').first().waitFor();
  await page.screenshot({ path: '.artifacts/studio-agent-right.png' });

  await page.locator('#account').click();
  await page.waitForURL('**/login**');
  await page.locator('#login-particles').waitFor();
  const loginCanvasBox = await page.locator('#login-particles').boundingBox();
  assert.ok(loginCanvasBox && loginCanvasBox.width > 400 && loginCanvasBox.height > 400, 'Login particle landing canvas must fill viewport');
  await page.screenshot({ path: '.artifacts/orcid-login.png' });
  await page.locator('#orcid-login:not([disabled])').click();
  await page.getByRole('button', { name: /공동 연구 workspace/ }).click();
  await page.waitForFunction(() => document.querySelector('#connection-label')?.textContent === 'WORKSPACE LIVE SYNC');

  // Stateless cookie session: the JWT lives only in an HttpOnly cookie, never in web storage, and a new tab stays signed in.
  const sessionCookie = (await first.cookies()).find(c => c.name === 'IRADE_SESSION');
  assert.ok(sessionCookie && sessionCookie.httpOnly && sessionCookie.secure && sessionCookie.sameSite === 'Strict', 'session cookie must be HttpOnly/Secure/Strict');
  assert.equal(await page.evaluate(() => [...Object.keys(localStorage), ...Object.keys(sessionStorage)].some(k => k.startsWith('irade.orcid.token'))), false);
  assert.equal(await page.evaluate(() => document.cookie.includes('IRADE_SESSION=')), false, 'session cookie must not be readable by scripts');
  const newTab = await first.newPage();
  await newTab.goto(`${origin}/login?next=/agdd/notebook`);
  await newTab.waitForURL('**/agdd/notebook**');
  await newTab.waitForFunction(() => document.querySelector('#account')?.textContent?.includes('계정'));
  await newTab.close();

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
  await peer.goto(origin + '/agdd/notebook?workspace=42', { waitUntil: 'networkidle' });
  await peer.waitForFunction(() => document.querySelector('#topbar-workspace')?.textContent === '공동 연구 workspace');
  await peer.locator('#cell-list [data-cell]', { hasText: '공동 연구' }).first().click();

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
  await page.waitForFunction(() =>
    document.querySelector('#topbar-workspace')?.textContent === '공동 연구 workspace'
    && document.querySelector('#workspace-name')?.textContent === '공동 연구 workspace'
    && document.querySelector('#notebook-title')?.textContent === '공동 연구 workspace',
  );
  await page.locator('#cell-list [data-cell]', { hasText: '공동 연구' }).first().click();
  await converged(page);
  // Verify code cell execution in shared workspace seamlessly executes (including host fallback when VM is unavailable).
  const wsPyCard = page.locator('.nb-cell', { hasText: '공동 연구' }).first();
  await wsPyCard.locator('[data-cell-run]').click();
  await page.waitForFunction(() => {
    const cards = Array.from(document.querySelectorAll('.nb-cell'));
    const py = cards.find(el => el.textContent?.includes('공동 연구'));
    const badge = py?.querySelector('.nb-output-badge')?.textContent || '';
    const order = py?.querySelector('[data-exec-order]')?.textContent?.trim() || '';
    return badge.includes('Executed') && order === '실행 #1';
  });

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
  assert.deepEqual(errors, [], 'No browser errors before XSS sandbox test');
  await page.locator('.nb-cell.active .monaco-editor .view-lines').click();
  await page.keyboard.press('Control+a');
  await page.keyboard.insertText('<h1>격리된 문서</h1><script>parent.document.body.dataset.injected="yes"</script><img src=x onerror="alert(1)">');
  await page.locator('.nb-cell.active iframe.nb-preview-frame').waitFor();
  await page.frameLocator('.nb-cell.active iframe.nb-preview-frame').getByRole('heading', { name: '격리된 문서' }).waitFor();
  assert.equal(await page.locator('body').getAttribute('data-injected'), null);
  assert.ok(errors.length >= 1 && errors.every(msg => msg.includes('Blocked a frame with origin "null"')), 'XSS parent-access attempt must be blocked by null-origin sandbox');
  errors.length = 0;

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
  const wsLeanCard = page.locator('.nb-cell', { hasText: '새로운 정리' }).first();
  await wsLeanCard.locator('[data-cell-run]').click();
  await page.waitForFunction(() => {
    const cards = Array.from(document.querySelectorAll('.nb-cell'));
    const lean = cards.find(el => el.textContent?.includes('새로운 정리'));
    return lean?.querySelector('.nb-output-badge')?.textContent?.includes('Proof Verified')
      && lean?.querySelector('.nb-cell-output')?.textContent?.includes('add_zero_identity')
      && lean?.querySelector('[data-exec-order]')?.textContent?.trim() === '실행 #2';
  });
  await page.screenshot({ path: '.artifacts/studio-shared-workspace.png' });

  // Verify shared AI Messenger cell: multi-user peer input, upward scroll pagination, auto-scroll triggers & AI response.
  const collabContext = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 950 } });
  await collabContext.addInitScript(origin => {
    if (window === window.top && location.origin === origin) {
      sessionStorage.setItem(`irade.orcid.token:${origin}`, 'fixture-peer');
    }
  }, origin);
  const collab = await collabContext.newPage();
  collab.setDefaultTimeout(20000);
  collab.on('pageerror', error => errors.push(error.message));
  await collab.goto(origin + '/agdd/notebook?workspace=42', { waitUntil: 'networkidle' });

  await page.getByRole('button', { name: '새 셀 만들기', exact: false }).click();
  await page.getByRole('button', { name: /에이전트 대화/ }).last().click();
  await Promise.all([
    page.locator('.nb-cell-messenger .nb-chat-shell').first().waitFor(),
    collab.locator('.nb-cell-messenger .nb-chat-shell').first().waitFor(),
  ]);

  const pageMessenger = page.locator('.nb-cell-messenger').first();
  const collabMessenger = collab.locator('.nb-cell-messenger').first();
  for (let i = 1; i <= 13; i += 1) {
    await pageMessenger.locator('[data-chat-input]').fill(`기록 메시지 #${String(i).padStart(2, '0')}`);
    await pageMessenger.locator('[data-chat-send-only]').click();
    await pageMessenger.locator(`.chat-row:has-text("기록 메시지 #${String(i).padStart(2, '0')}")`).waitFor();
  }

  // Initial view shows latest batch (8) scrolled to bottom with newest message (#13) last.
  const initialCount = await pageMessenger.locator('.chat-row').count();
  assert.equal(initialCount, 8, `Expected initial chat window of 8 rows, got ${initialCount}`);
  assert.ok((await pageMessenger.locator('.chat-row').last().innerText()).includes('기록 메시지 #13'));
  await pageMessenger.locator('[data-chat-load-more]').waitFor();

  // Scroll up inside chat-feed -> automatically loads older batch and keeps visual scroll anchored.
  const feedBox = pageMessenger.locator('[data-chat-feed]');
  await feedBox.evaluate(el => { el.scrollTop = 10; });
  await page.waitForFunction(() => (document.querySelector('.nb-cell-messenger .chat-row') ? document.querySelectorAll('.nb-cell-messenger .chat-row').length : 0) >= 14);
  const loadedAfterScroll = await pageMessenger.locator('.chat-row').count();
  assert.equal(loadedAfterScroll, 14, `Expected all 14 messages after upward pagination, got ${loadedAfterScroll}`);
  assert.ok((await pageMessenger.locator('.chat-row').first().innerText()).includes('IRADE AI'), 'Oldest starter message should appear at top after loading history');

  // Scroll up mid-history, then focus composer input -> must auto-scroll back to bottom.
  await feedBox.evaluate(el => { el.scrollTop = 40; });
  await pageMessenger.locator('[data-chat-input]').focus();
  await page.waitForFunction(() => {
    const feed = document.querySelector('.nb-cell-messenger [data-chat-feed]');
    return feed ? Math.abs((feed.scrollHeight - feed.clientHeight) - feed.scrollTop) < 8 : false;
  });

  // Scroll up again, then collaborator (Dr. Kim) sends a peer message -> must render as left peer bubble + auto-scroll viewer to bottom.
  await feedBox.evaluate(el => { el.scrollTop = 40; });
  await collabMessenger.locator('[data-chat-input]').fill('상대방 Dr. Kim 실시간 참여 의견입니다.');
  await collabMessenger.locator('[data-chat-send-only]').click();
  await pageMessenger.locator('.chat-row.is-peer').getByText('상대방 Dr. Kim 실시간 참여 의견입니다.').waitFor();
  assert.ok((await pageMessenger.locator('.chat-row.is-peer .chat-author').last().textContent()).includes('Dr. Kim'));
  await page.waitForFunction(() => {
    const feed = document.querySelector('.nb-cell-messenger [data-chat-feed]');
    return feed ? Math.abs((feed.scrollHeight - feed.clientHeight) - feed.scrollTop) < 8 : false;
  });

  // Scroll up again, then send prompt with AI auto-reply ON -> AI response appends at bottom + auto-scrolls to bottom.
  await feedBox.evaluate(el => { el.scrollTop = 60; });
  assert.equal(await pageMessenger.locator('[data-chat-ai-toggle]').isChecked(), true);
  await pageMessenger.locator('[data-chat-input]').fill('메신저에서 AI 요약 메모를 생성해 줘');
  await pageMessenger.locator('[data-chat-send]').click();
  await pageMessenger.locator('.chat-row.is-assistant:has-text("셀을 하단에 생성했습니다")').waitFor();
  assert.ok((await pageMessenger.locator('.chat-row').last().innerText()).includes('셀을 하단에 생성했습니다'));
  await page.waitForFunction(() => {
    const feed = document.querySelector('.nb-cell-messenger [data-chat-feed]');
    return feed ? Math.abs((feed.scrollHeight - feed.clientHeight) - feed.scrollTop) < 8 : false;
  });

  const wsBubbleWidths = await pageMessenger.locator('.chat-bubble-card').evaluateAll(els => els.map(el => Math.round(el.getBoundingClientRect().width)));
  assert.ok(wsBubbleWidths.length >= 10 && wsBubbleWidths.every(w => Math.abs(w - wsBubbleWidths[0]) <= 1), `Uniform bubble widths expected on desktop, got ${wsBubbleWidths.join(',')}`);
  await page.screenshot({ path: '.artifacts/studio-messenger.png' });

  // Verify shared Workspace inter-cell bottom insertion converges at the exact middle position on peer clients too.
  await page.locator('.nb-cell', { hasText: '공동 연구' }).first().scrollIntoViewIfNeeded();
  await page.locator('.nb-cell', { hasText: '공동 연구' }).first().locator('[data-insert-kind="markdown"]').click();
  await Promise.all([
    page.waitForFunction(() => document.querySelectorAll('.nb-cell')[1]?.querySelector('.nb-cell-kind')?.textContent?.includes('Markdown')),
    collab.waitForFunction(() => document.querySelectorAll('.nb-cell')[1]?.querySelector('.nb-cell-kind')?.textContent?.includes('Markdown')),
  ]);
  await pageMessenger.scrollIntoViewIfNeeded();
  await collabContext.close();

  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, 'Mobile messenger view must have zero horizontal overflow');
  await page.screenshot({ path: '.artifacts/studio-messenger-mobile.png' });
  await page.getByRole('tab', { name: 'Overview', exact: false }).click();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: '.artifacts/studio-mobile.png' });

  // Refresh while on Overview preserves Overview tab + real workspace name; closing workspace then closing Overview renders a completely empty screen without stale cells.
  await page.reload({ waitUntil: 'networkidle' });
  await page.setViewportSize({ width: 1440, height: 950 });
  await page.waitForFunction(() =>
    document.querySelector('#topbar-workspace')?.textContent === '공동 연구 workspace'
    && !document.querySelector('#overview')?.hasAttribute('hidden'),
  );
  await page.locator('#cell-tabs [data-action="notebook"]').click();
  await page.locator('#notebook-cells .nb-cell').first().waitFor();
  await page.locator('[data-action="close-workspace"]').click();
  await page.waitForFunction(() => !new URL(location.href).searchParams.has('workspace'));
  assert.equal(await page.locator('#topbar-workspace').textContent(), '나의 연구 공간');
  assert.equal(await page.locator('#overview').isHidden(), false);
  await page.locator('[data-action="close-overview"]').click();
  await page.waitForFunction(() =>
    document.querySelector('#overview')?.hasAttribute('hidden')
    && document.querySelector('#cell-view')?.hasAttribute('hidden')
    && document.querySelectorAll('#notebook-cells .nb-cell').length === 0,
  );
  await page.screenshot({ path: '.artifacts/studio-empty.png' });
  await page.reload({ waitUntil: 'networkidle' });
  assert.equal(await page.locator('#topbar-workspace').textContent(), '나의 연구 공간');
  assert.equal(await page.locator('#overview').isHidden(), false);

  // Verify mobile homepage get-started layout has zero horizontal overflow.
  await page.goto(origin, { waitUntil: 'networkidle' });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.locator('#interactive-lab').scrollIntoViewIfNeeded();
  await page.screenshot({ path: '.artifacts/studio-home-mobile.png' });

  const mfaContext = await browser.newContext({ ignoreHTTPSErrors: true });
  const mfaPage = await mfaContext.newPage();
  await mfaPage.goto(`${origin}/agdd/notebook`);
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
      await page.screenshot({ path: `.artifacts/studio-failure-${index}.png` }).catch(() => { });
      console.error((await page.locator('body').innerText()).slice(-6000));
    }
  }
  throw error;
} finally {
  await browser?.close();
  await cleanup();
}
