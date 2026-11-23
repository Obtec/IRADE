/*---------------------------------------------------------------------------------------------
 *  Copyright (c) 2026 hyeon su Lee. All rights reserved.
 *  Licensed under the MIT License. See LICENSE.txt in the web project root for license information.
 *--------------------------------------------------------------------------------------------*/
import {
	itemPermissionGroups,
	type CanvasCommitMessage,
	type CanvasIdentity,
	type CanvasItem,
	type CanvasSnapshotMessage,
	type CanvasWatermarkMessage,
	type PeerProfile,
	type RemoteEditorPresence,
} from './canvasSync.ts';

export interface Connection { origin: string; canvasId: number }
export interface SocketTransport {
	send(data: string): void; close(): void;
	onmessage: ((event: MessageEvent) => void) | null;
	onerror: ((event: Event) => void) | null;
	onclose: ((event: CloseEvent) => void) | null;
}
export interface Transport {
	fetch: typeof fetch;
	socket: (url: URL) => SocketTransport;
}
export interface AgentApiProfile {
	id: string;
	label: string;
	model: string;
	default_configured?: boolean;
}
export interface AgentQuotaWindow {
	limit?: number | null;
	remaining?: number | null;
}
export interface AgentTelemetry {
	api?: string;
	limits?: {
		second?: AgentQuotaWindow;
		minute?: AgentQuotaWindow;
		day?: AgentQuotaWindow;
	};
	observed_at?: number;
	limits_observed_at?: number | null;
	status?: number;
	retry_at?: number | null;
}
export interface AgentOptions {
	reasoning_effort?: 'low' | 'high' | 'max';
	max_tokens?: number;
	max_steps?: number;
	temperature?: number;
	top_p?: number;
}
export interface AgentHistoryTurn {
	role: 'user' | 'assistant';
	content: string;
}
export interface AgentRequest {
	operation: 'status' | 'run';
	api?: string;
	api_key?: string;
	message?: string;
	selected_ids?: string[];
	history?: AgentHistoryTurn[];
	allow_delete?: boolean;
	options?: AgentOptions;
}
export interface AgentStatusResult {
	ready?: boolean;
	configured?: boolean;
	credential_source?: 'server' | 'personal';
	default_configured?: boolean;
	api?: string;
	model?: string;
	apis?: AgentApiProfile[];
	defaults?: Required<AgentOptions>;
	telemetry?: AgentTelemetry;
}
export interface AgentRunResult {
	answer?: string;
	limited?: boolean;
	api?: string;
	model?: string;
	telemetry?: AgentTelemetry;
	error?: string;
	message?: string;
}
export interface RuntimeResult {
	exitCode?: number;
	output?: string;
	timedOut?: boolean;
	truncated?: boolean;
	environmentReset?: boolean;
	error?: string;
	message?: string;
}
interface RuntimePending {
	timer: ReturnType<typeof setTimeout>;
	resolve: (value: RuntimeResult | AgentStatusResult | AgentRunResult) => void;
	reject: (error: Error) => void;
	onOutput?: (chunk: string) => void;
}
interface ConnectWaiter {
	resolve: () => void;
	reject: (error: Error) => void;
}
export interface InitItemsMeta {
	revision: number;
	acceptedCount: number;
	identity: CanvasIdentity;
	canvasName?: string;
}

const transient = new Set([
	'BROKER_BUSY', 'BROKER_BACKEND_UNAVAILABLE', 'BROKER_QUEUE_FULL', 'RATE_LIMITED',
	'QUERY_OUTCOME_UNKNOWN', 'QUERY_UNAVAILABLE', 'STORAGE_UNAVAILABLE', 'CANVAS_NOT_READY',
	'RUNTIME_MANAGER_UNAVAILABLE', 'RUNTIME_VM_UNAVAILABLE', 'WORKSPACE_UNAVAILABLE',
]);

export class AgoraError extends Error {
	constructor(
		public readonly code: string,
		public readonly serverMessage?: string,
		public readonly payload?: Record<string, unknown>,
	) {
		super(code);
	}
}

export function connection(value: Connection): Connection {
	const origin = new URL(value.origin);
	if (origin.protocol !== 'https:' || origin.username || origin.password || origin.search || origin.hash || origin.pathname !== '/') {
		throw new Error('Enter an HTTPS origin, for example https://example.com');
	}
	if (!Number.isSafeInteger(value.canvasId) || value.canvasId <= 0) { throw new Error('Invalid workspace ID'); }
	return { origin: origin.origin, canvasId: value.canvasId };
}

function stringList(value: unknown): string[] {
	return Array.isArray(value)
		? value.filter((entry): entry is string => typeof entry === 'string' && entry.length > 0)
		: [];
}

function parseGroupBits(value: unknown): Record<string, number> | undefined {
	if (!value || typeof value !== 'object' || Array.isArray(value)) { return undefined; }
	const bits: Record<string, number> = {};
	for (const [key, bit] of Object.entries(value)) {
		if (typeof bit === 'number' && Number.isInteger(bit) && bit >= 0 && bit < 64) {
			bits[key] = bit;
		}
	}
	return Object.keys(bits).length ? bits : undefined;
}

function parseIdentity(message: Record<string, unknown>, previous?: CanvasIdentity): CanvasIdentity {
	const isAdmin = message.is_admin !== undefined ? message.is_admin === true : Boolean(previous?.isAdmin);
	const groups = message.groups !== undefined ? stringList(message.groups) : (previous?.groups || []);
	const memberGroups = message.member_groups !== undefined
		? stringList(message.member_groups)
		: (previous?.memberGroups.length ? previous.memberGroups : groups);
	const writeGroups = message.write_groups !== undefined ? stringList(message.write_groups) : (previous?.writeGroups || []);
	const executeGroups = message.execute_groups !== undefined ? stringList(message.execute_groups) : (previous?.executeGroups || []);
	const groupBits = message.group_bits !== undefined ? parseGroupBits(message.group_bits) : previous?.groupBits;
	const userId = message.sync_user_id != null ? String(message.sync_user_id) : previous?.userId;
	return { userId, groups, memberGroups, writeGroups, executeGroups, isAdmin, ...(groupBits ? { groupBits } : {}) };
}

/** Uses authenticated workspace and real-time channels. Exact request IDs survive retries. */
export class AgoraClient {
	private socket?: SocketTransport;
	private rtcSocket?: SocketTransport;
	private rtcReady = false;
	private ready = false;
	private stopped = false;
	private connecting = false;
	private retryAt = 0;
	private delay = 500;
	private runtimePending = new Map<string, RuntimePending>();
	private connectWaiters: ConnectWaiter[] = [];
	private timer: ReturnType<typeof setInterval>;
	private clientId = crypto.randomUUID();
	private passwordGrant?: string;
	private selfPeerId = '';
	private peerMap = new Map<string, PeerProfile>();
	private remoteEditorMap = new Map<string, RemoteEditorPresence>();
	private wsPort?: string;
	private accessToken?: string;
	private rtcConnectionId?: string;
	private rtcConnectionHash?: string;

	canWrite = false;
	canExecute = false;
	canvasName = '';
	identity: CanvasIdentity = { groups: [], memberGroups: [], writeGroups: [], executeGroups: [], isAdmin: false };
	get passwordToken(): string | undefined { return this.passwordGrant; }
	get socketClientId(): string { return this.clientId; }
	get peers(): PeerProfile[] { return [...this.peerMap.values()]; }
	get remoteEditors(): RemoteEditorPresence[] { return [...this.remoteEditorMap.values()]; }

	onState: (state: string) => void = () => {};
	onInitItems: (items: Record<string, CanvasItem>, meta: InitItemsMeta) => void = () => {};
	onCanvasMessage: (message: CanvasCommitMessage | CanvasSnapshotMessage | CanvasWatermarkMessage) => void = () => {};
	onCanvasError: (requestId: string | undefined, code: string) => void = () => {};
	onIdentityChange: (identity: CanvasIdentity) => void = () => {};
	onPresenceChange: (peers: PeerProfile[], editors: RemoteEditorPresence[]) => void = () => {};

	constructor(
		readonly config: Connection,
		private readonly token: () => Promise<string | undefined>,
		private readonly password: () => Promise<string | undefined> = async () => undefined,
		private readonly transport: Transport = { fetch: (...args) => fetch(...args), socket: url => new WebSocket(url) },
	) {
		connection(config);
		this.timer = setInterval(() => this.tick(), 1000);
	}

	ensureConnected(): Promise<void> {
		if (this.stopped) { return Promise.reject(new AgoraError('DISCONNECTED')); }
		if (this.ready) { return Promise.resolve(); }
		return new Promise<void>((resolve, reject) => {
			this.connectWaiters.push({ resolve, reject });
			this.tick();
		});
	}

	sendCanvasFrame(frame: Record<string, unknown>): boolean {
		if (this.stopped || !this.ready || !this.socket) { return false; }
		this.socket.send(JSON.stringify(frame));
		return true;
	}

	sendEditorPresence(itemId: string, editing: boolean, itemsById?: Record<string, CanvasItem>): boolean {
		if (this.stopped || !this.rtcReady || !this.rtcSocket || !this.peerMap.size) { return false; }
		const targetItem = itemsById?.[itemId];
		const requiredGroups = targetItem ? itemPermissionGroups(targetItem.permission) : [];
		const peerIds = [...this.peerMap.values()]
			.filter(peer => {
				if (!targetItem || peer.is_admin) { return true; }
				if (!Array.isArray(peer.groups) || !requiredGroups.length) { return true; }
				return requiredGroups.some(group => peer.groups!.includes(group));
			})
			.map(peer => peer.peer_id);
		if (!peerIds.length) { return false; }
		this.rtcSocket.send(JSON.stringify({
			type: 'canvas_relay',
			peer_ids: peerIds,
			payload: { type: 'editor_presence', item_id: itemId, editing },
		}));
		return true;
	}

	async executeRuntime(
		language: 'python' | 'bash',
		code: string,
		onOutput?: (chunk: string) => void,
		objectId?: string,
	): Promise<RuntimeResult> {
		if (this.stopped) { throw new AgoraError('DISCONNECTED'); }
		if (!objectId) { throw new AgoraError('RUNTIME_REQUEST_INVALID'); }
		if (!this.ready) { await this.ensureConnected(); }
		if (this.stopped || !this.socket) { throw new AgoraError('RUNTIME_NOT_CONNECTED'); }
		const requestId = crypto.randomUUID();
		return new Promise<RuntimeResult>((resolve, reject) => {
			const timer = setTimeout(() => {
				if (this.runtimePending.delete(requestId)) {
					reject(new AgoraError('RUNTIME_TIMEOUT'));
				}
			}, 90000);
			this.runtimePending.set(requestId, {
				timer,
				resolve: value => resolve(value as RuntimeResult),
				reject,
				onOutput,
			});
			this.socket!.send(JSON.stringify({
				type: 'runtime_execute',
				action: 'execute',
				request_id: requestId,
				object_id: objectId,
				language,
				code,
			}));
		});
	}

	async executeAgent(
		request: AgentRequest,
		onOutput?: (chunk: string) => void,
	): Promise<AgentStatusResult & AgentRunResult> {
		if (this.stopped) { throw new AgoraError('DISCONNECTED'); }
		if (!this.ready) { await this.ensureConnected(); }
		if (this.stopped || !this.socket) { throw new AgoraError('RUNTIME_NOT_CONNECTED'); }
		const requestId = crypto.randomUUID();
		const timeoutMs = request.operation === 'status' ? 20000 : 260000;
		return new Promise<AgentStatusResult & AgentRunResult>((resolve, reject) => {
			const timer = setTimeout(() => {
				if (this.runtimePending.delete(requestId)) {
					reject(new AgoraError('AGENT_TIMEOUT'));
				}
			}, timeoutMs);
			this.runtimePending.set(requestId, {
				timer,
				resolve: value => resolve(value as AgentStatusResult & AgentRunResult),
				reject,
				onOutput,
			});
			this.socket!.send(JSON.stringify({
				type: 'runtime_execute',
				request_id: requestId,
				action: 'agent',
				operation: request.operation,
				...(request.api ? { api: request.api } : {}),
				...(request.api_key !== undefined ? { api_key: request.api_key } : {}),
				...(request.message !== undefined ? { message: request.message } : {}),
				...(request.selected_ids?.length ? { selected_ids: request.selected_ids } : {}),
				...(request.history?.length ? { history: request.history } : {}),
				...(request.allow_delete !== undefined ? { allow_delete: request.allow_delete } : {}),
				...(request.options ? { options: request.options } : {}),
			}));
		});
	}

	private tick(): void {
		if (this.stopped) { return; }
		if (!this.socket && !this.connecting && Date.now() >= this.retryAt) {
			void this.connect();
		}
	}

	private async connect(): Promise<void> {
		this.connecting = true;
		this.onState(this.delay > 500 ? 'Reconnecting' : 'Connecting');
		try {
			const jwt = await this.token();
			if (!jwt) { this.abortPending(new AgoraError('TOKEN_REQUIRED')); return; }
			const access = await this.fetchAccess(jwt);
			if (!access || this.stopped) { return; }
			this.wsPort = String(access.ws_port);
			this.accessToken = String(access.canvas_access_token);
			const url = new URL(this.config.origin);
			url.protocol = 'wss:';
			url.pathname = `/wss/port/${encodeURIComponent(this.wsPort)}/workspace/${this.config.canvasId}`;
			url.searchParams.set('token', this.accessToken);
			url.searchParams.set('client_id', this.clientId);
			const socket = this.transport.socket(url);
			this.socket = socket;
			this.installSocketHandlers(socket);
		} catch (error) {
			const code = error instanceof AgoraError ? error.code : 'CONNECTION_FAILED';
			this.onState(code);
			this.scheduleRetry();
		} finally {
			this.connecting = false;
		}
	}

	private async fetchAccess(jwt: string): Promise<Record<string, unknown> | undefined> {
		const endpoint = `/api/irade/workspaces/${this.config.canvasId}/access`;
		let body: Record<string, string> | undefined = this.passwordGrant ? { password_token: this.passwordGrant } : undefined;
		for (let attempt = 0; attempt < 3; attempt++) {
			const response = await this.transport.fetch(`${this.config.origin}${endpoint}`, {
				method: 'POST',
				headers: { ...(jwt && jwt !== 'cookie-session' ? { Authorization: `Bearer ${jwt}` } : {}), 'Content-Type': 'application/json' },
				credentials: 'same-origin',
				...(body ? { body: JSON.stringify(body) } : {}),
			});
			const payload = await response.json().catch(() => ({})) as Record<string, unknown>;
			if (response.ok) {
				if (typeof payload.password_token === 'string') { this.passwordGrant = payload.password_token; }
				return payload;
			}
			const code = String(payload.code || payload.error || '');
			if (code === 'CANVAS_004' || code === 'CANVAS_005') {
				this.passwordGrant = undefined;
				const secret = await this.password();
				if (secret === undefined) {
					this.abortPending(new AgoraError('CANVAS_ACCESS_DENIED'));
					return undefined;
				}
				body = { password: secret };
				continue;
			}
			if (response.status === 401 || response.status === 403) {
				this.abortPending(new AgoraError(code || 'CANVAS_ACCESS_DENIED'));
				return undefined;
			}
			throw new AgoraError(code || 'CONNECTION_FAILED');
		}
		this.abortPending(new AgoraError('CANVAS_ACCESS_DENIED'));
		return undefined;
	}

	private installSocketHandlers(socket: SocketTransport): void {
		socket.onmessage = event => {
			if (this.socket !== socket || this.stopped) { return; }
			let message: Record<string, unknown>;
			try { message = JSON.parse(String(event.data)); } catch { return; }
			this.handleCanvasMessage(message);
		};
		socket.onerror = () => { /* Closed handler triggers reconnect. */ };
		socket.onclose = () => {
			if (this.socket !== socket) { return; }
			this.closeRtc();
			this.socket = undefined;
			this.ready = false;
			if (!this.stopped) { this.scheduleRetry(); }
		};
	}

	private handleCanvasMessage(message: Record<string, unknown>): void {
		const type = message.type;
		if (type === 'init_items') {
			this.ready = true;
			this.delay = 500;
			this.applyIdentity(parseIdentity(message));
			if (typeof message.canvas_name === 'string') { this.canvasName = message.canvas_name; }
			this.rtcConnectionId = typeof message.rtc_canvas_connection_id === 'string' ? message.rtc_canvas_connection_id : undefined;
			this.rtcConnectionHash = typeof message.rtc_canvas_connection_hash === 'string' ? message.rtc_canvas_connection_hash : undefined;
			const items = (message.items && typeof message.items === 'object' ? message.items : {}) as Record<string, CanvasItem>;
			const revision = typeof message.revision === 'number' ? message.revision : 0;
			const acceptedCount = typeof message.accepted_count === 'number' ? message.accepted_count : 0;
			const waiters = this.connectWaiters.splice(0);
			for (const waiter of waiters) { waiter.resolve(); }
			this.onInitItems(items, { revision, acceptedCount, identity: this.identity, canvasName: this.canvasName || undefined });
			this.onState('Connected');
			this.tick();
			if (this.rtcConnectionId && this.rtcConnectionHash) {
				this.connectRtc(this.rtcConnectionId, this.rtcConnectionHash);
			}
			return;
		}
		if (type === 'canvas_commit' || type === 'canvas_snapshot' || type === 'canvas_watermark') {
			this.onCanvasMessage(message as unknown as CanvasCommitMessage | CanvasSnapshotMessage | CanvasWatermarkMessage);
			return;
		}
		if (type === 'canvas_access_changed') {
			this.applyIdentity(parseIdentity(message, this.identity));
			this.onIdentityChange(this.identity);
			return;
		}
		if (type === 'canvas_access_revoked') {
			this.abortPending(new AgoraError('CANVAS_ACCESS_REVOKED'));
			return;
		}
		if (type === 'server_reconnect') {
			this.closeRtc();
			this.ready = false;
			this.socket?.close();
			this.socket = undefined;
			this.retryAt = Date.now() + (typeof message.retry_after_ms === 'number' ? Math.max(0, message.retry_after_ms) : 0);
			this.onState('Reconnecting');
			return;
		}
		if (type === 'runtime_output' && typeof message.request_id === 'string') {
			const entry = this.runtimePending.get(message.request_id);
			const chunk = typeof message.output === 'string' ? message.output : typeof message.chunk === 'string' ? message.chunk : '';
			if (entry && chunk) { entry.onOutput?.(chunk); }
			return;
		}
		if (type === 'runtime_result' && typeof message.request_id === 'string') {
			const entry = this.runtimePending.get(message.request_id);
			if (!entry) { return; }
			clearTimeout(entry.timer);
			this.runtimePending.delete(message.request_id);
			if (message.ok === true && message.result && typeof message.result === 'object') {
				entry.resolve(message.result as RuntimeResult);
			} else {
				entry.reject(new AgoraError(
					String(message.code || 'RUNTIME_VM_UNAVAILABLE'),
					typeof message.message === 'string' ? message.message : undefined,
					message,
				));
			}
			return;
		}
		if (type === 'error') {
			const requestId = typeof message.request_id === 'string' ? message.request_id : undefined;
			const code = String(message.code || 'CANVAS_ERROR');
			if (requestId && this.runtimePending.has(requestId)) {
				const entry = this.runtimePending.get(requestId)!;
				clearTimeout(entry.timer);
				this.runtimePending.delete(requestId);
				entry.reject(new AgoraError(code));
				return;
			}
			this.onCanvasError(requestId, code);
		}
	}

	private applyIdentity(next: CanvasIdentity): void {
		this.identity = next;
		this.canWrite = next.isAdmin || next.writeGroups.length > 0;
		this.canExecute = next.isAdmin || next.executeGroups.length > 0;
	}

	private connectRtc(connectionId: string, connectionHash: string): void {
		this.closeRtc();
		if (!this.wsPort || !this.accessToken) { return; }
		const url = new URL(this.config.origin);
		url.protocol = 'wss:';
		url.pathname = `/wss/port/${encodeURIComponent(this.wsPort)}/rtc/workspace/${this.config.canvasId}`;
		url.searchParams.set('token', this.accessToken);
		let rtc: SocketTransport;
		try {
			rtc = this.transport.socket(url);
		} catch {
			return;
		}
		this.rtcSocket = rtc;
		rtc.onmessage = event => {
			if (this.rtcSocket !== rtc || this.stopped) { return; }
			let data: Record<string, unknown>;
			try { data = JSON.parse(String(event.data)); } catch { return; }
			this.handleRtcMessage(rtc, data, connectionId, connectionHash);
		};
		rtc.onerror = () => { /* Ignore non-fatal presence transport drops. */ };
		rtc.onclose = () => {
			if (this.rtcSocket === rtc) {
				this.rtcSocket = undefined;
				this.rtcReady = false;
				this.peerMap.clear();
				this.remoteEditorMap.clear();
				this.onPresenceChange([], []);
			}
		};
	}

	private handleRtcMessage(
		rtc: SocketTransport,
		data: Record<string, unknown>,
		connectionId: string,
		connectionHash: string,
	): void {
		if (data.type === 'rtc_ready') {
			this.rtcReady = true;
			rtc.send(JSON.stringify({
				type: 'rtc_join',
				canvas_protocol: 2,
				canvas_transport: 'broker-v1',
				request_id: crypto.randomUUID(),
				canvas_connection_id: connectionId,
				canvas_connection_hash: connectionHash,
			}));
			return;
		}
		if (data.type === 'rtc_peers') {
			this.selfPeerId = typeof data.self_peer_id === 'string' ? data.self_peer_id : '';
			this.peerMap.clear();
			this.remoteEditorMap.clear();
			for (const peer of Array.isArray(data.peers) ? data.peers as PeerProfile[] : []) {
				if (peer?.peer_id && peer.peer_id !== this.selfPeerId) {
					this.peerMap.set(peer.peer_id, peer);
				}
			}
			this.onPresenceChange(this.peers, this.remoteEditors);
			return;
		}
		if (data.type === 'rtc_peer_joined' && data.peer && typeof data.peer === 'object') {
			const peer = data.peer as PeerProfile;
			if (peer.peer_id && peer.peer_id !== this.selfPeerId) {
				this.peerMap.set(peer.peer_id, peer);
				this.onPresenceChange(this.peers, this.remoteEditors);
			}
			return;
		}
		if (data.type === 'rtc_peer_left' && typeof data.peer_id === 'string') {
			this.peerMap.delete(data.peer_id);
			this.remoteEditorMap.delete(data.peer_id);
			this.onPresenceChange(this.peers, this.remoteEditors);
			return;
		}
		if (data.type === 'canvas_relay') {
			const rawPeer = data.peer as PeerProfile | undefined;
			const peer = rawPeer?.peer_id ? (this.peerMap.get(rawPeer.peer_id) || rawPeer) : undefined;
			const payload = data.payload as Record<string, unknown> | undefined;
			if (!peer?.peer_id || peer.peer_id === this.selfPeerId || !payload) { return; }
			if (payload.type === 'editor_presence' && typeof payload.item_id === 'string' && typeof payload.editing === 'boolean') {
				if (payload.editing) {
					this.remoteEditorMap.set(peer.peer_id, { peer, itemId: payload.item_id, editing: true, updatedAt: Date.now() });
				} else {
					const existing = this.remoteEditorMap.get(peer.peer_id);
					if (!existing || existing.itemId === payload.item_id) {
						this.remoteEditorMap.delete(peer.peer_id);
					}
				}
				this.onPresenceChange(this.peers, this.remoteEditors);
			}
		}
	}

	private closeRtc(): void {
		this.rtcReady = false;
		const rtc = this.rtcSocket;
		this.rtcSocket = undefined;
		rtc?.close();
		if (this.peerMap.size || this.remoteEditorMap.size) {
			this.peerMap.clear();
			this.remoteEditorMap.clear();
			this.onPresenceChange([], []);
		}
	}

	private scheduleRetry(): void {
		if (this.stopped) { return; }
		this.retryAt = Date.now() + this.delay;
		this.delay = Math.min(this.delay * 2, 15000);
	}

	private abortPending(error: Error): void {
		this.stopped = true;
		clearInterval(this.timer);
		this.closeRtc();
		this.ready = false;
		const socket = this.socket;
		this.socket = undefined;
		socket?.close();
		for (const waiter of this.connectWaiters.splice(0)) { waiter.reject(error); }
		for (const entry of this.runtimePending.values()) {
			clearTimeout(entry.timer);
			entry.reject(error);
		}
		this.runtimePending.clear();
		this.onState(error instanceof AgoraError ? error.code : 'DISCONNECTED');
	}

	dispose(): void {
		this.abortPending(new AgoraError('DISCONNECTED'));
	}
}
