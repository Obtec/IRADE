import assert from 'node:assert/strict';
import { test } from 'node:test';
import * as A from '@automerge/automerge';
import { SharedDocument, encode, replacement } from '../src/document.ts';

function base(text: string): string { return encode(A.save(A.from({ fileId: 'same-file', text }))); }

test('two disconnected editors converge without replacing each other’s edits', () => {
	const snapshot = base('a\nb\n');
	const first = new SharedDocument(snapshot);
	const second = new SharedDocument(snapshot);
	first.setText('AA\nb\n'); second.setText('a\nBB\n');
	const firstPending = first.snapshot;
	first.merge(second.snapshot); second.merge(firstPending);
	assert.deepEqual([first.text, second.text], ['AA\nBB\n', 'AA\nBB\n']);
	first.merge(second.snapshot);
	assert.equal(first.text, 'AA\nBB\n');
});

test('typing before a queued remote editor update uses the displayed replica as its base', () => {
	const snapshot = base('a\nb\n');
	const displayed = new SharedDocument(snapshot);
	const aggregate = new SharedDocument(snapshot);
	const remote = new SharedDocument(snapshot);
	remote.setText('AA\nb\n');
	aggregate.merge(remote.snapshot);
	// The user has not seen AA yet. The local edit must not delete that remote insertion.
	displayed.setText('a\nBB\n');
	aggregate.merge(displayed.snapshot);
	assert.equal(aggregate.text, 'AA\nBB\n');
});

test('Unicode, deletion, concurrent insertion and replaced file protection', () => {
	const snapshot = base('한글😀 proof');
	const a = new SharedDocument(snapshot); const b = new SharedDocument(snapshot);
	a.setText('한글😀 증명'); b.setText('함께 한글😀 proof');
	a.merge(b.snapshot);
	assert.equal(a.text, '함께 한글😀 증명');
	assert.throws(() => a.merge(encode(A.save(A.from({ fileId: 'recreated', text: '' })))), /FILE_REPLACED/);
	for (const [before, after] of [['a😀z', 'a😃z'], ['😀abc', '😃abc'], ['ab😀', 'ab'], ['한글', '한😀글']]) {
		const edit = replacement(before, after);
		assert.equal(before.slice(0, edit.start) + edit.text + before.slice(edit.end), after);
	}
});
