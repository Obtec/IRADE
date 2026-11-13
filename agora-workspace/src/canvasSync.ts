/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/
import * as A from '@automerge/automerge';
import { decode, encode } from './document.ts';

export type CanvasCapability = 'read' | 'write' | 'execute';

export interface CanvasIdentity {
	userId?: string;
	groups: string[];
	memberGroups: string[];
	writeGroups: string[];
	executeGroups: string[];
	isAdmin: boolean;
	groupBits?: Record<string, number>;
}

export interface TextDeltaEdit {
	at: number;
	remove: number;
	insert: string;
	base?: string;
}

export interface PatchOp {
	op: 'add' | 'replace' | 'remove' | 'text';
	path: string;
	value?: unknown;
}

export interface AutomergeChangeEntry {
	field?: string;
	change: string;
}

export interface CanvasItem {
	kind: string;
	text?: string;
	code?: string;
	language?: string;
	format?: string;
	renderMode?: string;
	title?: string;
	filename?: string;
	frameOrder?: number;
	permission?: string[] | string | Record<string, unknown>;
	locked?: boolean;
	automerge_snapshot?: string;
	automerge_changes?: Array<string | AutomergeChangeEntry>;
	[key: string]: unknown;
}

export type CanvasOperation =
	| {
		action: 'create';
		id: string;
		patch: CanvasItem;
	}
	| {
		action: 'patch';
		id: string;
		patch?: Record<string, unknown>;
		text_edits?: Record<string, TextDeltaEdit>;
		unset?: string[];
	}
	| {
		action: 'delete';
		id: string;
	};

export interface PeerProfile {
	peer_id: string;
	user_id?: string | number;
	nickname?: string;
	tag_number?: number;
	groups?: string[];
	is_admin?: boolean;
	[key: string]: unknown;
}

export interface RemoteEditorPresence {
	peer: PeerProfile;
	itemId: string;
	editing: boolean;
	updatedAt: number;
}

export interface CanvasCommitMessage {
	type: 'canvas_commit';
	revision?: number;
	author_id?: string | number;
	client_id?: string;
	request_id?: string;
	request_count?: number;
	items?: Record<string, CanvasItem | null>;
	patches?: Record<string, PatchOp[]>;
	changed_ids?: string[];
	permissions?: Record<string, unknown>;
	previous_permissions?: Record<string, unknown>;
}

export interface CanvasSnapshotMessage {
	type: 'canvas_snapshot';
	revision?: number;
	accepted_count?: number;
	items?: Record<string, CanvasItem>;
}

export interface CanvasWatermarkMessage {
	type: 'canvas_watermark';
	client_id?: string;
	accepted_count?: number;
	revision?: number;
}

export type CollaborativeDoc = A.Doc<{ content: string }>;

const utf8Encoder = new TextEncoder();
const utf8Decoder = new TextDecoder('utf-8', { fatal: true });
const FNV_OFFSET = 14695981039346656037n;
const FNV_PRIME = 1099511628211n;
const FNV_MASK = 0xffffffffffffffffn;
const FORBIDDEN_IDS = new Set(['__proto__', 'constructor', 'prototype']);
const COMPACT_AFTER_CHANGES = 24;

export function isValidItemId(id: string): boolean {
	if (!id || FORBIDDEN_IDS.has(id)) { return false; }
	const bytes = utf8Encoder.encode(id).length;
	return bytes >= 1 && bytes <= 128;
}

export function itemPermissionGroups(permission: unknown): string[] {
	if (typeof permission === 'string') { return permission ? [permission] : []; }
	if (Array.isArray(permission)) {
		return [...new Set(permission.filter((entry): entry is string => typeof entry === 'string' && entry.length > 0))];
	}
	if (permission && typeof permission === 'object') {
		return Object.entries(permission)
			.filter(([, level]) => level === true || (typeof level === 'number' && level > 0))
			.map(([group]) => group);
	}
	return [];
}

function compileGroupMaskMap(layout?: Record<string, number>): Map<string, bigint> | undefined {
	if (!layout || typeof layout !== 'object' || Array.isArray(layout)) { return undefined; }
	const entries = Object.entries(layout);
	if (!entries.length || entries.length > 64) { return undefined; }
	const bits = new Map<string, bigint>();
	let used = 0n;
	for (const [name, index] of entries) {
		if (!Number.isInteger(index) || index < 0 || index >= 64) { return undefined; }
		const bit = 1n << BigInt(index);
		if ((used & bit) !== 0n) { return undefined; }
		bits.set(name, bit);
		used |= bit;
	}
	return bits;
}

function groupSetMask(groups: string[], bits: Map<string, bigint>): bigint {
	let mask = 0n;
	for (const group of groups) { mask |= bits.get(group) ?? 0n; }
	return mask;
}

/** Matches CanvasGroupBits.hpp / CanvasQueryRules.hpp per-item read, write, and execute checks. */
export function canAccessItem(
	item: Pick<CanvasItem, 'permission'> | undefined,
	identity: CanvasIdentity | undefined,
	capability: CanvasCapability = 'read',
): boolean {
	if (!identity) { return true; }
	if (identity.isAdmin) { return true; }
	const userGroups = capability === 'write'
		? identity.writeGroups
		: capability === 'execute'
			? identity.executeGroups
			: identity.groups;
	const itemGroups = itemPermissionGroups(item?.permission);
	if (!userGroups.length || !itemGroups.length) { return false; }
	const bits = compileGroupMaskMap(identity.groupBits);
	if (bits) {
		return (groupSetMask(userGroups, bits) & groupSetMask(itemGroups, bits)) !== 0n;
	}
	const allowed = new Set(userGroups);
	return itemGroups.some(group => allowed.has(group));
}

export function allCanvasGroups(
	identity: CanvasIdentity | undefined,
	items: Record<string, CanvasItem>,
): string[] {
	const groups = new Set<string>();
	for (const group of Object.keys(identity?.groupBits || {})) { groups.add(group); }
	for (const list of [identity?.memberGroups, identity?.groups, identity?.writeGroups, identity?.executeGroups]) {
		for (const group of list || []) { if (group) { groups.add(group); } }
	}
	for (const item of Object.values(items)) {
		for (const group of itemPermissionGroups(item?.permission)) { groups.add(group); }
	}
	return [...groups].sort((left, right) => left.localeCompare(right));
}

export function defaultPermissionForCreator(identity: CanvasIdentity | undefined): string[] | undefined {
	if (!identity) { return undefined; }
	if (identity.memberGroups.length) { return [...identity.memberGroups]; }
	if (identity.writeGroups.length) { return [...identity.writeGroups]; }
	if (identity.groups.length) { return [...identity.groups]; }
	return undefined;
}

/** 64-bit FNV-1a decimal hash matching CanvasTextDelta.hpp::hash() and itemDelta.js. */
export function fnv1a64(text: string): string {
	let hash = FNV_OFFSET;
	for (const byte of utf8Encoder.encode(text)) {
		hash ^= BigInt(byte);
		hash = (hash * FNV_PRIME) & FNV_MASK;
	}
	return hash.toString(10);
}

/** Computes a UTF-8 byte delta on codepoint boundaries matching agora::textdelta::diff. */
export function computeTextDelta(before: string, after: string): TextDeltaEdit | null {
	if (before === after) { return null; }
	const a = Array.from(before);
	const b = Array.from(after);
	let prefix = 0;
	while (prefix < a.length && prefix < b.length && a[prefix] === b[prefix]) { prefix++; }
	let endA = a.length;
	let endB = b.length;
	while (endA > prefix && endB > prefix && a[endA - 1] === b[endB - 1]) {
		endA--;
		endB--;
	}
	const at = utf8Encoder.encode(a.slice(0, prefix).join('')).length;
	const remove = utf8Encoder.encode(a.slice(prefix, endA).join('')).length;
	const insert = b.slice(prefix, endB).join('');
	return { at, remove, insert, base: fnv1a64(before) };
}

export function applyTextDelta(before: string, ops: TextDeltaEdit | TextDeltaEdit[]): string {
	const list = Array.isArray(ops) ? ops : [ops];
	let current = before;
	for (const edit of list) {
		if (edit.base && fnv1a64(current) !== edit.base) {
			throw new Error('CANVAS_TEXT_CONFLICT');
		}
		const bytes = utf8Encoder.encode(current);
		if (!Number.isInteger(edit.at) || !Number.isInteger(edit.remove)
			|| edit.at < 0 || edit.remove < 0 || edit.at + edit.remove > bytes.length) {
			throw new Error('CANVAS_TEXT_CONFLICT');
		}
		const head = utf8Decoder.decode(bytes.subarray(0, edit.at));
		const tail = utf8Decoder.decode(bytes.subarray(edit.at + edit.remove));
		current = head + String(edit.insert ?? '') + tail;
	}
	return current;
}

function unescapeJsonPointerSegment(segment: string): string {
	return segment.replaceAll('~1', '/').replaceAll('~0', '~');
}

export function applyItemPatchList(previous: CanvasItem | undefined, ops: PatchOp[]): CanvasItem | undefined {
	if (!previous || typeof previous !== 'object') { return previous; }
	const next: CanvasItem = structuredClone(previous);
	for (const patch of ops) {
		if (typeof patch?.path !== 'string' || !patch.path.startsWith('/')) { continue; }
		const key = unescapeJsonPointerSegment(patch.path.slice(1).split('/')[0] || '');
		if (!key || FORBIDDEN_IDS.has(key)) { continue; }
		if (patch.op === 'remove') {
			delete next[key];
		} else if (patch.op === 'add' || patch.op === 'replace') {
			next[key] = structuredClone(patch.value);
		} else if (patch.op === 'text' && patch.value && typeof patch.value === 'object') {
			next[key] = applyTextDelta(
				typeof next[key] === 'string' ? next[key] as string : '',
				patch.value as TextDeltaEdit | TextDeltaEdit[],
			);
		}
	}
	return next;
}

export function collaborativeField(item: Pick<CanvasItem, 'kind'>): 'code' | 'text' {
	return item.kind === 'code' ? 'code' : 'text';
}

export function collaborativeContent(item: CanvasItem | undefined): string {
	if (!item) { return ''; }
	const field = collaborativeField(item);
	return typeof item[field] === 'string' ? item[field] as string : '';
}

export function isCollaborativeKind(kind: string): boolean {
	return kind === 'code' || kind === 'text' || kind === 'note';
}

export function createCollaborativeItem(item: CanvasItem): CanvasItem {
	if (!isCollaborativeKind(item.kind)) { return item; }
	const doc = A.from({ content: collaborativeContent(item) });
	return {
		...item,
		automerge_snapshot: encode(A.save(doc)),
		automerge_changes: [],
	};
}

function codepointSplice(before: string, after: string): { index: number; deleteCount: number; insertText: string } {
	const a = Array.from(before);
	const b = Array.from(after);
	let prefix = 0;
	while (prefix < a.length && prefix < b.length && a[prefix] === b[prefix]) { prefix++; }
	let endA = a.length;
	let endB = b.length;
	while (endA > prefix && endB > prefix && a[endA - 1] === b[endB - 1]) {
		endA--;
		endB--;
	}
	const index = a.slice(0, prefix).join('').length;
	const deleteCount = a.slice(prefix, endA).join('').length;
	const insertText = b.slice(prefix, endB).join('');
	return { index, deleteCount, insertText };
}

export function loadItemDoc(item: CanvasItem | undefined): CollaborativeDoc {
	try {
		let doc: CollaborativeDoc = typeof item?.automerge_snapshot === 'string' && item.automerge_snapshot
			? A.load<{ content: string }>(decode(item.automerge_snapshot))
			: A.from({ content: collaborativeContent(item) });
		for (const entry of item?.automerge_changes || []) {
			const encoded = typeof entry === 'string' ? entry : entry?.change;
			if (encoded) {
				doc = A.loadIncremental<{ content: string }>(doc, decode(encoded));
			}
		}
		const content = collaborativeContent(item);
		if (doc.content !== content && content.length > 0 && !item?.automerge_changes?.length) {
			doc = A.change(doc, draft => { A.updateText(draft, ['content'], content); });
		}
		return doc;
	} catch {
		return A.from({ content: collaborativeContent(item) });
	}
}

export function applyLocalTextToDoc(
	doc: CollaborativeDoc,
	nextText: string,
): { doc: CollaborativeDoc; changeBase64?: string; compactedSnapshot?: string } {
	if (doc.content === nextText) { return { doc }; }
	const splice = codepointSplice(doc.content, nextText);
	const nextDoc = A.change(doc, draft => {
		if (splice.deleteCount || splice.insertText) {
			A.splice(draft, ['content'], splice.index, splice.deleteCount, splice.insertText);
		}
	});
	const lastChange = A.getLastLocalChange(nextDoc);
	return {
		doc: nextDoc,
		changeBase64: lastChange ? encode(lastChange) : undefined,
	};
}

export function mergeWithRemoteItem(
	localDoc: CollaborativeDoc | undefined,
	remoteItem: CanvasItem,
	hasLocalUnsent: boolean,
): { doc: CollaborativeDoc; text: string } {
	const remoteDoc = loadItemDoc(remoteItem);
	const remoteText = collaborativeContent(remoteItem);
	if (!localDoc || !hasLocalUnsent) {
		if (remoteDoc.content !== remoteText) {
			const aligned = A.change(remoteDoc, draft => { A.updateText(draft, ['content'], remoteText); });
			return { doc: aligned, text: aligned.content };
		}
		return { doc: remoteDoc, text: remoteDoc.content };
	}
	try {
		const merged = A.merge(A.clone(localDoc), remoteDoc);
		return { doc: merged, text: merged.content };
	} catch {
		return { doc: remoteDoc, text: remoteText };
	}
}

export function applyOperationOptimistic(
	items: Record<string, CanvasItem>,
	op: CanvasOperation,
): void {
	if (op.action === 'delete') {
		delete items[op.id];
		return;
	}
	if (op.action === 'create' && op.patch) {
		items[op.id] = structuredClone(op.patch);
		return;
	}
	if (op.action === 'patch') {
		const existing = items[op.id];
		if (!existing) { return; }
		const next: CanvasItem = structuredClone(existing);
		if (op.patch) {
			for (const [key, value] of Object.entries(op.patch)) {
				if (FORBIDDEN_IDS.has(key)) { continue; }
				if (key === 'automerge_changes' && Array.isArray(value) && Array.isArray(existing.automerge_changes) && !op.patch.automerge_snapshot) {
					next.automerge_changes = [...existing.automerge_changes, ...value];
				} else if (value === undefined) {
					delete next[key];
				} else {
					next[key] = structuredClone(value);
				}
			}
		}
		if (op.unset) {
			for (const key of op.unset) {
				if (!FORBIDDEN_IDS.has(key)) { delete next[key]; }
			}
		}
		if (op.text_edits) {
			for (const [field, delta] of Object.entries(op.text_edits)) {
				const current = typeof next[field] === 'string' ? next[field] as string : '';
				next[field] = applyTextDelta(current, delta);
			}
		}
		items[op.id] = next;
	}
}

/** Builds compact delta operations compatible with CanvasQueryRules.hpp + CanvasBrokerOperations.hpp. */
export function buildTextPatchOperation(
	id: string,
	baseItem: CanvasItem,
	nextText: string,
	nextTitle: string,
	doc: CollaborativeDoc,
	changes: string[] = [],
): CanvasOperation | null {
	const field = collaborativeField(baseItem);
	const prevText = typeof baseItem[field] === 'string' ? baseItem[field] as string : '';
	const textDelta = computeTextDelta(prevText, nextText);
	const titleChanged = (baseItem.title || '') !== nextTitle;
	if (!textDelta && !titleChanged) { return null; }

	const patch: Record<string, unknown> = {};
	if (titleChanged) { patch.title = nextTitle; }

	const hasAutomerge = typeof baseItem.automerge_snapshot === 'string';
	const existingCount = Array.isArray(baseItem.automerge_changes) ? baseItem.automerge_changes.length : 0;
	if (!hasAutomerge || !changes.length || existingCount + changes.length >= COMPACT_AFTER_CHANGES) {
		patch.automerge_snapshot = encode(A.save(doc));
		patch.automerge_changes = [];
	} else {
		patch.automerge_changes = changes.map(change => ({ field, change }));
	}

	const op: CanvasOperation = { action: 'patch', id };
	if (Object.keys(patch).length) { op.patch = patch; }
	if (textDelta) { op.text_edits = { [field]: textDelta }; }
	return op;
}

interface PendingBatch {
	count: number;
	requestId: string;
	operations: CanvasOperation[];
	resolve: () => void;
	reject: (error: Error) => void;
}

export interface SessionEvents {
	onCommit?: (changedIds: string[], removedIds: string[], isOwnCommit: boolean, authorId?: string) => void;
	onSnapshot?: () => void;
	onError?: (code: string, error: Error) => void;
}

/**
 * Mirrors Project Agora FE's ServerCanvasSession:
 * durable request_count sequencing over canvas_ops / canvas_sync with optimistic rebase.
 */
export class ServerCanvasSession {
	revision = 0;
	acceptedCount = 0;
	requestCount = 0;
	serverItems: Record<string, CanvasItem> = {};
	items: Record<string, CanvasItem> = {};
	readonly pending = new Map<number, PendingBatch>();
	private syncInFlight = false;

	constructor(
		readonly clientId: string,
		private readonly sendFrame: (frame: Record<string, unknown>) => boolean,
		private readonly makeError: (code: string) => Error,
		private readonly events: SessionEvents = {},
	) {}

	loadInitial(items: Record<string, CanvasItem>, revision = 0, acceptedCount = 0): void {
		this.serverItems = structuredClone(items || {});
		this.revision = revision;
		this.acceptedCount = acceptedCount;
		this.requestCount = Math.max(this.requestCount, acceptedCount);
		this.recomputeOptimisticItems();
		this.events.onSnapshot?.();
	}

	mutate(operations: CanvasOperation[]): Promise<void> {
		const requestCount = ++this.requestCount;
		const requestId = crypto.randomUUID();
		return new Promise<void>((resolve, reject) => {
			this.pending.set(requestCount, { count: requestCount, requestId, operations, resolve, reject });
			this.recomputeOptimisticItems();
			this.sendBatch(requestCount, requestId, operations);
		});
	}

	requestSync(): void {
		if (this.syncInFlight) { return; }
		this.syncInFlight = true;
		this.sendFrame({
			type: 'canvas_sync',
			client_id: this.clientId,
			delta_commits: true,
		});
	}

	handleCommit(message: CanvasCommitMessage): void {
		if (typeof message.revision === 'number') {
			this.revision = Math.max(this.revision, message.revision);
		}
		const changedIds = new Set<string>();
		const removedIds = new Set<string>();

		if (message.items) {
			for (const [id, item] of Object.entries(message.items)) {
				if (FORBIDDEN_IDS.has(id)) { continue; }
				if (item === null) {
					if (id in this.serverItems) {
						delete this.serverItems[id];
						removedIds.add(id);
					}
				} else {
					this.serverItems[id] = structuredClone(item);
					changedIds.add(id);
				}
			}
		}

		if (message.patches) {
			for (const [id, patchOps] of Object.entries(message.patches)) {
				if (FORBIDDEN_IDS.has(id) || !Array.isArray(patchOps)) { continue; }
				const updated = applyItemPatchList(this.serverItems[id], patchOps);
				if (updated) {
					this.serverItems[id] = updated;
					changedIds.add(id);
				} else {
					// Patch landed for an item we missed locally: fall back to authoritative snapshot.
					this.requestSync();
					return;
				}
			}
		}

		const isOwnCommit = message.client_id === this.clientId && typeof message.request_count === 'number';
		if (isOwnCommit) {
			this.acceptedCount = Math.max(this.acceptedCount, message.request_count!);
			for (const [count, entry] of this.pending.entries()) {
				if (count <= message.request_count!) {
					this.pending.delete(count);
					entry.resolve();
				}
			}
		}

		this.recomputeOptimisticItems();
		const authorId = message.author_id != null ? String(message.author_id) : undefined;
		this.events.onCommit?.([...changedIds], [...removedIds], isOwnCommit, authorId);
	}

	handleSnapshot(message: CanvasSnapshotMessage): void {
		this.syncInFlight = false;
		this.serverItems = structuredClone(message.items || {});
		if (typeof message.revision === 'number') { this.revision = message.revision; }
		if (typeof message.accepted_count === 'number') {
			this.acceptedCount = message.accepted_count;
			for (const [count, entry] of this.pending.entries()) {
				if (count <= message.accepted_count) {
					this.pending.delete(count);
					entry.resolve();
				}
			}
			this.recomputeRequestCount();
		}
		this.recomputeOptimisticItems();
		this.events.onSnapshot?.();
	}

	handleWatermark(message: CanvasWatermarkMessage): void {
		if (message.client_id && message.client_id !== this.clientId) { return; }
		if (typeof message.accepted_count === 'number') {
			this.acceptedCount = Math.max(this.acceptedCount, message.accepted_count);
		}
		if (typeof message.revision === 'number') {
			this.revision = Math.max(this.revision, message.revision);
		}
		for (const [count, entry] of this.pending.entries()) {
			if (count <= this.acceptedCount) {
				this.pending.delete(count);
				entry.resolve();
			}
		}
		this.recomputeRequestCount();
		if (this.pending.size) {
			this.requestSync();
		}
	}

	handleError(requestId: string | undefined, code: string): void {
		if (requestId) {
			for (const [count, entry] of this.pending.entries()) {
				if (entry.requestId === requestId) {
					this.pending.delete(count);
					this.recomputeRequestCount();
					this.recomputeOptimisticItems();
					const error = this.makeError(code);
					entry.reject(error);
					if (code === 'CLIENT_SEQUENCE_CONFLICT') { this.requestSync(); }
					this.events.onError?.(code, error);
					return;
				}
			}
		}
		if (code === 'CLIENT_SEQUENCE_CONFLICT') { this.requestSync(); }
		this.events.onError?.(code, this.makeError(code));
	}

	private recomputeRequestCount(): void {
		let maxPending = this.acceptedCount;
		for (const count of this.pending.keys()) {
			if (count > maxPending) { maxPending = count; }
		}
		this.requestCount = maxPending;
	}

	replayPendingAfterReconnect(): void {
		this.requestCount = Math.max(this.requestCount, this.acceptedCount);
		const entries = [...this.pending.values()].sort((a, b) => a.count - b.count);
		for (const entry of entries) {
			if (entry.count <= this.acceptedCount) {
				this.pending.delete(entry.count);
				entry.resolve();
				continue;
			}
			this.sendBatch(entry.count, entry.requestId, entry.operations);
		}
	}

	dispose(): void {
		const error = this.makeError('DISCONNECTED');
		for (const entry of this.pending.values()) { entry.reject(error); }
		this.pending.clear();
	}

	private sendBatch(count: number, requestId: string, operations: CanvasOperation[]): void {
		this.sendFrame({
			type: 'canvas_ops',
			request_id: requestId,
			client_id: this.clientId,
			request_count: count,
			delta_commits: true,
			operations,
		});
	}

	private recomputeOptimisticItems(): void {
		const next: Record<string, CanvasItem> = structuredClone(this.serverItems);
		const ordered = [...this.pending.values()].sort((a, b) => a.count - b.count);
		for (const batch of ordered) {
			for (const op of batch.operations) {
				try {
					applyOperationOptimistic(next, op);
				} catch {
					// Wait for authoritative commit / resync if optimistic text delta cannot apply locally.
				}
			}
		}
		this.items = next;
	}
}
