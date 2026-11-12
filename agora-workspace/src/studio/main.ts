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
const tokenKey = `irade.research.token:${origin}`;
const demoKey = 'irade.research.scratch';
let token: string | undefined = sessionStorage.getItem(tokenKey) || undefined;
let workspace: ResearchWorkspace | undefined;
let workspaceName = '나의 연구 공간';
let cells: Cell[] = [];
let selected: string | undefined;
let demo = false;
let connected = false;
let filter = 'all';
let preview = false;
let deletingCellId: string | undefined;
let editingRemote = false;
let editor: monaco.editor.IStandaloneCodeEditor | undefined;
let inspectorTab = 'context';
let loadGeneration = 0;
let toastTimer: ReturnType<typeof setTimeout>;
const models = new Map<string, monaco.editor.ITextModel>();
const activities: { message: string; time: string }[] = [];
const modal = $<HTMLDialogElement>('#modal');
const starters: Record<CellKind, string> = {
  agent: '# 연구 대화\n\n## 사용자\n연구할 질문과 배경을 기록하세요.\n\n## 에이전트\n에이전트와 나눈 응답을 여기에 보관하세요.\n',
  lean: '-- 새로운 정리\n\ntheorem add_zero_identity (n : Nat) : n + 0 = n := by\n  rfl\n',
  rocq: '(* 새로운 증명 *)\n\nTheorem add_zero_identity : forall n : nat, n + 0 = n.\nProof.\n  intros n.\n  induction n; simpl; auto.\nQed.\n',
  python: '# 새로운 실험\n\ndef square(n):\n    return n * n\n\nvalues = [square(n) for n in range(6)]\nprint(values)\n',
  markdown: '# 연구 노트\n\n## 연구 질문\n여기에 연구할 질문을 적어 보세요.\n\n## 관찰과 기록\n- 가설과 실험 결과를 연결하세요.\n- 증명과 코드의 의미를 문서로 남기세요.\n',
  html: '<article>\n  <h1>연구 문서</h1>\n  <p>아이디어를 구조화하고 공유하세요.</p>\n</article>\n',
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
monaco.editor.defineTheme('irade', {
  base: 'vs-dark', inherit: true,
  rules: [{ token: 'comment', foreground: '718B79', fontStyle: 'italic' }, { token: 'keyword', foreground: 'C4B1E7' }, { token: 'string', foreground: 'B7D3A4' }, { token: 'number', foreground: 'E3B78C' }, { token: 'operator', foreground: '9CCBB4' }],
  colors: { 'editor.background': '#1B1E20', 'editor.foreground': '#D0DDD4', 'editorLineNumber.foreground': '#4B6353', 'editorLineNumber.activeForeground': '#A2BCA9', 'editorCursor.foreground': '#B1E6D0', 'editor.selectionBackground': '#B1E6D025', 'editor.lineHighlightBackground': '#B1E6D005', 'editorIndentGuide.background1': '#2A3830', 'editorWidget.background': '#25332A', 'editorWidget.border': '#4D6555', 'editorSuggestWidget.background': '#202A24', 'editorSuggestWidget.border': '#496150', 'focusBorder': '#8AAF98', 'scrollbarSlider.background': '#708C7730' },
});
function notify(message: string, error = false): void {
  clearTimeout(toastTimer); const toast = $('#toast');
  toast.textContent = message; toast.classList.toggle('error', error); toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, error ? 10000 : 4500);
}
function report(error: unknown): void {
  const raw = error instanceof Error ? error.message : String(error);
  const labels: Record<string, string> = { TOKEN_EXPIRED: '로그인이 만료되었습니다. 다시 로그인하세요.', TOKEN_REQUIRED: 'Agora 계정으로 로그인하세요.', CANVAS_ACCESS_REVOKED: 'Workspace 접근 권한이 회수되었습니다.', WORKSPACE_ACCESS_DENIED: '이 workspace를 변경할 권한이 없습니다.', ITEM_WRITE_DENIED: '셀 편집 권한이 없습니다.', CANVAS_ACCESS_DENIED: 'Workspace 접근 권한이나 비밀번호를 확인하세요.', FILE_TOO_LARGE: '셀 저장 한도(128 KiB)를 넘었습니다. 내용을 나누어 주세요.', FILE_REPLACED: '셀이 외부에서 교체되었습니다. 현재 내용을 내보내 보관한 후 다시 여세요.' };
  notify(labels[raw] || raw, true);
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
function updateAccount(): void { $('#account').textContent = token ? '계정 · 로그아웃' : '로그인 ↗'; }
async function signIn(): Promise<boolean> {
  return new Promise(resolve => {
    showModal('연구 workspace에 로그인', '<p class="modal-copy">기존 Agora 계정으로 로그인하고 공동 연구 공간을 연결하세요.</p><form id="login-form"><label class="field">이메일<input name="email" type="email" autocomplete="username" required placeholder="researcher@example.com"></label><label class="field">비밀번호<input name="password" type="password" autocomplete="current-password" required placeholder="Agora 비밀번호"></label><p class="dialog-error" id="login-error" role="alert"></p><div class="form-actions"><button class="button primary" type="submit">로그인 →</button></div></form>');
    const done = (): void => resolve(false); modal.addEventListener('close', done, { once: true });
    $('#login-form').addEventListener('submit', event => {
      event.preventDefault(); const form = event.currentTarget as HTMLFormElement; const button = form.querySelector('button')!;
      button.disabled = true;
      void (async () => {
        try {
          const data = new FormData(form);
          let result = await api<{ accessToken?: string; mfaRequired?: boolean; challengeToken?: string }>('/api/auth/login', { email: String(data.get('email')).trim(), password: data.get('password') });
          (form.elements.namedItem('password') as HTMLInputElement).value = '';
          while (result.mfaRequired && result.challengeToken) {
            modal.removeEventListener('close', done);
            const code = await ask('2단계 인증', '인증 앱 또는 복구 코드', true);
            if (!code) { resolve(false); return; }
            try { result = await api('/api/auth/mfa/verify', { challengeToken: result.challengeToken, code: code.trim() }); }
            catch (error) { report(error); }
          }
          if (!result.accessToken) { throw new Error('인증 토큰을 받지 못했습니다.'); }
          token = result.accessToken; sessionStorage.setItem(tokenKey, token); updateAccount();
          modal.removeEventListener('close', done); resolve(true); modal.close(); log('Agora 계정에 로그인했습니다.');
        } catch (error) { if ($('#login-error')) { $('#login-error').textContent = error instanceof Error ? error.message : String(error); } else { report(error); } }
        finally { button.disabled = false; }
      })();
    });
  });
}
async function ask(title: string, label: string, password = false): Promise<string | undefined> {
  return new Promise(resolve => {
    showModal(title, `<form id="ask-form"><label class="field">${escape(label)}<input name="value" type="${password ? 'password' : 'text'}" required autocomplete="off"></label><div class="form-actions"><button class="button primary">확인 →</button></div></form>`);
    const close = (): void => resolve(undefined); modal.addEventListener('close', close, { once: true });
    $('#ask-form').addEventListener('submit', event => { event.preventDefault(); const result = String(new FormData(event.currentTarget as HTMLFormElement).get('value')); modal.removeEventListener('close', close); resolve(result); modal.close(); });
  });
}
interface Canvas { canvas_id: number; canvas_name: string; description?: string }
async function openWorkspace(): Promise<void> {
  if (!token && !await signIn()) { return; }
  const available = await api<Canvas[]>('/api/canvases');
  showModal('Workspace 선택', '<p class="modal-copy">Agora 캔버스 하나가 하나의 공유 연구 workspace에 연결됩니다.</p><input class="search-input" id="workspace-search" type="search" aria-label="Workspace 검색" placeholder="Workspace 이름 검색…"><div class="choice-list" id="workspace-choices"></div><div class="form-actions"><button class="button ghost" id="join-workspace">초대 링크 / ID로 열기</button><button class="button primary" id="create-workspace">새 workspace ＋</button></div>');
  const render = (): void => {
    const query = $<HTMLInputElement>('#workspace-search').value.toLowerCase();
    $('#workspace-choices').innerHTML = available.filter(canvas => `${canvas.canvas_name} ${canvas.canvas_id}`.toLowerCase().includes(query)).map(canvas => `<button class="choice" data-canvas="${canvas.canvas_id}"><span class="workspace-avatar">${escape(canvas.canvas_name.charAt(0).toUpperCase())}</span><span><strong>${escape(canvas.canvas_name)}</strong><small>${escape(canvas.description || '공유 연구 workspace')} · #${canvas.canvas_id}</small></span><span class="choice-arrow">↗</span></button>`).join('') || '<p class="empty-search">아직 workspace가 없습니다. 새 연구 공간을 만들어 보세요.</p>';
    document.querySelectorAll<HTMLButtonElement>('[data-canvas]').forEach(button => button.addEventListener('click', () => {
      const canvas = available.find(canvas => canvas.canvas_id === Number(button.dataset.canvas))!;
      modal.close(); void connect(canvas.canvas_id, canvas.canvas_name).catch(report);
    }));
  };
  render(); $('#workspace-search').addEventListener('input', render);
  $('#join-workspace').addEventListener('click', () => { void (async () => {
    const value = await ask('공유 workspace 열기', 'Workspace ID 또는 공유 링크'); if (!value) { return; }
    let id = Number(value.trim());
    if (!id) { try { const url = new URL(value); if (url.protocol === 'agora:') { if (url.hostname !== location.hostname) { throw new Error(); } id = Number(url.username); } else { if (url.origin !== origin) { throw new Error(); } id = Number(url.searchParams.get('workspace')); } } catch { throw new Error('현재 Agora 서버의 workspace 링크를 입력하세요.'); } }
    await connect(id, available.find(canvas => canvas.canvas_id === id)?.canvas_name || `Workspace ${id}`);
  })().catch(report); });
  $('#create-workspace').addEventListener('click', () => { void (async () => {
    const name = await ask('새 연구 workspace', 'Workspace 이름'); if (!name?.trim()) { return; }
    const result = await api<Canvas>('/api/canvases', { canvasName: name.trim(), description: 'IRADE 연구 workspace', visibility: 'private' });
    await connect(result.canvas_id, result.canvas_name || name.trim());
  })().catch(report); });
}
function clearModels(): void { editor?.setModel(null); for (const model of models.values()) { model.dispose(); } models.clear(); }
async function connect(id: number, name: string): Promise<void> {
  if (!Number.isSafeInteger(id) || id <= 0) { throw new Error('올바른 workspace ID를 입력하세요.'); }
  if (workspace?.pending) { await workspace.flush(); }
  const generation = ++loadGeneration;
  workspace?.dispose(); clearModels(); demo = false; connected = false; selected = undefined; cells = []; workspaceName = name;
  const client = new AgoraClient({ origin, canvasId: id }, async () => token || (await signIn() ? token : undefined), async () => ask('비공개 workspace', '캔버스 비밀번호', true));
  const current = new ResearchWorkspace(client); workspace = current;
  current.onError = error => { if (generation === loadGeneration) { report(error); } };
  current.onState = state => {
    if (generation !== loadGeneration) { return; }
    connected = state === 'Connected' || state === 'Saving';
    const translated: Record<string, string> = { Connected: '실시간 연결됨', Saving: '변경 사항 저장 중…', Connecting: 'workspace 연결 중…', Reconnecting: '재연결 중 · 편집 내용 유지' };
    $('#status').textContent = translated[state] || state;
    $('#connection-label').textContent = state === 'Saving' ? '동기화 중' : connected ? 'LIVE SYNC' : '연결 중';
    $('#cell-save').textContent = state === 'Saving' ? '저장 중…' : connected ? '모든 변경 저장됨' : '로컬 변경 유지';
    $('#permission-label').textContent = connected ? client.canWrite ? '편집 가능' : '읽기 전용' : '';
    editor?.updateOptions({ readOnly: !client.canWrite });
    if (state === 'Connected' && current.pending) { void current.flush().catch(report); }
    if (!['Connected','Saving','Connecting','Reconnecting'].includes(state)) { report(new Error(state)); }
  };
  current.onCells = () => {
    if (generation !== loadGeneration) { return; }
    cells = current.cells;
    if (selected && !cells.some(cell => cell.id === selected)) {
      const removed = selected; selected = undefined;
      if (models.has(removed) && deletingCellId !== removed) {
        const model = models.get(removed)!; const value = model.getValue();
        showModal('공유 셀이 삭제되었습니다', '<p class="modal-copy">다른 참여자가 이 셀을 삭제했습니다. 현재 편집기의 내용을 내려받아 보관할 수 있습니다.</p><label class="field">현재 탭의 로컬 복사본<textarea id="recovery-text" readonly></textarea></label><div class="form-actions"><button class="button primary" id="recover-cell">로컬 복사본 내려받기 ↧</button></div>');
        $<HTMLTextAreaElement>('#recovery-text').value = value;
        $('#recover-cell').addEventListener('click', () => download(value, 'recovered-cell.txt'));
      }
      overview();
    }
    renderList(); renderInspector();
  };
  current.onText = cell => {
    const model = models.get(cell.id);
    if (model && model.getValue() !== cell.text) {
      editingRemote = true;
      try {
        // Apply a minimal range; preserve selection, undo stack and scroll position.
        const edit = replacement(model.getValue(), cell.text);
        const from = model.getPositionAt(edit.start), to = model.getPositionAt(edit.end);
        const selection = selected === cell.id ? editor?.getSelections() : undefined;
        model.pushEditOperations(selection || null, [{ range: new monaco.Range(from.lineNumber, from.column, to.lineNumber, to.column), text: edit.text }], () => selection || null);
      } finally { editingRemote = false; }
      if (selected === cell.id) { updateCellHeading(); renderPreview(); }
      log(`${cellTypes[cell.kind].short} 셀의 공유 변경을 반영했습니다.`);
    }
  };
  const url = new URL(location.href); url.searchParams.delete('folder'); url.searchParams.set('workspace', String(id)); history.replaceState(null, '', url);
  overview(); renderList();
  await current.load();
  if (generation !== loadGeneration) { return; }
  sessionStorage.setItem('irade.research.lastWorkspace', JSON.stringify({ id, name }));
  $('#workspace-subtitle').textContent = `공유 workspace · #${id}`;
  $('#workspace-mode').textContent = 'Agora Workspace'; $('#sync-description').textContent = '변경 내용은 같은 workspace에 실시간 공유';
  log(`${name} workspace를 연결했습니다.`); notify('공유 workspace에 연결했습니다.');
}
function renderList(): void {
  $('#workspace-name').textContent = workspaceName;
  $('#cell-count').textContent = String(cells.length); $('#status-cell-count').textContent = `${cells.length} cells`;
  $('#cell-list-label').textContent = filter === 'all' ? '연구 셀' : filter === 'agent' ? '에이전트 대화' : filter === 'proof' ? '증명 셀' : filter === 'python' ? '코드 셀' : '문서 셀';
  const visible = cells.filter(cell => filter === 'all' || cell.kind === filter || filter === 'proof' && ['lean','rocq'].includes(cell.kind) || filter === 'document' && ['markdown','html'].includes(cell.kind));
  $('#cell-list').innerHTML = visible.map(cell => `<button class="cell-list-item ${cell.id === selected ? 'selected' : ''}" data-cell="${escape(cell.id)}"><span class="mini-type ${cellTypes[cell.kind].color}">${cellTypes[cell.kind].icon}</span><span><strong>${escape(cell.title)}</strong><small>${cellTypes[cell.kind].short} · ${String(cells.indexOf(cell) + 1).padStart(2, '0')}</small></span></button>`).join('') || `<p class="empty-list">${workspace || demo ? '아직 셀이 없습니다.<br>새 셀로 연구를 시작하세요.' : '대화, 증명, 코드, 문서.<br>연구의 맥락이 여기에 모입니다.'}</p>`;
  $('#cell-tabs').innerHTML = cells.filter(cell => models.has(cell.id)).map(cell => `<button class="tab ${cell.id === selected ? 'selected' : ''}" role="tab" aria-selected="${cell.id === selected}" data-cell="${escape(cell.id)}"><span class="${cellTypes[cell.kind].color}">${cellTypes[cell.kind].icon}</span>${escape(cell.title.slice(0, 22))}</button>`).join('');
  document.querySelectorAll<HTMLButtonElement>('[data-cell]').forEach(button => button.addEventListener('click', () => selectCell(button.dataset.cell!)));
  $('#overview-tab').classList.toggle('selected', !selected); $('#overview-tab').setAttribute('aria-selected', String(!selected));
}
function ensureEditor(): monaco.editor.IStandaloneCodeEditor {
  if (!editor) {
    editor = monaco.editor.create($('#editor'), { theme: 'irade', automaticLayout: true, model: null, fontSize: Number(localStorage.getItem('irade.fontSize') || 14), fontFamily: "'SFMono-Regular', Consolas, 'Liberation Mono', monospace", lineHeight: 25, minimap: { enabled: false }, scrollBeyondLastLine: false, padding: { top: 20, bottom: 20 }, smoothScrolling: true, renderLineHighlight: 'gutter', lineNumbersMinChars: 3, wordWrap: 'on', tabSize: 4, readOnly: workspace ? !workspace.client.canWrite : false, fixedOverflowWidgets: true });
    editor.onDidChangeCursorPosition(event => { $('#cursor-position').textContent = `Ln ${event.position.lineNumber}, Col ${event.position.column}`; });
  }
  return editor;
}
function selectCell(id: string): void {
  const cell = cells.find(cell => cell.id === id); if (!cell) { return; }
  selected = id; $('#overview').hidden = true; $('#cell-view').hidden = false;
  let model = models.get(id);
  if (!model) {
    model = monaco.editor.createModel(cell.text, cellTypes[cell.kind].language, monaco.Uri.parse(`irade://cell/${cell.id}.${cellTypes[cell.kind].extension}`));
    models.set(id, model);
    model.onDidChangeContent(() => {
      if (editingRemote) { return; }
      try {
        const text = model!.getValue();
        if (workspace) { workspace.edit(id, text); } else { cell.text = text; cell.title = cellTitle(cell.kind, text); persistScratch(); }
        updateCellHeading(); renderList(); renderPreview();
      } catch (error) { report(error); }
    });
  }
  ensureEditor().setModel(model); updateCellHeading(); renderList(); renderInspector(); renderPreview();
}
function updateCellHeading(): void {
  const cell = cells.find(cell => cell.id === selected); if (!cell) { return; }
  const type = cellTypes[cell.kind];
  $('#cell-category').textContent = workspaceName; $('#cell-number').textContent = `Cell ${String(cells.indexOf(cell) + 1).padStart(2, '0')}`;
  $('#cell-kind').textContent = type.label.toUpperCase(); $('#cell-title').textContent = cell.title;
  $('#cell-type-icon').textContent = type.icon; $('#cell-type-icon').className = `type-icon ${type.color}`;
  $('#editor-language').textContent = type.short.toUpperCase(); $('#chat-tools').hidden = cell.kind !== 'agent';
  $('#preview-button').hidden = !['markdown','html','agent'].includes(cell.kind);
  $('#preview-button').textContent = preview ? '편집만 보기' : '미리보기';
  $('#cell-context').innerHTML = ['lean','rocq'].includes(cell.kind) ? '<span class="purple">⊢</span><span><b>Proof context</b> · 증명 소스를 편집하고 공유합니다. 증명 커널 연결 전에는 검증 완료로 표시하지 않습니다.</span>' : cell.kind === 'python' ? '<span class="blue">λ</span><span><b>Python context</b> · 코드와 연구 맥락을 함께 보관합니다. 현재 화면은 셀 편집·동기화를 지원하며 실행 커널은 연결되어 있지 않습니다.</span>' : cell.kind === 'agent' ? '<span class="mint">✧</span><span><b>Conversation context</b> · 대화 기록 셀입니다. 사용자와 에이전트 메시지를 기록하며 AI 응답을 자동 생성하지 않습니다.</span>' : '<span class="sand">≡</span><span><b>Document context</b> · 미리보기에서 문서를 확인하세요. HTML은 스크립트 실행 없이 격리된 화면으로 표시됩니다.</span>';
}
function overview(): void { selected = undefined; $('#overview').hidden = false; $('#cell-view').hidden = true; renderList(); renderInspector(); }
function renderPreview(): void {
  const cell = cells.find(cell => cell.id === selected);
  const visible = preview && !!cell && ['markdown','html','agent'].includes(cell.kind);
  $('#preview-panel').hidden = !visible;
  if (!visible || !cell) { return; }
  const source = cell.kind === 'html' ? cell.text : marked.parse(cell.text, { async: false });
  const safe = DOMPurify.sanitize(source, { FORBID_TAGS: ['script','style','form','iframe','object','embed','base','link','meta'], FORBID_ATTR: ['style','srcset'] });
  $<HTMLIFrameElement>('#document-preview').srcdoc = `<!doctype html><html lang="ko"><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"><meta name="color-scheme" content="dark"><style>body{font:13px/1.9 system-ui,sans-serif;color:#cbded0;background:#1b1e20;padding:20px;overflow-wrap:anywhere}h1,h2,h3{font-weight:550;line-height:1.5;color:#e3eee5}h1{font-size:24px}h2{font-size:19px;border-bottom:1px solid #344239;padding-bottom:10px;margin-top:28px}a{color:#b1e6d0}code{background:#b1e6d00d;padding:3px 6px;border-radius:3px}pre{padding:16px;background:#121a15;border:1px solid #344239;overflow:auto}pre code{padding:0;background:none}blockquote{border-left:2px solid #8eb39c;padding-left:15px;color:#8ca995}table{border-collapse:collapse;width:100%}th,td{border:1px solid #3b4d40;padding:7px 10px}img{max-width:100%}</style></head><body>${safe}</body></html>`;
}
function renderInspector(): void {
  document.querySelectorAll('.inspector-tabs>button').forEach(button => button.classList.toggle('selected', button.getAttribute('data-action') === inspectorTab));
  if (inspectorTab === 'activity') {
    $('#inspector-content').innerHTML = '<h3 class="context-heading">WORKSPACE ACTIVITY</h3>' + (activities.length ? activities.map(event => `<div class="activity-event">${escape(event.message)}<small>${event.time}</small></div>`).join('') : '<p class="context-copy">이 탭에서 연결하고 편집한 기록이 여기에 표시됩니다.</p>'); return;
  }
  const cell = cells.find(cell => cell.id === selected);
  const typeCounts = { agent: cells.filter(cell => cell.kind === 'agent').length, proof: cells.filter(cell => ['lean','rocq'].includes(cell.kind)).length, code: cells.filter(cell => cell.kind === 'python').length, document: cells.filter(cell => ['markdown','html'].includes(cell.kind)).length };
  $('#inspector-content').innerHTML = `<h3 class="context-heading">${cell ? 'CELL CONTEXT' : 'WORKSPACE CONTEXT'}</h3><h2 class="context-title">${escape(cell ? cellTypes[cell.kind].label : '연구의 흐름을\n하나로 연결하세요.').replace('\n','<br>')}</h2><p class="context-copy">${cell ? escape(cellTypes[cell.kind].description) : '아이디어를 탐색하고, 가설을 증명하고,<br>결과를 코드와 문서로 남기는 공간.'}</p>${cell && ['lean','rocq'].includes(cell.kind) ? '<div class="context-block"><h3 class="context-heading">PROOF STATE</h3><p class="context-copy">아직 검증 결과가 없습니다.<br>증명 커널 연결이 필요합니다.</p><span class="context-tag">NOT VERIFIED</span></div>' : ''}<div class="context-flow"><div class="flow-step"><span class="step-icon mint">✧</span><span>탐색하고 대화하기<small>AGENT CONVERSATIONS</small></span></div><div class="flow-step"><span class="step-icon purple">∀</span><span>논리로 증명하기<small>LEAN 4 / ROCQ PROOFS</small></span></div><div class="flow-step"><span class="step-icon blue">λ</span><span>코드로 실험하기<small>PYTHON CELLS</small></span></div><div class="flow-step"><span class="step-icon sand">≡</span><span>지식으로 정리하기<small>MARKDOWN / HTML</small></span></div></div><div class="context-block"><h3 class="context-heading">${cell ? 'THIS WORKSPACE' : 'WORKSPACE OVERVIEW'}</h3><div class="context-stat"><span>대화 기록</span><b>${String(typeCounts.agent).padStart(2,'0')}</b></div><div class="context-stat"><span>형식 증명</span><b>${String(typeCounts.proof).padStart(2,'0')}</b></div><div class="context-stat"><span>Python 코드</span><b>${String(typeCounts.code).padStart(2,'0')}</b></div><div class="context-stat"><span>연구 문서</span><b>${String(typeCounts.document).padStart(2,'0')}</b></div></div><div class="context-block"><h3 class="context-heading">COLLABORATION</h3><p class="context-copy">${demo ? '로컬 체험 모드입니다.<br>내용은 현재 브라우저 탭에만 저장됩니다.' : connected ? '같은 workspace를 연 참여자에게<br>변경 내용이 실시간으로 공유됩니다.' : 'Agora workspace를 연결하면<br>함께 셀을 편집할 수 있습니다.'}</p><span class="context-tag">${demo ? 'LOCAL SESSION' : connected ? 'LIVE SYNC' : 'AGORA WORKSPACE'}</span></div>`;
}
function persistScratch(): void { sessionStorage.setItem(demoKey, JSON.stringify(cells)); $('#cell-save').textContent = '현재 탭에 저장됨'; }
async function startDemo(): Promise<void> {
  if (workspace?.pending) { await workspace.flush(); }
  ++loadGeneration; workspace?.dispose(); workspace = undefined; connected = false; clearModels(); demo = true; workspaceName = 'Research playground'; selected = undefined;
  const cached = sessionStorage.getItem(demoKey);
  cells = cached ? JSON.parse(cached) as Cell[] : (['markdown','agent','lean','rocq','python','html'] as CellKind[]).map((kind, index) => ({ id: `${Date.now() + index}-${crypto.randomUUID()}`, kind, path: '', text: starters[kind], title: cellTitle(kind, starters[kind]) }));
  persistScratch(); const url = new URL(location.href); url.searchParams.delete('workspace'); url.searchParams.delete('folder'); history.replaceState(null,'',url);
  $('#workspace-subtitle').textContent = '로컬 체험 · 현재 탭에만 저장'; $('#workspace-mode').textContent = 'Local Playground'; $('#status').textContent = '로컬 체험 · 공유되지 않음'; $('#connection-label').textContent = 'LOCAL SESSION'; $('#sync-description').textContent = '체험 내용은 현재 탭에만 저장'; $('#permission-label').textContent = '로컬 편집';
  overview(); log('로컬 체험 workspace를 열었습니다.'); notify('로컬 체험입니다. 공동 편집하려면 Agora workspace를 연결하세요.');
}
async function addCell(kind?: CellKind): Promise<void> {
  if (!workspace && !demo) {
    showModal('연구 공간을 선택하세요', '<p class="modal-copy">Agora workspace에서 실시간으로 협업하거나 로컬 체험으로 셀을 살펴보세요.</p><div class="choice-list"><button class="choice" data-action="open"><span class="type-icon mint">↔</span><span><strong>공유 workspace 열기</strong><small>Agora 계정으로 로그인하고 공동 편집</small></span><span class="choice-arrow">↗</span></button><button class="choice" data-action="demo"><span class="type-icon blue">λ</span><span><strong>로컬 체험 시작</strong><small>계정 없이 현재 탭에서 셀 편집 체험</small></span><span class="choice-arrow">→</span></button></div>'); return;
  }
  if (workspace && !workspace.client.canWrite) { notify('이 workspace는 읽기 전용입니다.', true); return; }
  if (!kind) {
    showModal('새로운 연구 셀', '<p class="modal-copy">다음 아이디어에 맞는 셀을 선택하세요.</p><div class="choice-list">' + (Object.keys(cellTypes) as CellKind[]).map(kind => `<button class="choice" data-add-kind="${kind}"><span class="type-icon ${cellTypes[kind].color}">${cellTypes[kind].icon}</span><span><strong>${cellTypes[kind].label}</strong><small>${cellTypes[kind].description}</small></span><span class="choice-arrow">＋</span></button>`).join('') + '</div>');
    document.querySelectorAll<HTMLButtonElement>('[data-add-kind]').forEach(button => button.addEventListener('click', () => { modal.close(); void addCell(button.dataset.addKind as CellKind).catch(report); })); return;
  }
  let cell: Cell;
  if (workspace) { cell = await workspace.add(kind, starters[kind]); cells = workspace.cells; }
  else { cell = { id: `${Date.now()}-${crypto.randomUUID()}`, kind, path: '', text: starters[kind], title: cellTitle(kind, starters[kind]) }; cells.push(cell); persistScratch(); }
  log(`${cellTypes[kind].label} 셀을 만들었습니다.`); selectCell(cell.id);
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
  showModal('셀 삭제', `<p class="modal-copy">“${escape(cell.title)}” 셀을 삭제할까요? 공유 workspace에서는 다른 참여자에게도 적용됩니다.</p><div class="form-actions"><button class="button ghost" id="cancel-delete">취소</button><button class="button primary" id="confirm-delete">셀 삭제</button></div>`);
  $('#cancel-delete').addEventListener('click', () => modal.close());
  $('#confirm-delete').addEventListener('click', event => { (event.currentTarget as HTMLButtonElement).disabled = true; void (async () => {
    if (workspace) { deletingCellId = cell.id; try { await workspace.delete(cell.id); cells = workspace.cells; } finally { deletingCellId = undefined; } } else { cells = cells.filter(item => item.id !== cell.id); persistScratch(); }
    if (editor?.getModel() === models.get(cell.id)) { editor?.setModel(null); } models.get(cell.id)?.dispose(); models.delete(cell.id); overview(); modal.close(); log('셀을 삭제했습니다.');
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
function help(): void { showModal('연결된 연구를 시작하세요', '<div class="help-grid"><p><b>Workspace</b>는 공동 연구 공간입니다. 같은 Agora 캔버스를 열면 같은 셀을 편집하고 변경을 실시간으로 공유합니다.</p><p><b>에이전트 대화</b>는 질문과 응답을 기록합니다.<br><b>Lean 4 / Rocq</b>는 형식 증명 소스를 다룹니다.<br><b>Python</b>은 실험 코드를 작성합니다.<br><b>Markdown / HTML</b>은 문서와 미리보기를 제공합니다.</p><p>현재 셀 편집과 동기화를 지원합니다. AI 응답과 코드·증명 실행 커널은 아직 연결되지 않았습니다. 기존 workspace 데이터는 보존됩니다.</p><p><kbd>Ctrl K</kbd> 검색 · <kbd>Alt N</kbd> 새 셀<br><kbd>Ctrl S</kbd> 저장 · <kbd>Ctrl Shift F</kbd> 집중 모드</p></div>'); }
function settings(): void {
  showModal('연구 환경 설정', `<p class="modal-copy">설정은 이 브라우저에 적용됩니다.</p><label class="settings-row">편집기 글자 크기<select id="font-size">${[12,14,16,18,20].map(size => `<option value="${size}" ${Number(localStorage.getItem('irade.fontSize') || 14) === size ? 'selected' : ''}>${size}px</option>`).join('')}</select></label><label class="settings-row">편집기 줄 번호<select id="line-numbers"><option value="on">표시</option><option value="off">숨기기</option></select></label><div class="form-actions"><button class="button ghost" data-action="help">사용 안내</button><button class="button primary" id="save-settings">적용</button></div>`);
  $('#save-settings').addEventListener('click', () => { const size = Number($<HTMLSelectElement>('#font-size').value); const lines = $<HTMLSelectElement>('#line-numbers').value as 'on' | 'off'; localStorage.setItem('irade.fontSize', String(size)); editor?.updateOptions({ fontSize: size, lineNumbers: lines }); modal.close(); notify('편집기 설정을 적용했습니다.'); });
}
async function share(): Promise<void> {
  if (!workspace) { notify('공유하려면 Agora workspace를 먼저 연결하세요.'); return; }
  const url = new URL(location.pathname, origin); url.searchParams.set('workspace', String(workspace.client.config.canvasId));
  try { await navigator.clipboard.writeText(String(url)); notify('Workspace 링크를 복사했습니다. 참여자는 자신의 Agora 계정과 접근 권한이 필요합니다.'); }
  catch { showModal('Workspace 공유 링크', `<p class="modal-copy">참여자는 자신의 Agora 계정과 접근 권한이 필요합니다.</p><label class="field">공유 링크<input readonly value="${escape(String(url))}"></label>`); }
}
async function action(name: string): Promise<void> {
  if (name === 'open') { await openWorkspace(); }
  else if (name === 'login') {
    if (token) {
      if (workspace?.pending) { await workspace.flush(); }
      ++loadGeneration; workspace?.dispose(); workspace = undefined; token = undefined; sessionStorage.removeItem(tokenKey); updateAccount(); clearModels(); cells = []; selected = undefined; connected = false; workspaceName = '나의 연구 공간'; demo = false;
      const url = new URL(location.href); url.searchParams.delete('workspace'); history.replaceState(null,'',url); overview(); $('#workspace-subtitle').textContent = 'workspace를 연결하세요'; $('#status').textContent = '로그아웃됨'; $('#connection-label').textContent = '연결 대기'; $('#permission-label').textContent = ''; notify('로그아웃했습니다.');
    } else { if (await signIn()) { await openWorkspace(); } }
  }
  else if (name === 'demo') { modal.close(); await startDemo(); }
  else if (name === 'add') { await addCell(); }
  else if (name === 'overview') { overview(); }
  else if (name === 'search') { search(); }
  else if (name === 'focus') { document.body.classList.toggle('focus'); }
  else if (name === 'inspector') { document.body.classList.toggle('inspector-hidden'); }
  else if (name === 'preview') { preview = !preview; updateCellHeading(); renderPreview(); }
  else if (name === 'delete') { await deleteCell(); }
  else if (name === 'export') { exportCell(); }
  else if (name === 'message') { message(); }
  else if (name === 'help') { help(); }
  else if (name === 'settings') { settings(); }
  else if (name === 'share') { await share(); }
  else if (name === 'context' || name === 'activity') { inspectorTab = name; renderInspector(); }
  else if (name === 'workspace' || name.startsWith('filter-')) {
    filter = name === 'workspace' ? 'all' : name.slice(7);
    document.querySelectorAll('.rail-button').forEach(button => button.classList.toggle('active', button.getAttribute('data-action') === name)); renderList();
    if (window.innerWidth <= 760) { search(); }
  }
}
document.addEventListener('click', event => { const button = (event.target as Element).closest<HTMLButtonElement>('button[data-action]'); if (button) { void action(button.dataset.action!).catch(report); } });
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
if (!canvasId && query.has('folder')) { try { const uri = new URL(query.get('folder')!); if (uri.protocol === 'agora:' && uri.hostname === location.hostname) { canvasId = Number(uri.username); } } catch { /* Ignore unrelated legacy links. */ } }
if (canvasId) { void (async () => { if (token || await signIn()) { await connect(canvasId, `Workspace ${canvasId}`); } })().catch(report); }
