export const CHAT_INITIAL_VISIBLE = 8;
export const CHAT_LOAD_STEP = 6;

export type ChatSenderRole = 'user' | 'assistant' | 'system';
export type ChatAlignment = 'self' | 'peer' | 'assistant' | 'system';

export interface ChatMessage {
	id: string;
	role: ChatSenderRole;
	uid?: string;
	cid?: string;
	author: string;
	model?: string;
	ts?: number;
	body: string;
	limited?: boolean;
}

export interface ParsedAgentDoc {
	title: string;
	messages: ChatMessage[];
}

export interface ChatViewerIdentity {
	uid?: string;
	cid?: string;
	name?: string;
}

interface StoredMsgMeta {
	id?: unknown;
	role?: unknown;
	uid?: unknown;
	cid?: unknown;
	author?: unknown;
	model?: unknown;
	ts?: unknown;
	limited?: unknown;
}

const MSG_MARKER_RE = /^<!--\s*irade-msg:(\{[^\r\n]*\})\s*-->$/gm;
const LEGACY_HEADING_RE = /^##\s+(사용자|에이전트|연구\s*메모|IRADE\s*AI|AI|참여자|상대방|User|Assistant)\s*$/gmi;

export const AGENT_AVATAR_SVG = `<svg viewBox="0 0 36 36" width="36" height="36" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <defs>
    <linearGradient id="iradeAgentBg" x1="2" y1="2" x2="34" y2="34" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0d3b38"/>
      <stop offset="55%" stop-color="#111b2e"/>
      <stop offset="100%" stop-color="#1e1b4b"/>
    </linearGradient>
    <linearGradient id="iradeAgentSpark" x1="10" y1="8" x2="26" y2="28" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#6ee7b7"/>
      <stop offset="100%" stop-color="#38bdf8"/>
    </linearGradient>
  </defs>
  <rect x="0.75" y="0.75" width="34.5" height="34.5" rx="10.25" fill="url(#iradeAgentBg)" stroke="#34d399" stroke-opacity="0.45" stroke-width="1.5"/>
  <circle cx="18" cy="18" r="11" stroke="#34d399" stroke-opacity="0.28" stroke-width="1.1" stroke-dasharray="3 2.5"/>
  <path d="M18 8.2L20.35 15.65L27.8 18L20.35 20.35L18 27.8L15.65 20.35L8.2 18L15.65 15.65L18 8.2Z" fill="url(#iradeAgentSpark)"/>
  <circle cx="26.5" cy="9.5" r="2" fill="#34d399"/>
  <circle cx="9.5" cy="26.5" r="1.5" fill="#38bdf8" fill-opacity="0.85"/>
</svg>`;

export function escapeHtml(value: string): string {
	return value.replace(/[&<>"']/g, ch => ({
		'&': '&amp;',
		'<': '&lt;',
		'>': '&gt;',
		'"': '&quot;',
		"'": '&#39;',
	}[ch]!));
}

function splitTitleAndContent(raw: string): { title: string; content: string } {
	const normalized = (raw || '').replace(/\r\n?/g, '\n');
	const lines = normalized.split('\n');
	const firstLine = lines[0]?.trim() || '';
	if (firstLine.startsWith('# ') && firstLine.length > 2) {
		return {
			title: firstLine.slice(2).trim(),
			content: lines.slice(1).join('\n').replace(/^\n+/, ''),
		};
	}
	return { title: '연구 대화', content: normalized.trimStart() };
}

function parseLegacySection(section: string, idPrefix: string): ChatMessage[] {
	const trimmed = section.trim();
	if (!trimmed) { return []; }
	const matches: { index: number; end: number; label: string }[] = [];
	LEGACY_HEADING_RE.lastIndex = 0;
	let m: RegExpExecArray | null;
	while ((m = LEGACY_HEADING_RE.exec(trimmed)) !== null) {
		matches.push({ index: m.index, end: m.index + m[0].length, label: m[1].trim() });
	}
	if (!matches.length) {
		return [{
			id: `${idPrefix}-0`,
			role: 'user',
			author: '연구 기록',
			body: trimmed,
		}];
	}

	const out: ChatMessage[] = [];
	const preface = trimmed.slice(0, matches[0].index).trim();
	if (preface) {
		out.push({ id: `${idPrefix}-pre`, role: 'system', author: '안내', body: preface });
	}
	for (let i = 0; i < matches.length; i++) {
		const cur = matches[i];
		const nextStart = i + 1 < matches.length ? matches[i + 1].index : trimmed.length;
		const body = trimmed.slice(cur.end, nextStart).trim();
		if (!body) { continue; }
		const low = cur.label.toLowerCase();
		const isAssistant = low.includes('에이전트') || low.includes('ai') || low === 'assistant';
		const isSystem = low.includes('메모') || low === 'system';
		out.push({
			id: `${idPrefix}-${i}`,
			role: isAssistant ? 'assistant' : isSystem ? 'system' : 'user',
			uid: cur.label === '상대방' || cur.label === '참여자' ? 'peer' : undefined,
			author: isAssistant ? 'IRADE AI' : cur.label,
			body,
		});
	}
	return out;
}

export function parseAgentMessages(raw: string): ParsedAgentDoc {
	const { title, content } = splitTitleAndContent(raw);
	const markers: { index: number; end: number; rawMeta: string }[] = [];
	MSG_MARKER_RE.lastIndex = 0;
	let m: RegExpExecArray | null;
	while ((m = MSG_MARKER_RE.exec(content)) !== null) {
		markers.push({ index: m.index, end: m.index + m[0].length, rawMeta: m[1] });
	}

	if (!markers.length) {
		return { title, messages: parseLegacySection(content, 'legacy') };
	}

	const messages: ChatMessage[] = [];
	const prefix = content.slice(0, markers[0].index);
	if (prefix.trim()) {
		messages.push(...parseLegacySection(prefix, 'legacy'));
	}

	for (let i = 0; i < markers.length; i++) {
		const cur = markers[i];
		const nextStart = i + 1 < markers.length ? markers[i + 1].index : content.length;
		let meta: StoredMsgMeta = {};
		try {
			meta = JSON.parse(cur.rawMeta.replace(/--\\u003e/g, '-->')) as StoredMsgMeta;
		} catch {
			meta = {};
		}

		let bodyText = content.slice(cur.end, nextStart).replace(/^\n+/, '');
		const rawRole = meta.role === 'assistant' || meta.role === 'system' ? meta.role : 'user';
		const author = typeof meta.author === 'string' && meta.author.trim()
			? meta.author.trim()
			: rawRole === 'assistant' ? 'IRADE AI' : rawRole === 'system' ? '시스템' : '연구자';

		// Strip auto-written readability subheading right below the metadata comment.
		const lines = bodyText.split('\n');
		if (lines[0]?.trim() === `## ${author}` || lines[0]?.trim() === `## ${author} · AI`) {
			bodyText = lines.slice(1).join('\n').replace(/^\n+/, '');
		} else if (lines[0]?.startsWith('## ')) {
			const headingName = lines[0].slice(3).trim();
			if (headingName === author || headingName === '사용자' || headingName === '에이전트') {
				bodyText = lines.slice(1).join('\n').replace(/^\n+/, '');
			}
		}

		const body = bodyText.trimEnd();
		if (!body.trim()) { continue; }

		messages.push({
			id: typeof meta.id === 'string' && meta.id ? meta.id : `msg-${i}`,
			role: rawRole,
			...(typeof meta.uid === 'string' && meta.uid ? { uid: meta.uid } : {}),
			...(typeof meta.cid === 'string' && meta.cid ? { cid: meta.cid } : {}),
			author,
			...(typeof meta.model === 'string' && meta.model ? { model: meta.model } : {}),
			...(typeof meta.ts === 'number' && Number.isFinite(meta.ts) ? { ts: meta.ts } : {}),
			...(meta.limited === true ? { limited: true } : {}),
			body,
		});
	}

	return { title, messages };
}

export function serializeAgentTurn(entry: Omit<ChatMessage, 'id'> & { id?: string }): string {
	const meta: Record<string, unknown> = {
		id: entry.id || crypto.randomUUID().slice(0, 12),
		role: entry.role,
		...(entry.uid ? { uid: entry.uid } : {}),
		...(entry.cid ? { cid: entry.cid } : {}),
		author: entry.author,
		...(entry.model ? { model: entry.model } : {}),
		ts: typeof entry.ts === 'number' && Number.isFinite(entry.ts) ? entry.ts : Date.now(),
		...(entry.limited ? { limited: true } : {}),
	};
	const safeJson = JSON.stringify(meta).replace(/-->/g, '--\\u003e');
	return `<!-- irade-msg:${safeJson} -->\n## ${entry.author}\n${entry.body.trim()}\n`;
}

export function appendAgentMessage(raw: string, entry: Omit<ChatMessage, 'id'> & { id?: string }): string {
	const trimmed = (raw || '').replace(/\r\n?/g, '\n').trimEnd();
	const turn = serializeAgentTurn(entry);
	if (!trimmed) {
		return `# 연구 대화\n\n${turn}`;
	}
	const hasTitle = trimmed.startsWith('# ');
	const base = hasTitle ? trimmed : `# 연구 대화\n\n${trimmed}`;
	return `${base}\n\n${turn}`;
}

export function createStarterAgentDoc(): string {
	return appendAgentMessage('# 연구 대화', {
		id: 'starter-greeting',
		role: 'assistant',
		uid: 'irade-agent',
		author: 'IRADE AI',
		model: 'EXAONE 2.5',
		ts: 0,
		body: '안녕하세요, IRADE AI 에이전트입니다.\n연구 질문, 증명 아이디어, 코드 실험을 아래 입력창에 남기면 같은 Workspace 참여자들과 실시간으로 대화를 이어갈 수 있습니다.',
	});
}

export function resolveChatAlignment(
	msg: ChatMessage,
	viewer: ChatViewerIdentity,
	livePeerCids?: ReadonlySet<string>,
): ChatAlignment {
	if (msg.role === 'assistant') { return 'assistant'; }
	if (msg.role === 'system') { return 'system'; }
	if (msg.uid === 'peer') { return 'peer'; }
	if (msg.uid === 'self') { return 'self'; }
	if (viewer.uid && msg.uid) {
		if (msg.uid !== viewer.uid) { return 'peer'; }
		if (msg.cid && livePeerCids?.has(msg.cid)) { return 'peer'; }
		return 'self';
	}
	if (msg.cid && livePeerCids?.has(msg.cid)) { return 'peer'; }
	if (msg.cid && viewer.cid && msg.cid !== viewer.cid) { return 'peer'; }
	return msg.author === '상대방' ? 'peer' : 'self';
}

export function avatarMonogram(name: string): string {
	const stripped = (name || '').replace(/\([^)]*\)/g, ' ').replace(/[^\p{L}\p{N}\s]/gu, ' ').trim();
	const clean = stripped || (name || '').trim();
	if (!clean) { return '?'; }
	const words = clean.split(/\s+/).filter(Boolean);
	if (words.length >= 2) {
		const a = Array.from(words[0])[0] || '';
		const b = Array.from(words[1])[0] || '';
		return (a + b).toUpperCase();
	}
	const chars = Array.from(clean);
	if (/^[A-Za-z0-9]/.test(chars[0] || '')) {
		return chars.slice(0, 2).join('').toUpperCase();
	}
	return chars[0] || '?';
}

const PEER_TONES = ['tone-blue', 'tone-violet', 'tone-amber', 'tone-rose', 'tone-cyan'] as const;

export function peerAvatarTone(seed: string): string {
	let hash = 0;
	for (let i = 0; i < seed.length; i++) {
		hash = ((hash << 5) - hash + seed.charCodeAt(i)) | 0;
	}
	return PEER_TONES[Math.abs(hash) % PEER_TONES.length];
}

export function formatChatTime(ts?: number): string {
	if (typeof ts !== 'number' || !Number.isFinite(ts) || ts <= 0) { return ''; }
	return new Date(ts).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
}

function formatInlineChatText(text: string): string {
	let html = escapeHtml(text);
	html = html.replace(/`([^`\n]+)`/g, '<code class="chat-inline-code">$1</code>');
	html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
	return html.replace(/\n/g, '<br>');
}

export function formatChatBodyHtml(body: string): string {
	const normalized = (body || '').replace(/\r\n?/g, '\n').trim();
	if (!normalized) { return ''; }
	const fenceRe = /```([^\n`]*)\n([\s\S]*?)```/g;
	const parts: string[] = [];
	let lastIndex = 0;
	let m: RegExpExecArray | null;
	while ((m = fenceRe.exec(normalized)) !== null) {
		const before = normalized.slice(lastIndex, m.index).trim();
		if (before) {
			parts.push(`<div class="chat-text-chunk">${formatInlineChatText(before)}</div>`);
		}
		const lang = m[1]?.trim();
		const code = m[2].replace(/\n+$/, '');
		parts.push(`<pre class="chat-code-block">${lang ? `<span class="chat-code-lang">${escapeHtml(lang)}</span>` : ''}<code>${escapeHtml(code)}</code></pre>`);
		lastIndex = fenceRe.lastIndex;
	}
	const tail = normalized.slice(lastIndex).trim();
	if (tail) {
		parts.push(`<div class="chat-text-chunk">${formatInlineChatText(tail)}</div>`);
	}
	return parts.join('');
}
