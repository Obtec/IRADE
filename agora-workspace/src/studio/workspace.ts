import { AgoraClient, type FileRecord } from '../client.ts';
import { SharedDocument, encode } from '../document.ts';

export const cellTypes = {
  agent: { label: '에이전트 대화', short: 'Agent', extension: 'chat', language: 'markdown', icon: '✧', color: 'mint', description: '질문, 추론, 대화 기록을 연구의 맥락으로' },
  lean: { label: 'Lean 4 증명', short: 'Lean 4', extension: 'lean', language: 'lean4', icon: '∀', color: 'purple', description: '정의부터 정리까지, 형식 증명을 한곳에서' },
  rocq: { label: 'Rocq 증명', short: 'Rocq', extension: 'v', language: 'rocq', icon: '⊢', color: 'orange', description: '가설과 전술을 연결하는 대화형 증명' },
  python: { label: 'Python 코드', short: 'Python', extension: 'py', language: 'python', icon: 'λ', color: 'blue', description: '아이디어를 코드와 계산으로 구체화' },
  markdown: { label: 'Markdown 문서', short: 'Markdown', extension: 'md', language: 'markdown', icon: 'M↓', color: 'sand', description: '연구 노트와 설명을 읽기 좋은 문서로' },
  html: { label: 'HTML 문서', short: 'HTML', extension: 'html', language: 'html', icon: '⌘', color: 'rose', description: '구조화된 문서와 시각적인 실험' },
} as const;
export type CellKind = keyof typeof cellTypes;
export interface Cell { id: string; kind: CellKind; path: string; text: string; title: string }
interface Replica { cell: Cell; document: SharedDocument; version: number; sent: number; resourceId: string; timer?: ReturnType<typeof setTimeout>; flight?: Promise<void> }
export const cellDirectory = '/.irade/cells';
export function cellTitle(kind: CellKind, text: string): string {
  if (kind === 'html') {
    const heading = /<h[1-6]\b[^>]*>([\s\S]*?)<\/h[1-6]>/i.exec(text);
    if (heading) { return heading[1].replace(/<[^>]*>/g, '').trim().slice(0, 70) || '새 HTML 문서'; }
  }
  return text.split('\n').map(line => line.replace(/^\s*(?:#{1,6}\s*|--\s*|#\s*|\(\*\s*|<!--\s*)/, '').replace(/\s*(?:\*\)|-->)\s*$/, '').trim()).find(Boolean)?.slice(0, 70) || `새 ${cellTypes[kind].label}`;
}
export function parseCell(name: string): { id: string; kind: CellKind } | undefined {
  const match = /^(\d{13}-[0-9a-f-]{36})\.(chat|lean|v|py|md|html)$/.exec(name);
  if (!match) { return; }
  const kind = (Object.keys(cellTypes) as CellKind[]).find(kind => cellTypes[kind].extension === match[2])!;
  return { id: match[1], kind };
}

/** Each cell is a separate durable CRDT, so concurrent additions never rewrite a shared JSON index. */
export class ResearchWorkspace {
  readonly replicas = new Map<string, Replica>();
  onCells: () => void = () => {};
  onText: (cell: Cell) => void = () => {};
  onState: (state: string) => void = () => {};
  onError: (error: unknown) => void = () => {};
  private poll: ReturnType<typeof setInterval>;
  private refresh?: Promise<void>;
  private closed = false;
  constructor(readonly client: AgoraClient) {
    client.onState = state => this.onState(state);
    client.onChange = () => { void this.load().catch(this.onError); };
    this.poll = setInterval(() => { void this.load().catch(this.onError); }, 5000);
  }
  get cells(): Cell[] { return [...this.replicas.values()].map(replica => replica.cell).sort((a, b) => a.id.localeCompare(b.id)); }
  get pending(): boolean { return [...this.replicas.values()].some(replica => replica.sent < replica.version); }
  async load(): Promise<void> {
    if (this.closed) { return; }
    if (this.refresh) { return this.refresh; }
    this.refresh = this.readCells().finally(() => { this.refresh = undefined; });
    return this.refresh;
  }
  private async readCells(): Promise<void> {
    const entries: [string, string][] = [];
    let cursor: number | null = 0;
    try {
      do {
        const page: { entries: [string, string][]; cursor: number | null } = await this.client.request({ operation: 'list', path: cellDirectory, cursor });
        entries.push(...page.entries); cursor = page.cursor;
      } while (cursor !== null);
    } catch (error) {
      if (error instanceof Error && error.message === 'FILE_NOT_FOUND') { this.onCells(); return; }
      throw error;
    }
    const alive = new Set<string>();
    // Keep the broker queue bounded even for large workspaces.
    for (const [name, type] of entries) {
      const parsed = parseCell(name);
      if (type !== 'file' || !parsed) { continue; }
      alive.add(parsed.id);
      const path = `${cellDirectory}/${name}`;
      let record: FileRecord;
      try { record = await this.client.request<FileRecord>({ operation: 'read', path }); }
      catch (error) { if (error instanceof Error && error.message === 'FILE_NOT_FOUND') { alive.delete(parsed.id); continue; } throw error; }
      if (!record.document) { continue; }
      let replica = this.replicas.get(parsed.id);
      if (!replica) {
        const document = new SharedDocument(record.document);
        const cell: Cell = { ...parsed, path, text: document.text, title: cellTitle(parsed.kind, document.text) };
        replica = { cell, document, version: 0, sent: 0, resourceId: record.resourceId };
        this.replicas.set(parsed.id, replica);
      } else {
        replica.document.merge(record.document);
        replica.resourceId = record.resourceId;
        if (replica.cell.text !== replica.document.text) {
          replica.cell.text = replica.document.text;
          replica.cell.title = cellTitle(replica.cell.kind, replica.cell.text);
          this.onText(replica.cell);
        }
      }
    }
    for (const [id, replica] of this.replicas) {
      if (!alive.has(id)) {
        if (replica.version > replica.sent) { this.onError(new Error('다른 참여자가 이 셀을 삭제했습니다. 현재 탭에서 내용을 복사해 보관하세요.')); }
        clearTimeout(replica.timer); this.replicas.delete(id);
      }
    }
    this.onCells();
  }
  edit(id: string, text: string): void {
    if (!this.client.canWrite) { throw new Error('이 workspace는 읽기 전용입니다.'); }
    const replica = this.replicas.get(id);
    if (!replica || replica.cell.text === text) { return; }
    replica.document.setText(text); replica.cell.text = replica.document.text;
    replica.cell.title = cellTitle(replica.cell.kind, replica.cell.text); replica.version++;
    clearTimeout(replica.timer);
    replica.timer = setTimeout(() => { void this.flushCell(replica).catch(this.onError); }, 150);
    this.onState('Saving');
  }
  private async flushCell(replica: Replica): Promise<void> {
    clearTimeout(replica.timer);
    if (replica.flight) { await replica.flight; }
    replica.flight = (async () => {
      while (replica.sent < replica.version && !this.closed) {
        const version = replica.version;
        await this.client.request({ operation: 'write', path: replica.cell.path, document: replica.document.snapshot, fileId: replica.document.fileId, overwrite: true });
        replica.sent = version;
      }
      if (!this.pending) { this.onState('Connected'); }
    })().finally(() => { replica.flight = undefined; });
    return replica.flight;
  }
  async flush(): Promise<void> { for (const replica of this.replicas.values()) { await this.flushCell(replica); } }
  async add(kind: CellKind, text = ''): Promise<Cell> {
    for (const path of ['/.irade', cellDirectory]) {
      try { await this.client.request({ operation: 'mkdir', path }); }
      catch (error) { if (!(error instanceof Error) || error.message !== 'FILE_EXISTS') { throw error; } }
    }
    const id = `${Date.now()}-${crypto.randomUUID()}`;
    const path = `${cellDirectory}/${id}.${cellTypes[kind].extension}`;
    await this.client.request({ operation: 'write', path, content: encode(new TextEncoder().encode(text)), create: true, overwrite: false });
    // A concurrent invalidation may already be loading an older directory listing.
    await this.load(); await this.load();
    const cell = this.replicas.get(id)?.cell;
    if (!cell) { throw new Error('셀을 다시 불러오지 못했습니다.'); }
    return cell;
  }
  async delete(id: string): Promise<void> {
    const replica = this.replicas.get(id);
    if (!replica) { return; }
    await this.flushCell(replica);
    const current = await this.client.request<FileRecord>({ operation: 'stat', path: replica.cell.path });
    await this.client.request({ operation: 'delete', path: replica.cell.path, resourceId: current.resourceId, recursive: false });
    this.replicas.delete(id); this.onCells();
  }
  dispose(): void { this.closed = true; clearInterval(this.poll); for (const replica of this.replicas.values()) { clearTimeout(replica.timer); } this.client.dispose(); }
}
