import * as monaco from 'monaco-editor/esm/vs/editor/editor.api.js';
import 'monaco-editor/esm/vs/basic-languages/python/python.contribution.js';
import 'monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution.js';
import 'monaco-editor/esm/vs/basic-languages/html/html.contribution.js';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { AgoraClient } from '../client.ts';
import { replacement } from '../document.ts';
import { ResearchWorkspace, cellTypes, cellTitle, type Cell, type CellKind } from './workspace.ts';

const $ = <T extends HTMLElement = HTMLElement>(selector: string): T => document.querySelector<T>(selector)!;
const escape = (value: string): string => value.replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]!);
const origin = location.origin;
const tokenKey = `irade.orcid.token:${origin}`;
const demoKey = 'irade.research.scratch';
let token: string | undefined = sessionStorage.getItem(tokenKey) || undefined;
let workspace: ResearchWorkspace | undefined;
let workspaceName = '나의 연구 공간';
let cells: Cell[] = [];
let selected: string | undefined;
let demo = false;
let connected = false;
let filter = 'all';
let overviewOpen = true;
let viewMode: 'overview' | 'notebook' = 'overview';
let deletingCellId: string | undefined;
let editingRemote = false;
let inspectorTab = 'context';
let loadGeneration = 0;
let activePresenceCellId: string | undefined;
let toastTimer: ReturnType<typeof setTimeout>;
interface ExecutionOutput {
  running?: boolean;
  ok: boolean;
  exitCode: number | null;
  stdout: string;
  stderr: string;
  durationMs: number;
  status: 'running' | 'success' | 'verified' | 'error' | 'timeout';
  runtime?: 'agora-vm' | 'host';
}
const models = new Map<string, monaco.editor.ITextModel>();
const cellEditors = new Map<string, monaco.editor.IStandaloneCodeEditor>();
const previewCells = new Set<string>();
const cellOutputs = new Map<string, ExecutionOutput>();
const activities: { message: string; time: string }[] = [];
const modal = $<HTMLDialogElement>('#modal');
const starters: Record<CellKind, string> = {
  agent: '# 연구 대화\n\n## 사용자\n연구할 질문과 배경을 기록하세요.\n\n## 에이전트\n에이전트와 나눈 응답을 여기에 보관하세요.\n',
  lean: '-- 새로운 정리\n\ntheorem add_zero_identity (n : Nat) : n + 0 = n := by\n  rfl\n\n#check add_zero_identity\n',
  rocq: '(* 새로운 증명 *)\n\nTheorem add_zero_identity : forall n : nat, n + 0 = n.\nProof.\n  intros n.\n  induction n; simpl; auto.\nQed.\n',
  python: '# 새로운 실험\n\ndef square(n):\n    return n * n\n\nvalues = [square(n) for n in range(6)]\nprint("Squares:", values)\n',
  markdown: '# 연구 노트\n\n## 연구 질문\n여기에 연구할 질문을 적어 보세요.\n\n## 관찰과 기록\n- 가설과 실험 결과를 연결하세요.\n- 증명과 코드의 의미를 문서로 남기세요.\n',
  html: `<style>
  .card { padding: 16px; border-radius: 10px; border: 1px solid #3f3f46; background: #18181b; color: #f4f4f5; font-family: system-ui, sans-serif; }
  .btn { margin-top: 10px; padding: 6px 12px; border-radius: 6px; border: 1px solid #34d399; background: transparent; color: #34d399; cursor: pointer; }
</style>
<article class="card">
  <h2>인터랙티브 HTML 연구 문서</h2>
  <p id="status">버튼을 눌러 JavaScript 동작을 확인하세요.</p>
  <button class="btn" id="ping">실행 확인 (0)</button>
</article>
<script>
  let count = 0;
  document.getElementById('ping').addEventListener('click', (e) => {
    count++;
    e.target.textContent = '실행 확인 (' + count + ')';
    document.getElementById('status').textContent = 'CSS와 JavaScript가 정상 반영되었습니다.';
  });
</script>
`,
};

(globalThis as typeof globalThis & { MonacoEnvironment: unknown }).MonacoEnvironment = {
  getWorker: () => new Worker('/irade-assets/studio/editor.worker.js', { type: 'module' }),
};
for (const language of ['lean4', 'rocq']) {
  monaco.languages.register({ id: language });
  monaco.languages.setMonarchTokensProvider(language, {
    keywords: language === 'lean4' ? ['theorem','lemma','def','example','by','rfl','simp','exact','intro','intros','apply','rw','induction','case','cases','import','namespace','end','structure','where','have','let','fun','forall','sorry'] : ['Theorem','Lemma','Definition','Proof','Qed','Defined','Admitted','Require','Import','forall','intros','reflexivity','simpl','auto','apply','rewrite','induction','destruct','exact'],
    tokenizer: { root: [[/--.*$/, 'comment'], [/\(\*/, 'comment', '@comment'], [/"(?:[^"\\]|\\.)*"/, 'string'], [/\b\d+\b/, 'number'], [/[a-zA-Z_][\w']*/, { cases: { '@keywords': 'keyword', '@default': 'identifier' } }], [/[∀∃⊢→←↔λ:=+*<>]+/, 'operator']], comment: [[/[^(*]+/, 'comment'], [/\*\)/, 'comment', '@pop'], [/[(*]/, 'comment']] },
  });
}
monaco.editor.defineTheme('irade-dark', {
  base: 'vs-dark', inherit: true,
  rules: [{ token: 'comment', foreground: '71717A', fontStyle: 'italic' }, { token: 'keyword', foreground: 'C4B5FD' }, { token: 'string', foreground: '6EE7B7' }, { token: 'number', foreground: 'FCD34D' }, { token: 'operator', foreground: 'A1A1AA' }],
  colors: { 'editor.background': '#1B1B1F', 'editor.foreground': '#D4D4D8', 'editorLineNumber.foreground': '#52525B', 'editorLineNumber.activeForeground': '#A1A1AA', 'editorCursor.foreground': '#34D399', 'editor.selectionBackground': '#34D39928', 'editor.lineHighlightBackground': '#FFFFFF06', 'editorIndentGuide.background1': '#27272A', 'editorWidget.background': '#18181B', 'editorWidget.border': '#3F3F46', 'editorSuggestWidget.background': '#18181B', 'editorSuggestWidget.border': '#3F3F46', 'focusBorder': '#34D399', 'scrollbarSlider.background': '#52525B40' },
});
monaco.editor.defineTheme('irade-light', {
  base: 'vs', inherit: true,
  rules: [{ token: 'comment', foreground: '78716C', fontStyle: 'italic' }, { token: 'keyword', foreground: '6D28D9' }, { token: 'string', foreground: '047857' }, { token: 'number', foreground: 'B45309' }, { token: 'operator', foreground: '52525B' }],
  colors: { 'editor.background': '#FFFFFF', 'editor.foreground': '#27272A', 'editorLineNumber.foreground': '#A8A29E', 'editorLineNumber.activeForeground': '#57534E', 'editorCursor.foreground': '#059669', 'editor.selectionBackground': '#05966920', 'editor.lineHighlightBackground': '#00000005', 'editorIndentGuide.background1': '#E7E5E4', 'editorWidget.background': '#FAF9F6', 'editorWidget.border': '#D6D3D1', 'editorSuggestWidget.background': '#FAF9F6', 'editorSuggestWidget.border': '#D6D3D1', 'focusBorder': '#059669', 'scrollbarSlider.background': '#A8A29E40' },
});
const themeKey = 'irade-theme';
function currentTheme(): 'dark' | 'light' { return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark'; }
function applyTheme(next: 'dark' | 'light'): void {
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem(themeKey, next);
  monaco.editor.setTheme(next === 'light' ? 'irade-light' : 'irade-dark');
  const toggle = document.querySelector<HTMLButtonElement>('#theme-toggle');
  if (toggle) { toggle.textContent = next === 'light' ? '☾' : '☀'; }
  for (const id of previewCells) { renderCellPreview(id); }
}
applyTheme(localStorage.getItem(themeKey) === 'light' ? 'light' : 'dark');
function notify(message: string, error = false): void {
  clearTimeout(toastTimer); const toast = $('#toast');
  toast.textContent = message; toast.classList.toggle('error', error); toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, error ? 10000 : 4500);
}
const errorLabels: Record<string, string> = {
  TOKEN_EXPIRED: '로그인이 만료되었습니다. 다시 로그인하세요.',
  TOKEN_REQUIRED: 'ORCID로 IRADE에 로그인하세요.',
  CANVAS_ACCESS_REVOKED: 'Workspace 접근 권한이 회수되었습니다.',
  WORKSPACE_ACCESS_DENIED: '이 workspace를 변경할 권한이 없습니다.',
  ITEM_WRITE_DENIED: '이 셀에 대한 쓰기 권한이 없습니다.',
  ITEM_PERMISSION_DENIED: '셀 접근 그룹 변경은 Workspace 관리자만 가능하며, 참여 중인 그룹에만 부여할 수 있습니다.',
  ITEM_PERMISSION_INVALID: '최소 1개 이상의 접근 그룹을 선택해야 합니다.',
  ITEM_LOCKED: '이 셀은 잠겨 있습니다. 잠금을 해제한 뒤 편집하세요.',
  ITEM_ID_INVALID: '유효하지 않은 Canvas 객체 ID입니다.',
  CANVAS_READ_DENIED: '이 workspace 또는 셀을 읽을 권한이 없습니다.',
  CANVAS_ACCESS_DENIED: 'Workspace 접근 권한이나 비밀번호를 확인하세요.',
  FILE_TOO_LARGE: '셀 저장 한도(128 KiB)를 넘었습니다. 내용을 나누어 주세요.',
  FILE_REPLACED: '셀이 외부에서 교체되었습니다. 현재 내용을 내보내 보관한 후 다시 여세요.',
  RUNTIME_EXECUTION_DENIED: '이 셀의 그룹에는 코드/증명을 실행할 권한이 없습니다.',
  RUNTIME_NOT_CONNECTED: 'Agora Wall 브로커에 연결되지 않았습니다. 연결 상태를 확인하세요.',
  RUNTIME_TIMEOUT: 'Agora VM 실행 시간이 초과되었습니다.',
  RUNTIME_VM_UNAVAILABLE: 'Agora VM 런타임 서버에 연결할 수 없습니다.',
  RUNTIME_UNSUPPORTED_LANGUAGE: 'Agora VM에서 지원하지 않는 실행 언어입니다.',
  RUNTIME_CODE_TOO_LARGE: '실행할 코드 크기가 한도를 초과했습니다.',
};
function formatErrorLabel(raw: string): string {
  return errorLabels[raw] || raw;
}
function report(error: unknown): void {
  const raw = error instanceof Error ? error.message : String(error);
  notify(formatErrorLabel(raw), true);
  $('#cell-save').textContent = '저장 확인 필요';
}
function log(message: string): void {
  activities.unshift({ message, time: new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) });
  activities.splice(60); if (inspectorTab === 'activity') { renderInspector(); }
}
function showModal(title: string, html: string): void {
  $('#modal-title').textContent = title; $('#modal-body').innerHTML = html;
  if (!modal.open) { modal.showModal(); }
}
$('#modal-close').addEventListener('click', () => modal.close());
modal.addEventListener('click', event => { if (event.target === modal) { const bounds = modal.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) { modal.close(); } } });
async function api<T>(path: string, body?: object): Promise<T> {
  const response = await fetch(origin + path, { method: body ? 'POST' : 'GET', headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}), redirect: 'error', signal: AbortSignal.timeout(20000) });
  const result = await response.json();
  if (!response.ok) { if (response.status === 401) { token = undefined; sessionStorage.removeItem(tokenKey); updateAccount(); } throw new Error(result.message || `요청에 실패했습니다 (${response.status}).`); }
  return result as T;
}
function updateAccount(): void { $('#account').textContent = token ? 'ORCID · 계정' : 'ORCID 로그인 ↗'; }
async function signIn(): Promise<boolean> {
  if (workspace?.pending) { await workspace.flush(); }
  const url = new URL('/irade/login', origin);
  const id = workspace?.client.config.canvasId || Number(new URL(location.href).searchParams.get('workspace'));
  if (id) { url.searchParams.set('workspace', String(id)); }
  location.assign(String(url));
  return false;
}
async function ask(title: string, label: string, password = false): Promise<string | undefined> {
  return new Promise(resolve => {
    const headerNote = password
      ? `<div class="ask-security-banner"><span class="ask-lock-icon">🔒</span><div><strong>비공개 연구 공간</strong><small>접근 권한 확인을 위해 workspace 비밀번호를 입력하세요.</small></div></div>`
      : '';
    const inputControl = password
      ? `<div class="password-field"><input name="value" type="password" required autocomplete="current-password" placeholder="비밀번호 입력…"><button type="button" class="password-toggle" id="toggle-password" aria-label="비밀번호 표시">표시</button></div>`
      : `<input name="value" type="text" required autocomplete="off">`;
    showModal(title, `<form id="ask-form" class="ask-form">${headerNote}<label class="field"><span>${escape(label)}</span>${inputControl}</label><div class="form-actions"><button type="button" class="button ghost" id="ask-cancel">취소</button><button type="submit" class="button primary">${password ? '입장하기 →' : '확인 →'}</button></div></form>`);
    const form = $<HTMLFormElement>('#ask-form');
    const input = form.querySelector<HTMLInputElement>('input[name="value"]')!;
    const close = (): void => resolve(undefined);
    modal.addEventListener('close', close, { once: true });
    $('#ask-cancel').addEventListener('click', () => modal.close());
    if (password) {
      const toggle = $<HTMLButtonElement>('#toggle-password');
      toggle.addEventListener('click', () => {
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        toggle.textContent = show ? '숨김' : '표시';
        input.focus();
      });
    }
    form.addEventListener('submit', event => {
      event.preventDefault();
      const result = String(new FormData(event.currentTarget as HTMLFormElement).get('value'));
      modal.removeEventListener('close', close);
      resolve(result);
      modal.close();
    });
    requestAnimationFrame(() => input.focus());
  });
}
interface Canvas { canvas_id: number; canvas_name: string; description?: string }
async function openWorkspace(): Promise<void> {
  if (!token && !await signIn()) { return; }
  let available: Canvas[] = [];
  const convert = (row: { workspaceId: number; name: string; description?: string }): Canvas => ({ canvas_id: row.workspaceId, canvas_name: row.name, description: row.description });
  available = (await api<{ workspaceId: number; name: string; description?: string }[]>('/api/irade/workspaces')).map(convert);
  showModal('Workspace 선택', '<p class="modal-copy">공유 연구 workspace를 선택하거나 새로 만들면 모든 셀과 공동 작업을 실시간으로 동기화합니다.</p><input class="search-input" id="workspace-search" type="search" aria-label="Workspace 검색" placeholder="Workspace 이름 검색…"><div class="choice-list" id="workspace-choices"></div><div class="form-actions"><button class="button ghost" id="join-workspace">초대 링크 / ID로 열기</button><button class="button primary" id="create-workspace">새 workspace ＋</button></div>');
  const render = (): void => {
    const query = $<HTMLInputElement>('#workspace-search').value.toLowerCase();
    $('#workspace-choices').innerHTML = available.filter(canvas => `${canvas.canvas_name} ${canvas.canvas_id}`.toLowerCase().includes(query)).map(canvas => `<button class="choice" data-canvas="${canvas.canvas_id}"><span class="workspace-avatar">${escape(canvas.canvas_name.charAt(0).toUpperCase())}</span><span><strong>${escape(canvas.canvas_name)}</strong><small>${escape(canvas.description || '공유 연구 workspace')} · #${canvas.canvas_id}</small></span><span class="choice-arrow">↗</span></button>`).join('') || '<p class="empty-search">아직 workspace가 없습니다. 새 연구 공간을 만들어 보세요.</p>';
    document.querySelectorAll<HTMLButtonElement>('[data-canvas]').forEach(button => button.addEventListener('click', () => {
      const canvas = available.find(canvas => canvas.canvas_id === Number(button.dataset.canvas))!;
      modal.close(); void connect(canvas.canvas_id, canvas.canvas_name).catch(report);
    }));
  };
  render();
  let searchVersion = 0;
  let searchTimer: ReturnType<typeof setTimeout>;
  $('#workspace-search').addEventListener('input', () => {
    const version = ++searchVersion; clearTimeout(searchTimer);
    const value = $<HTMLInputElement>('#workspace-search').value;
    searchTimer = setTimeout(() => { void (async () => {
      const result = await api<{ workspaceId: number; name: string; description?: string }[]>('/api/irade/workspaces?q=' + encodeURIComponent(value));
      if (version === searchVersion && modal.open && $('#workspace-choices')) { available = result.map(convert); render(); }
    })().catch(report); }, 250);
  });
  $('#join-workspace').addEventListener('click', () => { void (async () => {
    const value = await ask('공유 workspace 열기', 'Workspace ID 또는 공유 링크'); if (!value) { return; }
    let id = Number(value.trim());
    if (!id) { try { const url = new URL(value); if (url.protocol === 'agora:') { if (url.hostname !== location.hostname) { throw new Error(); } id = Number(url.username); } else { if (url.origin !== origin) { throw new Error(); } id = Number(url.searchParams.get('workspace')); } } catch { throw new Error('현재 Agora 서버의 workspace 링크를 입력하세요.'); } }
    await connect(id, available.find(canvas => canvas.canvas_id === id)?.canvas_name || `Workspace ${id}`);
  })().catch(report); });
  $('#create-workspace').addEventListener('click', () => { void (async () => {
    const name = await ask('새 연구 workspace', 'Workspace 이름'); if (!name?.trim()) { return; }
    const result = convert(await api<{ workspaceId: number; name: string; description?: string }>('/api/irade/workspaces', { name: name.trim(), description: 'IRADE 연구 workspace' }));
    await connect(result.canvas_id, result.canvas_name || name.trim());
  })().catch(report); });
}
function clearModels(): void {
  activePresenceCellId = undefined;
  for (const ed of cellEditors.values()) { ed.setModel(null); ed.dispose(); }
  cellEditors.clear();
  previewCells.clear();
  cellOutputs.clear();
  for (const model of models.values()) { model.dispose(); }
  models.clear();
  const peersEl = document.querySelector<HTMLElement>('#notebook-peers');
  if (peersEl) { peersEl.hidden = true; peersEl.textContent = ''; }
}
function shortItemId(id: string): string {
  return id.length > 16 ? `${id.slice(0, 14)}…` : id;
}
function permissionSummary(cell: Cell): string {
  if (!cell.permission.length) { return '기본 그룹'; }
  return cell.permission.length === 1 ? cell.permission[0] : `${cell.permission[0]} +${cell.permission.length - 1}`;
}
function updatePermissionSummary(): void {
  if (!workspace || !connected) {
    $('#permission-label').textContent = demo ? '로컬 편집' : '';
    return;
  }
  const { isAdmin, groups, writeGroups, executeGroups } = workspace.identity;
  if (isAdmin) {
    $('#permission-label').textContent = `Admin · 전체 권한 (${groups.length || 1}그룹)`;
    return;
  }
  const caps = [
    groups.length ? '읽기' : '',
    writeGroups.length ? '쓰기' : '',
    executeGroups.length ? '실행' : '',
  ].filter(Boolean).join('/') || '읽기 전용';
  const groupText = groups.length ? groups.join(', ') : '권한 없음';
  $('#permission-label').textContent = `${groupText} · ${caps}`;
}
function setPresenceFocus(nextId: string | undefined): void {
  if (activePresenceCellId && activePresenceCellId !== nextId) {
    workspace?.sendCellPresence(activePresenceCellId, false);
  }
  activePresenceCellId = nextId;
  if (nextId) {
    workspace?.sendCellPresence(nextId, true);
  }
}
function syncPresenceUI(): void {
  const peersEl = document.querySelector<HTMLElement>('#notebook-peers');
  if (peersEl) {
    if (!workspace || !connected) {
      peersEl.hidden = true;
    } else {
      const peers = workspace.peers;
      peersEl.hidden = !peers.length;
      if (peers.length) {
        const names = peers.slice(0, 2).map(peer => peer.nickname || `Peer ${peer.peer_id.slice(0, 4)}`).join(', ');
        peersEl.textContent = `👥 ${peers.length}명${names ? ` · ${names}` : ''}`;
      }
    }
  }
  const byCell = new Map<string, string[]>();
  for (const entry of workspace?.remoteEditors || []) {
    if (!entry.editing) { continue; }
    const label = entry.peer.nickname || `참여자 ${entry.peer.peer_id.slice(0, 4)}`;
    const list = byCell.get(entry.itemId) || [];
    if (!list.includes(label)) { list.push(label); }
    byCell.set(entry.itemId, list);
  }
  document.querySelectorAll<HTMLElement>('[data-nb-presence]').forEach(container => {
    const cellId = container.dataset.nbPresence!;
    const editors = byCell.get(cellId) || [];
    container.innerHTML = editors.slice(0, 2).map(name => `<span class="nb-presence-chip"><span class="nb-presence-dot"></span>${escape(name)} 편집 중</span>`).join('');
  });
  if (inspectorTab === 'context') { renderInspector(); }
}
async function connect(id: number, name: string): Promise<void> {
  if (!Number.isSafeInteger(id) || id <= 0) { throw new Error('올바른 workspace ID를 입력하세요.'); }
  if (workspace?.pending) { await workspace.flush(); }
  const generation = ++loadGeneration;
  setPresenceFocus(undefined);
  workspace?.dispose(); clearModels(); demo = false; connected = false; selected = undefined; cells = []; workspaceName = name;
  const client = new AgoraClient({ origin, canvasId: id }, async () => token || (await signIn() ? token : undefined), async () => ask('비공개 workspace', 'Workspace 비밀번호', true));
  const current = new ResearchWorkspace(client); workspace = current;
  current.onError = error => { if (generation === loadGeneration) { report(error); } };
  current.onState = state => {
    if (generation !== loadGeneration) { return; }
    connected = state === 'Connected' || state === 'Saving';
    const translated: Record<string, string> = { Connected: '실시간 연결됨', Saving: '변경 사항 저장 중…', Connecting: 'workspace 연결 중…', Reconnecting: '재연결 중 · 편집 내용 유지' };
    $('#status').textContent = translated[state] || state;
    $('#connection-label').textContent = state === 'Saving' ? '동기화 중' : connected ? 'WORKSPACE LIVE SYNC' : '연결 중';
    $('#cell-save').textContent = state === 'Saving' ? '저장 중…' : connected ? 'Workspace 동기화됨' : '로컬 변경 유지';
    updatePermissionSummary();
    for (const [cellId, ed] of cellEditors.entries()) {
      const item = cells.find(entry => entry.id === cellId);
      ed.updateOptions({ readOnly: item ? !item.canWrite : !client.canWrite });
    }
    syncPresenceUI();
    if (state === 'Connected' && current.pending) { void current.flush().catch(report); }
    if (!['Connected','Saving','Connecting','Reconnecting'].includes(state)) { report(new Error(state)); }
  };
  current.onIdentityChange = () => {
    if (generation !== loadGeneration) { return; }
    updatePermissionSummary();
    for (const cell of cells) { updateCellCardMeta(cell.id); }
    renderInspector();
  };
  current.onPresence = () => {
    if (generation === loadGeneration) { syncPresenceUI(); }
  };
  current.onCells = () => {
    if (generation !== loadGeneration) { return; }
    cells = current.cells;
    if (selected && !cells.some(cell => cell.id === selected)) {
      const removed = selected; selected = undefined;
      if (models.has(removed) && deletingCellId !== removed) {
        const model = models.get(removed)!; const value = model.getValue();
        showModal('공유 셀이 삭제되었거나 접근 권한이 변경되었습니다', '<p class="modal-copy">다른 참여자가 이 셀을 삭제했거나 이 그룹의 접근 권한이 회수되었습니다. 현재 편집기의 내용을 내려받아 보관할 수 있습니다.</p><label class="field">현재 탭의 로컬 복사본<textarea id="recovery-text" readonly></textarea></label><div class="form-actions"><button class="button primary" id="recover-cell">로컬 복사본 내려받기 ↧</button></div>');
        $<HTMLTextAreaElement>('#recovery-text').value = value;
        $('#recover-cell').addEventListener('click', () => download(value, 'recovered-cell.txt'));
      }
      if (viewMode === 'notebook') { renderNotebook(); }
    }
    renderList(); renderInspector();
    if (viewMode === 'notebook') { renderNotebook(); }
  };
  current.onText = cell => {
    const model = models.get(cell.id);
    if (model && model.getValue() !== cell.text) {
      editingRemote = true;
      try {
        // Apply a minimal range; preserve selection, undo stack and scroll position.
        const edit = replacement(model.getValue(), cell.text);
        const from = model.getPositionAt(edit.start), to = model.getPositionAt(edit.end);
        const ed = cellEditors.get(cell.id);
        const selection = ed?.getSelections();
        model.pushEditOperations(selection || null, [{ range: new monaco.Range(from.lineNumber, from.column, to.lineNumber, to.column), text: edit.text }], () => selection || null);
      } finally { editingRemote = false; }
      updateCellCardMeta(cell.id);
      renderList();
      if (previewCells.has(cell.id)) { renderCellPreview(cell.id); }
      log(`${cellTypes[cell.kind].short} 셀의 Canvas 동기화 변경을 반영했습니다.`);
    }
  };
  const url = new URL(location.href); url.searchParams.set('workspace', String(id)); history.replaceState(null, '', url);
  openNotebook(); renderList();
  await current.load();
  if (generation !== loadGeneration) { return; }
  sessionStorage.setItem('irade.research.lastWorkspace', JSON.stringify({ id, name }));
  $('#workspace-subtitle').textContent = `Agora Canvas · #${id}`;
  $('#workspace-mode').textContent = 'Agora Canvas'; $('#sync-description').textContent = '각 셀은 Canvas 객체 ID·권한 ACL로 실시간 동기화';
  updatePermissionSummary();
  log(`${name} Canvas workspace를 연결했습니다.`); notify('Agora Canvas 실시간 협업에 연결했습니다.');
}
function visibleCells(): Cell[] {
  return cells.filter(cell => filter === 'all' || cell.kind === filter || filter === 'proof' && ['lean','rocq'].includes(cell.kind) || filter === 'document' && ['markdown','html'].includes(cell.kind));
}
function renderList(): void {
  $('#workspace-name').textContent = workspaceName;
  $('#cell-count').textContent = String(cells.length); $('#status-cell-count').textContent = `${cells.length} cells`;
  $('#cell-list-label').textContent = filter === 'all' ? '연구 셀' : filter === 'agent' ? '에이전트 대화' : filter === 'proof' ? '증명 셀' : filter === 'python' ? '코드 셀' : '문서 셀';
  const visible = visibleCells();
  $('#cell-list').innerHTML = visible.map(cell => `<button class="cell-list-item ${cell.id === selected ? 'selected' : ''}" data-cell="${escape(cell.id)}"><span class="mini-type ${cellTypes[cell.kind].color}">${cellTypes[cell.kind].icon}</span><span><strong>${escape(cell.title)}</strong><small>${cell.locked ? '🔒 ' : ''}${cellTypes[cell.kind].short} · ${String(cells.indexOf(cell) + 1).padStart(2, '0')} · ${escape(permissionSummary(cell))}</small></span></button>`).join('') || `<p class="empty-list">${workspace || demo ? '아직 셀이 없습니다.<br>새 셀로 연구를 시작하세요.' : '대화, 증명, 코드, 문서.<br>연구의 맥락이 여기에 모입니다.'}</p>`;
  const hasWorkspaceTab = Boolean(workspace || demo);
  $('#cell-tabs').innerHTML = hasWorkspaceTab
    ? `<button class="tab ${viewMode === 'notebook' ? 'selected' : ''}" role="tab" aria-selected="${viewMode === 'notebook'}" data-action="notebook"><span class="mint">▤</span><span>${escape(workspaceName)}</span></button>`
    : '';
  document.querySelectorAll<HTMLButtonElement>('[data-cell]').forEach(button => button.addEventListener('click', () => selectCell(button.dataset.cell!)));
  $('#overview-tab').hidden = !overviewOpen;
  $('#overview-tab').classList.toggle('selected', viewMode === 'overview');
  $('#overview-tab').setAttribute('aria-selected', String(viewMode === 'overview'));
}
function ensureModel(cell: Cell): monaco.editor.ITextModel {
  let model = models.get(cell.id);
  if (!model) {
    model = monaco.editor.createModel(cell.text, cellTypes[cell.kind].language, monaco.Uri.parse(`irade://cell/${cell.id}.${cellTypes[cell.kind].extension}`));
    models.set(cell.id, model);
    model.onDidChangeContent(() => {
      if (editingRemote) { return; }
      try {
        const text = model!.getValue();
        if (workspace) { workspace.edit(cell.id, text); } else { cell.text = text; cell.title = cellTitle(cell.kind, text); persistScratch(); }
        updateCellCardMeta(cell.id); renderList();
        if (previewCells.has(cell.id)) { renderCellPreview(cell.id); }
      } catch (error) { report(error); }
    });
  }
  return model;
}
function updateCellCardMeta(id: string): void {
  const cell = cells.find(item => item.id === id);
  const card = document.querySelector<HTMLElement>(`[data-nb-cell="${CSS.escape(id)}"]`);
  if (!cell || !card) { return; }
  const idx = cells.indexOf(cell);
  const idxEl = card.querySelector('.nb-cell-index');
  if (idxEl) { idxEl.textContent = `[${String(idx + 1).padStart(2, '0')}]`; }
  const titleEl = card.querySelector('.nb-cell-title');
  if (titleEl) { titleEl.textContent = cell.title; }
  const permBtn = card.querySelector<HTMLButtonElement>(`[data-cell-perm="${CSS.escape(id)}"]`);
  if (permBtn) {
    permBtn.textContent = `${cell.locked ? '🔒 ' : ''}${permissionSummary(cell)}`;
    permBtn.classList.toggle('locked', cell.locked);
  }
  const ed = cellEditors.get(id);
  if (ed) {
    ed.updateOptions({ readOnly: workspace ? !cell.canWrite : cell.locked });
  }
  const runBtn = card.querySelector<HTMLButtonElement>(`[data-cell-run="${CSS.escape(id)}"]`);
  if (runBtn && workspace) {
    runBtn.disabled = !cell.canExecute;
    runBtn.title = cell.canExecute ? '실행/검증 (Shift+Enter)' : '이 셀 그룹에 실행 권한이 없습니다.';
  }
  const delBtn = card.querySelector<HTMLButtonElement>(`[data-cell-delete="${CSS.escape(id)}"]`);
  if (delBtn) { delBtn.disabled = workspace ? !cell.canWrite : false; }
  const upBtn = card.querySelector<HTMLButtonElement>(`[data-cell-move-up="${CSS.escape(id)}"]`);
  if (upBtn) { upBtn.disabled = idx <= 0 || (workspace ? !cell.canWrite : false); }
  const downBtn = card.querySelector<HTMLButtonElement>(`[data-cell-move-down="${CSS.escape(id)}"]`);
  if (downBtn) { downBtn.disabled = idx < 0 || idx >= cells.length - 1 || (workspace ? !cell.canWrite : false); }
}
function isExecutable(kind: CellKind): boolean {
  return kind === 'python' || kind === 'lean' || kind === 'rocq';
}
function renderCellOutput(id: string): void {
  const card = document.querySelector<HTMLElement>(`[data-nb-cell="${CSS.escape(id)}"]`);
  if (!card) { return; }
  const container = card.querySelector<HTMLElement>(`[data-nb-output="${CSS.escape(id)}"]`);
  if (!container) { return; }
  const out = cellOutputs.get(id);
  if (!out) {
    container.hidden = true;
    container.classList.remove('error');
    container.innerHTML = '';
    return;
  }
  container.hidden = false;
  container.classList.toggle('error', !out.ok && !out.running);
  if (out.running) {
    const streamPre = out.stdout ? `<pre class="nb-output-pre">${escape(out.stdout)}</pre>` : '';
    const envNote = out.runtime === 'agora-vm' ? '<span>Agora VM · Broker/C++ 스트리밍…</span>' : '';
    container.innerHTML = `<div class="nb-output-head"><span class="nb-output-badge">⟳ 실행 중…</span>${envNote}</div>${streamPre}`;
    return;
  }
  const badge = out.status === 'verified' ? '✓ Proof Verified' : out.ok ? '✓ Executed' : out.status === 'timeout' ? '⏱ Timeout' : `✕ Error (exit ${out.exitCode ?? '?'})`;
  const envLabel = out.runtime === 'agora-vm' ? 'Agora VM · ' : out.runtime === 'host' ? 'Host Runner · ' : '';
  const combined = [out.stdout, out.stderr].filter(part => part && part.trim().length > 0).join('\n').trim() || '(출력 없음)';
  container.innerHTML = `<div class="nb-output-head"><span class="nb-output-badge ${out.ok ? 'ok' : 'err'}">${badge}</span><span>${envLabel}${out.durationMs}ms · <button type="button" class="text-button" data-clear-output="${escape(id)}">지우기</button></span></div><pre class="nb-output-pre">${escape(combined)}</pre>`;
  container.querySelector(`[data-clear-output="${CSS.escape(id)}"]`)?.addEventListener('click', e => {
    e.stopPropagation();
    cellOutputs.delete(id);
    renderCellOutput(id);
    renderInspector();
  });
}
async function executeOnHost(kind: CellKind, code: string): Promise<ExecutionOutput> {
  const endpoint = location.pathname.startsWith('/irade') ? '/irade/api/execute' : '/api/execute';
  const response = await fetch(origin + endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, code }),
    signal: AbortSignal.timeout(25000),
  });
  const contentType = response.headers.get('content-type') || '';
  if (!response.ok || !contentType.includes('application/json')) {
    throw new Error(`로컬 실행 엔드포인트를 사용할 수 없습니다 (${response.status}). Workspace에 연결해 Agora VM으로 실행하세요.`);
  }
  const result = await response.json() as ExecutionOutput;
  return { ...result, runtime: 'host' };
}
async function executeInAgoraVm(id: string, kind: 'python' | 'lean' | 'rocq', code: string): Promise<ExecutionOutput> {
  const active = workspace!;
  if (active.pending) { await active.flush(); }
  const started = Date.now();
  const marker = `__IRADE_${crypto.randomUUID().replace(/-/g, '')}__`;
  let language: 'python' | 'bash' = 'python';
  let payloadCode = code;
  if (kind === 'lean') {
    language = 'bash';
    payloadCode = `LEAN_BIN=$(command -v lean || ([ -x "$HOME/.elan/bin/lean" ] && echo "$HOME/.elan/bin/lean") || ([ -x "/root/.elan/bin/lean" ] && echo "/root/.elan/bin/lean") || true)\n[ -n "$LEAN_BIN" ] || exit 127\n"$LEAN_BIN" --stdin << '${marker}'\n${code}\n${marker}\n`;
  } else if (kind === 'rocq') {
    language = 'bash';
    payloadCode = `COQC_BIN=$(command -v coqc || true)\n[ -n "$COQC_BIN" ] || exit 127\nTMP_DIR=$(mktemp -d)\ntrap 'rm -rf "$TMP_DIR"' EXIT\ncat << '${marker}' > "$TMP_DIR/Cell.v"\n${code}\n${marker}\n"$COQC_BIN" "$TMP_DIR/Cell.v"\n`;
  }
  let streamed = '';
  let data;
  try {
    data = await active.client.executeRuntime(language, payloadCode, chunk => {
      streamed += chunk;
      cellOutputs.set(id, {
        running: true,
        ok: true,
        exitCode: null,
        stdout: streamed,
        stderr: '',
        durationMs: Date.now() - started,
        status: 'running',
        runtime: 'agora-vm',
      });
      renderCellOutput(id);
    }, id);
  } catch (error) {
    const raw = error instanceof Error ? error.message : String(error);
    throw new Error(formatErrorLabel(raw));
  }
  if (data.error && typeof data.exitCode !== 'number') {
    throw new Error(data.message || formatErrorLabel(data.error));
  }
  const exitCode = typeof data.exitCode === 'number' ? data.exitCode : 1;
  if ((kind === 'lean' || kind === 'rocq') && exitCode === 127) {
    try {
      return await executeOnHost(kind, code);
    } catch {
      const binName = kind === 'lean' ? 'Lean 4 (lean)' : 'Rocq (coqc)';
      return {
        ok: false,
        exitCode: 127,
        stdout: '',
        stderr: `Agora VM(${binName})에 해당 증명 도구가 설치되어 있지 않습니다 (exit 127). VM 이미지에 ${binName} 설치가 필요합니다.`,
        durationMs: Date.now() - started,
        status: 'error',
        runtime: 'agora-vm',
      };
    }
  }
  const durationMs = Date.now() - started;
  const outputText = String(data.output ?? streamed ?? '');
  const ok = !data.timedOut && !data.error && exitCode === 0;
  const isProof = kind === 'lean' || kind === 'rocq';
  return {
    ok,
    exitCode,
    stdout: ok ? (outputText || (isProof ? '✓ Proof verified in Agora VM (0 errors)' : '')) : '',
    stderr: ok ? '' : (outputText || data.message || (data.error ? formatErrorLabel(data.error) : '실행 중 오류가 발생했습니다.')),
    durationMs,
    status: data.timedOut ? 'timeout' : ok ? (isProof ? 'verified' : 'success') : 'error',
    runtime: 'agora-vm',
  };
}
async function runCell(id: string): Promise<void> {
  const cell = cells.find(item => item.id === id);
  if (!cell || !isExecutable(cell.kind)) { return; }
  if (workspace && !cell.canExecute) {
    notify(errorLabels.RUNTIME_EXECUTION_DENIED, true);
    return;
  }
  const code = models.get(id)?.getValue() ?? cell.text;
  const runBtn = document.querySelector<HTMLButtonElement>(`[data-cell-run="${CSS.escape(id)}"]`);
  if (runBtn) { runBtn.disabled = true; }
  cellOutputs.set(id, { running: true, ok: true, exitCode: null, stdout: '', stderr: '', durationMs: 0, status: 'running', runtime: workspace ? 'agora-vm' : 'host' });
  renderCellOutput(id);
  try {
    const result = workspace
      ? await executeInAgoraVm(id, cell.kind as 'python' | 'lean' | 'rocq', code)
      : await executeOnHost(cell.kind, code);
    cellOutputs.set(id, result);
    log(`${cellTypes[cell.kind].short} 셀을 ${result.runtime === 'agora-vm' ? 'Agora VM(Broker·C++ WS)에서 ' : ''}${result.ok ? '실행/검증' : '실행(오류)'}했습니다 (${result.durationMs}ms).`);
  } catch (error) {
    cellOutputs.set(id, {
      ok: false,
      exitCode: 1,
      stdout: '',
      stderr: error instanceof Error ? error.message : String(error),
      durationMs: 0,
      status: 'error',
    });
  } finally {
    if (runBtn) { runBtn.disabled = false; }
    renderCellOutput(id);
    renderInspector();
  }
}
async function runAllCells(): Promise<void> {
  const targets = visibleCells().filter(cell => isExecutable(cell.kind));
  if (!targets.length) { notify('실행할 수 있는 Python, Lean 4 또는 Rocq 셀이 없습니다.'); return; }
  for (const cell of targets) {
    await runCell(cell.id);
  }
  notify(`${targets.length}개 셀 실행을 완료했습니다.`);
}
function renderCellPreview(id: string): void {
  const cell = cells.find(item => item.id === id);
  const frame = document.querySelector<HTMLIFrameElement>(`[data-nb-preview="${CSS.escape(id)}"]`);
  if (!cell || !frame) { return; }
  const visible = previewCells.has(id) && ['markdown','html','agent'].includes(cell.kind);
  frame.hidden = !visible;
  if (!visible) { return; }
  const light = currentTheme() === 'light';
  if (cell.kind === 'html') {
    frame.setAttribute('sandbox', 'allow-scripts');
    const raw = models.get(id)?.getValue() ?? cell.text;
    const isFullDoc = /<!doctype|<html[\s>]/i.test(raw);
    const baseStyle = light
      ? '<style>html,body{margin:0;padding:16px;font-family:system-ui,-apple-system,sans-serif;color:#18181b;background:#ffffff;box-sizing:border-box}*,*::before,*::after{box-sizing:border-box}</style>'
      : '<style>html,body{margin:0;padding:16px;font-family:system-ui,-apple-system,sans-serif;color:#f4f4f5;background:#1b1b1f;box-sizing:border-box}*,*::before,*::after{box-sizing:border-box}</style>';
    frame.srcdoc = isFullDoc ? raw : `<!doctype html><html lang="ko"><head><meta charset="UTF-8"><meta name="color-scheme" content="${light ? 'light' : 'dark'}">${baseStyle}</head><body>${raw}</body></html>`;
    return;
  }
  frame.setAttribute('sandbox', '');
  const source = marked.parse(models.get(id)?.getValue() ?? cell.text, { async: false });
  const safe = DOMPurify.sanitize(source, { FORBID_TAGS: ['script','style','form','iframe','object','embed','base','link','meta'], FORBID_ATTR: ['style','srcset'] });
  const css = light
    ? 'body{font:13.5px/1.8 system-ui,sans-serif;color:#27272a;background:#ffffff;padding:18px 22px;margin:0;overflow-wrap:anywhere}h1,h2,h3{font-weight:600;line-height:1.4;color:#18181b}h1{font-size:20px}h2{font-size:17px;border-bottom:1px solid #e7e5e4;padding-bottom:6px;margin-top:20px}a{color:#059669}code{background:#f3f2ee;padding:2px 6px;border-radius:4px}pre{padding:12px;background:#faf9f6;border:1px solid #e7e5e4;border-radius:8px;overflow:auto}pre code{padding:0;background:none}blockquote{border-left:2px solid #059669;padding-left:14px;color:#78716c}table{border-collapse:collapse;width:100%}th,td{border:1px solid #e7e5e4;padding:6px 10px}img{max-width:100%}'
    : 'body{font:13.5px/1.8 system-ui,sans-serif;color:#d4d4d8;background:#1b1b1f;padding:18px 22px;margin:0;overflow-wrap:anywhere}h1,h2,h3{font-weight:600;line-height:1.4;color:#f4f4f5}h1{font-size:20px}h2{font-size:17px;border-bottom:1px solid #27272a;padding-bottom:6px;margin-top:20px}a{color:#34d399}code{background:#27272a;padding:2px 6px;border-radius:4px}pre{padding:12px;background:#141417;border:1px solid #27272a;border-radius:8px;overflow:auto}pre code{padding:0;background:none}blockquote{border-left:2px solid #34d399;padding-left:14px;color:#a1a1aa}table{border-collapse:collapse;width:100%}th,td{border:1px solid #3f3f46;padding:6px 10px}img{max-width:100%}';
  frame.srcdoc = `<!doctype html><html lang="ko"><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"><meta name="color-scheme" content="${light ? 'light' : 'dark'}"><style>${css}</style></head><body>${safe}</body></html>`;
}
function openCellPermissionModal(id: string): void {
  const cell = cells.find(item => item.id === id);
  if (!cell) { return; }
  const isAdmin = workspace?.identity.isAdmin === true;
  const canUnlock = workspace ? workspace.canUnlock(id) : true;
  const groups = workspace ? workspace.availableGroups : ['local'];
  const current = new Set(cell.permission);
  const canEdit = workspace ? cell.canWrite : !cell.locked;
  const canExec = workspace ? cell.canExecute : true;
  const groupHtml = groups.length
    ? groups.map(group => `<label class="perm-group-item"><input type="checkbox" name="perm-group" value="${escape(group)}" ${current.has(group) ? 'checked' : ''} ${isAdmin ? '' : 'disabled'}><span>${escape(group)}</span></label>`).join('')
    : '<p class="empty-search">할당된 Canvas 그룹이 없습니다.</p>';
  const lockNotice = !isAdmin && workspace
    ? '<p class="modal-copy">그룹 접근 권한 변경은 Workspace 관리자만 가능합니다. 쓰기 권한자는 잠금 상태를 전환할 수 있습니다.</p>'
    : '<p class="modal-copy">Agora Canvas 객체 단위로 읽기·쓰기·실행 그룹과 잠금 상태를 제어합니다.</p>';
  showModal(`셀 객체 권한 · ${cell.title}`, `${lockNotice}
    <div class="perm-id-row">
      <input id="perm-item-id" readonly value="${escape(cell.id)}" aria-label="Canvas Workspace Item ID">
      <button type="button" class="button ghost" id="perm-copy-id">ID 복사</button>
    </div>
    <div class="perm-caps">
      <span class="perm-cap allowed">읽기 ✓</span>
      <span class="perm-cap ${canEdit ? 'allowed' : ''}">쓰기 ${canEdit ? '✓' : '✕'}</span>
      ${isExecutable(cell.kind) ? `<span class="perm-cap ${canExec ? 'allowed' : ''}">실행 ${canExec ? '✓' : '✕'}</span>` : ''}
      ${isAdmin ? '<span class="perm-cap allowed">Admin</span>' : ''}
    </div>
    <form id="perm-form">
      <label class="perm-group-item"><input type="checkbox" id="perm-locked" ${cell.locked ? 'checked' : ''} ${canUnlock ? '' : 'disabled'}><span>셀 잠금 (잠금 시 쓰기·이동·삭제 제한)</span></label>
      <div class="field"><span>허용 그룹 ACL</span><div class="perm-group-list">${groupHtml}</div></div>
      <div class="form-actions">
        <button type="button" class="button ghost" id="perm-cancel">닫기</button>
        ${(isAdmin || canUnlock) ? '<button type="submit" class="button primary">권한 저장</button>' : ''}
      </div>
    </form>`);
  $('#perm-copy-id').addEventListener('click', () => {
    void navigator.clipboard.writeText(cell.id).then(() => notify('Canvas 객체 ID를 복사했습니다.')).catch(() => {});
  });
  $('#perm-cancel').addEventListener('click', () => modal.close());
  $('#perm-form').addEventListener('submit', event => {
    event.preventDefault();
    void (async () => {
      const nextLocked = $<HTMLInputElement>('#perm-locked').checked;
      const checkedGroups = Array.from(document.querySelectorAll<HTMLInputElement>('input[name="perm-group"]:checked'), el => el.value);
      if (workspace) {
        if (isAdmin && checkedGroups.length && checkedGroups.join(',') !== cell.permission.join(',')) {
          await workspace.updatePermission(cell.id, checkedGroups);
        } else if (isAdmin && !checkedGroups.length && groups.length) {
          throw new Error('최소 1개 이상의 그룹을 선택해야 합니다.');
        }
        if (canUnlock && nextLocked !== cell.locked) {
          await workspace.toggleLock(cell.id, nextLocked);
        }
        cells = workspace.cells;
      } else {
        cell.locked = nextLocked;
        cell.canWrite = !nextLocked;
        persistScratch();
      }
      updateCellCardMeta(cell.id);
      renderList();
      renderInspector();
      modal.close();
      notify('셀 객체 권한 설정을 반영했습니다.');
    })().catch(report);
  });
}

function renderNotebook(): void {
  if (viewMode !== 'notebook') { return; }
  $('#notebook-title').textContent = workspaceName;
  $('#notebook-kind').textContent = demo ? 'LOCAL PLAYGROUND NOTEBOOK' : connected ? 'AGORA WORKSPACE NOTEBOOK' : 'RESEARCH NOTEBOOK';
  const container = $('#notebook-cells');
  const visible = visibleCells();
  const visibleIds = new Set(visible.map(c => c.id));
  for (const [id, ed] of cellEditors.entries()) {
    if (!visibleIds.has(id)) {
      ed.setModel(null); ed.dispose(); cellEditors.delete(id);
    }
  }
  if (!visible.length) {
    container.innerHTML = `<div class="nb-empty"><h2>${workspace || demo ? '아직 셀이 없습니다' : '연구 공간을 선택하세요'}</h2><p>${workspace || demo ? '아래 셀 추가 버튼으로 증명, 코드, 문서, 에이전트 셀을 만들어 보세요.' : '상단 또는 사이드바에서 Workspace를 열거나 로컬 체험을 시작하세요.'}</p></div>`;
    return;
  }
  const existing = new Map<string, HTMLElement>();
  container.querySelectorAll<HTMLElement>('[data-nb-cell]').forEach(el => {
    const id = el.dataset.nbCell!;
    if (visibleIds.has(id)) { existing.set(id, el); } else { el.remove(); }
  });
  const emptyEl = container.querySelector('.nb-empty');
  if (emptyEl) { emptyEl.remove(); }

  const fontSize = Number(localStorage.getItem('irade.fontSize') || 14);
  const lineNumbers = localStorage.getItem('irade.lineNumbers') === 'off' ? 'off' : 'on';

  visible.forEach((cell, visibleIdx) => {
    const type = cellTypes[cell.kind];
    const index = cells.indexOf(cell);
    const indexLabel = `[${String(index + 1).padStart(2, '0')}]`;
    let card = existing.get(cell.id);
    if (!card) {
      card = document.createElement('article');
      card.className = 'nb-cell';
      card.dataset.nbCell = cell.id;
      card.id = `nb-cell-${cell.id}`;
      const canPreview = ['markdown', 'html', 'agent'].includes(cell.kind);
      const canRun = isExecutable(cell.kind);
      if (cell.kind === 'html' && !previewCells.has(cell.id)) { previewCells.add(cell.id); }
      const previewLabel = previewCells.has(cell.id) ? '미리보기 닫기' : '미리보기';
      const runLabel = cell.kind === 'python' ? '▶ 실행' : '▶ 검증';
      const canWrite = workspace ? cell.canWrite : !cell.locked;
      const canExecute = workspace ? cell.canExecute : true;
      card.innerHTML = `<header class="nb-cell-header"><span class="nb-cell-index">${indexLabel}</span><span class="nb-cell-kind"><span class="${type.color}">${type.icon}</span>${type.short}</span><button type="button" class="nb-cell-id-chip" data-cell-copy-id="${escape(cell.id)}" title="Canvas Workspace Item ID: ${escape(cell.id)} (클릭해 복사)">#${escape(shortItemId(cell.id))}</button><span class="nb-cell-title">${escape(cell.title)}</span><span class="nb-cell-presence-group" data-nb-presence="${escape(cell.id)}"></span><button type="button" class="nb-cell-perm-btn ${cell.locked ? 'locked' : ''}" data-cell-perm="${escape(cell.id)}" title="Canvas 객체 권한 및 잠금 설정">${cell.locked ? '🔒 ' : ''}${escape(permissionSummary(cell))}</button><div class="nb-cell-tools"><button class="icon-button" data-cell-move-up="${escape(cell.id)}" title="위로 이동" aria-label="위로 이동" ${index <= 0 || !canWrite ? 'disabled' : ''}>↑</button><button class="icon-button" data-cell-move-down="${escape(cell.id)}" title="아래로 이동" aria-label="아래로 이동" ${index >= cells.length - 1 || !canWrite ? 'disabled' : ''}>↓</button>${canRun ? `<button class="nb-run-btn" data-cell-run="${escape(cell.id)}" ${canExecute ? '' : 'disabled'} title="${canExecute ? '실행/검증 (Shift+Enter)' : '이 셀 그룹에 실행 권한이 없습니다.'}">${runLabel}</button>` : ''}${cell.kind === 'agent' ? `<button class="button tiny" data-cell-msg="${escape(cell.id)}">＋ 메시지</button>` : ''}${canPreview ? `<button class="button tiny" data-cell-preview="${escape(cell.id)}">${previewLabel}</button>` : ''}<button class="icon-button" data-cell-export="${escape(cell.id)}" title="셀 내보내기" aria-label="셀 내보내기">↧</button><button class="icon-button danger" data-cell-delete="${escape(cell.id)}" title="셀 삭제" aria-label="셀 삭제" ${canWrite ? '' : 'disabled'}>×</button></div></header><div class="nb-cell-body"><div class="nb-editor-host"></div><iframe class="nb-preview-frame" data-nb-preview="${escape(cell.id)}" title="셀 미리보기" sandbox="${cell.kind === 'html' ? 'allow-scripts' : ''}" referrerpolicy="no-referrer" hidden></iframe><div class="nb-cell-output" data-nb-output="${escape(cell.id)}" hidden></div></div><footer class="nb-cell-footer"><span>${type.label}${canRun ? ' · Shift+Enter 실행' : ''} · ID ${escape(shortItemId(cell.id))}</span><span data-nb-cursor="${escape(cell.id)}">Ln 1, Col 1</span></footer>`;
      container.insertBefore(card, container.children[visibleIdx] || null);

      const host = card.querySelector<HTMLElement>('.nb-editor-host')!;
      const model = ensureModel(cell);
      const ed = monaco.editor.create(host, {
        theme: currentTheme() === 'light' ? 'irade-light' : 'irade-dark',
        automaticLayout: true,
        model,
        fontSize,
        lineNumbers,
        fontFamily: "'JetBrains Mono', 'SFMono-Regular', Consolas, 'Liberation Mono', monospace",
        lineHeight: 22,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
        scrollbar: { vertical: 'hidden', handleMouseWheel: false },
        padding: { top: 12, bottom: 12 },
        smoothScrolling: true,
        renderLineHighlight: 'gutter',
        lineNumbersMinChars: 3,
        wordWrap: 'on',
        tabSize: 4,
        readOnly: !canWrite,
        fixedOverflowWidgets: true,
      });
      if (canRun) {
        ed.addCommand(monaco.KeyMod.Shift | monaco.KeyCode.Enter, () => { void runCell(cell.id); });
      }
      const updateHeight = (): void => {
        const h = Math.min(520, Math.max(96, ed.getContentHeight()));
        host.style.height = `${h}px`;
        ed.layout();
      };
      ed.onDidContentSizeChange(updateHeight);
      updateHeight();
      ed.onDidFocusEditorText(() => {
        setPresenceFocus(cell.id);
        if (selected !== cell.id) {
          selected = cell.id;
          syncActiveCellCard();
          renderList();
          renderInspector();
        }
      });
      ed.onDidBlurEditorText(() => {
        if (activePresenceCellId === cell.id) {
          setPresenceFocus(undefined);
        }
      });
      ed.onDidChangeCursorPosition(event => {
        const cursorEl = card!.querySelector(`[data-nb-cursor="${CSS.escape(cell.id)}"]`);
        if (cursorEl) { cursorEl.textContent = `Ln ${event.position.lineNumber}, Col ${event.position.column}`; }
      });
      cellEditors.set(cell.id, ed);

      card.addEventListener('click', () => {
        if (selected !== cell.id) {
          selected = cell.id;
          syncActiveCellCard();
          renderList();
          renderInspector();
        }
      });
      card.querySelector(`[data-cell-copy-id="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation();
        void navigator.clipboard.writeText(cell.id).then(() => notify(`객체 ID 복사됨: ${cell.id}`)).catch(() => {});
      });
      card.querySelector(`[data-cell-perm="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation();
        selected = cell.id;
        openCellPermissionModal(cell.id);
      });
      card.querySelector(`[data-cell-move-up="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation();
        void moveCellOrder(cell.id, -1).catch(report);
      });
      card.querySelector(`[data-cell-move-down="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation();
        void moveCellOrder(cell.id, 1).catch(report);
      });
      card.querySelector(`[data-cell-run="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation(); selected = cell.id; void runCell(cell.id);
      });
      card.querySelector(`[data-cell-preview="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation();
        if (previewCells.has(cell.id)) { previewCells.delete(cell.id); } else { previewCells.add(cell.id); }
        const btn = e.currentTarget as HTMLButtonElement;
        btn.textContent = previewCells.has(cell.id) ? '미리보기 닫기' : '미리보기';
        renderCellPreview(cell.id);
      });
      card.querySelector(`[data-cell-msg="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation(); selected = cell.id; message();
      });
      card.querySelector(`[data-cell-export="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation(); selected = cell.id; exportCell();
      });
      card.querySelector(`[data-cell-delete="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation(); selected = cell.id; void deleteCell().catch(report);
      });
      if (previewCells.has(cell.id)) { renderCellPreview(cell.id); }
      renderCellOutput(cell.id);
    } else {
      if (container.children[visibleIdx] !== card) {
        container.insertBefore(card, container.children[visibleIdx] || null);
      }
      updateCellCardMeta(cell.id);
    }
  });
  syncActiveCellCard();
  syncPresenceUI();
}
async function moveCellOrder(id: string, direction: -1 | 1): Promise<void> {
  if (workspace) {
    await workspace.moveCell(id, direction);
    cells = workspace.cells;
    renderNotebook();
    renderList();
  } else {
    moveLocalCell(id, direction);
  }
}
function moveLocalCell(id: string, direction: -1 | 1): void {
  const idx = cells.findIndex(item => item.id === id);
  const next = idx + direction;
  if (idx < 0 || next < 0 || next >= cells.length) { return; }
  const tmp = cells[idx];
  cells[idx] = cells[next];
  cells[next] = tmp;
  cells.forEach((cell, i) => { cell.frameOrder = i * 10; });
  persistScratch();
  renderNotebook();
  renderList();
}
function syncActiveCellCard(): void {
  document.querySelectorAll<HTMLElement>('[data-nb-cell]').forEach(el => {
    el.classList.toggle('active', el.dataset.nbCell === selected);
  });
}
function openNotebook(): void {
  viewMode = 'notebook';
  $('#overview').hidden = true;
  $('#cell-view').hidden = false;
  renderNotebook();
  renderList();
  renderInspector();
}
function selectCell(id: string, scrollBlock: ScrollLogicalPosition = 'center'): void {
  const cell = cells.find(cell => cell.id === id); if (!cell) { return; }
  selected = id;
  openNotebook();
  requestAnimationFrame(() => {
    const card = document.querySelector<HTMLElement>(`[data-nb-cell="${CSS.escape(id)}"]`);
    card?.scrollIntoView({ behavior: 'smooth', block: scrollBlock });
    cellEditors.get(id)?.focus();
  });
}
function overview(): void {
  overviewOpen = true;
  viewMode = 'overview';
  $('#overview').hidden = false;
  $('#cell-view').hidden = true;
  renderList();
  renderInspector();
}
function renderInspector(): void {
  document.querySelectorAll('.inspector-tabs>button').forEach(button => button.classList.toggle('selected', button.getAttribute('data-action') === inspectorTab));
  if (inspectorTab === 'activity') {
    $('#inspector-content').innerHTML = '<h3 class="context-heading">WORKSPACE ACTIVITY</h3>' + (activities.length ? activities.map(event => `<div class="activity-event">${escape(event.message)}<small>${event.time}</small></div>`).join('') : '<p class="context-copy">이 탭에서 연결하고 편집한 기록이 여기에 표시됩니다.</p>'); return;
  }
  const cell = cells.find(cell => cell.id === selected);
  const cellOut = cell ? cellOutputs.get(cell.id) : undefined;
  const proofStateHtml = cell && ['lean','rocq'].includes(cell.kind)
    ? cellOut?.running
      ? '<div class="context-block"><h3 class="context-heading">PROOF STATE</h3><p class="context-copy">증명을 검증하고 있습니다…</p><span class="context-tag">VERIFYING</span></div>'
      : cellOut?.status === 'verified'
        ? `<div class="context-block"><h3 class="context-heading">PROOF STATE</h3><p class="context-copy">증명 검증이 완료되었습니다 (${cellOut.durationMs}ms).</p><span class="context-tag">VERIFIED</span></div>`
        : cellOut && !cellOut.ok
          ? '<div class="context-block"><h3 class="context-heading">PROOF STATE</h3><p class="context-copy">증명 오류가 발생했습니다. 셀 출력 영역을 확인하세요.</p><span class="context-tag">ERROR</span></div>'
          : '<div class="context-block"><h3 class="context-heading">PROOF STATE</h3><p class="context-copy">▶ 검증 버튼 또는 Shift+Enter로<br>증명을 실행해 검증할 수 있습니다.</p><span class="context-tag">NOT VERIFIED</span></div>'
    : '';
  const cellAclHtml = cell
    ? `<div class="context-block"><h3 class="context-heading">WORKSPACE ITEM & ACL</h3><div class="context-stat"><span>객체 ID</span><b>${escape(shortItemId(cell.id))}</b></div><div class="context-stat"><span>접근 그룹</span><b>${escape(permissionSummary(cell))}</b></div><div class="context-stat"><span>쓰기 / 잠금</span><b>${cell.locked ? '잠금됨' : cell.canWrite ? '편집 가능' : '읽기 전용'}</b></div>${isExecutable(cell.kind) ? `<div class="context-stat"><span>실행 권한</span><b>${cell.canExecute ? '허용' : '제한'}</b></div>` : ''}<button type="button" class="button ghost" id="inspector-perm-btn" style="margin-top:8px;width:100%">객체 권한 설정…</button></div>`
    : '';
  const peersHtml = workspace && connected
    ? `<div class="context-block"><h3 class="context-heading">CONNECTED PEERS (${workspace.peers.length})</h3><p class="context-copy">${workspace.peers.length ? workspace.peers.map(p => escape(p.nickname || `Peer ${p.peer_id.slice(0, 4)}`)).join(', ') : '현재 다른 접속자가 없습니다.'}</p></div>`
    : '';
  const typeCounts = { agent: cells.filter(cell => cell.kind === 'agent').length, proof: cells.filter(cell => ['lean','rocq'].includes(cell.kind)).length, code: cells.filter(cell => cell.kind === 'python').length, document: cells.filter(cell => ['markdown','html'].includes(cell.kind)).length };
  $('#inspector-content').innerHTML = `<h3 class="context-heading">${cell ? 'CELL CONTEXT' : 'WORKSPACE CONTEXT'}</h3><h2 class="context-title">${escape(cell ? cellTypes[cell.kind].label : '연구의 흐름을\n하나로 연결하세요.').replace('\n','<br>')}</h2><p class="context-copy">${cell ? escape(cellTypes[cell.kind].description) : '아이디어를 탐색하고, 가설을 증명하고,<br>결과를 코드와 문서로 남기는 공간.'}</p>${proofStateHtml}${cellAclHtml}<div class="context-flow"><div class="flow-step"><span class="step-icon mint">✧</span><span>탐색하고 대화하기<small>AGENT CONVERSATIONS</small></span></div><div class="flow-step"><span class="step-icon purple">∀</span><span>논리로 증명하기<small>LEAN 4 / ROCQ PROOFS</small></span></div><div class="flow-step"><span class="step-icon blue">λ</span><span>코드로 실험하기<small>PYTHON CELLS</small></span></div><div class="flow-step"><span class="step-icon sand">≡</span><span>지식으로 정리하기<small>MARKDOWN / HTML</small></span></div></div><div class="context-block"><h3 class="context-heading">${cell ? 'THIS WORKSPACE' : 'WORKSPACE OVERVIEW'}</h3><div class="context-stat"><span>대화 기록</span><b>${String(typeCounts.agent).padStart(2,'0')}</b></div><div class="context-stat"><span>형식 증명</span><b>${String(typeCounts.proof).padStart(2,'0')}</b></div><div class="context-stat"><span>Python 코드</span><b>${String(typeCounts.code).padStart(2,'0')}</b></div><div class="context-stat"><span>연구 문서</span><b>${String(typeCounts.document).padStart(2,'0')}</b></div></div>${peersHtml}<div class="context-block"><h3 class="context-heading">COLLABORATION</h3><p class="context-copy">${demo ? '로컬 체험 모드입니다.<br>내용은 현재 브라우저 탭에만 저장됩니다.' : connected ? 'Agora Workspace 객체 ID·권한 ACL로<br>실시간 델타·CRDT 동기화 중입니다.' : 'Agora workspace를 연결하면<br>함께 셀을 편집할 수 있습니다.'}</p><span class="context-tag">${demo ? 'LOCAL SESSION' : connected ? 'WORKSPACE SYNC' : 'AGORA WORKSPACE'}</span></div>`;
  if (cell) {
    document.querySelector('#inspector-perm-btn')?.addEventListener('click', () => openCellPermissionModal(cell.id));
  }
}
function persistScratch(): void { sessionStorage.setItem(demoKey, JSON.stringify(cells)); $('#cell-save').textContent = '현재 탭에 저장됨'; }
async function startDemo(): Promise<void> {
  if (workspace?.pending) { await workspace.flush(); }
  ++loadGeneration; setPresenceFocus(undefined); workspace?.dispose(); workspace = undefined; connected = false; clearModels(); demo = true; workspaceName = 'Research playground'; selected = undefined;
  const cached = sessionStorage.getItem(demoKey);
  const parsed = cached ? JSON.parse(cached) as Partial<Cell>[] : undefined;
  cells = parsed
    ? parsed.map((item, index) => ({
      id: item.id || `${Date.now() + index}-${crypto.randomUUID().slice(0, 8)}`,
      kind: (item.kind || 'markdown') as CellKind,
      path: item.path || '',
      text: item.text || '',
      title: item.title || cellTitle((item.kind || 'markdown') as CellKind, item.text || ''),
      permission: Array.isArray(item.permission) ? item.permission : ['local'],
      frameOrder: typeof item.frameOrder === 'number' ? item.frameOrder : index * 10,
      locked: Boolean(item.locked),
      canWrite: item.locked ? false : true,
      canExecute: true,
    }))
    : (['markdown','agent','lean','rocq','python','html'] as CellKind[]).map((kind, index) => ({
      id: `demo-${Date.now() + index}-${crypto.randomUUID().slice(0, 8)}`,
      kind,
      path: '',
      text: starters[kind],
      title: cellTitle(kind, starters[kind]),
      permission: ['local'],
      frameOrder: index * 10,
      locked: false,
      canWrite: true,
      canExecute: true,
    }));
  cells.sort((a, b) => a.frameOrder - b.frameOrder || a.id.localeCompare(b.id));
  persistScratch(); const url = new URL(location.href); url.searchParams.delete('workspace'); history.replaceState(null,'',url);
  $('#workspace-subtitle').textContent = '로컬 체험 · 현재 탭에만 저장'; $('#workspace-mode').textContent = 'Local Playground'; $('#status').textContent = '로컬 체험 · 공유되지 않음'; $('#connection-label').textContent = 'LOCAL SESSION'; $('#sync-description').textContent = '체험 내용은 현재 탭에만 저장'; $('#permission-label').textContent = '로컬 편집';
  openNotebook(); log('로컬 체험 workspace를 열었습니다.'); notify('로컬 체험입니다. 공동 편집하려면 Agora workspace를 연결하세요.');
}
let addingCell = false;
async function addCell(kind?: CellKind): Promise<void> {
  if (!workspace && !demo) {
    showModal('연구 공간을 선택하세요', '<p class="modal-copy">Agora workspace에서 실시간으로 협업하거나 로컬 체험으로 셀을 살펴보세요.</p><div class="choice-list"><button class="choice" data-action="open"><span class="type-icon mint">↔</span><span><strong>공유 workspace 열기</strong><small>ORCID로 로그인하고 공동 편집</small></span><span class="choice-arrow">↗</span></button><button class="choice" data-action="demo"><span class="type-icon blue">λ</span><span><strong>로컬 체험 시작</strong><small>계정 없이 현재 탭에서 셀 편집 체험</small></span><span class="choice-arrow">→</span></button></div>'); return;
  }
  if (workspace && !workspace.client.canWrite) { notify('이 workspace는 읽기 전용입니다.', true); return; }
  if (!kind) {
    showModal('새로운 연구 셀', '<p class="modal-copy">다음 아이디어에 맞는 셀을 선택하세요.</p><div class="choice-list">' + (Object.keys(cellTypes) as CellKind[]).map(kind => `<button class="choice" data-add-kind="${kind}"><span class="type-icon ${cellTypes[kind].color}">${cellTypes[kind].icon}</span><span><strong>${cellTypes[kind].label}</strong><small>${cellTypes[kind].description}</small></span><span class="choice-arrow">＋</span></button>`).join('') + '</div>');
    document.querySelectorAll<HTMLButtonElement>('[data-add-kind]').forEach(button => button.addEventListener('click', () => { modal.close(); void addCell(button.dataset.addKind as CellKind).catch(report); })); return;
  }
  if (addingCell) { return; }
  let cell: Cell;
  if (workspace) {
    addingCell = true;
    const buttons = document.querySelectorAll<HTMLButtonElement>('[data-quick-add], [data-action="add"]');
    buttons.forEach(button => { button.disabled = true; });
    $('#cell-save').textContent = '새 셀 생성 중…';
    try {
      cell = await workspace.add(kind, starters[kind]);
      cells = workspace.cells;
    } finally {
      addingCell = false;
      buttons.forEach(button => { button.disabled = false; });
      if (!workspace.pending) { $('#cell-save').textContent = connected ? 'Workspace 동기화됨' : '로컬 변경 유지'; }
    }
  } else {
    const nextOrder = cells.reduce((max, item) => Math.max(max, item.frameOrder), -10) + 10;
    cell = { id: `demo-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`, kind, path: '', text: starters[kind], title: cellTitle(kind, starters[kind]), permission: ['local'], frameOrder: nextOrder, locked: false, canWrite: true, canExecute: true };
    cells.push(cell);
    persistScratch();
  }
  if (filter !== 'all' && !visibleCells().some(item => item.id === cell.id)) {
    filter = 'all';
    document.querySelectorAll('.rail-button').forEach(button => button.classList.toggle('active', button.getAttribute('data-action') === 'workspace'));
  }
  log(`${cellTypes[kind].label} 셀을 만들었습니다.`); selectCell(cell.id, 'end');
}
function search(): void {
  showModal('Workspace 빠른 이동', '<input class="search-input" id="cell-search" type="search" aria-label="셀 검색" placeholder="셀 제목, 종류, 내용 검색…"><div class="choice-list" id="search-results"></div>');
  const render = (): void => {
    const query = $<HTMLInputElement>('#cell-search').value.toLowerCase();
    $('#search-results').innerHTML = cells.filter(cell => `${cell.title} ${cellTypes[cell.kind].label} ${cellTypes[cell.kind].short} ${cell.text}`.toLowerCase().includes(query)).map(cell => `<button class="choice" data-search-cell="${cell.id}"><span class="type-icon ${cellTypes[cell.kind].color}">${cellTypes[cell.kind].icon}</span><span><strong>${escape(cell.title)}</strong><small>${cellTypes[cell.kind].label}</small></span></button>`).join('') || '<p class="empty-search">일치하는 셀이 없습니다. Workspace를 열거나 새 셀을 만들어 보세요.</p>';
    document.querySelectorAll<HTMLButtonElement>('[data-search-cell]').forEach(button => button.addEventListener('click', () => { selectCell(button.dataset.searchCell!); modal.close(); }));
  };
  render(); $('#cell-search').addEventListener('input', render); $('#cell-search').focus();
}
async function deleteCell(): Promise<void> {
  const cell = cells.find(cell => cell.id === selected); if (!cell) { return; }
  if (workspace && !cell.canWrite) { notify(errorLabels[cell.locked ? 'ITEM_LOCKED' : 'ITEM_WRITE_DENIED'], true); return; }
  showModal('셀 삭제', `<p class="modal-copy">“${escape(cell.title)}” 셀을 삭제할까요? 공유 workspace에서는 다른 참여자에게도 적용됩니다.</p><div class="form-actions"><button class="button ghost" id="cancel-delete">취소</button><button class="button primary" id="confirm-delete">셀 삭제</button></div>`);
  $('#cancel-delete').addEventListener('click', () => modal.close());
  $('#confirm-delete').addEventListener('click', event => { (event.currentTarget as HTMLButtonElement).disabled = true; void (async () => {
    if (activePresenceCellId === cell.id) { setPresenceFocus(undefined); }
    if (workspace) { deletingCellId = cell.id; try { await workspace.delete(cell.id); cells = workspace.cells; } finally { deletingCellId = undefined; } } else { cells = cells.filter(item => item.id !== cell.id); persistScratch(); }
    const ed = cellEditors.get(cell.id);
    if (ed) { ed.setModel(null); ed.dispose(); cellEditors.delete(cell.id); }
    previewCells.delete(cell.id);
    cellOutputs.delete(cell.id);
    models.get(cell.id)?.dispose(); models.delete(cell.id);
    if (selected === cell.id) { selected = cells[0]?.id; }
    renderList(); renderInspector();
    if (viewMode === 'notebook') { renderNotebook(); }
    modal.close(); log('셀을 삭제했습니다.');
  })().catch(error => { report(error); (event.target as HTMLButtonElement).disabled = false; }); });
}
function exportCell(): void {
  const cell = cells.find(cell => cell.id === selected); if (!cell) { return; }
  const value = models.get(cell.id)?.getValue() ?? cell.text;
  download(value, `${cell.title.replace(/[^\p{L}\p{N}_ -]/gu,'').slice(0,60) || 'cell'}.${cellTypes[cell.kind].extension}`);
}
function download(value: string, name: string): void {
  const url = URL.createObjectURL(new Blob([value], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function message(): void {
  const cell = cells.find(cell => cell.id === selected && cell.kind === 'agent'); if (!cell) { return; }
  showModal('대화 메시지 기록', '<p class="modal-copy">외부 에이전트와 나눈 대화나 자신의 질문을 기록합니다. AI 응답은 생성하지 않습니다.</p><form id="message-form"><label class="field">역할<select name="role"><option value="사용자">사용자</option><option value="에이전트">에이전트</option><option value="연구 메모">연구 메모</option></select></label><label class="field">메시지<textarea name="text" required placeholder="연구 맥락에 남길 내용을 입력하세요."></textarea></label><div class="form-actions"><button class="button primary">기록에 추가 ＋</button></div></form>');
  $('#message-form').addEventListener('submit', event => { event.preventDefault(); const data = new FormData(event.currentTarget as HTMLFormElement); const model = models.get(cell.id)!; const text = `${model.getValue().trimEnd()}\n\n## ${data.get('role')}\n${String(data.get('text')).trim()}\n`; model.pushEditOperations(null, [{ range: model.getFullModelRange(), text }], () => null); modal.close(); log('대화 기록에 메시지를 추가했습니다.'); });
}
function help(): void { showModal('연결된 연구를 시작하세요', '<div class="help-grid"><p><b>Workspace</b>는 공동 연구 공간입니다. 같은 workspace를 열면 같은 셀을 편집하고 변경을 실시간으로 공유합니다.</p><p><b>에이전트 대화</b>는 질문과 응답을 기록합니다.<br><b>Lean 4 / Rocq</b>는 형식 증명 소스를 다룹니다.<br><b>Python</b>은 실험 코드를 작성합니다.<br><b>Markdown / HTML</b>은 문서와 미리보기를 제공합니다.</p><p>현재 셀 편집과 동기화를 지원합니다. AI 응답과 코드·증명 실행 커널은 아직 연결되지 않았습니다. 기존 workspace 데이터는 보존됩니다.</p><p><kbd>Ctrl K</kbd> 검색 · <kbd>Alt N</kbd> 새 셀<br><kbd>Ctrl S</kbd> 저장 · <kbd>Ctrl Shift F</kbd> 집중 모드</p></div>'); }
function settings(): void {
  const savedLines = localStorage.getItem('irade.lineNumbers') === 'off' ? 'off' : 'on';
  showModal('연구 환경 설정', `<p class="modal-copy">설정은 이 브라우저에 적용됩니다.</p><label class="settings-row">편집기 글자 크기<select id="font-size">${[12,14,16,18,20].map(size => `<option value="${size}" ${Number(localStorage.getItem('irade.fontSize') || 14) === size ? 'selected' : ''}>${size}px</option>`).join('')}</select></label><label class="settings-row">편집기 줄 번호<select id="line-numbers"><option value="on" ${savedLines === 'on' ? 'selected' : ''}>표시</option><option value="off" ${savedLines === 'off' ? 'selected' : ''}>숨기기</option></select></label><div class="form-actions"><button class="button ghost" data-action="help">사용 안내</button><button class="button primary" id="save-settings">적용</button></div>`);
  $('#save-settings').addEventListener('click', () => { const size = Number($<HTMLSelectElement>('#font-size').value); const lines = $<HTMLSelectElement>('#line-numbers').value as 'on' | 'off'; localStorage.setItem('irade.fontSize', String(size)); localStorage.setItem('irade.lineNumbers', lines); for (const ed of cellEditors.values()) { ed.updateOptions({ fontSize: size, lineNumbers: lines }); } modal.close(); notify('편집기 설정을 적용했습니다.'); });
}
async function share(): Promise<void> {
  if (!workspace) { notify('공유하려면 Agora workspace를 먼저 연결하세요.'); return; }
  const url = new URL(location.pathname, origin); url.searchParams.set('workspace', String(workspace.client.config.canvasId));
  try { await navigator.clipboard.writeText(String(url)); notify('Workspace 링크를 복사했습니다. 참여자는 자신의 ORCID 로그인과 접근 권한이 필요합니다.'); }
  catch { showModal('Workspace 공유 링크', `<p class="modal-copy">참여자는 자신의 ORCID 로그인과 접근 권한이 필요합니다.</p><label class="field">공유 링크<input readonly value="${escape(String(url))}"></label>`); }
}
async function action(name: string): Promise<void> {
  if (name === 'open') { await openWorkspace(); }
  else if (name === 'login') {
    if (token) {
      const profile = await api<{ name: string; orcid: string; issuer: string }>('/api/irade/auth/me');
      showModal('연구자 계정', `<p class="modal-copy">${escape(profile.name)}</p><a class="orcid-profile" href="${escape(profile.issuer)}/${escape(profile.orcid)}" target="_blank" rel="noopener noreferrer"><img src="/irade-assets/orcid-id.svg" width="24" height="24" alt="ORCID iD"> ${escape(profile.issuer)}/${escape(profile.orcid)}</a><div class="form-actions"><button class="button ghost" data-action="logout">이 탭에서 로그아웃</button></div>`);
    } else { await signIn(); }
  }
  else if (name === 'logout') {
      modal.close();
      if (workspace?.pending) { await workspace.flush(); }
      ++loadGeneration; setPresenceFocus(undefined); workspace?.dispose(); workspace = undefined; token = undefined; sessionStorage.removeItem(tokenKey); updateAccount(); clearModels(); cells = []; selected = undefined; connected = false; workspaceName = '나의 연구 공간'; demo = false;
      const url = new URL(location.href); url.searchParams.delete('workspace'); history.replaceState(null,'',url); overview(); $('#workspace-subtitle').textContent = 'workspace를 연결하세요'; $('#status').textContent = '로그아웃됨'; $('#connection-label').textContent = '연결 대기'; $('#permission-label').textContent = ''; notify('로그아웃했습니다.');
  }
  else if (name === 'demo') { modal.close(); await startDemo(); }
  else if (name === 'run-all') { await runAllCells(); }
  else if (name === 'add') { await addCell(); }
  else if (name === 'overview') { overview(); }
  else if (name === 'notebook') { openNotebook(); }
  else if (name === 'close-overview') {
    overviewOpen = false;
    if (workspace || demo) { openNotebook(); }
    else { $('#overview').hidden = true; $('#cell-view').hidden = false; renderNotebook(); renderList(); }
  }
  else if (name === 'search') { search(); }
  else if (name === 'focus') { document.body.classList.toggle('focus'); }
  else if (name === 'theme') { applyTheme(currentTheme() === 'light' ? 'dark' : 'light'); }
  else if (name === 'inspector') { document.body.classList.toggle('inspector-hidden'); }
  else if (name === 'delete') { await deleteCell(); }
  else if (name === 'export') { exportCell(); }
  else if (name === 'message') { message(); }
  else if (name === 'help') { help(); }
  else if (name === 'settings') { settings(); }
  else if (name === 'share') { await share(); }
  else if (name === 'context' || name === 'activity') { inspectorTab = name; renderInspector(); }
  else if (name === 'workspace' || name.startsWith('filter-')) {
    filter = name === 'workspace' ? 'all' : name.slice(7);
    document.querySelectorAll('.rail-button').forEach(button => button.classList.toggle('active', button.getAttribute('data-action') === name));
    renderList();
    if (viewMode === 'notebook') { renderNotebook(); }
    if (window.innerWidth <= 760) { search(); }
  }
}
document.addEventListener('click', event => {
  const target = event.target as Element;
  const quickAdd = target.closest<HTMLButtonElement>('button[data-quick-add]');
  if (quickAdd) { void addCell(quickAdd.dataset.quickAdd as CellKind).catch(report); return; }
  const actionEl = target.closest<HTMLElement>('[data-action]');
  if (actionEl) {
    if (actionEl.dataset.action === 'close-overview') { event.stopPropagation(); }
    void action(actionEl.dataset.action!).catch(report);
  }
});
document.addEventListener('keydown', event => {
  if (modal.open) { return; }
  const mod = event.ctrlKey || event.metaKey;
  if (mod && event.key.toLowerCase() === 'k') { event.preventDefault(); search(); }
  else if (mod && event.key.toLowerCase() === 's') { event.preventDefault(); void (workspace ? workspace.flush().then(() => notify('모든 변경을 저장했습니다.')) : Promise.resolve(persistScratch())).catch(report); }
  else if (mod && event.shiftKey && event.key.toLowerCase() === 'f') { event.preventDefault(); document.body.classList.toggle('focus'); }
  else if (event.altKey && event.key.toLowerCase() === 'n') { event.preventDefault(); void addCell().catch(report); }
});
window.addEventListener('beforeunload', event => { if (workspace?.pending) { event.preventDefault(); event.returnValue = ''; } });
const groups: { kinds: CellKind[]; title: string; description: string; icon: string; color: string }[] = [
  { kinds: ['agent'], title: '에이전트 대화', description: '질문과 추론, 대화의 맥락을 보관하세요.', icon: '✧', color: 'mint' },
  { kinds: ['lean','rocq'], title: 'Lean 4 · Rocq 증명', description: '가설에서 정리까지, 논리를 연결하세요.', icon: '∀', color: 'purple' },
  { kinds: ['python'], title: 'Python 코드', description: '작은 실험으로 아이디어를 구체화하세요.', icon: 'λ', color: 'blue' },
  { kinds: ['markdown','html'], title: 'Markdown · HTML 문서', description: '관찰과 발견을 지식으로 남기세요.', icon: '≡', color: 'sand' },
];
$('#type-cards').innerHTML = groups.map((group,index) => `<button class="type-card" data-group="${index}"><span class="type-icon ${group.color}">${group.icon}</span><span><h3>${group.title}</h3><p>${group.description}</p></span><span class="card-arrow">↗</span><span class="card-number">0${index + 1}</span></button>`).join('');
document.querySelectorAll<HTMLButtonElement>('[data-group]').forEach(button => button.addEventListener('click', () => { const group = groups[Number(button.dataset.group)]; void addCell(group.kinds.length === 1 ? group.kinds[0] : undefined).catch(report); }));
updateAccount(); renderList(); renderInspector();
const query = new URL(location.href).searchParams;
let canvasId = Number(query.get('workspace'));
void (async () => {
  if (token) {
    try { await api('/api/irade/auth/me'); }
    catch { token = undefined; sessionStorage.removeItem(tokenKey); updateAccount(); }
  }
  if (canvasId) { if (token) { await connect(canvasId, `Workspace ${canvasId}`); } else { await signIn(); } }
  else if (token && query.has('workspaces')) { await openWorkspace(); }
})().catch(report);
