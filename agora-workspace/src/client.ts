/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/
export interface Connection { origin: string; canvasId: number; audience?: 'irade' }
export interface FileRecord {
	path: string; type: 'file' | 'directory'; size: number; mtime: number; ctime: number;
	fileId: string; etag: string; document: string | null; content: string;
	resourceId: string;
}
export interface FileChange { path: string; operation: string; target?: string; created?: boolean; request_id?: string; self?: boolean }
export type FileRequest = { operation: string; path: string; [key: string]: string | number | boolean };
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
interface Pending {
	frame: string; sent: number; resolve: (value: object) => void; reject: (error: Error) => void;
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
	resolve: (value: RuntimeResult) => void;
	reject: (error: Error) => void;
	onOutput?: (chunk: string) => void;
}
const transient = new Set(['BROKER_BUSY', 'BROKER_BACKEND_UNAVAILABLE', 'BROKER_QUEUE_FULL', 'RATE_LIMITED',
	'QUERY_OUTCOME_UNKNOWN', 'QUERY_UNAVAILABLE', 'STORAGE_UNAVAILABLE', 'CANVAS_NOT_READY', 'RUNTIME_MANAGER_UNAVAILABLE', 'RUNTIME_VM_UNAVAILABLE', 'WORKSPACE_UNAVAILABLE']);

export class AgoraError extends Error {
	constructor(public readonly code: string) { super(code); }
}

export function connection(value: Connection): Connection {
	const origin = new URL(value.origin);
	if (origin.protocol !== 'https:' || origin.username || origin.password || origin.search || origin.hash || origin.pathname !== '/') {
		throw new Error('Enter an HTTPS origin, for example https://agora.example.com');
	}
	if (!Number.isSafeInteger(value.canvasId) || value.canvasId <= 0) { throw new Error('Invalid canvas ID'); }
	if (value.audience !== undefined && value.audience !== 'irade') { throw new Error('Invalid workspace audience'); }
	return { origin: origin.origin, canvasId: value.canvasId, ...(value.audience ? { audience: value.audience } : {}) };
}

/** Uses the existing authenticated canvas channel. Exact request IDs survive retries. */
export class AgoraClient {
	private socket?: SocketTransport;
	private ready = false;
	private stopped = false;
	private connecting = false;
	private retryAt = 0;
	private delay = 500;
	private pending = new Map<string, Pending>();
	private runtimePending = new Map<string, RuntimePending>();
	private recentRequestIds = new Set<string>();
	private timer: ReturnType<typeof setInterval>;
	private clientId = crypto.randomUUID();
	private passwordGrant?: string;
	canWrite = false;
	canExecute = false;
	get passwordToken(): string | undefined { return this.passwordGrant; }
	onChange: (change: FileChange) => void = () => {};
	onState: (state: string) => void = () => {};

	constructor(readonly config: Connection, private readonly token: () => Promise<string | undefined>,
		private readonly password: () => Promise<string | undefined> = async () => undefined,
		private readonly transport: Transport = { fetch: (...args) => fetch(...args), socket: url => new WebSocket(url) }) {
		connection(config);
		this.timer = setInterval(() => this.tick(), 1000);
	}

	request<T extends object>(request: FileRequest): Promise<T> {
		if (this.stopped) { return Promise.reject(new AgoraError('DISCONNECTED')); }
		if (this.pending.size >= 64) { return Promise.reject(new AgoraError('TOO_MANY_PENDING_REQUESTS')); }
		const id = crypto.randomUUID();
		const frame = JSON.stringify({ ...request, type: 'workspace_files', request_id: id });
		if (new TextEncoder().encode(frame).length > 700000) { return Promise.reject(new AgoraError('FILE_TOO_LARGE')); }
		this.recentRequestIds.add(id);
		if (this.recentRequestIds.size > 256) {
			const oldest = this.recentRequestIds.values().next().value;
			if (oldest) { this.recentRequestIds.delete(oldest); }
		}
		return new Promise<T>((resolve, reject) => {
			this.pending.set(id, { frame, sent: 0, resolve: value => resolve(value as T), reject });
			this.tick();
		});
	}

	executeRuntime(language: 'python' | 'javascript' | 'c' | 'cpp' | 'bash' | 'java', code: string,
		onOutput?: (chunk: string) => void): Promise<RuntimeResult> {
		if (this.stopped) { return Promise.reject(new AgoraError('DISCONNECTED')); }
		if (!this.ready || !this.socket) { return Promise.reject(new AgoraError('RUNTIME_NOT_CONNECTED')); }
		const id = crypto.randomUUID();
		const frame = JSON.stringify({
			type: 'runtime_execute',
			request_id: id,
			action: 'execute',
			object_id: 'irade-workspace',
			language,
			code,
		});
		if (new TextEncoder().encode(frame).length > 700000) { return Promise.reject(new AgoraError('FILE_TOO_LARGE')); }
		return new Promise<RuntimeResult>((resolve, reject) => {
			const timer = setTimeout(() => {
				this.runtimePending.delete(id);
				reject(new AgoraError('RUNTIME_TIMEOUT'));
			}, 200_000);
			this.runtimePending.set(id, { timer, resolve, reject, onOutput });
			try {
				this.socket!.send(frame);
			} catch (error) {
				clearTimeout(timer);
				this.runtimePending.delete(id);
				reject(error instanceof Error ? error : new AgoraError('DISCONNECTED'));
			}
		});
	}

	private tick(): void {
		if (this.stopped) { return; }
		if (!this.ready) {
			if (!this.connecting && Date.now() >= this.retryAt) { void this.connect(); }
			return;
		}
		for (const pending of this.pending.values()) {
			if (Date.now() - pending.sent > 5000) {
				try { this.socket!.send(pending.frame); pending.sent = Date.now(); }
				catch { this.socket?.close(); break; }
			}
		}
	}

	private async connect(): Promise<void> {
		this.connecting = true;
		this.onState('Connecting');
		try {
			const token = await this.token();
			if (!token) { throw new AgoraError('TOKEN_REQUIRED'); }
			let body: object = this.passwordGrant ? { canvas_password_token: this.passwordGrant } : {};
			let response = await this.access(token, body);
			if (response.status === 403 && !this.passwordGrant) {
				const password = await this.password();
				if (password === undefined) { throw new AgoraError('CANVAS_ACCESS_DENIED'); }
				body = { password };
				response = await this.access(token, body);
			}
			if (!response.ok) { throw new AgoraError(response.status === 401 ? 'TOKEN_EXPIRED' : response.status === 403 ? 'CANVAS_ACCESS_DENIED' : 'ACCESS_UNAVAILABLE'); }
			const access = await response.json() as { ws_port: string; canvas_access_token: string; canvas_password_token?: string };
			if (this.stopped) { return; }
			this.passwordGrant = access.canvas_password_token || this.passwordGrant;
			const url = new URL(`/wss/port/${encodeURIComponent(access.ws_port)}/canvas/${this.config.canvasId}`, this.config.origin);
			url.protocol = 'wss:';
			url.searchParams.set('token', access.canvas_access_token);
			url.searchParams.set('client_id', this.clientId);
			const socket = this.transport.socket(url);
			this.socket = socket;
			const timeout = setTimeout(() => socket.close(), 40000);
			socket.onmessage = event => {
				if (this.socket !== socket) { return; }
				try {
					const message = JSON.parse(String(event.data)) as Record<string, unknown>;
					if (message.type === 'init_items') {
						clearTimeout(timeout); this.ready = true; this.delay = 500;
						this.canWrite = message.is_admin === true || (Array.isArray(message.write_groups) && message.write_groups.length > 0);
						this.canExecute = message.is_admin === true || (Array.isArray(message.execute_groups) && message.execute_groups.length > 0);
						this.onState('Connected');
						for (const pending of this.pending.values()) { pending.sent = 0; }
						this.tick();
						this.onChange({ operation: 'reconnect', path: '/' });
					} else if (message.type === 'workspace_files_changed') {
						const change = message as unknown as FileChange;
						if (typeof change.request_id === 'string' && this.recentRequestIds.has(change.request_id)) {
							change.self = true;
						}
						this.onChange(change);
					} else if (message.type === 'canvas_access_changed') {
						this.canWrite = message.is_admin === true || (Array.isArray(message.write_groups) && message.write_groups.length > 0);
						this.canExecute = message.is_admin === true || (Array.isArray(message.execute_groups) && message.execute_groups.length > 0);
						this.onState('Connected');
						this.onChange({ operation: 'access', path: '/' });
					} else if (message.type === 'canvas_access_revoked') {
						this.fail(new AgoraError('CANVAS_ACCESS_REVOKED'));
					} else if (message.type === 'server_reconnect') { socket.close(); }
					else if (message.type === 'runtime_output' && typeof message.request_id === 'string') {
						const rt = this.runtimePending.get(message.request_id);
						if (rt) { rt.onOutput?.(String(message.output || '')); }
					} else if ((message.type === 'runtime_result' || message.type === 'error') && typeof message.request_id === 'string' && this.runtimePending.has(message.request_id)) {
						const rt = this.runtimePending.get(message.request_id)!;
						clearTimeout(rt.timer);
						this.runtimePending.delete(message.request_id);
						if (message.type === 'runtime_result' && message.ok === true && message.result && typeof message.result === 'object') {
							rt.resolve(message.result as RuntimeResult);
						} else {
							rt.reject(new AgoraError(String(message.message || message.code || 'RUNTIME_EXECUTION_FAILED')));
						}
					}
					else if (typeof message.request_id === 'string' && this.pending.has(message.request_id)) {
						const pending = this.pending.get(message.request_id)!;
						if (message.ok === true) { this.pending.delete(message.request_id); pending.resolve(message.result as object); }
						else if (!transient.has(String(message.code))) {
							this.pending.delete(message.request_id); pending.reject(new AgoraError(String(message.code || 'WORKSPACE_REQUEST_FAILED')));
						}
					}
				} catch { this.fail(new AgoraError('PROTOCOL_INVALID')); }
			};
			socket.onerror = () => socket.close();
			socket.onclose = () => {
				clearTimeout(timeout);
				if (this.socket === socket) {
					this.ready = false; this.connecting = false;
					for (const rt of this.runtimePending.values()) { clearTimeout(rt.timer); rt.reject(new AgoraError('DISCONNECTED')); }
					this.runtimePending.clear();
					this.retryAt = Date.now() + this.delay; this.delay = Math.min(10000, this.delay * 2);
					if (!this.stopped) { this.onState('Reconnecting'); }
				}
			};
		} catch (error) {
			this.connecting = false;
			this.retryAt = Date.now() + this.delay; this.delay = Math.min(10000, this.delay * 2);
			if (error instanceof AgoraError && ['TOKEN_REQUIRED', 'TOKEN_EXPIRED', 'CANVAS_ACCESS_DENIED'].includes(error.code)) { this.fail(error); }
			else { this.onState('Reconnecting'); }
		}
	}

	private access(token: string, body: object): Promise<Response> {
		return this.transport.fetch(`${this.config.origin}/api/${this.config.audience === 'irade' ? 'irade/workspaces' : 'canvases'}/${this.config.canvasId}/access`, {
			method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
			body: JSON.stringify(body), redirect: 'error', signal: AbortSignal.timeout(15000),
		});
	}

	private fail(error: Error): void {
		this.onState(error.message);
		for (const pending of this.pending.values()) { pending.reject(error); }
		this.pending.clear();
		for (const rt of this.runtimePending.values()) { clearTimeout(rt.timer); rt.reject(error); }
		this.runtimePending.clear();
		this.dispose();
	}

	dispose(): void {
		this.stopped = true; this.ready = false; clearInterval(this.timer); this.socket?.close();
		for (const pending of this.pending.values()) { pending.reject(new AgoraError('DISCONNECTED')); }
		this.pending.clear();
		for (const rt of this.runtimePending.values()) { clearTimeout(rt.timer); rt.reject(new AgoraError('DISCONNECTED')); }
		this.runtimePending.clear();
	}
}
