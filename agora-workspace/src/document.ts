/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/
import * as A from '@automerge/automerge';

export interface FileDocument { fileId: string; text: string }

export function encode(bytes: Uint8Array): string {
	let value = '';
	for (let index = 0; index < bytes.length; index += 8192) {
		value += String.fromCharCode(...bytes.subarray(index, index + 8192));
	}
	return btoa(value);
}

export function decode(value: string): Uint8Array {
	return Uint8Array.from(atob(value), character => character.charCodeAt(0));
}

/** A replica survives reconnects; incoming snapshots merge with unsent local edits. */
export class SharedDocument {
	private doc: A.Doc<FileDocument>;
	constructor(snapshot: string) {
		this.doc = A.load<FileDocument>(decode(snapshot));
	}
	get text(): string { return this.doc.text; }
	get fileId(): string { return this.doc.fileId; }
	get snapshot(): string { return encode(A.save(this.doc)); }
	setText(text: string): void {
		if (this.doc.text !== text) {
			this.doc = A.change(this.doc, draft => A.updateText(draft, ['text'], text));
		}
	}
	merge(snapshot: string): void {
		const remote = A.load<FileDocument>(decode(snapshot));
		if (remote.fileId !== this.doc.fileId) { throw new Error('FILE_REPLACED'); }
		this.doc = A.merge(this.doc, remote);
	}
}

/** The smallest replacement preserves the unaffected selection and editor scroll. */
export function replacement(before: string, after: string): { start: number; end: number; text: string } {
	let start = 0;
	while (start < before.length && start < after.length && before[start] === after[start]) { start++; }
	// Do not split a UTF-16 surrogate pair at a replacement boundary.
	if (start > 0 && /[\uD800-\uDBFF]/.test(before[start - 1])) { start--; }
	let end = before.length;
	let nextEnd = after.length;
	while (end > start && nextEnd > start && before[end - 1] === after[nextEnd - 1]) { end--; nextEnd--; }
	if (end < before.length && /[\uDC00-\uDFFF]/.test(before[end])) { end++; nextEnd++; }
	return { start, end, text: after.slice(start, nextEnd) };
}
