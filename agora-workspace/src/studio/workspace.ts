import { AgoraClient, type FileChange, type FileRecord } from '../client.ts';
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
interface Replica { cell: Cell; document: SharedDocument; version: number; sent: number; resourceId: string; epoch: number; timer?: ReturnType<typeof setTimeout>; flight?: Promise<void> }
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
async function sha256Hex(text: string): Promise<string | undefined> {
  if (!globalThis.crypto?.subtle) { return undefined; }
  try {
    const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  } catch {
    return undefined;
  }
}

/** Each cell is a separate durable CRDT, so concurrent additions never rewrite a shared JSON index. */
export class ResearchWorkspace {
  readonly replicas = new Map<string, Replica>();
  onCells: () => void = () => {};
  onText: (cell: Cell) => void = () => {};
  onState: (state: string) => void = () => {};
  onError: (error: unknown) => void = () => {};
  private poll?: ReturnType<typeof setInterval>;
  private refresh?: Promise<void>;
  private dirPromise?: Promise<void>;
  private closed = false;
  private dirsReady = false;
  private listEpoch = 0;
  private activeAdds = 0;
  private creatingPaths = new Set<string>();
  private deletingPaths = new Set<string>();
  constructor(readonly client: AgoraClient, pollMs = 20000) {
    client.onState = state => this.onState(state);
    client.onChange = change => { void this.handleChange(change).catch(this.onError); };
    if (pollMs > 0) {
      this.poll = setInterval(() => {
        if (!this.refresh && this.activeAdds === 0 && !this.pending) {
          void this.load().catch(this.onError);
        }
      }, pollMs);
    }
  }
  get cells(): Cell[] { return [...this.replicas.values()].map(replica => replica.cell).sort((a, b) => a.id.localeCompare(b.id)); }
  get pending(): boolean { return [...this.replicas.values()].some(replica => replica.sent < replica.version); }
  private parseCellPath(path: string): { id: string; kind: CellKind } | undefined {
    if (!path.startsWith(`${cellDirectory}/`)) { return undefined; }
    return parseCell(path.slice(cellDirectory.length + 1));
  }
  private applyRecord(parsed: { id: string; kind: CellKind }, path: string, record: FileRecord, epoch: number): 'added' | 'updated' | 'unchanged' | 'skip' {
    if (!record.document) { return 'skip'; }
    let replica = this.replicas.get(parsed.id);
    if (!replica) {
      const document = new SharedDocument(record.document);
      const cell: Cell = { ...parsed, path, text: document.text, title: cellTitle(parsed.kind, document.text) };
      replica = { cell, document, version: 0, sent: 0, resourceId: record.resourceId, epoch };
      this.replicas.set(parsed.id, replica);
      return 'added';
    }
    replica.document.merge(record.document);
    replica.resourceId = record.resourceId;
    replica.epoch = Math.max(replica.epoch, epoch);
    if (replica.cell.text !== replica.document.text) {
      replica.cell.text = replica.document.text;
      replica.cell.title = cellTitle(replica.cell.kind, replica.cell.text);
      this.onText(replica.cell);
      return 'updated';
    }
    return 'unchanged';
  }
  private async handleChange(change: FileChange): Promise<void> {
    if (this.closed) { return; }
    // Self-initiated mutations already update replicas directly and verify etags against concurrent merges.
    if (change.self) { return; }
    const parsed = this.parseCellPath(change.path);
    if (change.operation === 'delete') {
      if (change.path === cellDirectory || change.path === '/.irade') {
        this.dirsReady = false;
        await this.load();
        return;
      }
      if (this.deletingPaths.has(change.path)) { return; }
      if (parsed) {
        const replica = this.replicas.get(parsed.id);
        if (replica) {
          if (replica.version > replica.sent) { this.onError(new Error('다른 참여자가 이 셀을 삭제했습니다. 현재 탭에서 내용을 복사해 보관하세요.')); }
          clearTimeout(replica.timer); this.replicas.delete(parsed.id);
          this.onCells();
        }
        return;
      }
    }
    if ((change.operation === 'write' || change.created === true) && parsed) {
      if (this.creatingPaths.has(change.path)) { return; }
      await this.loadCell(change.path, true);
      return;
    }
    if (change.operation === 'mkdir' && (change.path === '/.irade' || change.path === cellDirectory)) {
      if (change.path === cellDirectory) { this.dirsReady = true; }
      if (!this.replicas.size) { await this.load(); }
      return;
    }
    await this.load();
  }
  private async loadCell(path: string, notify: boolean, minEpoch?: number): Promise<Cell | undefined> {
    const parsed = this.parseCellPath(path);
    if (!parsed) { return undefined; }
    const epoch = minEpoch ?? this.listEpoch;
    let record: FileRecord;
    try {
      record = await this.client.request<FileRecord>({ operation: 'read', path });
    } catch (error) {
      if (error instanceof Error && error.message === 'FILE_NOT_FOUND') { return undefined; }
      throw error;
    }
    const outcome = this.applyRecord(parsed, path, record, epoch);
    if (notify && (outcome === 'added' || outcome === 'updated')) { this.onCells(); }
    return this.replicas.get(parsed.id)?.cell;
  }
  async load(): Promise<void> {
    if (this.closed) { return; }
    if (this.refresh) { return this.refresh; }
    this.refresh = this.readCells().finally(() => { this.refresh = undefined; });
    return this.refresh;
  }
  private async readCells(): Promise<void> {
    const startedEpoch = this.listEpoch;
    const entries: [string, string][] = [];
    let cursor: number | null = 0;
    try {
      do {
        const page: { entries: [string, string][]; cursor: number | null } = await this.client.request({ operation: 'list', path: cellDirectory, cursor });
        entries.push(...page.entries); cursor = page.cursor;
      } while (cursor !== null);
      this.dirsReady = true;
    } catch (error) {
      if (error instanceof Error && error.message === 'FILE_NOT_FOUND') { this.dirsReady = false; this.onCells(); return; }
      throw error;
    }
    const targets: { parsed: { id: string; kind: CellKind }; path: string }[] = [];
    for (const [name, type] of entries) {
      const parsed = parseCell(name);
      if (type === 'file' && parsed) { targets.push({ parsed, path: `${cellDirectory}/${name}` }); }
    }
    const records = new Array<FileRecord | undefined>(targets.length);
    let next = 0;
    const concurrency = Math.min(4, targets.length);
    await Promise.all(Array.from({ length: concurrency }, async () => {
      while (next < targets.length && !this.closed) {
        const index = next++;
        try { records[index] = await this.client.request<FileRecord>({ operation: 'read', path: targets[index].path }); }
        catch (error) { if (error instanceof Error && error.message === 'FILE_NOT_FOUND') { continue; } throw error; }
      }
    }));
    const alive = new Set<string>();
    for (let index = 0; index < targets.length; index++) {
      const record = records[index];
      if (!record?.document) { continue; }
      const { parsed, path } = targets[index];
      alive.add(parsed.id);
      this.applyRecord(parsed, path, record, startedEpoch);
    }
    for (const [id, replica] of this.replicas) {
      if (!alive.has(id) && replica.epoch <= startedEpoch && !this.creatingPaths.has(replica.cell.path)) {
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
        const sentText = replica.cell.text;
        const result = await this.client.request<{ etag?: string }>({
          operation: 'write',
          path: replica.cell.path,
          document: replica.document.snapshot,
          fileId: replica.document.fileId,
          overwrite: true,
        });
        replica.sent = version;
        const sentDigest = await sha256Hex(sentText);
        if (!sentDigest || !result.etag || result.etag !== sentDigest) {
          await this.loadCell(replica.cell.path, false);
        }
      }
      if (!this.pending) { this.onState('Connected'); }
    })().finally(() => { replica.flight = undefined; });
    return replica.flight;
  }
  async flush(): Promise<void> { for (const replica of this.replicas.values()) { await this.flushCell(replica); } }
  private async ensureDirs(): Promise<void> {
    if (this.dirsReady) { return; }
    if (this.dirPromise) { return this.dirPromise; }
    this.dirPromise = (async () => {
      for (const path of ['/.irade', cellDirectory]) {
        try { await this.client.request({ operation: 'mkdir', path }); }
        catch (error) { if (!(error instanceof Error) || error.message !== 'FILE_EXISTS') { throw error; } }
      }
      this.dirsReady = true;
    })().finally(() => { this.dirPromise = undefined; });
    return this.dirPromise;
  }
  async add(kind: CellKind, text = ''): Promise<Cell> {
    this.activeAdds++;
    const id = `${Date.now()}-${crypto.randomUUID()}`;
    const path = `${cellDirectory}/${id}.${cellTypes[kind].extension}`;
    this.creatingPaths.add(path);
    try {
      await this.ensureDirs();
      const writeEpoch = ++this.listEpoch;
      const payload = { operation: 'write', path, content: encode(new TextEncoder().encode(text)), create: true, overwrite: false } as const;
      try {
        await this.client.request(payload);
      } catch (error) {
        if (error instanceof Error && error.message === 'FILE_NOT_FOUND') {
          this.dirsReady = false;
          await this.ensureDirs();
          await this.client.request(payload);
        } else {
          throw error;
        }
      }
      const cell = await this.loadCell(path, true, writeEpoch);
      if (!cell) { throw new Error('셀을 다시 불러오지 못했습니다.'); }
      return cell;
    } finally {
      this.creatingPaths.delete(path);
      this.activeAdds--;
    }
  }
  async delete(id: string): Promise<void> {
    const replica = this.replicas.get(id);
    if (!replica) { return; }
    this.deletingPaths.add(replica.cell.path);
    this.listEpoch++;
    try {
      await this.flushCell(replica);
      const current = await this.client.request<FileRecord>({ operation: 'stat', path: replica.cell.path });
      await this.client.request({ operation: 'delete', path: replica.cell.path, resourceId: current.resourceId, recursive: false });
      this.replicas.delete(id); this.onCells();
    } finally {
      this.deletingPaths.delete(replica.cell.path);
    }
  }
  dispose(): void { this.closed = true; clearInterval(this.poll); for (const replica of this.replicas.values()) { clearTimeout(replica.timer); } this.client.dispose(); }
}
