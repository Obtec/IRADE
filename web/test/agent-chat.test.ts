import test from 'node:test';
import assert from 'node:assert/strict';
import {
	CHAT_INITIAL_VISIBLE,
	CHAT_LOAD_STEP,
	appendAgentMessage,
	avatarMonogram,
	formatChatBodyHtml,
	parseAgentMessages,
	peerAvatarTone,
	resolveChatAlignment,
} from '../src/studio/agentChat.ts';

test('parses and appends structured turns without splitting embedded markdown headings', () => {
	let doc = appendAgentMessage('# 수학 연구 방', {
		id: 'm1',
		role: 'user',
		uid: '7',
		cid: 'tab-a',
		author: 'Alice',
		ts: 1700000000000,
		body: '이 정리를 검증해 줘.\n\n## 세부 가정\n양수 조건 포함',
	});
	doc = appendAgentMessage(doc, {
		id: 'm2',
		role: 'assistant',
		uid: 'irade-agent',
		author: 'IRADE AI',
		model: 'exaone-2.5',
		ts: 1700000010000,
		body: '## 요약\n증명이 성립합니다.\n```lean4\ntheorem ok : True := trivial\n```',
	});

	const parsed = parseAgentMessages(doc);
	assert.equal(parsed.title, '수학 연구 방');
	assert.equal(parsed.messages.length, 2);
	assert.equal(parsed.messages[0].author, 'Alice');
	assert.ok(parsed.messages[0].body.includes('## 세부 가정'));
	assert.equal(parsed.messages[1].role, 'assistant');
	assert.equal(parsed.messages[1].model, 'exaone-2.5');
	assert.ok(parsed.messages[1].body.includes('## 요약'));
});

test('supports legacy heading turns and migrates cleanly when appending new messages', () => {
	const legacy = '# 연구 대화\n\n## 사용자\n첫 질문\n\n## 에이전트\n초기 응답\n';
	const before = parseAgentMessages(legacy);
	assert.equal(before.messages.length, 2);
	assert.equal(before.messages[0].role, 'user');
	assert.equal(before.messages[1].role, 'assistant');

	const migrated = appendAgentMessage(legacy, {
		id: 'm3',
		role: 'user',
		uid: '8',
		author: 'Dr. Kim',
		ts: 1700000020000,
		body: '후속 코멘트 --> 확인',
	});
	const after = parseAgentMessages(migrated);
	assert.equal(after.messages.length, 3);
	assert.equal(after.messages[2].author, 'Dr. Kim');
	assert.equal(after.messages[2].body, '후속 코멘트 --> 확인');
});

test('resolves alignments accurately across self, peer by uid, peer by live cid, and assistant', () => {
	const viewer = { uid: '7', cid: 'tab-1', name: 'Alice' };
	const livePeers = new Set(['tab-2']);

	assert.equal(resolveChatAlignment({ id: '1', role: 'assistant', author: 'IRADE AI', body: 'hi' }, viewer, livePeers), 'assistant');
	assert.equal(resolveChatAlignment({ id: '2', role: 'user', uid: '7', cid: 'tab-1', author: 'Alice', body: 'mine' }, viewer, livePeers), 'self');
	assert.equal(resolveChatAlignment({ id: '3', role: 'user', uid: '8', cid: 'tab-9', author: 'Dr. Kim', body: 'theirs' }, viewer, livePeers), 'peer');
	assert.equal(resolveChatAlignment({ id: '4', role: 'user', uid: '7', cid: 'tab-2', author: 'Alice', body: 'other tab' }, viewer, livePeers), 'peer');
});

test('formats markdown code/bold safely with XSS escaping and stable avatar helpers', () => {
	const html = formatChatBodyHtml('확인 **중요**: <script>alert(1)</script> & `x + 1`');
	assert.ok(!html.includes('<script>'));
	assert.ok(html.includes('&lt;script&gt;'));
	assert.ok(html.includes('<strong>중요</strong>'));
	assert.ok(html.includes('<code class="chat-inline-code">x + 1</code>'));

	assert.equal(avatarMonogram('Dr. Kim'), 'DK');
	assert.equal(avatarMonogram('김연구'), '김');
	assert.equal(avatarMonogram('나 (체험)'), '나');
	assert.equal(peerAvatarTone('user-8'), peerAvatarTone('user-8'));
	assert.ok(CHAT_INITIAL_VISIBLE >= 6);
	assert.ok(CHAT_LOAD_STEP >= 4);
});
