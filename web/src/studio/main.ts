import { SESSION, bearer, hasSession, endSession, migrateLegacyToken } from './authToken.ts';
// Inside the AGDD console's IDE tab: adopt the console palette and leave workspace/account chrome to the host.
const EMBED = new URL(location.href).searchParams.get('embed') === '1' || window.top !== window.self;
if (EMBED) { document.documentElement.classList.add('agdd-embed'); }
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api.js';
import 'monaco-editor/esm/vs/basic-languages/python/python.contribution.js';
import 'monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution.js';
import 'monaco-editor/esm/vs/basic-languages/html/html.contribution.js';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { AgoraClient, AgoraError } from '../client.ts';
import { AgentPanelController } from './agentPanel.ts';
import { DockArcadeController } from './dockGames.ts';
import { PerfMonitorController } from './perfMonitor.ts';
import {
  deleteVerifiedTheoremFromDb,
  extractClientProofRelations,
  fetchProofKnowledgeSnapshot,
  normalizeLeanCodeForExecution,
  renderProofDbInspectorHtml,
  renderProofDbModalHtml,
  syncVerifiedProofToDb,
  type ProofKnowledgeSnapshot,
} from './proofKnowledge.ts';
import {
  AGENT_AVATAR_SVG,
  CHAT_INITIAL_VISIBLE,
  CHAT_LOAD_STEP,
  appendAgentMessage,
  avatarMonogram,
  createStarterAgentDoc,
  formatChatBodyHtml,
  formatChatTime,
  parseAgentMessages,
  peerAvatarTone,
  resolveChatAlignment,
  type ChatViewerIdentity,
} from './agentChat.ts';
import { replacement } from '../document.ts';
import { ResearchWorkspace, cellTypes, cellTitle, type Cell, type CellKind } from './workspace.ts';

const $ = <T extends HTMLElement = HTMLElement>(selector: string): T => document.querySelector<T>(selector)!;
const escape = (value: string): string => value.replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]!);
const origin = location.origin;
const demoKey = 'irade.research.scratch';
let token: string | undefined = hasSession() ? SESSION : undefined;
let workspace: ResearchWorkspace | undefined;
let workspaceName = '나의 연구 공간';
let cells: Cell[] = [];
let selected: string | undefined;
let demo = false;
let connected = false;
let filter = 'all';
let overviewOpen = true;
let viewMode: 'overview' | 'notebook' | 'empty' = 'overview';
let deletingCellId: string | undefined;
let editingRemote = false;
let inspectorTab = 'context';
let proofKnowledgeSnapshot: ProofKnowledgeSnapshot | undefined;
let proofKnowledgeLoading = false;
let proofKnowledgeError: string | undefined;
let rightPaneMode: 'agent' | 'inspector' = 'agent';
let bottomDockTab: 'output' | 'activity' | 'games' = 'output';
let bottomDockOpen = false;
let bottomDockUserToggled = false;
let dockArcade: DockArcadeController | undefined;
let perfMonitor: PerfMonitorController | undefined;
let dockHeightBeforeGames: string | null = null;
let dockGamesTargetText = '스네이크 · 0점';
let lastOutputCellId: string | undefined;
let loadGeneration = 0;
let activePresenceCellId: string | undefined;
let toastTimer: ReturnType<typeof setTimeout>;
let vmCellSyncTimer: ReturnType<typeof setTimeout> | undefined;
let syncingFromVm = false;
const lastVmCellHashes = new Map<string, string>();
const vmCellPaths = new Map<string, string>();
const importedVmUntrackedKeys = new Set<string>();
let vmSyncInFlight = false;
let vmSyncState: 'idle' | 'syncing' | 'synced' | 'offline' = 'idle';
let vmSyncError = '';
let pendingVmUntracked: Array<{ path: string; kind: CellKind; text: string; sha256: string }> = [];
let lastDeletedCell: { kind: CellKind; text: string; afterId?: string; title: string } | undefined;
interface ExecutionOutput {
  execOrder?: number;
  running?: boolean;
  ok: boolean;
  exitCode: number | null;
  stdout: string;
  stderr: string;
  durationMs: number;
  status: 'running' | 'success' | 'verified' | 'error' | 'timeout';
  runtime?: 'agora-vm' | 'host';
  proofDbSync?: {
    state: 'syncing' | 'synced' | 'error';
    theorems: number;
    lemmaRelations: number;
    definitions: number;
    message?: string;
  };
}
const models = new Map<string, monaco.editor.ITextModel>();
const cellEditors = new Map<string, monaco.editor.IStandaloneCodeEditor>();
const manualCellHeights = new Map<string, number>();
const previewCells = new Set<string>();
const previewDocs = new WeakMap<HTMLIFrameElement, string>();
const previewTimers = new Map<string, ReturnType<typeof setTimeout>>();
let previewRenderSeq = 0;
const cellOutputs = new Map<string, ExecutionOutput>();
const cellExecOrders = new Map<string, number>();
let executionSeq = 0;
const chatSessionKey = 'irade.chat.sid';
const selfSessionId = sessionStorage.getItem(chatSessionKey) || (() => {
  const sid = crypto.randomUUID().slice(0, 8);
  sessionStorage.setItem(chatSessionKey, sid);
  return sid;
})();
let selfProfile: { uid?: string; name: string } = { name: '연구자' };
const livePeerCids = new Set<string>();
const chatVisibleCounts = new Map<string, number>();
const chatPendingAi = new Map<string, string>();
const chatAiMode = new Map<string, boolean>();
const chatDemoSpeaker = new Map<string, 'self' | 'peer'>();
const chatLastSnapshot = new Map<string, { count: number; tailKey: string }>();
const chatFeedActivePeers = new Map<string, number>();
const activities: { message: string; time: string }[] = [];
const modal = $<HTMLDialogElement>('#modal');
const starters: Record<CellKind, string> = {
  agent: createStarterAgentDoc(),
  lean: '-- 새로운 정리\n\ndef idNat (n : Nat) : Nat := n\n\nlemma add_zero_lemma (n : Nat) : n + 0 = n := by\n  rfl\n\ntheorem add_zero_identity (n : Nat) : idNat n + 0 = n := by\n  rw [idNat]\n  exact add_zero_lemma n\n\n#check add_zero_identity\n',
  rocq: '(* 새로운 증명 *)\n\nDefinition id_nat (n : nat) : nat := n.\n\nLemma add_zero_lemma : forall n : nat, n + 0 = n.\nProof.\n  intros n.\n  induction n; simpl; auto.\nQed.\n\nTheorem add_zero_identity : forall n : nat, id_nat n + 0 = n.\nProof.\n  intros n.\n  unfold id_nat.\n  apply add_zero_lemma.\nQed.\n',
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
  getWorker: () => new Worker('/assets/studio/editor.worker.js', { type: 'module' }),
};
for (const language of ['lean4', 'rocq']) {
  monaco.languages.register({ id: language });
  monaco.languages.setMonarchTokensProvider(language, {
    keywords: language === 'lean4' ? ['theorem', 'lemma', 'def', 'example', 'by', 'rfl', 'simp', 'exact', 'intro', 'intros', 'apply', 'rw', 'induction', 'case', 'cases', 'import', 'namespace', 'end', 'structure', 'where', 'have', 'let', 'fun', 'forall', 'sorry'] : ['Theorem', 'Lemma', 'Definition', 'Proof', 'Qed', 'Defined', 'Admitted', 'Require', 'Import', 'forall', 'intros', 'reflexivity', 'simpl', 'auto', 'apply', 'rewrite', 'induction', 'destruct', 'exact'],
    tokenizer: { root: [[/--.*$/, 'comment'], [/\(\*/, 'comment', '@comment'], [/"(?:[^"\\]|\\.)*"/, 'string'], [/\b\d+\b/, 'number'], [/[a-zA-Z_][\w']*/, { cases: { '@keywords': 'keyword', '@default': 'identifier' } }], [/[∀∃⊢→←↔λ:=+*<>]+/, 'operator']], comment: [[/[^(*]+/, 'comment'], [/\*\)/, 'comment', '@pop'], [/[(*]/, 'comment']] },
  });
}
monaco.editor.defineTheme('irade-dark', {
  base: 'vs-dark', inherit: true,
  rules: [{ token: 'comment', foreground: '6A9955', fontStyle: 'italic' }, { token: 'keyword', foreground: 'C586C0' }, { token: 'string', foreground: 'CE9178' }, { token: 'number', foreground: 'B5CEA8' }, { token: 'operator', foreground: 'D4D4D4' }],
  colors: { 'editor.background': '#1F1F1F', 'editor.foreground': '#CCCCCC', 'editorLineNumber.foreground': '#6E7681', 'editorLineNumber.activeForeground': '#CCCCCC', 'editorCursor.foreground': '#34D399', 'editor.selectionBackground': '#264F78', 'editor.lineHighlightBackground': '#FFFFFF08', 'editorIndentGuide.background1': '#2B2B2B', 'editorWidget.background': '#252526', 'editorWidget.border': '#3C3C3C', 'editorSuggestWidget.background': '#252526', 'editorSuggestWidget.border': '#3C3C3C', 'focusBorder': '#0078D4', 'scrollbarSlider.background': '#4E4E5240' },
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
  dockArcade?.syncUi();
  perfMonitor?.syncUi();
}
if (EMBED) {
  const scheme = matchMedia('(prefers-color-scheme: dark)');
  applyTheme(scheme.matches ? 'dark' : 'light');
  scheme.addEventListener('change', event => applyTheme(event.matches ? 'dark' : 'light'));
} else {
  applyTheme(localStorage.getItem(themeKey) === 'light' ? 'light' : 'dark');
}
function notify(message: string, error = false): void {
  clearTimeout(toastTimer); const toast = $('#toast');
  toast.textContent = message; toast.classList.toggle('error', error); toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, error ? 10000 : 4500);
}
function notifyWithUndo(message: string, undoLabel: string, onUndo: () => Promise<void> | void): void {
  clearTimeout(toastTimer); const toast = $('#toast');
  toast.classList.remove('error');
  toast.innerHTML = `<span class="toast-msg">${escape(message)}</span><button type="button" class="toast-action">${escape(undoLabel)}</button>`;
  toast.hidden = false;
  toast.querySelector<HTMLButtonElement>('.toast-action')?.addEventListener('click', () => {
    clearTimeout(toastTimer);
    toast.hidden = true;
    void Promise.resolve(onUndo()).catch(report);
  }, { once: true });
  toastTimer = setTimeout(() => { toast.hidden = true; }, 8000);
}
function renderVmSyncBadge(): void {
  const badge = document.querySelector<HTMLButtonElement>('#vm-sync-badge');
  if (!badge) { return; }
  if (!workspace || demo || vmSyncState === 'idle') {
    badge.hidden = true;
    return;
  }
  badge.hidden = false;
  badge.classList.remove('syncing', 'synced', 'offline', 'has-untracked');
  if (pendingVmUntracked.length) {
    badge.classList.add('has-untracked');
    badge.textContent = `VM +${pendingVmUntracked.length}`;
    badge.title = `VM cells/ 폴더에 새 파일 ${pendingVmUntracked.length}개 — 클릭해 동기화`;
  } else if (vmSyncState === 'syncing') {
    badge.classList.add('syncing');
    badge.textContent = 'VM 동기화 중…';
    badge.title = 'VM /workspace/cells와 동기화하는 중입니다.';
  } else if (vmSyncState === 'synced') {
    badge.classList.add('synced');
    badge.textContent = 'VM 동기화됨';
    badge.title = 'VM /workspace/cells와 동기화됨 (클릭해 지금 다시 동기화)';
  } else {
    badge.classList.add('offline');
    badge.textContent = 'VM 대기';
    badge.title = vmSyncError ? `VM 동기화 대기 (${vmSyncError}) — 클릭해 재시도` : 'AGDD VM 동기화 미연결 — 클릭해 재시도';
  }
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
  RUNTIME_NOT_CONNECTED: '실행 브로커에 연결되지 않았습니다. 연결 상태를 확인하세요.',
  RUNTIME_TIMEOUT: '격리 실행 VM의 실행 시간이 초과되었습니다.',
  RUNTIME_VM_UNAVAILABLE: '격리 실행 VM에 연결할 수 없습니다.',
  RUNTIME_UNSUPPORTED_LANGUAGE: '격리 실행 VM에서 지원하지 않는 언어입니다.',
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
  activities.splice(60);
  if (inspectorTab === 'activity') { renderInspector(); }
  renderBottomDock();
}
function syncMonitorTriggerState(active: boolean): void {
  const btn = document.querySelector<HTMLButtonElement>('#monitor-toggle');
  if (!btn) { return; }
  btn.classList.toggle('active', active);
  btn.setAttribute('aria-pressed', active ? 'true' : 'false');
}
function stopPerfMonitor(): void {
  if (perfMonitor) {
    perfMonitor.dispose();
    perfMonitor = undefined;
  }
  syncMonitorTriggerState(false);
}
function showModal(title: string, html: string, wide = false): void {
  stopPerfMonitor();
  modal.classList.toggle('modal-wide', wide);
  $('#modal-title').textContent = title; $('#modal-body').innerHTML = html;
  if (!modal.open) { modal.showModal(); }
}
$('#modal-close').addEventListener('click', () => modal.close());
modal.addEventListener('close', () => {
  modal.classList.remove('modal-wide');
  stopPerfMonitor();
});
modal.addEventListener('click', event => { if (event.target === modal) { const bounds = modal.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) { modal.close(); } } });
async function api<T>(path: string, body?: object): Promise<T> {
  const response = await fetch(origin + path, { method: body ? 'POST' : 'GET', credentials: 'same-origin', headers: { ...bearer(token), ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}), redirect: 'error', signal: AbortSignal.timeout(20000) });
  const result = await response.json();
  if (!response.ok) { if (response.status === 401) { token = undefined; void endSession(); updateAccount(); } throw new Error(result.message || `요청에 실패했습니다 (${response.status}).`); }
  return result as T;
}
function updateAccount(): void { $('#account').textContent = token ? 'ORCID · 계정' : 'ORCID 로그인 ↗'; }
async function refreshSelfProfile(): Promise<void> {
  if (!token) {
    selfProfile = { name: demo ? '나 (체험)' : '연구자' };
    return;
  }
  const me = await api<{ userId: number; name: string; orcid: string; issuer: string }>('/api/irade/auth/me');
  selfProfile = { uid: String(me.userId), name: me.name || me.orcid || '연구자' };
  for (const cell of cells) {
    if (cell.kind === 'agent') { renderAgentChat(cell.id, { scrollToBottom: false }); }
  }
}
async function signIn(): Promise<boolean> {
  if (workspace?.pending) { await workspace.flush(); }
  const url = new URL('/login', origin);
  url.searchParams.set('next', EMBED ? '/agdd/' : '/agdd/notebook');
  const id = workspace?.client.config.canvasId || Number(new URL(location.href).searchParams.get('workspace'));
  if (id) { url.searchParams.set('workspace', String(id)); }
  // Embedded in the AGDD console: log in at the top level. Navigating the iframe itself would load
  // the login page (and then the whole console) inside the Studio frame.
  if (EMBED && window.top && window.top !== window.self) {
    try { window.top.location.assign(String(url)); return false; } catch { /* cross-origin parent: fall back to the frame */ }
  }
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
    searchTimer = setTimeout(() => {
      void (async () => {
        const result = await api<{ workspaceId: number; name: string; description?: string }[]>('/api/irade/workspaces?q=' + encodeURIComponent(value));
        if (version === searchVersion && modal.open && $('#workspace-choices')) { available = result.map(convert); render(); }
      })().catch(report);
    }, 250);
  });
  $('#join-workspace').addEventListener('click', () => {
    void (async () => {
      const value = await ask('공유 workspace 열기', 'Workspace ID 또는 공유 링크'); if (!value) { return; }
      let id = Number(value.trim());
      if (!id) { try { const url = new URL(value); if (url.protocol === 'agora:') { if (url.hostname !== location.hostname) { throw new Error(); } id = Number(url.username); } else { if (url.origin !== origin) { throw new Error(); } id = Number(url.searchParams.get('workspace')); } } catch { throw new Error('현재 서버의 workspace 링크를 입력하세요.'); } }
      const matchedName = available.find(canvas => canvas.canvas_id === id)?.canvas_name || await resolveWorkspaceName(id);
      await connect(id, matchedName);
    })().catch(report);
  });
  $('#create-workspace').addEventListener('click', () => {
    void (async () => {
      const name = await ask('새 연구 workspace', 'Workspace 이름'); if (!name?.trim()) { return; }
      const result = convert(await api<{ workspaceId: number; name: string; description?: string }>('/api/irade/workspaces', { name: name.trim(), description: 'IRADE 연구 workspace' }));
      await connect(result.canvas_id, result.canvas_name || name.trim());
    })().catch(report);
  });
}
function clearModels(): void {
  activePresenceCellId = undefined;
  for (const ed of cellEditors.values()) { ed.setModel(null); ed.dispose(); }
  cellEditors.clear();
  manualCellHeights.clear();
  for (const t of previewTimers.values()) { clearTimeout(t); }
  previewTimers.clear();
  previewCells.clear();
  cellOutputs.clear();
  lastOutputCellId = undefined;
  cellExecOrders.clear();
  executionSeq = 0;
  livePeerCids.clear();
  chatVisibleCounts.clear();
  chatPendingAi.clear();
  chatAiMode.clear();
  chatDemoSpeaker.clear();
  chatLastSnapshot.clear();
  chatFeedActivePeers.clear();
  vmCellPaths.clear();
  importedVmUntrackedKeys.clear();
  pendingVmUntracked = [];
  vmSyncState = 'idle';
  vmSyncError = '';
  renderVmSyncBadge();
  for (const model of models.values()) { model.dispose(); }
  models.clear();
  const notebookCells = document.querySelector<HTMLElement>('#notebook-cells');
  if (notebookCells) { notebookCells.innerHTML = ''; }
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
  document.querySelectorAll<HTMLElement>('[data-chat-typing]').forEach(bar => {
    const cellId = bar.dataset.chatTyping!;
    const editors = byCell.get(cellId) || [];
    const prevCount = chatFeedActivePeers.get(cellId) || 0;
    chatFeedActivePeers.set(cellId, editors.length);
    if (!editors.length) {
      bar.hidden = true;
      bar.textContent = '';
    } else {
      bar.hidden = false;
      bar.innerHTML = `<span class="chat-typing-dots"><span></span><span></span><span></span></span><span>${escape(editors.slice(0, 2).join(', '))} 님이 입력 중…</span>`;
      if (prevCount === 0) {
        const feed = document.querySelector<HTMLElement>(`[data-chat-feed="${CSS.escape(cellId)}"]`);
        if (feed) { scrollChatFeedToBottom(feed); }
      }
    }
  });
  if (inspectorTab === 'context') { renderInspector(); }
}
async function resolveWorkspaceName(id: number): Promise<string> {
  let cached: string | undefined;
  try {
    const saved = JSON.parse(sessionStorage.getItem('irade.research.lastWorkspace') || 'null') as { id?: number; name?: string } | null;
    if (saved?.id === id && typeof saved.name === 'string' && saved.name.trim() && saved.name !== `Workspace ${id}`) {
      cached = saved.name.trim();
    }
  } catch { /* ignore invalid cache */ }
  if (token) {
    try {
      const item = await api<{ workspaceId: number; name?: string }>(`/api/irade/workspaces/${id}`);
      if (item?.name?.trim()) { return item.name.trim(); }
    } catch {
      try {
        const list = await api<{ workspaceId: number; name?: string }[]>('/api/irade/workspaces');
        const matched = list.find(entry => entry.workspaceId === id);
        if (matched?.name?.trim()) { return matched.name.trim(); }
      } catch { /* fallback below */ }
    }
  }
  return cached || `Workspace ${id}`;
}
async function connect(id: number, name: string, restoreView = false): Promise<void> {
  if (!Number.isSafeInteger(id) || id <= 0) { throw new Error('올바른 workspace ID를 입력하세요.'); }
  if (workspace?.pending) { await workspace.flush(); }
  const generation = ++loadGeneration;
  setPresenceFocus(undefined);
  workspace?.dispose(); clearModels(); demo = false; connected = false; selected = undefined; cells = []; workspaceName = name;
  proofKnowledgeSnapshot = undefined; proofKnowledgeError = undefined;
  const client = new AgoraClient({ origin, canvasId: id }, async () => token || (await signIn() ? token : undefined), async () => ask('비공개 workspace', 'Workspace 비밀번호', true));
  const current = new ResearchWorkspace(client); workspace = current;
  void refreshProofKnowledge();
  current.onError = error => { if (generation === loadGeneration) { report(error); } };
  current.onState = state => {
    if (generation !== loadGeneration) { return; }
    connected = state === 'Connected' || state === 'Saving';
    const translated: Record<string, string> = { Connected: '실시간 연결됨', Saving: '변경 사항 저장 중…', Connecting: 'workspace 연결 중…', Reconnecting: '재연결 중 · 편집 내용 유지' };
    $('#status').textContent = translated[state] || state;
    $('#connection-label').textContent = state === 'Saving' ? '동기화 중' : connected ? 'WORKSPACE LIVE SYNC' : '연결 중';
    $('#cell-save').textContent = state === 'Saving' ? '저장 중…' : connected ? 'Workspace 동기화됨' : '로컬 변경 유지';
    updatePermissionSummary();
    syncWorkbenchChrome();
    for (const [cellId, ed] of cellEditors.entries()) {
      const item = cells.find(entry => entry.id === cellId);
      ed.updateOptions({ readOnly: item ? !item.canWrite : !client.canWrite });
    }
    syncPresenceUI();
    if (state === 'Connected' && current.pending) { void current.flush().catch(report); }
    agentPanel.onWorkspaceChanged();
    if (!['Connected', 'Saving', 'Connecting', 'Reconnecting'].includes(state)) { report(new Error(state)); }
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
    agentPanel.onSelectionChanged();
    if (viewMode === 'notebook') { renderNotebook(); }
  };
  current.onText = cell => {
    cells = current.cells;
    if (cell.kind === 'agent') {
      const parsed = parseAgentMessages(cell.text);
      for (const msg of parsed.messages) {
        if (msg.cid && msg.cid !== selfSessionId) { livePeerCids.add(msg.cid); }
      }
      renderAgentChat(cell.id, { scrollToBottom: true });
      updateCellCardMeta(cell.id);
      renderList();
      if (selected === cell.id) { renderInspector(); }
      log(`${cellTypes[cell.kind].short} 셀의 새 대화 메시지를 동기화했습니다.`);
      return;
    }
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
  document.querySelector<HTMLAnchorElement>('#agdd-link')?.setAttribute('href', `/agdd/?tab=ide&pane=studio&workspace=${id}`);
  document.querySelector<HTMLAnchorElement>('#agdd-collab-link')?.setAttribute('href', `/agdd/?tab=collab&workspace=${id}`);
  document.querySelector<HTMLAnchorElement>('#agdd-pgraph-link')?.setAttribute('href', `/agdd/?tab=pgraph&workspace=${id}`);
  if (EMBED) { try { window.parent.postMessage({ type: 'irade:workspace', id }, location.origin); } catch { /* ignore */ } }
  if (token && !selfProfile.uid) { await refreshSelfProfile().catch(() => { }); }
  const savedView = restoreView ? sessionStorage.getItem(`irade.research.view:${id}`) : null;
  if (savedView === 'overview') { overview(); } else { openNotebook(); }
  renderList();
  await current.load();
  if (generation !== loadGeneration) { return; }
  lastVmCellHashes.clear();
  void pushCellsToVm(false);
  for (const cell of cells) {
    if (cell.kind === 'agent') { renderAgentChat(cell.id, { scrollToBottom: true }); }
  }
  sessionStorage.setItem('irade.research.lastWorkspace', JSON.stringify({ id, name }));
  $('#workspace-subtitle').textContent = `IRADE Workspace · #${id}`;
  $('#workspace-mode').textContent = 'IRADE Workspace'; $('#sync-description').textContent = '각 연구 셀을 권한에 따라 실시간 동기화';
  updatePermissionSummary();
  agentPanel.onWorkspaceChanged();
  log(`${name} workspace를 연결했습니다.`); notify('공유 Workspace에 연결했습니다.');
}
function visibleCells(): Cell[] {
  return cells.filter(cell => filter === 'all' || cell.kind === filter || filter === 'proof' && ['lean', 'rocq'].includes(cell.kind) || filter === 'document' && ['markdown', 'html'].includes(cell.kind));
}
function syncWorkbenchChrome(): void {
  const topbarWs = document.querySelector<HTMLElement>('#topbar-workspace');
  if (topbarWs) { topbarWs.textContent = workspaceName; }
  const activeCell = cells.find(cell => cell.id === selected);
  const crumbEl = document.querySelector<HTMLElement>('#notebook-active-crumb');
  if (crumbEl) {
    crumbEl.textContent = activeCell ? `${cellTypes[activeCell.kind].short} · ${activeCell.title}` : '셀 목록';
  }
  const statusKind = document.querySelector<HTMLElement>('#status-cell-kind');
  if (statusKind) {
    statusKind.textContent = viewMode === 'overview'
      ? 'Overview'
      : viewMode === 'empty'
        ? 'Empty'
        : activeCell
          ? cellTypes[activeCell.kind].short
          : 'Notebook';
  }
  const pill = document.querySelector<HTMLElement>('#status-pill');
  if (pill) {
    pill.classList.toggle('live', connected);
    pill.classList.toggle('local', !connected && demo);
  }
  const tabSync = document.querySelector<HTMLElement>('#tabbar-sync-text');
  if (tabSync) {
    tabSync.textContent = connected ? 'LIVE SYNC' : demo ? 'LOCAL' : 'STANDBY';
  }
  const dock = document.querySelector<HTMLElement>('#bottom-dock');
  if (dock) { dock.hidden = !bottomDockOpen; }
  const resizerBottom = document.querySelector<HTMLElement>('#resizer-bottom');
  if (resizerBottom) { resizerBottom.hidden = !bottomDockOpen; }
  dockArcade?.setVisible(bottomDockOpen && bottomDockTab === 'games');
  const setLayoutToggle = (selector: string, open: boolean): void => {
    const btn = document.querySelector<HTMLElement>(selector);
    if (!btn) { return; }
    btn.classList.toggle('active', open);
    btn.setAttribute('aria-pressed', open ? 'true' : 'false');
  };
  const sidebarOpen = !document.body.classList.contains('sidebar-hidden');
  const rightOpen = document.body.classList.contains('agent-open') || !document.body.classList.contains('inspector-hidden');
  setLayoutToggle('#sidebar-toggle', sidebarOpen);
  setLayoutToggle('#bottom-panel-toggle', bottomDockOpen);
  setLayoutToggle('#inspector-toggle', rightOpen);
}
function ensureDockArcade(): DockArcadeController | undefined {
  if (dockArcade) { return dockArcade; }
  const pane = document.querySelector<HTMLElement>('#bottom-dock-games');
  if (!pane) { return undefined; }
  dockArcade = new DockArcadeController(
    pane,
    label => {
      dockGamesTargetText = label;
      const targetEl = document.querySelector<HTMLElement>('#bottom-dock-target');
      if (targetEl && bottomDockTab === 'games') { targetEl.textContent = label; }
    },
    expanded => {
      document.documentElement.style.setProperty('--bottom-dock-height', expanded ? '430px' : '304px');
    },
  );
  return dockArcade;
}
function switchBottomDockTab(nextTab: 'output' | 'activity' | 'games'): void {
  const rootStyle = document.documentElement.style;
  if (nextTab === 'games' && bottomDockTab !== 'games') {
    dockHeightBeforeGames = rootStyle.getPropertyValue('--bottom-dock-height') || '';
    const curHeight = document.querySelector<HTMLElement>('#bottom-dock')?.getBoundingClientRect().height || 176;
    if (curHeight < 290) {
      rootStyle.setProperty('--bottom-dock-height', '304px');
    }
  } else if (nextTab !== 'games' && bottomDockTab === 'games' && dockHeightBeforeGames !== null) {
    if (dockHeightBeforeGames) {
      rootStyle.setProperty('--bottom-dock-height', dockHeightBeforeGames);
    } else {
      rootStyle.removeProperty('--bottom-dock-height');
    }
    dockHeightBeforeGames = null;
  }
  bottomDockTab = nextTab;
  bottomDockOpen = true;
  bottomDockUserToggled = true;
  syncWorkbenchChrome();
  renderBottomDock();
}
function renderBottomDock(): void {
  const dock = document.querySelector<HTMLElement>('#bottom-dock');
  if (!dock) { return; }
  document.querySelectorAll<HTMLButtonElement>('[data-dock-tab]').forEach(btn => {
    btn.classList.toggle('selected', btn.dataset.dockTab === bottomDockTab);
  });
  const outputPane = document.querySelector<HTMLElement>('#bottom-dock-output');
  const activityPane = document.querySelector<HTMLElement>('#bottom-dock-activity');
  const gamesPane = document.querySelector<HTMLElement>('#bottom-dock-games');
  if (outputPane) { outputPane.hidden = bottomDockTab !== 'output'; }
  if (activityPane) { activityPane.hidden = bottomDockTab !== 'activity'; }
  if (gamesPane) { gamesPane.hidden = bottomDockTab !== 'games'; }

  const arcade = bottomDockTab === 'games' ? ensureDockArcade() : dockArcade;
  arcade?.setVisible(bottomDockOpen && bottomDockTab === 'games');

  const clearBtn = document.querySelector<HTMLButtonElement>('#bottom-dock-clear');
  if (clearBtn) {
    clearBtn.textContent = bottomDockTab === 'games' ? '새 게임' : '지우기';
  }

  const targetId = selected && cellOutputs.has(selected) ? selected : lastOutputCellId;
  const targetCell = targetId ? cells.find(item => item.id === targetId) : undefined;
  const out = targetId ? cellOutputs.get(targetId) : undefined;
  const targetEl = document.querySelector<HTMLElement>('#bottom-dock-target');
  if (targetEl) {
    if (bottomDockTab === 'games') {
      targetEl.textContent = dockGamesTargetText;
    } else {
      const orderPrefix = out?.execOrder ? `#${out.execOrder} · ` : '';
      targetEl.textContent = targetCell && out
        ? `${orderPrefix}${cellTypes[targetCell.kind].short} · ${targetCell.title}`
        : '실행 대기';
    }
  }
  if (outputPane) {
    if (!out || !targetCell) {
      outputPane.innerHTML = '<div class="bottom-dock-empty">Lean 4 · Rocq · Python 셀에서 <kbd>Shift+Enter</kbd> 또는 <b>▶</b> 버튼을 누르면 실행 결과가 여기에 표시됩니다. <button type="button" class="text-button" data-open-dock-games>🎮 미니게임(스네이크·블록 낙하) 열기 →</button></div>';
    } else if (out.running) {
      outputPane.innerHTML = `<div class="bottom-dock-status running">[*] 실행 중… <span>${out.runtime === 'agora-vm' ? '격리 VM' : '로컬 실행기'}</span></div>${out.stdout ? `<pre class="bottom-dock-pre">${escape(out.stdout)}</pre>` : ''}`;
    } else {
      const badge = out.status === 'verified' ? '✓ Proof Verified' : out.ok ? '✓ Executed' : out.status === 'timeout' ? '⏱ Timeout' : `✕ Error (exit ${out.exitCode ?? '?'})`;
      const combined = [out.stdout, out.stderr].filter(part => part && part.trim().length > 0).join('\n').trim() || '(출력 없음)';
      const seqLabel = out.execOrder ? `실행 #${out.execOrder} · ` : '';
      outputPane.innerHTML = `<div class="bottom-dock-status ${out.ok ? 'ok' : 'err'}"><span>${seqLabel}${badge}</span><small>${out.runtime === 'agora-vm' ? '격리 VM' : '로컬 실행기'} · ${out.durationMs}ms</small></div><pre class="bottom-dock-pre">${escape(combined)}</pre>`;
    }
  }
  if (activityPane) {
    activityPane.innerHTML = activities.length
      ? activities.slice(0, 30).map(ev => `<div class="bottom-activity-row"><small>${ev.time}</small><span>${escape(ev.message)}</span></div>`).join('')
      : '<div class="bottom-dock-empty">아직 기록된 workspace 활동이 없습니다.</div>';
  }
}
function renderList(): void {
  $('#workspace-name').textContent = workspaceName;
  $('#cell-count').textContent = String(cells.length); $('#status-cell-count').textContent = `${cells.length} cells`;
  $('#cell-list-label').textContent = filter === 'all' ? '연구 셀' : filter === 'agent' ? '에이전트 대화' : filter === 'proof' ? '증명 셀' : filter === 'python' ? '코드 셀' : '문서 셀';
  const visible = visibleCells();
  $('#cell-list').innerHTML = visible.map(cell => `<button class="cell-list-item ${cell.id === selected ? 'selected' : ''}" data-cell="${escape(cell.id)}"><span class="mini-type ${cellTypes[cell.kind].color}">${cellTypes[cell.kind].icon}</span><span><strong>${escape(cell.title)}</strong><small>${cell.locked ? '🔒 ' : ''}${cellTypes[cell.kind].short} · ${String(cells.indexOf(cell) + 1).padStart(2, '0')} · ${escape(permissionSummary(cell))}</small></span></button>`).join('') || `<p class="empty-list">${workspace || demo ? '아직 셀이 없습니다.<br>새 셀로 연구를 시작하세요.' : '대화, 증명, 코드, 문서.<br>연구의 맥락이 여기에 모입니다.'}</p>`;
  const hasWorkspaceTab = Boolean(workspace || demo);
  $('#cell-tabs').innerHTML = hasWorkspaceTab
    ? `<button class="tab ${viewMode === 'notebook' ? 'selected' : ''}" role="tab" aria-selected="${viewMode === 'notebook'}" data-action="notebook"><span class="mint">▤</span><span>${escape(workspaceName)}</span><span class="tab-close" data-action="close-workspace" role="button" aria-label="Workspace 닫기" title="Workspace 연결 닫기">×</span></button>`
    : '';
  document.querySelectorAll<HTMLButtonElement>('[data-cell]').forEach(button => button.addEventListener('click', () => selectCell(button.dataset.cell!)));
  $('#overview-tab').hidden = !overviewOpen;
  $('#overview-tab').classList.toggle('selected', viewMode === 'overview');
  $('#overview-tab').setAttribute('aria-selected', String(viewMode === 'overview'));
  syncWorkbenchChrome();
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
        if (workspace) { workspace.edit(cell.id, text); scheduleVmCellSync(); } else { cell.text = text; cell.title = cellTitle(cell.kind, text); persistScratch(); }
        updateCellCardMeta(cell.id); renderList();
        if (previewCells.has(cell.id)) { scheduleCellPreview(cell.id); }
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
  if (cell.kind === 'agent') {
    syncAgentComposerAccess(card, cell);
  }
  syncCellExecBadge(id);
  const canAddBelow = workspace ? workspace.client.canWrite : true;
  const dupBtn = card.querySelector<HTMLButtonElement>(`[data-cell-duplicate="${CSS.escape(id)}"]`);
  if (dupBtn) { dupBtn.disabled = !canAddBelow; }
  card.querySelectorAll<HTMLButtonElement>('[data-insert-kind]').forEach(btn => {
    btn.disabled = !canAddBelow;
  });
}

function syncCellExecBadge(id: string): void {
  const card = document.querySelector<HTMLElement>(`[data-nb-cell="${CSS.escape(id)}"]`);
  const badge = card?.querySelector<HTMLElement>(`[data-exec-order="${CSS.escape(id)}"]`);
  if (!badge) { return; }
  const out = cellOutputs.get(id);
  const order = cellExecOrders.get(id) ?? out?.execOrder;
  badge.classList.remove('idle', 'running', 'done');
  if (out?.running) {
    badge.classList.add('running');
    badge.textContent = '[*]';
    badge.title = '실행 중…';
  } else if (typeof order === 'number' && order > 0) {
    badge.classList.add('done');
    badge.textContent = `실행 #${order}`;
    badge.title = `${order}번째로 실행됨`;
  } else {
    badge.classList.add('idle');
    badge.textContent = '[ · ]';
    badge.title = '아직 실행되지 않음';
  }
}

function viewerIdentity(): ChatViewerIdentity {
  return { uid: selfProfile.uid, cid: selfSessionId, name: selfProfile.name };
}

function scrollChatFeedToBottom(feed: HTMLElement): void {
  feed.dataset.suppressAutoLoad = '1';
  feed.scrollTop = feed.scrollHeight;
  const assignedAt = feed.scrollTop;
  requestAnimationFrame(() => {
    const userScrolledAway = Math.abs(feed.scrollTop - assignedAt) >= 16;
    if (!userScrolledAway) {
      feed.scrollTop = feed.scrollHeight;
    }
    delete feed.dataset.suppressAutoLoad;
    if (userScrolledAway && feed.scrollTop <= 24) {
      const cellId = feed.dataset.chatFeed;
      if (cellId) { loadOlderAgentMessages(cellId); }
    }
  });
}

function applyChatFeedHeight(card: HTMLElement, cellId: string): void {
  const feed = card.querySelector<HTMLElement>(`[data-chat-feed="${CSS.escape(cellId)}"]`);
  if (!feed) { return; }
  const manual = manualCellHeights.get(cellId);
  if (manual) {
    feed.style.height = `${manual}px`;
    feed.style.maxHeight = `${manual}px`;
  } else {
    feed.style.removeProperty('height');
    feed.style.removeProperty('max-height');
  }
}

function syncAgentComposerAccess(card: HTMLElement, cell: Cell): void {
  const canWrite = workspace ? cell.canWrite : !cell.locked;
  const aiBusy = chatPendingAi.has(cell.id);
  const input = card.querySelector<HTMLTextAreaElement>(`[data-chat-input="${CSS.escape(cell.id)}"]`);
  const sendBtn = card.querySelector<HTMLButtonElement>(`[data-chat-send="${CSS.escape(cell.id)}"]`);
  const sendOnlyBtn = card.querySelector<HTMLButtonElement>(`[data-chat-send-only="${CSS.escape(cell.id)}"]`);
  const aiToggle = card.querySelector<HTMLInputElement>(`[data-chat-ai-toggle="${CSS.escape(cell.id)}"]`);
  const collabHint = card.querySelector<HTMLElement>('.chat-collab-hint');
  if (collabHint) {
    collabHint.textContent = connected ? '공동 참여자 · 실시간 입력 동기화' : '로컬 대화 체험 모드';
  }
  if (input) {
    input.disabled = !canWrite;
    input.placeholder = !canWrite
      ? '이 대화 셀은 잠겨 있거나 읽기 전용입니다.'
      : 'AI 에이전트 또는 공동 연구자에게 메시지를 입력하세요… (Enter 전송, Shift+Enter 줄바꿈)';
  }
  if (sendBtn) {
    sendBtn.disabled = !canWrite || aiBusy;
    sendBtn.textContent = aiBusy ? 'AI 생성 중…' : '전송 ↵';
  }
  if (sendOnlyBtn) {
    sendOnlyBtn.disabled = !canWrite;
  }
  if (aiToggle) {
    aiToggle.disabled = !canWrite;
    const enabled = chatAiMode.get(cell.id) ?? connected;
    aiToggle.checked = enabled;
  }
}

function loadOlderAgentMessages(cellId: string): void {
  const cell = cells.find(c => c.id === cellId && c.kind === 'agent');
  const card = document.querySelector<HTMLElement>(`[data-nb-cell="${CSS.escape(cellId)}"]`);
  const feed = card?.querySelector<HTMLElement>(`[data-chat-feed="${CSS.escape(cellId)}"]`);
  if (!cell || !feed) { return; }
  const { messages } = parseAgentMessages(cell.text);
  const curLimit = chatVisibleCounts.get(cellId) ?? CHAT_INITIAL_VISIBLE;
  if (curLimit >= messages.length) { return; }
  const prevScrollHeight = feed.scrollHeight;
  const prevScrollTop = feed.scrollTop;
  chatVisibleCounts.set(cellId, Math.min(messages.length, curLimit + CHAT_LOAD_STEP));
  renderAgentChat(cellId, {
    scrollToBottom: false,
    preserveFrom: { scrollHeight: prevScrollHeight, scrollTop: prevScrollTop },
  });
}

function renderAgentChat(
  cellId: string,
  opts?: { scrollToBottom?: boolean; preserveFrom?: { scrollHeight: number; scrollTop: number } },
): void {
  const cell = cells.find(c => c.id === cellId && c.kind === 'agent');
  const card = document.querySelector<HTMLElement>(`[data-nb-cell="${CSS.escape(cellId)}"]`);
  const feed = card?.querySelector<HTMLElement>(`[data-chat-feed="${CSS.escape(cellId)}"]`);
  if (!cell || !card || !feed) { return; }

  const { messages } = parseAgentMessages(cell.text);
  const viewer = viewerIdentity();
  for (const msg of messages) {
    if (msg.cid && msg.cid !== selfSessionId) { livePeerCids.add(msg.cid); }
  }

  const limit = chatVisibleCounts.get(cellId) ?? CHAT_INITIAL_VISIBLE;
  const shownStart = Math.max(0, messages.length - limit);
  const shown = messages.slice(shownStart);
  const remaining = shownStart;

  const loadMoreHtml = remaining > 0
    ? `<div class="chat-load-more-wrap"><button type="button" class="chat-load-more" data-chat-load-more="${escape(cellId)}">↑ 이전 대화 ${remaining}개 중 ${Math.min(CHAT_LOAD_STEP, remaining)}개 더 불러오기</button></div>`
    : '';

  const rowsHtml = shown.length
    ? shown.map(msg => {
      const align = resolveChatAlignment(msg, viewer, livePeerCids);
      const timeText = formatChatTime(msg.ts);
      const displayName = align === 'assistant' ? (msg.author || 'IRADE AI') : (msg.author || (align === 'peer' ? '상대방' : '나'));
      const avatarHtml = align === 'assistant'
        ? `<div class="chat-avatar chat-avatar-agent" title="IRADE AI 에이전트">${AGENT_AVATAR_SVG}</div>`
        : align === 'self'
          ? `<div class="chat-avatar chat-avatar-self" title="${escape(displayName)}">${escape(avatarMonogram(displayName))}</div>`
          : `<div class="chat-avatar chat-avatar-peer ${peerAvatarTone(msg.uid || displayName)}" title="${escape(displayName)}">${escape(avatarMonogram(displayName))}</div>`;
      const roleTag = align === 'assistant'
        ? `<span class="chat-role-badge ai">${escape(msg.model || 'AI')}</span>`
        : align === 'peer'
          ? '<span class="chat-role-badge peer">참여자</span>'
          : align === 'self'
            ? '<span class="chat-role-badge self">나</span>'
            : '<span class="chat-role-badge">안내</span>';
      return `<div class="chat-row is-${align}" data-chat-msg-id="${escape(msg.id)}">${align === 'self' ? '' : avatarHtml}<div class="chat-bubble-col"><div class="chat-bubble-meta"><span class="chat-author">${escape(displayName)}</span>${roleTag}${timeText ? `<span class="chat-time">${escape(timeText)}</span>` : ''}</div><div class="chat-bubble-card">${formatChatBodyHtml(msg.body)}</div></div>${align === 'self' ? avatarHtml : ''}</div>`;
    }).join('')
    : '<div class="chat-empty-state">아직 대화가 없습니다. 아래 입력창에서 질문하거나 참여자와 대화를 시작해 보세요.</div>';

  const pendingProgress = chatPendingAi.get(cellId);
  const typingBubbleHtml = pendingProgress !== undefined
    ? `<div class="chat-row is-assistant is-typing"><div class="chat-avatar chat-avatar-agent" title="IRADE AI 에이전트">${AGENT_AVATAR_SVG}</div><div class="chat-bubble-col"><div class="chat-bubble-meta"><span class="chat-author">IRADE AI</span><span class="chat-role-badge ai">응답 생성 중</span></div><div class="chat-bubble-card"><div class="chat-typing-dots"><span></span><span></span><span></span></div>${pendingProgress ? `<div class="chat-progress-note">${escape(pendingProgress)}</div>` : ''}</div></div></div>`
    : '';

  feed.innerHTML = `${loadMoreHtml}${rowsHtml}${typingBubbleHtml}`;

  feed.querySelector<HTMLButtonElement>(`[data-chat-load-more="${CSS.escape(cellId)}"]`)?.addEventListener('click', e => {
    e.stopPropagation();
    loadOlderAgentMessages(cellId);
  });

  const speakerSlot = card.querySelector<HTMLElement>(`[data-chat-speaker-slot="${CSS.escape(cellId)}"]`);
  if (speakerSlot) {
    if (connected) {
      speakerSlot.innerHTML = `<span class="chat-identity-chip"><b>${escape(selfProfile.name)}</b> (나) · 상대방 입력 가능</span>`;
    } else {
      const currentSpeaker = chatDemoSpeaker.get(cellId) || 'self';
      speakerSlot.innerHTML = `<label class="chat-speaker-picker">작성자 <select data-chat-speaker="${escape(cellId)}"><option value="self" ${currentSpeaker === 'self' ? 'selected' : ''}>나</option><option value="peer" ${currentSpeaker === 'peer' ? 'selected' : ''}>상대방 (참여자)</option></select></label>`;
      speakerSlot.querySelector<HTMLSelectElement>(`[data-chat-speaker="${CSS.escape(cellId)}"]`)?.addEventListener('change', e => {
        const val = (e.currentTarget as HTMLSelectElement).value === 'peer' ? 'peer' : 'self';
        chatDemoSpeaker.set(cellId, val);
      });
    }
  }

  const modelEl = card.querySelector<HTMLElement>(`[data-chat-model="${CSS.escape(cellId)}"]`);
  if (modelEl) { modelEl.textContent = agentPanel.getActiveApiLabel(); }

  const cursorEl = card.querySelector<HTMLElement>(`[data-nb-cursor="${CSS.escape(cellId)}"]`);
  if (cursorEl) {
    cursorEl.textContent = remaining > 0 ? `대화 ${messages.length}개 (최근 ${shown.length}개)` : `대화 ${messages.length}개`;
  }

  const tailMsg = messages[messages.length - 1];
  chatLastSnapshot.set(cellId, {
    count: messages.length,
    tailKey: tailMsg ? `${tailMsg.id}:${tailMsg.body.length}:${pendingProgress ? 1 : 0}` : (pendingProgress ? 'pending' : ''),
  });

  applyChatFeedHeight(card, cellId);
  syncAgentComposerAccess(card, cell);

  if (opts?.preserveFrom) {
    feed.dataset.suppressAutoLoad = '1';
    const delta = feed.scrollHeight - opts.preserveFrom.scrollHeight;
    feed.scrollTop = Math.max(0, opts.preserveFrom.scrollTop + delta);
    requestAnimationFrame(() => { delete feed.dataset.suppressAutoLoad; });
  } else if (opts?.scrollToBottom) {
    scrollChatFeedToBottom(feed);
  }
}

function commitAgentCellText(cellId: string, nextText: string): void {
  const cell = cells.find(c => c.id === cellId && c.kind === 'agent');
  if (!cell) { return; }
  if (workspace) {
    workspace.edit(cellId, nextText);
    cells = workspace.cells;
    void workspace.flush().catch(report);
    scheduleVmCellSync();
  } else {
    cell.text = nextText;
    cell.title = cellTitle(cell.kind, nextText);
    persistScratch();
  }
  updateCellCardMeta(cellId);
  renderList();
  if (selected === cellId) { renderInspector(); }
}

async function submitAgentChatMessage(cellId: string, forceNoAi = false): Promise<void> {
  const cell = cells.find(c => c.id === cellId && c.kind === 'agent');
  const card = document.querySelector<HTMLElement>(`[data-nb-cell="${CSS.escape(cellId)}"]`);
  const input = card?.querySelector<HTMLTextAreaElement>(`[data-chat-input="${CSS.escape(cellId)}"]`);
  if (!cell || !card || !input) { return; }

  const canWrite = workspace ? cell.canWrite : !cell.locked;
  if (!canWrite) {
    notify(errorLabels[cell.locked ? 'ITEM_LOCKED' : 'ITEM_WRITE_DENIED'], true);
    return;
  }

  const text = input.value.trim();
  if (!text) { return; }

  const prevMessages = parseAgentMessages(cell.text).messages;
  const history = prevMessages
    .filter((entry): entry is typeof entry & { role: 'user' | 'assistant' } => entry.role === 'user' || entry.role === 'assistant')
    .slice(-12)
    .map(entry => ({ role: entry.role, content: entry.body.slice(0, 8000) }));

  const isDemoPeer = !connected && chatDemoSpeaker.get(cellId) === 'peer';
  const msgEntry = {
    role: 'user' as const,
    uid: connected ? selfProfile.uid : (isDemoPeer ? 'peer' : 'self'),
    cid: selfSessionId,
    author: connected ? selfProfile.name : (isDemoPeer ? '상대방' : (selfProfile.name || '나')),
    ts: Date.now(),
    body: text,
  };

  input.value = '';
  const updatedText = appendAgentMessage(cell.text, msgEntry);
  commitAgentCellText(cellId, updatedText);
  renderAgentChat(cellId, { scrollToBottom: true });

  const aiEnabled = !forceNoAi && (chatAiMode.get(cellId) ?? connected);
  if (!aiEnabled) { return; }

  if (!workspace || !connected) {
    if (chatPendingAi.has(cellId)) {
      notify('로컬 어시스턴트가 응답을 작성 중입니다.');
      return;
    }
    chatPendingAi.set(cellId, '로컬 어시스턴트가 응답을 작성 중입니다…');
    renderAgentChat(cellId, { scrollToBottom: true });
    try {
      const localRes = await runLocalAssistantTurn(text, cell);
      chatPendingAi.delete(cellId);
      const latestCell = cells.find(c => c.id === cellId) || cell;
      const withAnswer = appendAgentMessage(latestCell.text, {
        role: 'assistant',
        uid: 'irade-agent',
        author: 'IRADE AI',
        model: '로컬 어시스턴트',
        ts: Date.now(),
        body: localRes.answer,
      });
      commitAgentCellText(cellId, withAnswer);
      renderAgentChat(cellId, { scrollToBottom: true });
    } catch (error) {
      chatPendingAi.delete(cellId);
      report(error);
    }
    return;
  }
  if (chatPendingAi.has(cellId)) {
    notify('이 대화에서 AI 에이전트가 응답을 생성 중입니다.');
    return;
  }

  chatPendingAi.set(cellId, '에이전트에게 요청을 전송했습니다…');
  renderAgentChat(cellId, { scrollToBottom: true });
  try {
    const res = await agentPanel.runCellPrompt(cellId, text, history, chunk => {
      const prev = chatPendingAi.get(cellId) || '';
      const next = (prev ? `${prev}\n` : '') + chunk.trimEnd();
      chatPendingAi.set(cellId, next.length > 400 ? next.slice(-400) : next);
      renderAgentChat(cellId, { scrollToBottom: true });
    });
    chatPendingAi.delete(cellId);
    const latestCell = cells.find(c => c.id === cellId) || cell;
    const withAnswer = appendAgentMessage(latestCell.text, {
      role: 'assistant',
      uid: 'irade-agent',
      author: 'IRADE AI',
      model: res.model || res.api,
      limited: res.limited,
      ts: Date.now(),
      body: res.answer,
    });
    commitAgentCellText(cellId, withAnswer);
    renderAgentChat(cellId, { scrollToBottom: true });
  } catch (error) {
    chatPendingAi.delete(cellId);
    const errMsg = error instanceof Error ? error.message : String(error);
    notify(errMsg, true);
    const latestCell = cells.find(c => c.id === cellId) || cell;
    const withNotice = appendAgentMessage(latestCell.text, {
      role: 'system',
      author: '시스템 안내',
      ts: Date.now(),
      body: `AI 응답 생성 중 오류가 발생했습니다: ${errMsg}`,
    });
    commitAgentCellText(cellId, withNotice);
    renderAgentChat(cellId, { scrollToBottom: true });
  }
}
function isExecutable(kind: CellKind): boolean {
  return kind === 'python' || kind === 'lean' || kind === 'rocq';
}
function renderCellOutput(id: string): void {
  lastOutputCellId = id;
  syncCellExecBadge(id);
  renderBottomDock();
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
    const envNote = out.runtime === 'agora-vm' ? '<span>격리 실행 VM · 실시간 출력 수신 중…</span>' : '';
    container.innerHTML = `<div class="nb-output-head"><span class="nb-output-badge"><span class="nb-output-order">[*]</span> ⟳ 실행 중…</span>${envNote}</div>${streamPre}`;
    return;
  }
  const badge = out.status === 'verified' ? '✓ Proof Verified' : out.ok ? '✓ Executed' : out.status === 'timeout' ? '⏱ Timeout' : `✕ Error (exit ${out.exitCode ?? '?'})`;
  const envLabel = out.runtime === 'agora-vm' ? '격리 VM · ' : out.runtime === 'host' ? '로컬 실행기 · ' : '';
  const combined = [out.stdout, out.stderr].filter(part => part && part.trim().length > 0).join('\n').trim() || '(출력 없음)';
  const seqChip = out.execOrder ? `<span class="nb-output-order">실행 #${out.execOrder}</span> ` : '';
  const proofDbChip = (out.status === 'verified' && out.proofDbSync)
    ? out.proofDbSync.state === 'synced'
      ? `<button type="button" class="nb-proof-db-chip" data-open-proof-db="${escape(id)}">🗄 DB 저장됨 · 정리 ${out.proofDbSync.theorems} · 보조관계 ${out.proofDbSync.lemmaRelations} · 정의 ${out.proofDbSync.definitions}</button>`
      : out.proofDbSync.state === 'error'
        ? `<button type="button" class="nb-proof-db-chip error" data-open-proof-db="${escape(id)}" title="${escape(out.proofDbSync.message || '')}">⚠ DB 저장 확인</button>`
        : '<span class="nb-proof-db-chip">🗄 DB 저장 중…</span>'
    : '';
  container.innerHTML = `<div class="nb-output-head"><span class="nb-output-badge ${out.ok ? 'ok' : 'err'}">${seqChip}${badge}</span>${proofDbChip}<span>${envLabel}${out.durationMs}ms · <button type="button" class="text-button" data-clear-output="${escape(id)}">지우기</button></span></div><pre class="nb-output-pre">${escape(combined)}</pre>`;
  container.querySelector(`[data-clear-output="${CSS.escape(id)}"]`)?.addEventListener('click', e => {
    e.stopPropagation();
    cellOutputs.delete(id);
    cellExecOrders.delete(id);
    renderCellOutput(id);
    renderInspector();
    renderBottomDock();
  });
}
async function executeOnHost(kind: CellKind, code: string): Promise<ExecutionOutput> {
  const endpoint = '/api/execute';
  const response = await fetch(origin + endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, code }),
    signal: AbortSignal.timeout(30000),
  });
  const contentType = response.headers.get('content-type') || '';
  if (!response.ok || !contentType.includes('application/json')) {
    throw new Error(`실행 엔드포인트를 사용할 수 없습니다 (${response.status}).`);
  }
  const result = await response.json() as ExecutionOutput;
  return { ...result, runtime: 'host' };
}
function isVmFallbackError(codeOrMessage: string): boolean {
  return /RUNTIME_MANAGER_UNAVAILABLE|RUNTIME_VM_UNAVAILABLE|RUNTIME_KERNEL_UNAVAILABLE|RUNTIME_NOT_CONNECTED|RUNTIME_REQUEST_INVALID|RUNTIME_REQUEST_TOO_LARGE|RUNTIME_RESPONSE_TOO_LARGE|RUNTIME_TIMEOUT|RUNTIME_UNSUPPORTED_LANGUAGE|UNSUPPORTED_LANGUAGE|CONNECTION_FAILED|DISCONNECTED|VM 커널|전달하지 못했습니다/.test(codeOrMessage);
}
async function executeInIsolatedVm(id: string, kind: 'python' | 'lean' | 'rocq', code: string): Promise<ExecutionOutput> {
  const active = workspace!;
  if (active.pending) { await active.flush(); }
  const started = Date.now();
  const marker = `__IRADE_${crypto.randomUUID().replace(/-/g, '')}__`;
  let language: 'python' | 'bash' = 'python';
  let payloadCode = code;
  if (kind === 'lean') {
    const normalizedLean = normalizeLeanCodeForExecution(code);
    language = 'bash';
    payloadCode = `LEAN_BIN=$(command -v lean || ([ -x "$HOME/.elan/bin/lean" ] && echo "$HOME/.elan/bin/lean") || ([ -x "/root/.elan/bin/lean" ] && echo "/root/.elan/bin/lean") || ls -1 /opt/elan/toolchains/*/bin/lean 2>/dev/null | head -n1 || true)\n[ -n "$LEAN_BIN" ] || exit 127\n"$LEAN_BIN" --version >/dev/null 2>&1 || exit 127\n"$LEAN_BIN" --stdin 2>&1 << '${marker}'\n${normalizedLean}\n${marker}\n`;
  } else if (kind === 'rocq') {
    language = 'bash';
    payloadCode = `COQC_BIN=$(command -v coqc || true)\n[ -n "$COQC_BIN" ] || exit 127\n"$COQC_BIN" --version >/dev/null 2>&1 || exit 127\nTMP_DIR=$(mktemp -d)\ntrap 'rm -rf "$TMP_DIR"' EXIT\ncat << '${marker}' > "$TMP_DIR/Cell.v"\n${code}\n${marker}\n"$COQC_BIN" "$TMP_DIR/Cell.v" 2>&1\n`;
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
    const raw = error instanceof AgoraError
      ? `${error.code} ${error.serverMessage || ''}`.trim()
      : (error instanceof Error ? error.message : String(error));
    if (isVmFallbackError(raw)) {
      return await executeOnHost(kind, code);
    }
    throw new Error(error instanceof AgoraError ? (error.serverMessage || formatErrorLabel(error.code)) : formatErrorLabel(raw));
  }
  if (data.error && typeof data.exitCode !== 'number') {
    const msg = data.message || formatErrorLabel(data.error);
    if (isVmFallbackError(String(data.error)) || isVmFallbackError(msg)) {
      return await executeOnHost(kind, code);
    }
    throw new Error(msg);
  }
  const exitCode = typeof data.exitCode === 'number' ? data.exitCode : 1;
  const outputText = String(data.output ?? streamed ?? '');
  const ok = !data.timedOut && !data.error && exitCode === 0;
  if (exitCode === 127 || exitCode === 126 || (!ok && !data.timedOut && !outputText.trim() && !data.error)) {
    try {
      return await executeOnHost(kind, code);
    } catch {
      if (exitCode === 127 || exitCode === 126) {
        const binName = kind === 'lean' ? 'Lean 4 (lean)' : kind === 'rocq' ? 'Rocq (coqc)' : 'Python 3';
        return {
          ok: false,
          exitCode,
          stdout: '',
          stderr: `격리 실행 VM(${binName})에 해당 실행 도구가 설치되어 있지 않습니다 (exit ${exitCode}).`,
          durationMs: Date.now() - started,
          status: 'error',
          runtime: 'agora-vm',
        };
      }
    }
  }
  const durationMs = Date.now() - started;
  const isProof = kind === 'lean' || kind === 'rocq';
  return {
    ok,
    exitCode,
    stdout: ok ? (outputText || (isProof ? '✓ Proof verified in isolated runtime (0 errors)' : '')) : '',
    stderr: ok ? '' : (outputText || data.message || (data.error ? formatErrorLabel(data.error) : `실행이 종료되었습니다 (exit ${exitCode}).`)),
    durationMs,
    status: data.timedOut ? 'timeout' : ok ? (isProof ? 'verified' : 'success') : 'error',
    runtime: 'agora-vm',
  };
}
function advanceAfterExecution(id: string): void {
  if (viewMode !== 'notebook') { return; }
  const visible = visibleCells();
  const idx = visible.findIndex(item => item.id === id);
  if (idx < 0) { return; }
  const scrollBox = document.querySelector<HTMLElement>('#notebook-scroll');
  if (idx + 1 < visible.length) {
    const next = visible[idx + 1];
    selected = next.id;
    syncActiveCellCard();
    renderList();
    renderInspector();
    agentPanel.onSelectionChanged();
    requestAnimationFrame(() => {
      const nextCard = document.querySelector<HTMLElement>(`[data-nb-cell="${CSS.escape(next.id)}"]`);
      if (nextCard && scrollBox) {
        const sRect = scrollBox.getBoundingClientRect();
        const cRect = nextCard.getBoundingClientRect();
        const nextTop = cRect.top - sRect.top + scrollBox.scrollTop;
        const nextBottom = cRect.bottom - sRect.top + scrollBox.scrollTop;
        const minVisibleTop = Math.max(scrollBox.scrollTop, nextBottom - scrollBox.clientHeight + 48);
        const preferredTop = Math.max(0, nextTop - 150);
        const targetTop = Math.min(
          Math.max(0, scrollBox.scrollHeight - scrollBox.clientHeight),
          Math.max(minVisibleTop, preferredTop),
        );
        if (targetTop > scrollBox.scrollTop + 4) {
          scrollBox.scrollTo({ top: targetTop, behavior: 'smooth' });
        }
      } else {
        nextCard?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
      if (next.kind === 'agent') {
        nextCard?.querySelector<HTMLTextAreaElement>(`[data-chat-input="${CSS.escape(next.id)}"]`)?.focus({ preventScroll: true });
      } else {
        const textarea = nextCard?.querySelector<HTMLTextAreaElement>('.monaco-editor textarea.inputarea');
        if (textarea) { textarea.focus({ preventScroll: true }); }
        else { cellEditors.get(next.id)?.focus(); }
      }
    });
  } else {
    requestAnimationFrame(() => {
      const curCard = document.querySelector<HTMLElement>(`[data-nb-cell="${CSS.escape(id)}"]`);
      if (curCard && scrollBox) {
        const sRect = scrollBox.getBoundingClientRect();
        const cRect = curCard.getBoundingClientRect();
        const cardBottom = cRect.bottom - sRect.top + scrollBox.scrollTop;
        if (cardBottom + 36 > scrollBox.scrollTop + scrollBox.clientHeight) {
          const target = Math.min(
            Math.max(0, scrollBox.scrollHeight - scrollBox.clientHeight),
            cardBottom - scrollBox.clientHeight + 52,
          );
          scrollBox.scrollTo({ top: Math.max(scrollBox.scrollTop, target), behavior: 'smooth' });
        }
      }
    });
  }
}

function currentProofWorkspaceScope(): { workspaceId?: number; workspaceKey: string } {
  const wsId = workspace?.client.config.canvasId;
  if (typeof wsId === 'number' && Number.isFinite(wsId) && wsId > 0) {
    return { workspaceId: wsId, workspaceKey: `workspace-${wsId}` };
  }
  return { workspaceKey: 'local-playground' };
}

async function refreshProofKnowledge(silent = true): Promise<void> {
  const { workspaceKey } = currentProofWorkspaceScope();
  proofKnowledgeLoading = true;
  proofKnowledgeError = undefined;
  if (inspectorTab === 'proof-db') { renderInspector(); }
  try {
    proofKnowledgeSnapshot = await fetchProofKnowledgeSnapshot(workspaceKey, token);
  } catch (error) {
    proofKnowledgeError = error instanceof Error ? error.message : String(error);
    if (!silent) { notify(proofKnowledgeError, true); }
  } finally {
    proofKnowledgeLoading = false;
    if (inspectorTab === 'proof-db') { renderInspector(); }
    if (modal.open && modal.querySelector('.proof-db-modal-wrap') && proofKnowledgeSnapshot) {
      $('#modal-body').innerHTML = renderProofDbModalHtml(proofKnowledgeSnapshot);
    }
  }
}

async function syncVerifiedProofCell(cell: Cell, code: string, result: ExecutionOutput): Promise<void> {
  if ((cell.kind !== 'lean' && cell.kind !== 'rocq') || result.status !== 'verified') { return; }
  const proverEngine = cell.kind === 'rocq' ? 'ROCQ' : 'LEAN4';
  const parsed = extractClientProofRelations(proverEngine, code);
  if (!parsed.theorems.length && !parsed.definitions.length) { return; }

  const { workspaceId, workspaceKey } = currentProofWorkspaceScope();
  const prevOut = cellOutputs.get(cell.id);
  if (prevOut) {
    cellOutputs.set(cell.id, {
      ...prevOut,
      proofDbSync: { state: 'syncing', theorems: 0, lemmaRelations: 0, definitions: 0 },
    });
    renderCellOutput(cell.id);
  }

  try {
    const snapshot = await syncVerifiedProofToDb({
      workspaceId,
      workspaceKey,
      proverEngine,
      verificationStatus: 'VERIFIED',
      cellId: cell.id,
      sourceCode: code,
      verifierOutput: [result.stdout, result.stderr].filter(Boolean).join('\n').trim() || 'Verified cleanly',
      lemmaRelations: parsed.lemmaRelations,
      definitionUsages: parsed.definitionUsages,
    }, token);
    proofKnowledgeSnapshot = snapshot;
    proofKnowledgeError = undefined;
    const latestOut = cellOutputs.get(cell.id);
    if (latestOut) {
      cellOutputs.set(cell.id, {
        ...latestOut,
        proofDbSync: {
          state: 'synced',
          theorems: snapshot.theorems.length,
          lemmaRelations: snapshot.lemmaRelations.length,
          definitions: snapshot.definitions.length,
        },
      });
    }
    log(`[관계형 DB] ${proverEngine} 정리 ${snapshot.theorems.length}건 · 보조관계 ${snapshot.lemmaRelations.length}건 · 정의 ${snapshot.definitions.length}건 저장됨.`);
  } catch (error) {
    const msg = error instanceof Error ? error.message : String(error);
    proofKnowledgeError = msg;
    const latestOut = cellOutputs.get(cell.id);
    if (latestOut) {
      cellOutputs.set(cell.id, {
        ...latestOut,
        proofDbSync: { state: 'error', theorems: 0, lemmaRelations: 0, definitions: 0, message: msg },
      });
    }
  }
}

async function runCell(id: string, advanceOnFinish = true): Promise<void> {
  const cell = cells.find(item => item.id === id);
  if (!cell || !isExecutable(cell.kind)) { return; }
  if (workspace && !cell.canExecute) {
    notify(errorLabels.RUNTIME_EXECUTION_DENIED, true);
    return;
  }
  const code = models.get(id)?.getValue() ?? cell.text;
  const runBtn = document.querySelector<HTMLButtonElement>(`[data-cell-run="${CSS.escape(id)}"]`);
  if (runBtn) { runBtn.disabled = true; }
  if (window.innerWidth > 760) {
    switchBottomDockTab('output');
  }
  const prevOrder = cellExecOrders.get(id);
  cellOutputs.set(id, { execOrder: prevOrder, running: true, ok: true, exitCode: null, stdout: '', stderr: '', durationMs: 0, status: 'running', runtime: workspace ? 'agora-vm' : 'host' });
  renderCellOutput(id);
  try {
    const result = workspace
      ? await executeInIsolatedVm(id, cell.kind as 'python' | 'lean' | 'rocq', code)
      : await executeOnHost(cell.kind, code);
    const seq = ++executionSeq;
    cellExecOrders.set(id, seq);
    const completedOutput: ExecutionOutput = { ...result, execOrder: seq };
    cellOutputs.set(id, completedOutput);
    log(`[실행 #${seq}] ${cellTypes[cell.kind].short} 셀을 ${result.runtime === 'agora-vm' ? '격리 실행 VM에서 ' : ''}${result.ok ? '실행/검증' : '실행(오류)'}했습니다 (${result.durationMs}ms).`);
    if ((cell.kind === 'lean' || cell.kind === 'rocq') && result.status === 'verified') {
      await syncVerifiedProofCell(cell, code, completedOutput);
    }
  } catch (error) {
    const seq = ++executionSeq;
    cellExecOrders.set(id, seq);
    cellOutputs.set(id, {
      execOrder: seq,
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
    if (advanceOnFinish && cells.some(item => item.id === id)) {
      advanceAfterExecution(id);
    }
  }
}
async function runAllCells(): Promise<void> {
  const ordered = visibleCells().filter(cell => isExecutable(cell.kind) || cell.kind === 'html');
  if (!ordered.length) {
    notify('실행할 수 있는 Python, Lean 4, Rocq 또는 HTML 셀이 없습니다.');
    return;
  }
  let execCount = 0;
  let htmlCount = 0;
  for (let i = 0; i < ordered.length; i += 1) {
    const cell = ordered[i];
    const isLast = i === ordered.length - 1;
    if (cell.kind === 'html') {
      htmlCount += 1;
      renderHtmlCell(cell.id, isLast);
    } else {
      execCount += 1;
      await runCell(cell.id, isLast);
    }
  }
  notify(`${execCount}개 코드 셀 실행${htmlCount ? ` · ${htmlCount}개 HTML 렌더링` : ''}을 완료했습니다.`);
}
function renderHtmlCell(id: string, advanceOnFinish = true): void {
  previewCells.add(id);
  const btn = document.querySelector<HTMLButtonElement>(`[data-cell-preview="${CSS.escape(id)}"]`);
  if (btn) { btn.textContent = '미리보기 닫기'; }
  const seq = ++executionSeq;
  cellExecOrders.set(id, seq);
  syncCellExecBadge(id);
  renderCellPreview(id);
  renderInspector();
  log(`[실행 #${seq}] HTML 셀을 렌더링했습니다.`);
  if (advanceOnFinish) {
    advanceAfterExecution(id);
  }
}
function scheduleCellPreview(id: string): void {
  clearTimeout(previewTimers.get(id));
  const cell = cells.find(item => item.id === id);
  if (cell?.kind === 'html') {
    previewTimers.set(id, setTimeout(() => {
      previewTimers.delete(id);
      renderCellPreview(id);
    }, 120));
    return;
  }
  renderCellPreview(id);
}
function renderCellPreview(id: string): void {
  clearTimeout(previewTimers.get(id));
  previewTimers.delete(id);
  const cell = cells.find(item => item.id === id);
  const frame = document.querySelector<HTMLIFrameElement>(`[data-nb-preview="${CSS.escape(id)}"]`);
  if (!cell || !frame) { return; }
  const visible = previewCells.has(id) && ['markdown', 'html'].includes(cell.kind);
  frame.hidden = !visible;
  if (!visible) { delete frame.dataset.renderedHtml; return; }
  const light = currentTheme() === 'light';
  if (cell.kind === 'html') {
    frame.setAttribute('sandbox', 'allow-scripts');
    const raw = models.get(id)?.getValue() ?? cell.text;
    const isFullDoc = /<!doctype|<html[\s>]/i.test(raw);
    const baseStyle = light
      ? '<style>html,body{margin:0;padding:16px;font-family:system-ui,-apple-system,sans-serif;color:#18181b;background:#ffffff;box-sizing:border-box}*,*::before,*::after{box-sizing:border-box}</style>'
      : '<style>html,body{margin:0;padding:16px;font-family:system-ui,-apple-system,sans-serif;color:#f4f4f5;background:#1b1b1f;box-sizing:border-box}*,*::before,*::after{box-sizing:border-box}</style>';
    const htmlDoc = isFullDoc
      ? raw
      : `<!doctype html><html lang="ko"><head><meta charset="UTF-8"><meta name="color-scheme" content="${light ? 'light' : 'dark'}">${baseStyle}</head><body>${raw}</body></html>`;
    if (frame.dataset.renderedHtml === htmlDoc && frame.src) {
      return;
    }
    frame.dataset.renderedHtml = htmlDoc;
    previewDocs.set(frame, htmlDoc);
    if (!frame.dataset.previewBound) {
      frame.dataset.previewBound = '1';
      frame.addEventListener('load', () => {
        if (frame.dataset.awaitingPreview !== '1') { return; }
        frame.dataset.awaitingPreview = '';
        const pendingHtml = previewDocs.get(frame);
        if (pendingHtml !== undefined) {
          frame.contentWindow?.postMessage({ type: 'irade:render-html', html: pendingHtml }, '*');
        }
      });
    }
    frame.removeAttribute('srcdoc');
    frame.dataset.awaitingPreview = '1';
  const previewBase = '/preview';
    frame.src = `${previewBase}?r=${++previewRenderSeq}`;
    return;
  }
  frame.removeAttribute('src');
  frame.dataset.awaitingPreview = '';
  frame.setAttribute('sandbox', '');
  const source = marked.parse(models.get(id)?.getValue() ?? cell.text, { async: false });
  const safe = DOMPurify.sanitize(source, { FORBID_TAGS: ['script', 'style', 'form', 'iframe', 'object', 'embed', 'base', 'link', 'meta'], FORBID_ATTR: ['style', 'srcset'] });
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
    ? groups.map(group => {
      const groupLabel = workspace ? group : `${group} (로컬 세션)`;
      return `<label class="perm-group-item ${isAdmin ? '' : 'disabled'}"><input type="checkbox" name="perm-group" value="${escape(group)}" ${current.has(group) ? 'checked' : ''} ${isAdmin ? '' : 'disabled'}><span>${escape(groupLabel)}</span></label>`;
    }).join('')
    : '<p class="empty-search">할당된 Canvas 그룹이 없습니다.</p>';
  const lockNotice = !workspace
    ? '<p class="modal-copy">로컬 체험 셀의 잠금 상태를 설정합니다. 그룹별 ACL 제어는 공유 Workspace에서 사용할 수 있습니다.</p>'
    : !isAdmin
      ? '<p class="modal-copy">그룹 접근 권한 변경은 Workspace 관리자만 가능합니다. 쓰기 권한자는 잠금 상태를 전환할 수 있습니다.</p>'
      : '<p class="modal-copy">연구 셀 단위로 읽기·쓰기·실행 그룹과 잠금 상태를 제어합니다.</p>';
  showModal(`셀 객체 권한 · ${cell.title}`, `${lockNotice}
    <div class="field">
      <span class="perm-field-label">객체 ID</span>
      <div class="perm-id-row">
        <input id="perm-item-id" readonly value="${escape(cell.id)}" aria-label="Canvas Workspace Item ID">
        <button type="button" class="button ghost" id="perm-copy-id">ID 복사</button>
      </div>
    </div>
    <div class="perm-caps">
      <span class="perm-cap allowed">읽기 ✓</span>
      <span class="perm-cap ${canEdit ? 'allowed' : ''}">쓰기 ${canEdit ? '✓' : '✕'}</span>
      ${isExecutable(cell.kind) ? `<span class="perm-cap ${canExec ? 'allowed' : ''}">실행 ${canExec ? '✓' : '✕'}</span>` : ''}
      ${isAdmin ? '<span class="perm-cap allowed">Admin</span>' : ''}
    </div>
    <form id="perm-form">
      <label class="perm-group-item ${canUnlock ? '' : 'disabled'}"><input type="checkbox" id="perm-locked" ${cell.locked ? 'checked' : ''} ${canUnlock ? '' : 'disabled'}><span>셀 잠금 (쓰기·이동·삭제 제한)</span></label>
      <div class="field"><span class="perm-field-label">허용 그룹 ACL</span><div class="perm-group-list">${groupHtml}</div></div>
      <div class="form-actions">
        <button type="button" class="button ghost" id="perm-cancel">닫기</button>
        ${(isAdmin || canUnlock) ? '<button type="submit" class="button primary">권한 저장</button>' : ''}
      </div>
    </form>`);
  $('#perm-copy-id').addEventListener('click', () => {
    void navigator.clipboard.writeText(cell.id).then(() => notify('Canvas 객체 ID를 복사했습니다.')).catch(() => { });
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
  const container = $('#notebook-cells');
  if (!(workspace || demo)) {
    for (const ed of cellEditors.values()) { ed.setModel(null); ed.dispose(); }
    cellEditors.clear();
    container.innerHTML = '';
    return;
  }
  if (viewMode !== 'notebook') { return; }
  $('#notebook-title').textContent = workspaceName;
  $('#notebook-kind').textContent = demo ? 'LOCAL PLAYGROUND NOTEBOOK' : connected ? 'SHARED WORKSPACE NOTEBOOK' : 'RESEARCH NOTEBOOK';
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
      card.className = cell.kind === 'agent' ? 'nb-cell nb-cell-messenger' : 'nb-cell';
      card.dataset.nbCell = cell.id;
      card.id = `nb-cell-${cell.id}`;
      const canPreview = ['markdown', 'html'].includes(cell.kind);
      const canRun = isExecutable(cell.kind);
      const hasExecOrder = canRun || cell.kind === 'html';
      if (cell.kind === 'html' && !previewCells.has(cell.id)) { previewCells.add(cell.id); }
      const previewLabel = previewCells.has(cell.id) ? '미리보기 닫기' : '미리보기';
      const runLabel = cell.kind === 'python' ? '▶ 실행' : '▶ 검증';
      const canWrite = workspace ? cell.canWrite : !cell.locked;
      const canExecute = workspace ? cell.canExecute : true;
      const canAddBelow = workspace ? workspace.client.canWrite : true;
      const bodyHtml = cell.kind === 'agent'
        ? `<div class="nb-chat-shell" data-nb-chat="${escape(cell.id)}"><div class="chat-topbar"><div class="chat-agent-badge"><span class="chat-avatar chat-avatar-agent chat-avatar-sm">${AGENT_AVATAR_SVG}</span><span class="chat-agent-title">IRADE AI 메신저</span><span class="chat-agent-model" data-chat-model="${escape(cell.id)}">${escape(agentPanel.getActiveApiLabel())}</span></div><span class="chat-collab-hint">${connected ? '공동 참여자 · 실시간 입력 동기화' : '로컬 대화 체험 모드'}</span></div><div class="chat-feed" data-chat-feed="${escape(cell.id)}" tabindex="0" aria-label="AI 메신저 대화 내용"></div><div class="chat-typing-bar" data-chat-typing="${escape(cell.id)}" hidden></div><div class="nb-cell-resize-handle" data-cell-resize="${escape(cell.id)}" role="separator" aria-orientation="horizontal" aria-label="메신저 창 높이 조절" title="드래그해 대화창 높이 조절 (더블클릭: 기본 높이)"></div><form class="chat-composer" data-chat-form="${escape(cell.id)}"><div class="chat-composer-meta"><span class="chat-speaker-slot" data-chat-speaker-slot="${escape(cell.id)}"></span><label class="chat-ai-toggle" title="켜두면 메시지 전송 시 AI 에이전트가 자동으로 응답합니다."><input type="checkbox" data-chat-ai-toggle="${escape(cell.id)}"><span>✦ AI 자동 응답</span></label></div><div class="chat-composer-row"><textarea class="chat-input" data-chat-input="${escape(cell.id)}" rows="2" placeholder="AI 에이전트 또는 공동 연구자에게 메시지를 입력하세요… (Enter 전송, Shift+Enter 줄바꿈)"></textarea><div class="chat-composer-buttons"><button type="button" class="button ghost chat-send-only-btn" data-chat-send-only="${escape(cell.id)}" title="AI 응답 없이 참여자 대화만 전송">메시지만</button><button type="submit" class="button primary chat-send-btn" data-chat-send="${escape(cell.id)}">전송 ↵</button></div></div></form></div><div class="nb-cell-output" data-nb-output="${escape(cell.id)}" hidden></div>`
        : `<div class="nb-editor-host"></div><div class="nb-cell-resize-handle" data-cell-resize="${escape(cell.id)}" role="separator" aria-orientation="horizontal" aria-label="셀 편집기 높이 조절" title="드래그해 셀 편집기 높이 조절 (더블클릭: 자동 높이)"></div><iframe class="nb-preview-frame" data-nb-preview="${escape(cell.id)}" title="셀 미리보기" sandbox="${cell.kind === 'html' ? 'allow-scripts' : ''}" referrerpolicy="no-referrer" hidden></iframe><div class="nb-cell-output" data-nb-output="${escape(cell.id)}" hidden></div>`;
      const footerHint = cell.kind === 'agent'
        ? ' · 메신저 대화 · 위로 스크롤해 이전 내역 불러오기'
        : canRun
          ? ' · Shift+Enter 실행'
          : canPreview
            ? (cell.kind === 'html' ? ' · Shift+Enter 렌더링' : ' · Shift+Enter 미리보기')
            : '';
      const insertButtonsHtml = (['python', 'lean', 'rocq', 'agent', 'markdown', 'html'] as CellKind[]).map(kind =>
        `<button type="button" class="nb-insert-chip" data-insert-kind="${kind}" data-insert-target="${escape(cell.id)}" ${canAddBelow ? '' : 'disabled'}><span class="${cellTypes[kind].color}">${cellTypes[kind].icon}</span>${escape(cellTypes[kind].short)}</button>`,
      ).join('');

      card.innerHTML = `<header class="nb-cell-header"><span class="nb-cell-index">${indexLabel}</span>${hasExecOrder ? `<span class="nb-exec-order idle" data-exec-order="${escape(cell.id)}" title="아직 실행되지 않음">[ · ]</span>` : ''}<span class="nb-cell-kind"><span class="${type.color}">${type.icon}</span>${type.short}</span><button type="button" class="nb-cell-id-chip" data-cell-copy-id="${escape(cell.id)}" title="Canvas Workspace Item ID: ${escape(cell.id)} (클릭해 복사)">#${escape(shortItemId(cell.id))}</button><span class="nb-cell-title">${escape(cell.title)}</span><span class="nb-cell-presence-group" data-nb-presence="${escape(cell.id)}"></span><button type="button" class="nb-cell-perm-btn ${cell.locked ? 'locked' : ''}" data-cell-perm="${escape(cell.id)}" title="Canvas 객체 권한 및 잠금 설정">${cell.locked ? '🔒 ' : ''}${escape(permissionSummary(cell))}</button><div class="nb-cell-tools"><button class="icon-button" data-cell-move-up="${escape(cell.id)}" title="위로 이동" aria-label="위로 이동" ${index <= 0 || !canWrite ? 'disabled' : ''}>↑</button><button class="icon-button" data-cell-move-down="${escape(cell.id)}" title="아래로 이동" aria-label="아래로 이동" ${index >= cells.length - 1 || !canWrite ? 'disabled' : ''}>↓</button>${canRun ? `<button class="nb-run-btn" data-cell-run="${escape(cell.id)}" ${canExecute ? '' : 'disabled'} title="${canExecute ? '실행/검증 (Shift+Enter)' : '이 셀 그룹에 실행 권한이 없습니다.'}">${runLabel}</button>` : ''}${cell.kind === 'html' ? `<button class="nb-run-btn" data-cell-render="${escape(cell.id)}" title="HTML 렌더링 (Shift+Enter)">▶ 렌더링</button>` : ''}${cell.kind === 'agent' ? `<button class="button tiny" data-cell-agent="${escape(cell.id)}">✦ AI 설정</button>` : ''}${canPreview ? `<button class="button tiny" data-cell-preview="${escape(cell.id)}">${previewLabel}</button>` : ''}<button class="icon-button" data-cell-duplicate="${escape(cell.id)}" title="셀 복제" aria-label="셀 복제" ${canAddBelow ? '' : 'disabled'}>⧉</button><button class="icon-button" data-cell-export="${escape(cell.id)}" title="셀 내보내기" aria-label="셀 내보내기">⤓</button><button class="icon-button danger" data-cell-delete="${escape(cell.id)}" title="셀 삭제" aria-label="셀 삭제" ${canWrite ? '' : 'disabled'}>×</button></div></header><div class="nb-cell-body">${bodyHtml}</div><footer class="nb-cell-footer"><span>${type.label}${footerHint} · ID ${escape(shortItemId(cell.id))}</span><span data-nb-cursor="${escape(cell.id)}">${cell.kind === 'agent' ? '대화 0개' : 'Ln 1, Col 1'}</span></footer><div class="nb-cell-insert-zone" data-insert-after="${escape(cell.id)}" style="--insert-prox:0" role="group" aria-label="아래에 새 셀 추가"><div class="nb-cell-insert-line"></div><div class="nb-cell-insert-bar"><span class="nb-insert-prefix">＋ 아래에 추가</span>${insertButtonsHtml}</div></div>`;
      container.insertBefore(card, container.children[visibleIdx] || null);

      if (cell.kind === 'agent') {
        const feed = card.querySelector<HTMLElement>(`[data-chat-feed="${CSS.escape(cell.id)}"]`)!;
        const input = card.querySelector<HTMLTextAreaElement>(`[data-chat-input="${CSS.escape(cell.id)}"]`)!;
        const form = card.querySelector<HTMLFormElement>(`[data-chat-form="${CSS.escape(cell.id)}"]`)!;
        const aiToggle = card.querySelector<HTMLInputElement>(`[data-chat-ai-toggle="${CSS.escape(cell.id)}"]`)!;
        const sendOnlyBtn = card.querySelector<HTMLButtonElement>(`[data-chat-send-only="${CSS.escape(cell.id)}"]`)!;
        const resizeHandle = card.querySelector<HTMLElement>(`[data-cell-resize="${CSS.escape(cell.id)}"]`);

        if (!chatAiMode.has(cell.id)) { chatAiMode.set(cell.id, connected); }

        let pinnedToBottom = true;
        feed.addEventListener('scroll', () => {
          pinnedToBottom = (feed.scrollHeight - feed.scrollTop - feed.clientHeight) < 28;
          const hasTopOverflow = (feed.scrollHeight - feed.clientHeight) > 48;
          if (feed.scrollTop <= 24 && (feed.dataset.suppressAutoLoad !== '1' || hasTopOverflow)) {
            delete feed.dataset.suppressAutoLoad;
            loadOlderAgentMessages(cell.id);
          }
        });
        if (typeof ResizeObserver !== 'undefined') {
          const feedResizeObserver = new ResizeObserver(() => {
            if (!document.contains(feed)) { feedResizeObserver.disconnect(); return; }
            if (pinnedToBottom && feed.dataset.suppressAutoLoad !== '1') {
              scrollChatFeedToBottom(feed);
            }
          });
          feedResizeObserver.observe(feed);
        }
        if (resizeHandle) {
          resizeHandle.addEventListener('pointerdown', event => {
            event.stopPropagation();
            const startY = event.clientY;
            const startHeight = feed.getBoundingClientRect().height;
            startResizeDrag(event, resizeHandle, (_clientX, clientY) => {
              const next = Math.round(Math.max(200, Math.min(760, startHeight + (clientY - startY))));
              manualCellHeights.set(cell.id, next);
              applyChatFeedHeight(card!, cell.id);
            });
          });
          resizeHandle.addEventListener('dblclick', e => {
            e.stopPropagation();
            manualCellHeights.delete(cell.id);
            applyChatFeedHeight(card!, cell.id);
          });
        }
        const onComposeActive = (): void => {
          setPresenceFocus(cell.id);
          if (selected !== cell.id) {
            selected = cell.id;
            syncActiveCellCard();
            renderList();
            renderInspector();
            agentPanel.onSelectionChanged();
          }
          scrollChatFeedToBottom(feed);
        };
        input.addEventListener('pointerdown', onComposeActive);
        input.addEventListener('focus', onComposeActive);
        input.addEventListener('input', () => scrollChatFeedToBottom(feed));
        input.addEventListener('blur', () => {
          if (activePresenceCellId === cell.id) { setPresenceFocus(undefined); }
        });
        input.addEventListener('keydown', event => {
          if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            void submitAgentChatMessage(cell.id);
          }
        });
        aiToggle.addEventListener('change', () => {
          chatAiMode.set(cell.id, aiToggle.checked);
        });
        sendOnlyBtn.addEventListener('click', e => {
          e.stopPropagation();
          void submitAgentChatMessage(cell.id, true);
        });
        form.addEventListener('submit', event => {
          event.preventDefault();
          void submitAgentChatMessage(cell.id);
        });
        renderAgentChat(cell.id, { scrollToBottom: true });
      } else {
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
        } else if (cell.kind === 'html') {
          ed.addCommand(monaco.KeyMod.Shift | monaco.KeyCode.Enter, () => { renderHtmlCell(cell.id); });
        } else if (canPreview) {
          ed.addCommand(monaco.KeyMod.Shift | monaco.KeyCode.Enter, () => {
            previewCells.add(cell.id);
            const btn = card?.querySelector<HTMLButtonElement>(`[data-cell-preview="${CSS.escape(cell.id)}"]`);
            if (btn) { btn.textContent = '미리보기 닫기'; }
            renderCellPreview(cell.id);
          });
        }
        const updateHeight = (): void => {
          const manual = manualCellHeights.get(cell.id);
          const h = manual ?? Math.min(520, Math.max(96, ed.getContentHeight()));
          host.style.height = `${h}px`;
          ed.updateOptions({
            scrollbar: manual
              ? { vertical: 'auto', handleMouseWheel: true }
              : { vertical: 'hidden', handleMouseWheel: false },
          });
          ed.layout();
        };
        ed.onDidContentSizeChange(updateHeight);
        updateHeight();
        const resizeHandle = card.querySelector<HTMLElement>(`[data-cell-resize="${CSS.escape(cell.id)}"]`);
        if (resizeHandle) {
          resizeHandle.addEventListener('pointerdown', event => {
            event.stopPropagation();
            const startY = event.clientY;
            const startHeight = host.getBoundingClientRect().height;
            startResizeDrag(event, resizeHandle, (clientX, clientY) => {
              void clientX;
              const next = Math.round(Math.max(80, Math.min(820, startHeight + (clientY - startY))));
              manualCellHeights.set(cell.id, next);
              updateHeight();
            });
          });
          resizeHandle.addEventListener('dblclick', e => {
            e.stopPropagation();
            manualCellHeights.delete(cell.id);
            updateHeight();
          });
        }
        ed.onDidFocusEditorText(() => {
          setPresenceFocus(cell.id);
          if (selected !== cell.id) {
            selected = cell.id;
            syncActiveCellCard();
            renderList();
            renderInspector();
            agentPanel.onSelectionChanged();
          }
        });
        ed.onDidBlurEditorText(() => {
          if (activePresenceCellId === cell.id) {
            setPresenceFocus(undefined);
          }
        });
        ed.onDidChangeCursorPosition(event => {
          const pos = `Ln ${event.position.lineNumber}, Col ${event.position.column}`;
          const cursorEl = card!.querySelector(`[data-nb-cursor="${CSS.escape(cell.id)}"]`);
          if (cursorEl) { cursorEl.textContent = pos; }
          if (selected === cell.id) {
            const statusCursor = document.querySelector<HTMLElement>('#status-cursor');
            if (statusCursor) { statusCursor.textContent = pos; }
          }
        });
        cellEditors.set(cell.id, ed);
      }

      card.addEventListener('click', () => {
        if (selected !== cell.id) {
          selected = cell.id;
          syncActiveCellCard();
          renderList();
          renderInspector();
          agentPanel.onSelectionChanged();
        }
      });
      card.querySelectorAll<HTMLButtonElement>('[data-insert-kind]').forEach(btn => {
        btn.addEventListener('click', e => {
          e.stopPropagation();
          void addCell(btn.dataset.insertKind as CellKind, cell.id).catch(report);
        });
      });
      card.querySelector(`[data-cell-copy-id="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation();
        void navigator.clipboard.writeText(cell.id).then(() => notify(`객체 ID 복사됨: ${cell.id}`)).catch(() => { });
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
      card.querySelector(`[data-cell-render="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation(); selected = cell.id; renderHtmlCell(cell.id);
      });
      card.querySelector(`[data-cell-preview="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation();
        if (previewCells.has(cell.id)) { previewCells.delete(cell.id); } else { previewCells.add(cell.id); }
        const btn = e.currentTarget as HTMLButtonElement;
        btn.textContent = previewCells.has(cell.id) ? '미리보기 닫기' : '미리보기';
        renderCellPreview(cell.id);
      });
      card.querySelector(`[data-cell-agent="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation();
        selected = cell.id;
        syncActiveCellCard();
        renderList();
        renderInspector();
        agentPanel.onSelectionChanged();
        agentPanel.open();
      });
      card.querySelector(`[data-cell-duplicate="${CSS.escape(cell.id)}"]`)?.addEventListener('click', e => {
        e.stopPropagation(); void duplicateCell(cell.id).catch(report);
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
      if (cell.kind === 'agent') {
        const parsed = parseAgentMessages(cell.text);
        const prev = chatLastSnapshot.get(cell.id);
        const tail = parsed.messages[parsed.messages.length - 1];
        const tailKey = tail ? `${tail.id}:${tail.body.length}:${chatPendingAi.has(cell.id) ? 1 : 0}` : '';
        if (!prev || prev.count !== parsed.messages.length || prev.tailKey !== tailKey) {
          renderAgentChat(cell.id, { scrollToBottom: true });
        }
      }
      updateCellCardMeta(cell.id);
    }
  });
  syncActiveCellCard();
  syncPresenceUI();
}
async function moveCellOrder(id: string, direction: -1 | 1): Promise<void> {
  if (workspace) {
    try {
      await workspace.moveCell(id, direction);
      cells = workspace.cells;
      renderNotebook();
      renderList();
      scheduleVmCellSync();
    } catch (error) { throw error; }
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
  syncWorkbenchChrome();
  renderBottomDock();
}
function openNotebook(): void {
  viewMode = 'notebook';
  if (workspace) { sessionStorage.setItem(`irade.research.view:${workspace.client.config.canvasId}`, 'notebook'); }
  $('#overview').hidden = true;
  $('#cell-view').hidden = false;
  if (!bottomDockUserToggled && window.innerWidth > 760) {
    bottomDockOpen = true;
  }
  renderNotebook();
  renderList();
  renderInspector();
  renderBottomDock();
}
function selectCell(id: string, scrollBlock: ScrollLogicalPosition = 'center'): void {
  const cell = cells.find(cell => cell.id === id); if (!cell) { return; }
  selected = id;
  openNotebook();
  agentPanel.onSelectionChanged();
  requestAnimationFrame(() => {
    const card = document.querySelector<HTMLElement>(`[data-nb-cell="${CSS.escape(id)}"]`);
    card?.scrollIntoView({ behavior: 'smooth', block: scrollBlock });
    if (cell.kind === 'agent') {
      card?.querySelector<HTMLTextAreaElement>(`[data-chat-input="${CSS.escape(id)}"]`)?.focus();
    } else {
      cellEditors.get(id)?.focus();
    }
  });
}
function overview(): void {
  overviewOpen = true;
  viewMode = 'overview';
  if (workspace) { sessionStorage.setItem(`irade.research.view:${workspace.client.config.canvasId}`, 'overview'); }
  $('#overview').hidden = false;
  $('#cell-view').hidden = true;
  if (!bottomDockUserToggled) {
    bottomDockOpen = false;
  }
  renderList();
  renderInspector();
  renderBottomDock();
}
function enterEmptyView(): void {
  overviewOpen = false;
  viewMode = 'empty';
  $('#overview').hidden = true;
  $('#cell-view').hidden = true;
  const notebookCells = document.querySelector<HTMLElement>('#notebook-cells');
  if (notebookCells) { notebookCells.innerHTML = ''; }
  if (!bottomDockUserToggled) {
    bottomDockOpen = false;
  }
  renderList();
  renderInspector();
  renderBottomDock();
}
function renderInspector(): void {
  document.querySelectorAll('.inspector .inspector-tabs > button').forEach(button => button.classList.toggle('selected', button.getAttribute('data-action') === inspectorTab));
  if (inspectorTab === 'proof-db') {
    $('#inspector-content').innerHTML = renderProofDbInspectorHtml(proofKnowledgeSnapshot, proofKnowledgeLoading, proofKnowledgeError);
    return;
  }
  if (inspectorTab === 'activity') {
    $('#inspector-content').innerHTML = '<h3 class="context-heading">WORKSPACE ACTIVITY</h3>' + (activities.length ? activities.map(event => `<div class="activity-event">${escape(event.message)}<small>${event.time}</small></div>`).join('') : '<p class="context-copy">이 탭에서 연결하고 편집한 기록이 여기에 표시됩니다.</p>'); return;
  }
  const cell = cells.find(cell => cell.id === selected);
  const cellOut = cell ? cellOutputs.get(cell.id) : undefined;
  const proofStateHtml = cell && ['lean', 'rocq'].includes(cell.kind)
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
  const cellText = cell ? (models.get(cell.id)?.getValue() ?? cell.text) : '';
  const cellMetricsHtml = cell
    ? `<div class="context-block"><h3 class="context-heading">CELL METRICS</h3><div class="context-stat"><span>유형 / 언어</span><b>${escape(cellTypes[cell.kind].short)}</b></div><div class="context-stat"><span>줄 수</span><b>${cellText ? cellText.split('\n').length : 0} lines</b></div><div class="context-stat"><span>글자 수</span><b>${cellText.length} chars</b></div></div>`
    : '<div class="context-flow"><div class="flow-step"><span class="step-icon mint">✧</span><span>탐색하고 대화하기<small>AGENT CONVERSATIONS</small></span></div><div class="flow-step"><span class="step-icon purple">∀</span><span>논리로 증명하기<small>LEAN 4 / ROCQ PROOFS</small></span></div><div class="flow-step"><span class="step-icon blue">λ</span><span>코드로 실험하기<small>PYTHON CELLS</small></span></div><div class="flow-step"><span class="step-icon sand">≡</span><span>지식으로 정리하기<small>MARKDOWN / HTML</small></span></div></div>';
  const typeCounts = { agent: cells.filter(cell => cell.kind === 'agent').length, proof: cells.filter(cell => ['lean', 'rocq'].includes(cell.kind)).length, code: cells.filter(cell => cell.kind === 'python').length, document: cells.filter(cell => ['markdown', 'html'].includes(cell.kind)).length };
  $('#inspector-content').innerHTML = `<h3 class="context-heading">${cell ? 'CELL CONTEXT' : 'WORKSPACE CONTEXT'}</h3><h2 class="context-title">${escape(cell ? cellTypes[cell.kind].label : '연구의 흐름을\n하나로 연결하세요.').replace('\n', '<br>')}</h2><p class="context-copy">${cell ? escape(cellTypes[cell.kind].description) : '아이디어를 탐색하고, 가설을 증명하고,<br>결과를 코드와 문서로 남기는 공간.'}</p>${proofStateHtml}${cellAclHtml}${cellMetricsHtml}<div class="context-block"><h3 class="context-heading">${cell ? 'THIS WORKSPACE' : 'WORKSPACE OVERVIEW'}</h3><div class="context-stat"><span>대화 기록</span><b>${String(typeCounts.agent).padStart(2, '0')}</b></div><div class="context-stat"><span>형식 증명</span><b>${String(typeCounts.proof).padStart(2, '0')}</b></div><div class="context-stat"><span>Python 코드</span><b>${String(typeCounts.code).padStart(2, '0')}</b></div><div class="context-stat"><span>연구 문서</span><b>${String(typeCounts.document).padStart(2, '0')}</b></div></div>${peersHtml}<div class="context-block"><h3 class="context-heading">COLLABORATION</h3><p class="context-copy">${demo ? '로컬 체험 모드입니다.<br>내용은 현재 브라우저 탭에만 저장됩니다.' : connected ? 'Workspace 셀 권한과 CRDT를 사용해<br>실시간으로 동기화 중입니다.' : '공유 Workspace를 연결하면<br>함께 셀을 편집할 수 있습니다.'}</p><span class="context-tag">${demo ? 'LOCAL SESSION' : connected ? 'WORKSPACE SYNC' : 'SHARED WORKSPACE'}</span></div>`;
  if (cell) {
    document.querySelector('#inspector-perm-btn')?.addEventListener('click', () => openCellPermissionModal(cell.id));
  }
}
function persistScratch(): void { sessionStorage.setItem(demoKey, JSON.stringify(cells)); $('#cell-save').textContent = '현재 탭에 저장됨'; }
async function startDemo(): Promise<void> {
  if (workspace?.pending) { await workspace.flush(); }
  ++loadGeneration; setPresenceFocus(undefined); workspace?.dispose(); workspace = undefined; connected = false; clearModels(); demo = true; workspaceName = 'Research playground'; selected = undefined; selfProfile = { name: '나 (체험)' };
  proofKnowledgeSnapshot = undefined; proofKnowledgeError = undefined; void refreshProofKnowledge();
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
    : (['markdown', 'agent', 'lean', 'rocq', 'python', 'html'] as CellKind[]).map((kind, index) => ({
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
  persistScratch(); const url = new URL(location.href); url.searchParams.delete('workspace'); history.replaceState(null, '', url);
  $('#workspace-subtitle').textContent = '로컬 체험 · 현재 탭에만 저장'; $('#workspace-mode').textContent = 'Local Playground'; $('#status').textContent = '로컬 체험 · 공유되지 않음'; $('#connection-label').textContent = 'LOCAL SESSION'; $('#sync-description').textContent = '체험 내용은 현재 탭에만 저장'; $('#permission-label').textContent = '로컬 편집';
  openNotebook(); agentPanel.onWorkspaceChanged(); log('로컬 체험 workspace를 열었습니다.'); notify('로컬 체험입니다. 공동 편집하려면 공유 Workspace를 연결하세요.');
}
let addingCell = false;
async function addCell(kind?: CellKind, afterId?: string, initialText?: string, skipVmSync = false): Promise<Cell | undefined> {
  if (!workspace && !demo) {
    showModal('연구 공간을 선택하세요', '<p class="modal-copy">공유 Workspace에서 실시간으로 협업하거나 로컬 체험으로 셀을 살펴보세요.</p><div class="choice-list"><button class="choice" data-action="open"><span class="type-icon mint">↔</span><span><strong>공유 workspace 열기</strong><small>ORCID로 로그인하고 공동 편집</small></span><span class="choice-arrow">↗</span></button><button class="choice" data-action="demo"><span class="type-icon blue">λ</span><span><strong>로컬 체험 시작</strong><small>계정 없이 현재 탭에서 셀 편집 체험</small></span><span class="choice-arrow">→</span></button></div>'); return undefined;
  }
  if (workspace && !workspace.client.canWrite) { notify('이 workspace는 읽기 전용입니다.', true); return undefined; }
  if (!kind) {
    showModal('새로운 연구 셀', '<p class="modal-copy">다음 아이디어에 맞는 셀을 선택하세요.</p><div class="choice-list">' + (Object.keys(cellTypes) as CellKind[]).map(kind => `<button class="choice" data-add-kind="${kind}"><span class="type-icon ${cellTypes[kind].color}">${cellTypes[kind].icon}</span><span><strong>${cellTypes[kind].label}</strong><small>${cellTypes[kind].description}</small></span><span class="choice-arrow">＋</span></button>`).join('') + '</div>');
    document.querySelectorAll<HTMLButtonElement>('[data-add-kind]').forEach(button => button.addEventListener('click', () => { modal.close(); void addCell(button.dataset.addKind as CellKind, afterId).catch(report); })); return undefined;
  }
  if (addingCell) { return undefined; }
  const textToUse = initialText ?? starters[kind];
  let cell: Cell;
  if (workspace) {
    addingCell = true;
    const buttons = document.querySelectorAll<HTMLButtonElement>('[data-quick-add], [data-action="add"], [data-insert-kind], [data-cell-duplicate]');
    buttons.forEach(button => { button.disabled = true; });
    $('#cell-save').textContent = '새 셀 생성 중…';
    try {
      cell = await workspace.add(kind, textToUse, afterId ? { afterId } : undefined);
      cells = workspace.cells;
      if (!skipVmSync) { scheduleVmCellSync(); }
    } finally {
      addingCell = false;
      buttons.forEach(button => { button.disabled = !workspace?.client.canWrite; });
      if (!workspace.pending) { $('#cell-save').textContent = connected ? 'Workspace 동기화됨' : '로컬 변경 유지'; }
    }
  } else {
    const insertIdx = afterId ? cells.findIndex(item => item.id === afterId) : -1;
    cell = { id: `demo-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`, kind, path: '', text: textToUse, title: cellTitle(kind, textToUse), permission: ['local'], frameOrder: 0, locked: false, canWrite: true, canExecute: true };
    if (insertIdx >= 0 && insertIdx < cells.length - 1) {
      cells.splice(insertIdx + 1, 0, cell);
    } else {
      cells.push(cell);
    }
    cells.forEach((item, index) => { item.frameOrder = index * 10; });
    persistScratch();
  }
  if (filter !== 'all' && !visibleCells().some(item => item.id === cell.id)) {
    filter = 'all';
    document.querySelectorAll('.rail-button').forEach(button => button.classList.toggle('active', button.getAttribute('data-action') === 'workspace'));
  }
  log(`${cellTypes[kind].label} 셀을${afterId ? ' 바로 아래에' : ''} 만들었습니다.`); selectCell(cell.id, afterId ? 'nearest' : 'end');
  return cell;
}
async function duplicateCell(cellId: string): Promise<void> {
  const orig = cells.find(item => item.id === cellId);
  if (!orig) { return; }
  const sourceText = models.get(cellId)?.getValue() ?? orig.text;
  const dup = await addCell(orig.kind, cellId, sourceText);
  if (dup) { notify('셀을 복제했습니다.'); }
}
async function restoreLastDeletedCell(): Promise<void> {
  const snap = lastDeletedCell;
  if (!snap) { return; }
  lastDeletedCell = undefined;
  const anchor = snap.afterId && cells.some(c => c.id === snap.afterId) ? snap.afterId : undefined;
  await addCell(snap.kind, anchor, snap.text);
  log(`삭제된 “${snap.title}” 셀을 복원했습니다.`);
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
  $('#confirm-delete').addEventListener('click', event => {
    (event.currentTarget as HTMLButtonElement).disabled = true; void (async () => {
      const idx = cells.findIndex(item => item.id === cell.id);
      const prevAfterId = idx > 0 ? cells[idx - 1].id : undefined;
      const backupText = models.get(cell.id)?.getValue() ?? cell.text;
      lastDeletedCell = { kind: cell.kind, text: backupText, afterId: prevAfterId, title: cell.title };
      if (activePresenceCellId === cell.id) { setPresenceFocus(undefined); }
      if (workspace) { deletingCellId = cell.id; try { await workspace.delete(cell.id); cells = workspace.cells; scheduleVmCellSync(); } finally { deletingCellId = undefined; } } else { cells = cells.filter(item => item.id !== cell.id); persistScratch(); }
      const ed = cellEditors.get(cell.id);
      if (ed) { ed.setModel(null); ed.dispose(); cellEditors.delete(cell.id); }
      manualCellHeights.delete(cell.id);
      clearTimeout(previewTimers.get(cell.id));
      previewTimers.delete(cell.id);
      previewCells.delete(cell.id);
      cellOutputs.delete(cell.id);
      cellExecOrders.delete(cell.id);
      chatVisibleCounts.delete(cell.id);
      chatPendingAi.delete(cell.id);
      chatAiMode.delete(cell.id);
      chatDemoSpeaker.delete(cell.id);
      chatLastSnapshot.delete(cell.id);
      chatFeedActivePeers.delete(cell.id);
      vmCellPaths.delete(cell.id);
      models.get(cell.id)?.dispose(); models.delete(cell.id);
      if (selected === cell.id) { selected = cells[0]?.id; }
      renderList(); renderInspector();
      agentPanel.onSelectionChanged();
      if (viewMode === 'notebook') { renderNotebook(); }
      modal.close(); log('셀을 삭제했습니다.');
      notifyWithUndo(`“${cell.title}” 셀을 삭제했습니다.`, '되돌리기', restoreLastDeletedCell);
    })().catch(error => { report(error); (event.target as HTMLButtonElement).disabled = false; });
  });
}
function exportCell(): void {
  const cell = cells.find(cell => cell.id === selected); if (!cell) { return; }
  const value = models.get(cell.id)?.getValue() ?? cell.text;
  download(value, `${cell.title.replace(/[^\p{L}\p{N}_ -]/gu, '').slice(0, 60) || 'cell'}.${cellTypes[cell.kind].extension}`);
}
function download(value: string, name: string): void {
  const url = URL.createObjectURL(new Blob([value], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function message(): void {
  const cell = cells.find(cell => cell.id === selected && cell.kind === 'agent') || cells.find(cell => cell.kind === 'agent');
  if (!cell) {
    notify('먼저 AI 대화 셀을 만들거나 선택하세요.');
    return;
  }
  selectCell(cell.id, 'center');
}
function help(): void { showModal('연결된 연구를 시작하세요', '<div class="help-grid"><p><b>Workspace</b>는 공동 연구 공간입니다. 같은 workspace를 열면 같은 셀을 편집하고 변경을 실시간으로 공유합니다.</p><p><b>AI 에이전트</b>는 EXAONE 2.5와 MCP 도구로 Workspace 셀을 생성·수정하며 API별 잔여 한도를 표시합니다.<br><b>Lean 4 / Rocq</b>는 형식 증명 소스를 검증합니다.<br><b>Python</b>은 격리 실행 VM에서 실험 코드를 실행합니다.<br><b>Markdown / HTML</b>은 문서와 미리보기를 제공합니다.</p><p><kbd>Ctrl K</kbd> 검색 · <kbd>Alt N</kbd> 새 셀<br><kbd>Ctrl S</kbd> 저장 · <kbd>Ctrl Shift F</kbd> 집중 모드</p></div>'); }
function settings(initialTab: 'general' | 'monitoring' = 'general'): void {
  const savedLines = localStorage.getItem('irade.lineNumbers') === 'off' ? 'off' : 'on';
  const savedSize = Number(localStorage.getItem('irade.fontSize') || 14);
  const isMonitoring = initialTab === 'monitoring';
  showModal(
    '연구 환경 설정',
    `<div class="settings-tabs" role="tablist" aria-label="설정 항목">
      <button type="button" class="settings-tab ${isMonitoring ? '' : 'selected'}" role="tab" aria-selected="${!isMonitoring}" data-settings-tab="general">일반 설정</button>
      <button type="button" class="settings-tab ${isMonitoring ? 'selected' : ''}" role="tab" aria-selected="${isMonitoring}" data-settings-tab="monitoring"><span class="monitor-live-dot" aria-hidden="true"></span><span>모니터링</span></button>
    </div>
    <div id="settings-general-pane" class="settings-pane" ${isMonitoring ? 'hidden' : ''}>
      <p class="modal-copy">설정은 이 브라우저에 적용됩니다.</p>
      <label class="settings-row">편집기 글자 크기<select id="font-size">${[12, 14, 16, 18, 20].map(size => `<option value="${size}" ${savedSize === size ? 'selected' : ''}>${size}px</option>`).join('')}</select></label>
      <label class="settings-row">편집기 줄 번호<select id="line-numbers"><option value="on" ${savedLines === 'on' ? 'selected' : ''}>표시</option><option value="off" ${savedLines === 'off' ? 'selected' : ''}>숨기기</option></select></label>
      <div class="form-actions"><button class="button ghost" data-action="help">사용 안내</button><button class="button primary" id="save-settings">적용</button></div>
    </div>
    <div id="settings-monitor-pane" class="monitor-pane" ${isMonitoring ? '' : 'hidden'}></div>`,
    isMonitoring,
  );

  const generalPane = $<HTMLElement>('#settings-general-pane');
  const monitorPane = $<HTMLElement>('#settings-monitor-pane');
  const activateTab = (tab: 'general' | 'monitoring'): void => {
    const showMonitor = tab === 'monitoring';
    modal.classList.toggle('modal-wide', showMonitor);
    generalPane.hidden = showMonitor;
    monitorPane.hidden = !showMonitor;
    document.querySelectorAll<HTMLButtonElement>('[data-settings-tab]').forEach(btn => {
      const selectedTab = btn.dataset.settingsTab === tab;
      btn.classList.toggle('selected', selectedTab);
      btn.setAttribute('aria-selected', selectedTab ? 'true' : 'false');
    });
    if (showMonitor) {
      if (!perfMonitor) {
        perfMonitor = new PerfMonitorController(monitorPane, {
          getCellCount: () => cells.length,
          getEditorCount: () => cellEditors.size,
          getPreviewCount: () => previewCells.size,
          getConnectionLabel: () => document.querySelector<HTMLElement>('#connection-label')?.textContent?.trim() || '연결 대기',
        });
      }
      perfMonitor.start();
      syncMonitorTriggerState(true);
    } else {
      perfMonitor?.stop();
      syncMonitorTriggerState(false);
    }
  };

  document.querySelectorAll<HTMLButtonElement>('[data-settings-tab]').forEach(btn => {
    btn.addEventListener('click', () => {
      activateTab(btn.dataset.settingsTab === 'monitoring' ? 'monitoring' : 'general');
    });
  });

  $('#save-settings').addEventListener('click', () => {
    const size = Number($<HTMLSelectElement>('#font-size').value);
    const lines = $<HTMLSelectElement>('#line-numbers').value as 'on' | 'off';
    localStorage.setItem('irade.fontSize', String(size));
    localStorage.setItem('irade.lineNumbers', lines);
    for (const ed of cellEditors.values()) { ed.updateOptions({ fontSize: size, lineNumbers: lines }); }
    modal.close();
    notify('편집기 설정을 적용했습니다.');
  });

  if (isMonitoring) {
    activateTab('monitoring');
  }
}
async function share(): Promise<void> {
  if (!workspace) { notify('공유하려면 Workspace를 먼저 연결하세요.'); return; }
  const url = new URL(location.pathname, origin); url.searchParams.set('workspace', String(workspace.client.config.canvasId));
  try { await navigator.clipboard.writeText(String(url)); notify('Workspace 링크를 복사했습니다. 참여자는 자신의 ORCID 로그인과 접근 권한이 필요합니다.'); }
  catch { showModal('Workspace 공유 링크', `<p class="modal-copy">참여자는 자신의 ORCID 로그인과 접근 권한이 필요합니다.</p><label class="field">공유 링크<input readonly value="${escape(String(url))}"></label>`); }
}
async function action(name: string): Promise<void> {
  if (name === 'open') { await openWorkspace(); }
  else if (name === 'login') {
    if (token) {
      const profile = await api<{ name: string; orcid: string; issuer: string }>('/api/irade/auth/me');
      showModal('연구자 계정', `<p class="modal-copy">${escape(profile.name)}</p><a class="orcid-profile" href="${escape(profile.issuer)}/${escape(profile.orcid)}" target="_blank" rel="noopener noreferrer"><img src="/assets/orcid-id.svg" width="24" height="24" alt="ORCID iD"> ${escape(profile.issuer)}/${escape(profile.orcid)}</a><div class="form-actions"><button class="button ghost" data-action="logout">이 탭에서 로그아웃</button></div>`);
    } else { await signIn(); }
  }
  else if (name === 'logout') {
    modal.close();
    if (workspace?.pending) { await workspace.flush(); }
    const logoutId = workspace?.client.config.canvasId;
    ++loadGeneration; setPresenceFocus(undefined); workspace?.dispose(); workspace = undefined; token = undefined; void endSession(); sessionStorage.removeItem('irade.research.lastWorkspace'); if (logoutId) { sessionStorage.removeItem(`irade.research.view:${logoutId}`); } selfProfile = { name: '연구자' }; updateAccount(); clearModels(); cells = []; selected = undefined; connected = false; workspaceName = '나의 연구 공간'; demo = false;
    const url = new URL(location.href); url.searchParams.delete('workspace'); url.searchParams.delete('workspaces'); history.replaceState(null, '', url); overview(); agentPanel.onWorkspaceChanged(); $('#workspace-subtitle').textContent = 'workspace를 연결하세요'; $('#status').textContent = '로그아웃됨'; $('#connection-label').textContent = '연결 대기'; $('#permission-label').textContent = ''; notify('로그아웃했습니다.');
  }
  else if (name === 'demo') { modal.close(); await startDemo(); }
  else if (name === 'run-all') { await runAllCells(); }
  else if (name === 'add') { await addCell(); }
  else if (name === 'overview') { overview(); }
  else if (name === 'notebook') { openNotebook(); }
  else if (name === 'close-overview') {
    overviewOpen = false;
    if (workspace || demo) { openNotebook(); }
    else { enterEmptyView(); }
  }
  else if (name === 'close-workspace') {
    if (workspace?.pending) { await workspace.flush(); }
    const closingId = workspace?.client.config.canvasId;
    ++loadGeneration; setPresenceFocus(undefined); workspace?.dispose(); workspace = undefined;
    clearModels(); cells = []; selected = undefined; connected = false; demo = false; workspaceName = '나의 연구 공간';
    selfProfile = { name: token ? selfProfile.name : '연구자', ...(token && selfProfile.uid ? { uid: selfProfile.uid } : {}) };
    sessionStorage.removeItem('irade.research.lastWorkspace');
    if (closingId) { sessionStorage.removeItem(`irade.research.view:${closingId}`); }
    const url = new URL(location.href); url.searchParams.delete('workspace'); url.searchParams.delete('workspaces'); history.replaceState(null, '', url);
    overview(); agentPanel.onWorkspaceChanged();
    proofKnowledgeSnapshot = undefined; proofKnowledgeError = undefined;
    $('#workspace-subtitle').textContent = 'workspace를 연결하세요';
    $('#status').textContent = token ? '연결 대기' : '로그아웃됨';
    $('#connection-label').textContent = '연결 대기';
    $('#permission-label').textContent = '';
    notify('Workspace 연결을 닫았습니다.');
  }
  else if (name === 'search') { search(); }
  else if (name === 'focus') { document.body.classList.toggle('focus'); syncWorkbenchChrome(); }
  else if (name === 'theme') { applyTheme(currentTheme() === 'light' ? 'dark' : 'light'); }
  else if (name === 'sidebar') { document.body.classList.toggle('sidebar-hidden'); syncWorkbenchChrome(); }
  else if (name === 'bottom-panel') { bottomDockUserToggled = true; bottomDockOpen = !bottomDockOpen; renderBottomDock(); syncWorkbenchChrome(); }
  else if (name === 'inspector') {
    const rightOpen = document.body.classList.contains('agent-open') || !document.body.classList.contains('inspector-hidden');
    if (rightOpen) {
      if (document.body.classList.contains('agent-open')) {
        rightPaneMode = 'agent';
        agentPanel.close();
      } else {
        rightPaneMode = 'inspector';
      }
      document.body.classList.add('inspector-hidden');
    } else {
      document.body.classList.remove('inspector-hidden');
      if (rightPaneMode === 'agent') {
        agentPanel.open({ focus: false });
      }
    }
    syncWorkbenchChrome();
  }
  else if (name === 'delete') { await deleteCell(); }
  else if (name === 'export') { exportCell(); }
  else if (name === 'message') { message(); }
  else if (name === 'help') { help(); }
  else if (name === 'settings') { settings('general'); }
  else if (name === 'monitoring') { settings('monitoring'); }
  else if (name === 'share') { await share(); }
  else if (name === 'agent') {
    rightPaneMode = 'agent';
    document.body.classList.remove('inspector-hidden');
    agentPanel.open({ focus: true });
    syncWorkbenchChrome();
  }
  else if (name === 'context' || name === 'activity' || name === 'proof-db') {
    rightPaneMode = 'inspector';
    inspectorTab = name;
    document.body.classList.remove('inspector-hidden');
    agentPanel.close();
    renderInspector();
    if (name === 'proof-db') {
      void refreshProofKnowledge(true);
    }
    syncWorkbenchChrome();
  }
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
  if (target.closest('[data-open-proof-db]')) {
    void action('proof-db').catch(report);
    return;
  }
  if (target.closest('[data-open-proof-db-modal]')) {
    const snap = proofKnowledgeSnapshot ?? {
      workspaceKey: currentProofWorkspaceScope().workspaceKey,
      theorems: [],
      definitions: [],
      lemmaRelations: [],
      definitionRelations: [],
    };
    showModal('형식 증명 관계형 데이터베이스 (MS SQL Server)', renderProofDbModalHtml(snap), true);
    void refreshProofKnowledge(true);
    return;
  }
  if (target.closest('[data-refresh-proof-db]')) {
    void refreshProofKnowledge(false);
    return;
  }
  const delThmBtn = target.closest<HTMLElement>('[data-delete-theorem-id]');
  if (delThmBtn) {
    const theoremId = Number(delThmBtn.dataset.deleteTheoremId);
    if (Number.isFinite(theoremId) && theoremId > 0) {
      void deleteVerifiedTheoremFromDb(theoremId, token)
        .then(async () => {
          notify('DB에서 정리와 연결 관계를 삭제했습니다.');
          await refreshProofKnowledge(true);
        })
        .catch(report);
    }
    return;
  }
  if (target.closest('[data-open-dock-games]')) {
    switchBottomDockTab('games');
    return;
  }
  const dockTab = target.closest<HTMLButtonElement>('button[data-dock-tab]');
  if (dockTab) {
    const rawTab = dockTab.dataset.dockTab;
    const nextTab = rawTab === 'activity' ? 'activity' : rawTab === 'games' ? 'games' : 'output';
    switchBottomDockTab(nextTab);
    return;
  }
  if (target.closest('#bottom-dock-clear')) {
    if (bottomDockTab === 'games') {
      ensureDockArcade()?.resetCurrent(false);
    } else if (bottomDockTab === 'activity') {
      activities.length = 0;
      if (inspectorTab === 'activity') { renderInspector(); }
    } else if (lastOutputCellId) {
      cellOutputs.delete(lastOutputCellId);
      renderCellOutput(lastOutputCellId);
    }
    renderBottomDock();
    return;
  }
  const quickAdd = target.closest<HTMLButtonElement>('button[data-quick-add]');
  if (quickAdd) { void addCell(quickAdd.dataset.quickAdd as CellKind).catch(report); return; }
  const actionEl = target.closest<HTMLElement>('[data-action]');
  if (actionEl) {
    if (actionEl.dataset.action === 'close-overview' || actionEl.dataset.action === 'close-workspace') { event.stopPropagation(); }
    void action(actionEl.dataset.action!).catch(report);
  }
});
document.addEventListener('keydown', event => {
  if (modal.open || event.isComposing) { return; }
  const mod = event.ctrlKey || event.metaKey;
  const code = event.code || '';
  if (mod && code === 'KeyK') { event.preventDefault(); search(); }
  else if (mod && code === 'KeyS') { event.preventDefault(); void (workspace ? workspace.flush().then(() => notify('모든 변경을 저장했습니다.')) : Promise.resolve(persistScratch())).catch(report); }
  else if (mod && event.shiftKey && code === 'KeyF') { event.preventDefault(); document.body.classList.toggle('focus'); }
  else if (event.altKey && code === 'KeyN') { event.preventDefault(); void addCell().catch(report); }
});
window.addEventListener('beforeunload', event => { if (workspace?.pending) { event.preventDefault(); event.returnValue = ''; } });

async function applyVmExternalChanges(items: Array<{ id: string; text: string; sha256?: string }>): Promise<void> {
  if (!workspace || !items.length) { return; }
  syncingFromVm = true;
  try {
    let changed = false;
    for (const item of items) {
      const cell = cells.find(c => c.id === item.id);
      if (!cell || !cell.canWrite || typeof item.text !== 'string') { continue; }
      if (item.sha256) { lastVmCellHashes.set(item.id, item.sha256); }
      if (cell.text === item.text) { continue; }
      workspace.edit(cell.id, item.text);
      const model = models.get(cell.id);
      if (model && model.getValue() !== item.text) {
        editingRemote = true;
        try { model.setValue(item.text); } finally { editingRemote = false; }
      }
      updateCellCardMeta(cell.id);
      changed = true;
    }
    if (changed) {
      cells = workspace.cells;
      renderList();
      if (viewMode === 'notebook') { renderNotebook(); }
      await workspace.flush().catch(() => { });
      log('VM/IDE 파일 변경 내용을 셀에 반영했습니다.');
    }
  } finally {
    syncingFromVm = false;
  }
}

async function pushCellsToVm(force = true): Promise<void> {
  if (!workspace || !token || demo || syncingFromVm || vmSyncInFlight) { return; }
  const id = workspace.client.config.canvasId;
  if (!Number.isSafeInteger(id) || id <= 0) { return; }
  vmSyncInFlight = true;
  vmSyncState = 'syncing';
  vmSyncError = '';
  renderVmSyncBadge();
  try {
    const resp = await fetch(`${origin}/agdd/api/workspaces/irade-${id}/vm/cells/sync`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        cells: workspace.cells.map(c => ({
          id: c.id,
          kind: c.kind,
          title: c.title,
          text: c.text,
          frameOrder: c.frameOrder,
          ...(vmCellPaths.has(c.id) ? { path: vmCellPaths.get(c.id) } : {}),
        })),
        delete_missing: true,
        force,
      }),
      signal: AbortSignal.timeout(15000),
    });
    if (!resp.ok) {
      vmSyncState = 'offline';
      vmSyncError = `HTTP ${resp.status}`;
      return;
    }
    const data = await resp.json() as {
      manifest?: Record<string, { path?: string; sha256?: string }>;
      external_changes?: Array<{ id: string; text: string; sha256?: string }>;
    };
    if (data.manifest) {
      for (const [cid, info] of Object.entries(data.manifest)) {
        if (info?.sha256) { lastVmCellHashes.set(cid, info.sha256); }
        if (info?.path) { vmCellPaths.set(cid, info.path); }
      }
    }
    vmSyncState = 'synced';
    if (data.external_changes?.length) {
      await applyVmExternalChanges(data.external_changes);
    }
  } catch (err) {
    vmSyncState = 'offline';
    vmSyncError = err instanceof Error ? err.message : 'offline';
  } finally {
    vmSyncInFlight = false;
    renderVmSyncBadge();
  }
}

async function pullCellsFromVm(): Promise<void> {
  if (!workspace || !token || demo || syncingFromVm || vmSyncInFlight) { return; }
  const id = workspace.client.config.canvasId;
  if (!Number.isSafeInteger(id) || id <= 0) { return; }
  try {
    const resp = await fetch(`${origin}/agdd/api/workspaces/irade-${id}/vm/cells`, {
      credentials: 'same-origin',
      signal: AbortSignal.timeout(10000),
    });
    if (!resp.ok) {
      vmSyncState = 'offline';
      vmSyncError = `HTTP ${resp.status}`;
      renderVmSyncBadge();
      return;
    }
    const data = await resp.json() as {
      cells?: Array<{ id: string; path?: string; text: string; sha256?: string }>;
      untracked?: Array<{ path: string; kind: CellKind; text: string; sha256: string }>;
    };
    vmSyncState = 'synced';
    vmSyncError = '';
    for (const item of data.cells || []) {
      if (item.path) { vmCellPaths.set(item.id, item.path); }
    }
    const updates = (data.cells || []).filter(item => {
      const local = cells.find(c => c.id === item.id);
      return local && local.text !== item.text && item.sha256 !== lastVmCellHashes.get(item.id);
    });
    if (updates.length) {
      await applyVmExternalChanges(updates);
    }
    const trackedPaths = new Set(vmCellPaths.values());
    const freshUntracked = (data.untracked || []).filter(u =>
      u.path && !trackedPaths.has(u.path) && !importedVmUntrackedKeys.has(`${u.path}:${u.sha256}`),
    );
    if (freshUntracked.length && workspace.client.canWrite) {
      let imported = 0;
      for (const u of freshUntracked) {
        const key = `${u.path}:${u.sha256}`;
        importedVmUntrackedKeys.add(key);
        const created = await addCell(u.kind, undefined, u.text, true);
        if (created) {
          vmCellPaths.set(created.id, u.path);
          lastVmCellHashes.set(created.id, u.sha256);
          imported += 1;
        }
      }
      pendingVmUntracked = [];
      if (imported > 0) {
        await pushCellsToVm(false);
        notify(`VM cells/ 폴더의 새 파일 ${imported}개를 셀로 가져왔습니다.`);
      }
    } else {
      pendingVmUntracked = freshUntracked;
    }
    renderVmSyncBadge();
  } catch (err) {
    vmSyncState = 'offline';
    vmSyncError = err instanceof Error ? err.message : 'offline';
    renderVmSyncBadge();
  }
}

document.querySelector('#vm-sync-badge')?.addEventListener('click', () => {
  void (async () => {
    await pullCellsFromVm();
    await pushCellsToVm(true);
    if (vmSyncState === 'synced') {
      notify('VM /workspace/cells와 동기화했습니다.');
    } else {
      notify('AGDD VM 동기화 서버에 연결할 수 없습니다.', true);
    }
  })().catch(report);
});

function scheduleVmCellSync(delayMs = 500): void {
  if (!workspace || !token || demo || syncingFromVm) { return; }
  clearTimeout(vmCellSyncTimer);
  vmCellSyncTimer = setTimeout(() => { void pushCellsToVm(true); }, delayMs);
}

window.addEventListener('message', event => {
  if (event.origin === origin && event.data?.type === 'irade:vm-cells-updated') {
    void pullCellsFromVm();
  }
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') { void pullCellsFromVm(); }
});
const groups: { kinds: CellKind[]; title: string; description: string; icon: string; color: string }[] = [
  { kinds: ['agent'], title: '에이전트 대화', description: '질문과 추론, 대화의 맥락을 보관하세요.', icon: '✧', color: 'mint' },
  { kinds: ['lean', 'rocq'], title: 'Lean 4 · Rocq 증명', description: '가설에서 정리까지, 논리를 연결하세요.', icon: '∀', color: 'purple' },
  { kinds: ['python'], title: 'Python 코드', description: '작은 실험으로 아이디어를 구체화하세요.', icon: 'λ', color: 'blue' },
  { kinds: ['markdown', 'html'], title: 'Markdown · HTML 문서', description: '관찰과 발견을 지식으로 남기세요.', icon: '≡', color: 'sand' },
];
$('#type-cards').innerHTML = groups.map((group, index) => `<button class="type-card" data-group="${index}"><span class="type-icon ${group.color}">${group.icon}</span><span><h3>${group.title}</h3><p>${group.description}</p></span><span class="card-arrow">↗</span><span class="card-number">0${index + 1}</span></button>`).join('');
document.querySelectorAll<HTMLButtonElement>('[data-group]').forEach(button => button.addEventListener('click', () => { const group = groups[Number(button.dataset.group)]; void addCell(group.kinds.length === 1 ? group.kinds[0] : undefined).catch(report); }));
async function runLocalAssistantTurn(message: string, selectedCell?: Cell): Promise<{ answer: string }> {
  if (!workspace && !demo) {
    await startDemo();
  }
  const lower = message.toLowerCase();
  const targetKind: CellKind | undefined = /lean|정리|증명/.test(lower)
    ? 'lean'
    : /rocq|coq/.test(lower)
      ? 'rocq'
      : /python|실험|코드|수치/.test(lower)
        ? 'python'
        : /html|시각화|웹/.test(lower)
          ? 'html'
          : /요약|markdown|노트|정리해/.test(lower) && /생성|추가|만들|작성/.test(lower)
            ? 'markdown'
            : undefined;

  if (targetKind && /생성|추가|만들|작성/.test(lower)) {
    await addCell(targetKind);
    const created = cells.find(item => item.id === selected);
    const kindMeta = cellTypes[targetKind];
    return {
      answer: `새 **${kindMeta.label}** 셀(${created ? `\`${created.title}\`` : kindMeta.short})을 작업 공간에 추가했습니다.\n\n- 실행/렌더링: 셀 상단 실행 버튼 또는 \`Shift+Enter\`\n- 실시간 **EXAONE 2.5 MCP** 자동 코드 생성은 상단 \`ORCID 로그인 ↗\` 후 공유 Workspace에서 동작합니다.`,
    };
  }

  if (selectedCell) {
    const previewLines = selectedCell.text.split(/\r?\n/).slice(0, 6).join('\n');
    return {
      answer: `선택한 **${cellTypes[selectedCell.kind].label}** 셀 \`${selectedCell.title}\` 기준 요약입니다.\n\n\`\`\`${cellTypes[selectedCell.kind].language}\n${previewLines}\n\`\`\`\n\n- 총 ${selectedCell.text.split(/\r?\n/).length}줄 · 권한: \`${permissionSummary(selectedCell)}\`\n- 공유 Workspace에 연결하면 EXAONE 2.5 에이전트가 셀 초안 작성과 수정을 지원합니다.`,
    };
  }

  const kindsSummary = Object.entries(
    cells.reduce<Record<string, number>>((acc, c) => {
      const label = cellTypes[c.kind].short;
      acc[label] = (acc[label] || 0) + 1;
      return acc;
    }, {}),
  ).map(([k, v]) => `${k} ${v}개`).join(', ');

  return {
    answer: `현재 **${workspaceName}**에는 총 ${cells.length}개의 셀(${kindsSummary || '없음'})이 있습니다.\n\n- 예: \`"Lean 4 정리 셀 생성해 줘"\`, \`"Python 실험 코드 추가"\`처럼 지시하면 즉시 셀을 추가할 수 있습니다.\n- AI 추론·MCP 편집은 ORCID 로그인 후 공유 Workspace에서 사용할 수 있습니다.`,
  };
}

const agentPanel = new AgentPanelController({
  getWorkspace: () => workspace,
  getCells: () => cells,
  getSelectedId: () => selected,
  isConnected: () => connected,
  isDemo: () => demo,
  onLocalPrompt: runLocalAssistantTurn,
  onLog: msg => log(msg),
  onNotify: (msg, isError) => notify(msg, isError),
  onSelectCell: id => selectCell(id),
  onVisibilityChange: () => syncWorkbenchChrome(),
  onClosePanel: () => {
    document.body.classList.add('inspector-hidden');
    syncWorkbenchChrome();
  },
});
function startResizeDrag(
  event: PointerEvent,
  handle: HTMLElement,
  onMove: (clientX: number, clientY: number) => void,
  onEnd?: () => void,
): void {
  event.preventDefault();
  document.body.classList.add('is-resizing');
  handle.classList.add('dragging');
  const move = (e: PointerEvent): void => {
    onMove(e.clientX, e.clientY);
  };
  const stop = (): void => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', stop);
    window.removeEventListener('pointercancel', stop);
    document.body.classList.remove('is-resizing');
    handle.classList.remove('dragging');
    onEnd?.();
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', stop);
  window.addEventListener('pointercancel', stop);
}

function initLayoutResizers(): void {
  const root = document.documentElement;
  const keys = { sidebar: 'irade.layout.sidebarWidth', right: 'irade.layout.rightWidth', dock: 'irade.layout.dockHeight' };
  const restore = (key: string, cssVar: string, min: number, max: number): void => {
    const saved = Number(localStorage.getItem(key));
    if (Number.isFinite(saved) && saved >= min && saved <= max) {
      root.style.setProperty(cssVar, `${Math.round(saved)}px`);
    }
  };
  restore(keys.sidebar, '--sidebar-width', 180, 440);
  restore(keys.right, '--right-width', 240, 560);
  restore(keys.dock, '--bottom-dock-height', 96, 460);

  const sidebarHandle = document.querySelector<HTMLElement>('#resizer-sidebar');
  sidebarHandle?.addEventListener('pointerdown', event => {
    const startX = event.clientX;
    const startWidth = document.querySelector<HTMLElement>('.sidebar')?.getBoundingClientRect().width || 244;
    let latest = startWidth;
    startResizeDrag(event, sidebarHandle, clientX => {
      latest = Math.round(Math.max(180, Math.min(440, startWidth + (clientX - startX))));
      root.style.setProperty('--sidebar-width', `${latest}px`);
      for (const ed of cellEditors.values()) { ed.layout(); }
    }, () => localStorage.setItem(keys.sidebar, String(latest)));
  });
  sidebarHandle?.addEventListener('dblclick', () => {
    localStorage.removeItem(keys.sidebar);
    root.style.removeProperty('--sidebar-width');
    for (const ed of cellEditors.values()) { ed.layout(); }
  });

  const rightHandle = document.querySelector<HTMLElement>('#resizer-right');
  rightHandle?.addEventListener('pointerdown', event => {
    const startX = event.clientX;
    const agentOpen = document.body.classList.contains('agent-open');
    const targetEl = document.querySelector<HTMLElement>(agentOpen ? '#agent-drawer' : '.inspector');
    const startWidth = targetEl?.getBoundingClientRect().width || 348;
    const cssVar = '--right-width';
    let latest = startWidth;
    startResizeDrag(event, rightHandle, clientX => {
      latest = Math.round(Math.max(240, Math.min(560, startWidth + (startX - clientX))));
      root.style.setProperty(cssVar, `${latest}px`);
      for (const ed of cellEditors.values()) { ed.layout(); }
    }, () => localStorage.setItem(keys.right, String(latest)));
  });
  rightHandle?.addEventListener('dblclick', () => {
    localStorage.removeItem(keys.right);
    root.style.removeProperty('--right-width');
    for (const ed of cellEditors.values()) { ed.layout(); }
  });

  const bottomHandle = document.querySelector<HTMLElement>('#resizer-bottom');
  bottomHandle?.addEventListener('pointerdown', event => {
    const startY = event.clientY;
    const startHeight = document.querySelector<HTMLElement>('#bottom-dock')?.getBoundingClientRect().height || 176;
    let latest = startHeight;
    startResizeDrag(event, bottomHandle, (_clientX, clientY) => {
      latest = Math.round(Math.max(96, Math.min(540, startHeight + (startY - clientY))));
      root.style.setProperty('--bottom-dock-height', `${latest}px`);
    }, () => { if (bottomDockTab !== 'games') { localStorage.setItem(keys.dock, String(latest)); } });
  });
  bottomHandle?.addEventListener('dblclick', () => {
    localStorage.removeItem(keys.dock);
    root.style.removeProperty('--bottom-dock-height');
  });
}

function initInsertProximity(): void {
  const scrollEl = document.querySelector<HTMLElement>('#notebook-scroll');
  if (!scrollEl) { return; }
  let rafId = 0;
  let lastX = -9999;
  let lastY = -9999;
  const maxDist = 115;
  const fullDist = 12;

  const updateAll = (reset = false): void => {
    rafId = 0;
    scrollEl.querySelectorAll<HTMLElement>('.nb-cell-insert-zone').forEach(zone => {
      if (reset) {
        zone.style.setProperty('--insert-prox', '0');
        return;
      }
      const rect = zone.getBoundingClientRect();
      const dx = Math.max(rect.left - lastX, 0, lastX - rect.right);
      const dy = Math.max(rect.top - lastY, 0, lastY - rect.bottom);
      const dist = Math.hypot(dx, dy);
      const prox = dist >= maxDist
        ? 0
        : dist <= fullDist
          ? 1
          : Math.pow(1 - (dist - fullDist) / (maxDist - fullDist), 1.35);
      zone.style.setProperty('--insert-prox', prox.toFixed(3));
    });
  };

  scrollEl.addEventListener('pointermove', event => {
    if (event.pointerType === 'touch') { return; }
    lastX = event.clientX;
    lastY = event.clientY;
    if (!rafId) { rafId = requestAnimationFrame(() => updateAll(false)); }
  });
  scrollEl.addEventListener('scroll', () => {
    if (lastX > -9000 && !rafId) { rafId = requestAnimationFrame(() => updateAll(false)); }
  }, { passive: true });
  scrollEl.addEventListener('pointerleave', () => {
    lastX = -9999;
    lastY = -9999;
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    updateAll(true);
  });
}

initLayoutResizers();
initInsertProximity();
updateAccount(); renderList(); renderInspector();
const narrowLayoutMq = window.matchMedia('(max-width: 1100px)');
const syncRightAgentDockForViewport = (): void => {
  if (narrowLayoutMq.matches) {
    if (document.body.classList.contains('agent-open')) {
      agentPanel.close();
      syncWorkbenchChrome();
    }
  } else if (rightPaneMode === 'agent' && !document.body.classList.contains('inspector-hidden') && !document.body.classList.contains('agent-open')) {
    agentPanel.open({ focus: false });
    syncWorkbenchChrome();
  }
};
narrowLayoutMq.addEventListener('change', syncRightAgentDockForViewport);
if (!narrowLayoutMq.matches && !document.body.classList.contains('inspector-hidden')) {
  rightPaneMode = 'agent';
  agentPanel.open({ focus: false });
}
syncWorkbenchChrome();
const query = new URL(location.href).searchParams;
let canvasId = Number(query.get('workspace'));
void (async () => {
  if (!token && await migrateLegacyToken()) { token = SESSION; updateAccount(); }
  if (token) {
    try { await refreshSelfProfile(); }
    // Await the logout so the stale hint cookie is gone before signIn() reaches /login (else it bounces back).
    catch { token = undefined; await endSession(); updateAccount(); }
  }
  if (canvasId) {
    if (token) {
      const resolvedName = await resolveWorkspaceName(canvasId);
      await connect(canvasId, resolvedName, true);
    } else {
      await signIn();
    }
  }
  else if (token && query.has('workspaces')) { await openWorkspace(); }
  else if (query.has('demo')) { await startDemo(); }
})().catch(report);
