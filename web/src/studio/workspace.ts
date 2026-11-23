/*---------------------------------------------------------------------------------------------
 *  Copyright (c) 2026 hyeon su Lee. All rights reserved.
 *  Licensed under the MIT License. See LICENSE.txt in the web project root for license information.
 *--------------------------------------------------------------------------------------------*/
import { AgoraClient, AgoraError } from '../client.ts';
import {
	allCanvasGroups,
	applyLocalTextToDoc,
	buildTextPatchOperation,
	canAccessItem,
	collaborativeContent,
	createCollaborativeItem,
	defaultPermissionForCreator,
	isValidItemId,
	itemPermissionGroups,
	loadItemDoc,
	mergeWithRemoteItem,
	ServerCanvasSession,
	type CanvasIdentity,
	type CanvasItem,
	type CanvasOperation,
	type CollaborativeDoc,
	type PeerProfile,
	type RemoteEditorPresence,
} from '../canvasSync.ts';

export type CellKind = 'agent' | 'lean' | 'rocq' | 'python' | 'markdown' | 'html';

export interface Cell {
	id: string;
	kind: CellKind;
	path: string;
	text: string;
	title: string;
	permission: string[];
	frameOrder: number;
	locked: boolean;
	canWrite: boolean;
	canExecute: boolean;
}

export const cellDirectory = '/.irade/cells';

export const cellTypes: Record<CellKind, {
	label: string; short: string; description: string; extension: string; language: string; icon: string; color: string;
}> = {
	agent: { label: '에이전트 대화', short: 'Agent', description: '질문과 추론, 대화 맥락을 기록합니다.', extension: 'md', language: 'markdown', icon: '✧', color: 'mint' },
	lean: { label: 'Lean 4 증명', short: 'Lean 4', description: 'Lean 4 형식 정리와 증명을 작성하고 검증합니다.', extension: 'lean', language: 'lean4', icon: '∀', color: 'purple' },
	rocq: { label: 'Rocq 증명', short: 'Rocq', description: 'Coq/Rocq 형식 명제와 전술을 검증합니다.', extension: 'v', language: 'rocq', icon: '⊢', color: 'purple' },
	python: { label: 'Python 코드', short: 'Python', description: '실험 코드와 수치 계산을 실행합니다.', extension: 'py', language: 'python', icon: 'λ', color: 'blue' },
	markdown: { label: 'Markdown 문서', short: 'Markdown', description: '연구 노트와 수식·문서를 정리합니다.', extension: 'md', language: 'markdown', icon: '≡', color: 'sand' },
	html: { label: 'HTML 인터랙티브', short: 'HTML', description: 'CSS/JS가 반영되는 인터랙티브 문서를 작성합니다.', extension: 'html', language: 'html', icon: '⟨⟩', color: 'sand' },
};

const MAX_FRAME_ORDER = 1000000;

export function cellTitle(kind: CellKind, text: string): string {
	const source = kind === 'html' ? text.replace(/<(style|script)\b[^>]*>[\s\S]*?<\/\1>/gi, '') : text;
	for (const raw of source.split(/\r?\n/)) {
		const cleaned = raw.trim()
			.replace(/^#+\s*/, '')
			.replace(/^(--|\/\/|\(\*|#)\s*/, '')
			.replace(/^<!--\s*|\s*-->$/g, '')
			.replace(/^\*+\s*|\*+\)$/, '')
			.replace(/<\/?[^>]+>/g, '')
			.trim();
		if (cleaned) { return cleaned.slice(0, 72); }
	}
	return `${cellTypes[kind].short} 셀`;
}

function inferCellKind(item: CanvasItem): CellKind | undefined {
	if (item.kind === 'code') {
		const mode = String(item.renderMode || '').toLowerCase();
		const lang = String(item.language || '').toLowerCase();
		if (mode === 'irade-lean' || lang === 'lean' || lang === 'lean4') { return 'lean'; }
		if (mode === 'irade-rocq' || lang === 'rocq' || lang === 'coq') { return 'rocq'; }
		return 'python';
	}
	if (item.kind === 'text' || item.kind === 'note') {
		const mode = String(item.renderMode || '').toLowerCase();
		const format = String(item.format || '').toLowerCase();
		if (mode === 'code') {
			const lang = String(item.language || '').toLowerCase();
			if (lang === 'lean' || lang === 'lean4') { return 'lean'; }
			if (lang === 'rocq' || lang === 'coq') { return 'rocq'; }
			return 'python';
		}
		if (mode === 'irade-agent') { return 'agent'; }
		if (mode === 'irade-html' || format === 'html') { return 'html'; }
		return 'markdown';
	}
	return undefined;
}

function hasValidFrameOrder(item: CanvasItem): boolean {
	return typeof item.frameOrder === 'number'
		&& Number.isInteger(item.frameOrder)
		&& item.frameOrder >= 0
		&& item.frameOrder <= MAX_FRAME_ORDER;
}

function canvasItemToCell(id: string, item: CanvasItem, identity: CanvasIdentity, fallbackOrder = MAX_FRAME_ORDER): Cell | undefined {
	const kind = inferCellKind(item);
	if (!kind) { return undefined; }
	const text = collaborativeContent(item);
	const locked = item.locked === true;
	const permission = itemPermissionGroups(item.permission);
	const frameOrder = hasValidFrameOrder(item) ? item.frameOrder! : fallbackOrder;
	return {
		id,
		kind,
		path: typeof item.filename === 'string' && item.filename ? item.filename : `irade/${id}.${cellTypes[kind].extension}`,
		text,
		title: typeof item.title === 'string' && item.title ? item.title : cellTitle(kind, text),
		permission,
		frameOrder,
		locked,
		canWrite: !locked && canAccessItem(item, identity, 'write'),
		canExecute: canAccessItem(item, identity, 'execute'),
	};
}

function planAppendOrder(cells: readonly Cell[]): { nextOrder: number; patches: CanvasOperation[] } {
	if (!cells.length) { return { nextOrder: 0, patches: [] }; }
	const maxOrder = cells.reduce((max, cell) => Math.max(max, cell.frameOrder), -10);
	if (maxOrder + 10 <= MAX_FRAME_ORDER) {
		return { nextOrder: maxOrder + 10, patches: [] };
	}
	if (maxOrder < MAX_FRAME_ORDER) {
		return { nextOrder: maxOrder + 1, patches: [] };
	}

	let startIdx = cells.length;
	while (startIdx > 0 && cells[startIdx - 1].canWrite) {
		startIdx -= 1;
	}
	if (startIdx >= cells.length) {
		return { nextOrder: MAX_FRAME_ORDER, patches: [] };
	}

	const anchor = startIdx > 0 ? cells[startIdx - 1].frameOrder : -1;
	const available = MAX_FRAME_ORDER - Math.max(-1, anchor);
	const count = cells.length - startIdx;
	if (available < count + 1) {
		return { nextOrder: MAX_FRAME_ORDER, patches: [] };
	}

	const step = available >= (count + 1) * 10 ? 10 : 1;
	const base = anchor < 0 ? 0 : anchor + step;
	const patches: CanvasOperation[] = [];
	for (let i = startIdx; i < cells.length; i += 1) {
		const targetOrder = base + (i - startIdx) * step;
		if (cells[i].frameOrder !== targetOrder) {
			patches.push({ action: 'patch', id: cells[i].id, patch: { frameOrder: targetOrder } });
		}
	}
	return { nextOrder: base + count * step, patches };
}

function planInsertAfterOrder(cells: readonly Cell[], afterId: string): { nextOrder: number; patches: CanvasOperation[] } {
	const idx = cells.findIndex(cell => cell.id === afterId);
	if (idx < 0 || idx >= cells.length - 1) {
		return planAppendOrder(cells);
	}
	const prevOrder = cells[idx].frameOrder;
	const nextOrder = cells[idx + 1].frameOrder;
	if (nextOrder - prevOrder >= 2) {
		return {
			nextOrder: prevOrder + Math.floor((nextOrder - prevOrder) / 2),
			patches: [],
		};
	}

	// Shift only the minimal writable trailing cluster by step 2 so a local gap opens up.
	const forwardPatches: CanvasOperation[] = [];
	let cursor = prevOrder + 2;
	let forwardOk = true;
	for (let j = idx + 1; j < cells.length; j += 1) {
		if (cells[j].frameOrder >= cursor) { break; }
		if (!cells[j].canWrite || cursor > MAX_FRAME_ORDER) {
			forwardOk = false;
			break;
		}
		forwardPatches.push({ action: 'patch', id: cells[j].id, patch: { frameOrder: cursor } });
		cursor += 2;
	}
	if (forwardOk && prevOrder + 1 < MAX_FRAME_ORDER) {
		return { nextOrder: prevOrder + 1, patches: forwardPatches };
	}

	// Fallback: shift a tight writable prefix backward toward 0 if trailing neighbor is locked.
	const backwardPatches: CanvasOperation[] = [];
	let backCursor = cells[idx].frameOrder - 2;
	let backOk = prevOrder >= 2;
	if (backOk) {
		for (let j = idx; j >= 0; j -= 1) {
			if (cells[j].frameOrder <= backCursor) { break; }
			if (!cells[j].canWrite || backCursor < 0) {
				backOk = false;
				break;
			}
			backwardPatches.push({ action: 'patch', id: cells[j].id, patch: { frameOrder: backCursor } });
			backCursor -= 2;
		}
	}
	if (backOk) {
		return { nextOrder: prevOrder - 1, patches: backwardPatches };
	}

	return planAppendOrder(cells);
}

function buildCanvasItemForCell(
	id: string,
	kind: CellKind,
	text: string,
	frameOrder: number,
	permission?: string[],
): CanvasItem {
	const base: CanvasItem = kind === 'python' || kind === 'lean' || kind === 'rocq'
		? {
			kind: 'code',
			code: text,
			language: kind === 'lean' ? 'lean4' : kind === 'rocq' ? 'rocq' : 'python',
			renderMode: `irade-${kind}`,
			filename: `irade/${id}.${cellTypes[kind].extension}`,
			title: cellTitle(kind, text),
			frameOrder,
			locked: false,
		}
		: {
			kind: 'text',
			text,
			format: kind === 'html' ? 'html' : 'markdown',
			renderMode: `irade-${kind}`,
			filename: `irade/${id}.${cellTypes[kind].extension}`,
			title: cellTitle(kind, text),
			frameOrder,
			locked: false,
		};
	if (permission?.length) { base.permission = permission; }
	return createCollaborativeItem(base);
}

/**
 * Syncs IRADE research cells as first-class shared workspace items.
 * with per-item IDs, group ACLs, delta text streaming, Automerge convergence, and RTC presence.
 */
export class ResearchWorkspace {
	private readonly session: ServerCanvasSession;
	private readonly docs = new Map<string, CollaborativeDoc>();
	private readonly dirty = new Map<string, { text: string; changes: string[] }>();
	private readonly dirtyTimers = new Map<string, ReturnType<typeof setTimeout>>();
	private inFlightTexts = new Map<string, string>();
	private snapshotWaiters: Array<() => void> = [];
	private flushing = false;
	private queueFlush = false;
	private lastSnapshotLoaded = false;
	cells: Cell[] = [];

	onCells: () => void = () => {};
	onText: (cell: Cell) => void = () => {};
	onError: (error: Error) => void = () => {};
	onState: (state: string) => void = () => {};
	onIdentityChange: (identity: CanvasIdentity) => void = () => {};
	onPresence: (peers: PeerProfile[], editors: RemoteEditorPresence[]) => void = () => {};

	constructor(readonly client: AgoraClient, private readonly debounceMs = 60) {
		this.session = new ServerCanvasSession(
			client.socketClientId,
			frame => client.sendCanvasFrame(frame),
			code => new AgoraError(code),
			{
				onSnapshot: () => this.rebuildFromSnapshot(),
				onCommit: (changedIds, removedIds, isOwnCommit, authorId) => this.applyRemoteCommit(changedIds, removedIds, isOwnCommit, authorId),
				onError: (code, error) => this.handleSessionError(code, error),
			},
		);

		client.onState = state => this.onState(state);
		client.onInitItems = (items, meta) => {
			const hadLoaded = this.lastSnapshotLoaded;
			this.lastSnapshotLoaded = true;
			this.session.loadInitial(items, meta.revision, meta.acceptedCount);
			if (hadLoaded && this.session.pending.size) {
				this.session.replayPendingAfterReconnect();
			}
			this.onIdentityChange(meta.identity);
		};
		client.onCanvasMessage = message => {
			if (message.type === 'canvas_commit') { this.session.handleCommit(message); }
			else if (message.type === 'canvas_snapshot') { this.session.handleSnapshot(message); }
			else if (message.type === 'canvas_watermark') { this.session.handleWatermark(message); }
		};
		client.onCanvasError = (requestId, code) => this.session.handleError(requestId, code);
		client.onIdentityChange = identity => {
			this.rebuildCellsOnly();
			this.onIdentityChange(identity);
			this.onCells();
		};
		client.onPresenceChange = (peers, editors) => this.onPresence(peers, editors);
	}

	get pending(): boolean { return this.dirty.size > 0 || this.session.pending.size > 0; }
	get identity(): CanvasIdentity { return this.client.identity; }
	get availableGroups(): string[] { return allCanvasGroups(this.client.identity, this.session.items); }
	get peers(): PeerProfile[] { return this.client.peers; }
	get remoteEditors(): RemoteEditorPresence[] { return this.client.remoteEditors; }
	get revision(): number { return this.session.revision; }

	canvasItem(id: string): CanvasItem | undefined { return this.session.items[id]; }
	canUnlock(id: string): boolean {
		const item = this.session.items[id];
		return Boolean(item && canAccessItem(item, this.client.identity, 'write'));
	}

	async load(): Promise<void> {
		await this.client.ensureConnected();
	}

	async add(kind: CellKind, text = '', options?: { id?: string; permission?: string[]; afterId?: string }): Promise<Cell> {
		if (!this.lastSnapshotLoaded) { await this.load(); }
		const id = options?.id || `cell-${Date.now()}-${crypto.randomUUID().slice(0, 8)}`;
		if (!isValidItemId(id)) { throw new AgoraError('ITEM_ID_INVALID'); }
		const { nextOrder, patches } = options?.afterId
			? planInsertAfterOrder(this.cells, options.afterId)
			: planAppendOrder(this.cells);
		const permission = options?.permission?.length
			? [...new Set(options.permission)]
			: defaultPermissionForCreator(this.client.identity);
		const item = buildCanvasItemForCell(id, kind, text, nextOrder, permission);
		this.docs.set(id, loadItemDoc(item));

		this.onState('Saving');
		try {
			await this.session.mutate([...patches, { action: 'create', id, patch: item }]);
			this.rebuildCellsOnly();
			return this.cells.find(cell => cell.id === id) || canvasItemToCell(id, item, this.client.identity)!;
		} finally {
			this.onState(this.pending ? 'Saving' : 'Connected');
		}
	}

	edit(id: string, text: string): void {
		const currentCell = this.cells.find(cell => cell.id === id);
		if (!currentCell || currentCell.text === text) { return; }
		if (!currentCell.canWrite) { return; }
		const item = this.session.items[id];
		const baseDoc = this.docs.get(id) || loadItemDoc(item);
		const applied = applyLocalTextToDoc(baseDoc, text);
		this.docs.set(id, applied.doc);

		const title = cellTitle(currentCell.kind, text);
		currentCell.text = text;
		currentCell.title = title;

		const prevDirty = this.dirty.get(id);
		const changes = prevDirty ? [...prevDirty.changes] : [];
		if (applied.changeBase64) { changes.push(applied.changeBase64); }
		this.dirty.set(id, { text, changes });
		clearTimeout(this.dirtyTimers.get(id));
		this.dirtyTimers.set(id, setTimeout(() => {
			this.dirtyTimers.delete(id);
			void this.flush().catch(error => this.onError(error instanceof Error ? error : new Error(String(error))));
		}, this.debounceMs));
	}

	async flush(): Promise<void> {
		for (const timer of this.dirtyTimers.values()) { clearTimeout(timer); }
		this.dirtyTimers.clear();
		if (this.flushing) {
			this.queueFlush = true;
			return;
		}
		this.flushing = true;
		let conflictRetries = 0;
		try {
			do {
				this.queueFlush = false;
				const ops: CanvasOperation[] = [];
				const batchTexts = new Map<string, string>();
				for (const [id, pendingEdit] of this.dirty.entries()) {
					const serverItem = this.session.serverItems[id] || this.session.items[id];
					const cell = this.cells.find(item => item.id === id);
					if (!serverItem || !cell || !cell.canWrite) {
						this.dirty.delete(id);
						continue;
					}
					const doc = this.docs.get(id) || loadItemDoc(serverItem);
					const title = cellTitle(cell.kind, pendingEdit.text);
					const op = buildTextPatchOperation(id, serverItem, pendingEdit.text, title, doc, pendingEdit.changes);
					if (op) {
						ops.push(op);
						batchTexts.set(id, pendingEdit.text);
					} else {
						this.dirty.delete(id);
					}
				}
				if (!ops.length) { continue; }
				this.inFlightTexts = batchTexts;
				this.onState('Saving');
				try {
					await this.session.mutate(ops);
					for (const [id, sentText] of batchTexts.entries()) {
						if (this.dirty.get(id)?.text === sentText) {
							this.dirty.delete(id);
						}
					}
				} catch (error) {
					const code = error instanceof AgoraError ? error.code : '';
					if ((code === 'CANVAS_TEXT_CONFLICT' || code === 'CLIENT_SEQUENCE_CONFLICT') && conflictRetries < 3) {
						conflictRetries += 1;
						this.session.requestSync();
						await this.waitForSnapshot();
						this.queueFlush = true;
						continue;
					}
					throw error;
				} finally {
					this.inFlightTexts.clear();
				}
			} while (this.queueFlush || this.dirty.size > 0);
		} finally {
			this.flushing = false;
			this.onState(this.pending ? 'Saving' : 'Connected');
		}
	}

	private waitForSnapshot(): Promise<void> {
		return new Promise(resolve => {
			this.snapshotWaiters.push(resolve);
		});
	}

	async updatePermission(id: string, groups: string[]): Promise<void> {
		const cleaned = [...new Set(groups.map(group => group.trim()).filter(Boolean))];
		if (!cleaned.length || cleaned.length > 64) { throw new AgoraError('ITEM_PERMISSION_INVALID'); }
		if (!this.session.items[id]) { return; }
		await this.flush();
		this.onState('Saving');
		try {
			await this.session.mutate([{ action: 'patch', id, patch: { permission: cleaned } }]);
			this.rebuildCellsOnly();
			this.onCells();
		} finally {
			this.onState(this.pending ? 'Saving' : 'Connected');
		}
	}

	async toggleLock(id: string, locked: boolean): Promise<void> {
		if (!this.session.items[id]) { return; }
		if (!locked || this.dirty.has(id)) {
			// When locking a dirty item, flush pending edits before setting locked=true.
			if (this.dirty.has(id)) { await this.flush(); }
		}
		this.onState('Saving');
		try {
			await this.session.mutate([{ action: 'patch', id, patch: { locked } }]);
			this.rebuildCellsOnly();
			this.onCells();
		} finally {
			this.onState(this.pending ? 'Saving' : 'Connected');
		}
	}

	async moveCell(id: string, direction: -1 | 1): Promise<void> {
		const index = this.cells.findIndex(cell => cell.id === id);
		const neighborIndex = index + direction;
		if (index < 0 || neighborIndex < 0 || neighborIndex >= this.cells.length) { return; }
		const current = this.cells[index];
		const neighbor = this.cells[neighborIndex];
		if (!current.canWrite || !neighbor.canWrite) { throw new AgoraError('ITEM_WRITE_DENIED'); }

		const currentOrder = current.frameOrder === neighbor.frameOrder ? index * 10 : current.frameOrder;
		const neighborOrder = current.frameOrder === neighbor.frameOrder ? neighborIndex * 10 : neighbor.frameOrder;
		await this.flush();
		this.onState('Saving');
		try {
			await this.session.mutate([
				{ action: 'patch', id: current.id, patch: { frameOrder: neighborOrder } },
				{ action: 'patch', id: neighbor.id, patch: { frameOrder: currentOrder } },
			]);
			this.rebuildCellsOnly();
			this.onCells();
		} finally {
			this.onState(this.pending ? 'Saving' : 'Connected');
		}
	}

	async delete(id: string): Promise<void> {
		clearTimeout(this.dirtyTimers.get(id));
		this.dirtyTimers.delete(id);
		this.dirty.delete(id);
		this.docs.delete(id);
		if (!this.session.items[id]) { return; }
		this.onState('Saving');
		try {
			await this.session.mutate([{ action: 'delete', id }]);
			this.rebuildCellsOnly();
			this.onCells();
		} finally {
			this.onState(this.pending ? 'Saving' : 'Connected');
		}
	}

	setEditingCell(itemId: string | null): void {
		for (const cell of this.cells) {
			if (itemId && cell.id === itemId) {
				this.client.sendEditorPresence(itemId, true, this.session.items);
				return;
			}
		}
		if (itemId === null && this.cells.length) {
			this.client.sendEditorPresence(this.cells[0].id, false, this.session.items);
		}
	}

	sendCellPresence(itemId: string, editing: boolean): void {
		this.client.sendEditorPresence(itemId, editing, this.session.items);
	}

	private rebuildFromSnapshot(): void {
		const previousTexts = new Map(this.cells.map(cell => [cell.id, cell.text]));
		for (const id of Object.keys(this.session.items)) {
			const serverItem = this.session.serverItems[id] || this.session.items[id];
			const dirty = this.dirty.get(id);
			if (dirty && serverItem) {
				const merged = mergeWithRemoteItem(this.docs.get(id), serverItem, true);
				this.docs.set(id, merged.doc);
				this.dirty.set(id, { text: merged.text, changes: [] });
			} else if (serverItem) {
				this.docs.set(id, loadItemDoc(serverItem));
			}
		}
		this.rebuildCellsOnly();
		this.onCells();
		for (const cell of this.cells) {
			const prev = previousTexts.get(cell.id);
			if (prev !== undefined && prev !== cell.text) {
				this.onText(cell);
			}
		}
		for (const resolve of this.snapshotWaiters.splice(0)) { resolve(); }
	}

	private applyRemoteCommit(changedIds: string[], removedIds: string[], isOwnCommit: boolean, _authorId?: string): void {
		for (const removedId of removedIds) {
			clearTimeout(this.dirtyTimers.get(removedId));
			this.dirtyTimers.delete(removedId);
			this.dirty.delete(removedId);
			this.inFlightTexts.delete(removedId);
			this.docs.delete(removedId);
		}

		const previousById = new Map(this.cells.map(cell => [cell.id, cell]));
		const textChangedCells: Cell[] = [];
		let structureChanged = removedIds.length > 0;

		for (const id of changedIds) {
			const serverItem = this.session.serverItems[id];
			if (!serverItem) { continue; }
			const previous = previousById.get(id);
			if (!previous) { structureChanged = true; }

			if (isOwnCommit) {
				const sentText = this.inFlightTexts.get(id);
				if (sentText !== undefined && this.dirty.get(id)?.text === sentText) {
					this.dirty.delete(id);
				}
				continue;
			}

			const localDirty = this.dirty.get(id);
			if (localDirty) {
				const merged = mergeWithRemoteItem(this.docs.get(id), serverItem, true);
				this.docs.set(id, merged.doc);
				if (merged.text !== localDirty.text) {
					this.dirty.set(id, { text: merged.text, changes: [] });
				}
			} else {
				this.docs.set(id, loadItemDoc(serverItem));
			}
		}

		this.rebuildCellsOnly();

		for (const cell of this.cells) {
			const prev = previousById.get(cell.id);
			if (!prev) { continue; }
			if (prev.frameOrder !== cell.frameOrder
				|| prev.locked !== cell.locked
				|| prev.canWrite !== cell.canWrite
				|| prev.canExecute !== cell.canExecute
				|| prev.permission.join(',') !== cell.permission.join(',')) {
				structureChanged = true;
			}
			if (prev.text !== cell.text) {
				textChangedCells.push(cell);
			}
		}

		if (structureChanged) {
			this.onCells();
		}
		for (const cell of textChangedCells) {
			this.onText(cell);
		}
	}

	private rebuildCellsOnly(): void {
		const entries = Object.entries(this.session.items);
		let explicitMax = -10;
		for (const [, item] of entries) {
			if (hasValidFrameOrder(item)) {
				explicitMax = Math.max(explicitMax, item.frameOrder!);
			}
		}
		const unpositionedIds = entries
			.filter(([, item]) => !hasValidFrameOrder(item))
			.map(([id]) => id)
			.sort((a, b) => a.localeCompare(b));
		const fallbackById = new Map<string, number>();
		const baseFallback = explicitMax < MAX_FRAME_ORDER ? explicitMax : MAX_FRAME_ORDER;
		unpositionedIds.forEach((id, index) => {
			fallbackById.set(id, Math.min(MAX_FRAME_ORDER, baseFallback + (index + 1) * 10));
		});

		const next: Cell[] = [];
		for (const [id, item] of entries) {
			const cell = canvasItemToCell(id, item, this.client.identity, fallbackById.get(id) ?? MAX_FRAME_ORDER);
			if (!cell) { continue; }
			const dirty = this.dirty.get(id);
			if (dirty) {
				cell.text = dirty.text;
				cell.title = cellTitle(cell.kind, dirty.text);
			}
			next.push(cell);
		}
		next.sort((a, b) => a.frameOrder - b.frameOrder || a.id.localeCompare(b.id));
		this.cells = next;
	}

	private handleSessionError(code: string, error: Error): void {
		if (code === 'CANVAS_TEXT_CONFLICT' || code === 'CLIENT_SEQUENCE_CONFLICT') {
			this.session.requestSync();
			return;
		}
		this.onError(error);
	}

	dispose(): void {
		for (const timer of this.dirtyTimers.values()) { clearTimeout(timer); }
		this.dirtyTimers.clear();
		for (const resolve of this.snapshotWaiters.splice(0)) { resolve(); }
		this.session.dispose();
		this.client.dispose();
	}
}
