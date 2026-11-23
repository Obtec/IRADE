/*---------------------------------------------------------------------------------------------
 *  Copyright (c) 2026 hyeon su Lee. All rights reserved.
 *  Licensed under the MIT License. See LICENSE.txt in the web project root for license information.
 *--------------------------------------------------------------------------------------------*/

/** Base64 utilities for Automerge binary state and minimal text diff helper. */
export function encode(bytes: Uint8Array): string {
	let binary = '';
	for (let index = 0; index < bytes.length; index += 0x8000) {
		binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
	}
	return btoa(binary);
}

export function decode(value: string): Uint8Array {
	return Uint8Array.from(atob(value), character => character.charCodeAt(0));
}

export function replacement(before: string, after: string): { start: number; end: number; text: string } {
	let start = 0;
	const limit = Math.min(before.length, after.length);
	while (start < limit && before[start] === after[start]) { start++; }
	let beforeEnd = before.length;
	let afterEnd = after.length;
	while (beforeEnd > start && afterEnd > start && before[beforeEnd - 1] === after[afterEnd - 1]) {
		beforeEnd--;
		afterEnd--;
	}
	return { start, end: beforeEnd, text: after.slice(start, afterEnd) };
}
