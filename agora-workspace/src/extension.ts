/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/
import * as vscode from 'vscode';
import { AgoraClient, AgoraError, connection, type Connection, type FileChange, type FileRecord, type FileRequest } from './client.ts';
import { SharedDocument, decode, encode, replacement } from './document.ts';

interface OpenFile {
	uri: vscode.Uri; record: FileRecord; replica: SharedDocument; view: SharedDocument;
	version: number; sent: number; timer?: ReturnType<typeof setTimeout>; flight?: Promise<void>;
	applying?: { text: string; snapshot: string }; error?: string;
}

function config(uri: vscode.Uri): Connection {
	const url = new URL(`https://${uri.authority}`);
	if (url.password || !/^\d+$/.test(url.username)) { throw new Error('Invalid Agora workspace URI'); }
	return connection({ origin: url.origin, canvasId: Number(url.username) });
}

function workspaceUri(value: Connection): vscode.Uri {
	const url = new URL(value.origin);
	return vscode.Uri.from({ scheme: 'agora', authority: `${value.canvasId}@${url.host}`, path: '/' });
}

function fileError(error: unknown, uri: vscode.Uri): Error {
	if (error instanceof AgoraError) {
		if (error.code === 'FILE_NOT_FOUND') { return vscode.FileSystemError.FileNotFound(uri); }
		if (error.code === 'FILE_EXISTS') { return vscode.FileSystemError.FileExists(uri); }
		if (['WORKSPACE_ACCESS_DENIED', 'ITEM_WRITE_DENIED', 'CANVAS_READ_DENIED', 'CANVAS_ACCESS_REVOKED'].includes(error.code)) { return vscode.FileSystemError.NoPermissions(uri); }
	}
	return vscode.FileSystemError.Unavailable(error instanceof Error ? error.message : String(error));
}

/** Files are on the VM; this provider holds only open text replicas and credentials in SecretStorage. */
class WorkspaceFiles implements vscode.FileSystemProvider, vscode.Disposable {
	private readonly changes = new vscode.EventEmitter<vscode.FileChangeEvent[]>();
	readonly onDidChangeFile = this.changes.event;
	private readonly clients = new Map<string, AgoraClient>();
	private readonly files = new Map<string, OpenFile>();
	private readonly disposables: vscode.Disposable[] = [this.changes];
	private readonly watched = new Map<string, { uri: vscode.Uri; count: number }>();
	private readonly refreshing = new Set<string>();
	private readonly poll: ReturnType<typeof setInterval>;
	private readonly status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 20);
	private disposed = false;

	constructor(private readonly context: vscode.ExtensionContext) {
		this.status.command = 'agora.openWorkspace';
		this.status.tooltip = vscode.l10n.t('Shared Files in the Agora VM /workspace');
		this.disposables.push(this.status,
			vscode.workspace.onDidChangeTextDocument(event => this.changed(event)),
			vscode.window.onDidChangeVisibleTextEditors(() => {
				for (const file of this.files.values()) { void this.applyRemote(file).catch(error => this.report(file, error)); }
			}),
			vscode.workspace.onDidCloseTextDocument(document => {
				const file = this.files.get(document.uri.toString());
				if (file) {
					void this.flush(file).then(() => {
						if (!vscode.workspace.textDocuments.some(doc => doc.uri.toString() === document.uri.toString())) { this.files.delete(document.uri.toString()); }
					}).catch(error => this.report(file, error));
				}
			}));
		this.poll = setInterval(() => {
			for (const client of this.clients.values()) { this.invalidate(client, { operation: 'poll', path: '/' }); }
		}, 5000);
	}

	private client(uri: vscode.Uri): AgoraClient {
		let client = this.clients.get(uri.authority);
		if (!client) {
			const value = config(uri);
			client = new AgoraClient(value, async () => {
				const key = `agora.token:${value.origin}`;
				let token = await this.context.secrets.get(key);
				if (!token) {
					token = await vscode.window.showInputBox({ password: true, ignoreFocusOut: true,
						prompt: vscode.l10n.t('Agora access token for {0}', value.origin) });
					if (token) { await this.context.secrets.store(key, token); }
				}
				return token;
			}, async () => vscode.window.showInputBox({ password: true, ignoreFocusOut: true, prompt: vscode.l10n.t('Canvas Password') }));
			const current = client;
			client.onChange = change => this.invalidate(current, change);
			client.onState = state => {
				this.status.text = `$(broadcast) Agora: ${state}`;
				this.status.show();
				if (state === 'Connected' && current.canWrite) {
					for (const file of this.files.values()) {
						if (file.uri.authority === uri.authority && file.sent < file.version) { void this.flush(file).catch(error => this.report(file, error)); }
					}
				}
			};
			this.clients.set(uri.authority, client);
		}
		return client;
	}

	private async request<T extends object>(uri: vscode.Uri, values: Omit<FileRequest, 'path'>): Promise<T> {
		try { return await this.client(uri).request<T>({ ...values, operation: String(values.operation), path: uri.path || '/' }); }
		catch (error) { throw fileError(error, uri); }
	}

	watch(uri: vscode.Uri): vscode.Disposable {
		const key = uri.toString();
		const entry = this.watched.get(key) || { uri, count: 0 };
		entry.count++; this.watched.set(key, entry);
		return new vscode.Disposable(() => { if (--entry.count === 0) { this.watched.delete(key); } });
	}

	async stat(uri: vscode.Uri): Promise<vscode.FileStat> {
		const result = await this.request<FileRecord>(uri, { operation: 'stat' });
		return { ...result, mtime: result.type === 'file' && this.files.has(uri.toString()) ? 0 : result.mtime,
			type: result.type === 'directory' ? vscode.FileType.Directory : vscode.FileType.File,
			permissions: this.client(uri).canWrite ? undefined : vscode.FilePermission.Readonly };
	}

	async readDirectory(uri: vscode.Uri): Promise<[string, vscode.FileType][]> {
		const result: [string, vscode.FileType][] = [];
		let cursor: number | null = 0;
		do {
			const page: { entries: [string, string][]; cursor: number | null } = await this.request(uri, { operation: 'list', cursor });
			result.push(...page.entries.map(([name, type]): [string, vscode.FileType] => [name, type === 'directory' ? vscode.FileType.Directory : vscode.FileType.File]));
			cursor = page.cursor;
		} while (cursor !== null);
		return result;
	}

	async readFile(uri: vscode.Uri): Promise<Uint8Array> {
		const record = await this.request<FileRecord>(uri, { operation: 'read' });
		const key = uri.toString();
		const existing = this.files.get(key);
		if (existing) {
			if (record.fileId !== existing.replica.fileId) { throw vscode.FileSystemError.Unavailable('FILE_REPLACED'); }
			if (!record.document) { throw vscode.FileSystemError.Unavailable('FILE_TYPE_CHANGED'); }
			existing.replica.merge(record.document); existing.record = record;
			return new TextEncoder().encode(existing.replica.text);
		}
		if (record.document) {
			const replica = new SharedDocument(record.document);
			this.files.set(key, { uri, record, replica, view: new SharedDocument(record.document), version: 0, sent: 0 });
			return new TextEncoder().encode(replica.text);
		}
		return decode(record.content);
	}

	async writeFile(uri: vscode.Uri, content: Uint8Array, options: { create: boolean; overwrite: boolean }): Promise<void> {
		const file = this.files.get(uri.toString());
		if (file && vscode.workspace.textDocuments.some(document => document.uri.toString() === uri.toString())) {
			// Live edits are already represented as CRDT operations; a delayed save must not replace them.
			await this.flush(file);
			return;
		}
		let current: FileRecord | undefined;
		try { current = await this.request<FileRecord>(uri, { operation: 'read' }); }
		catch (error) { if (!(error instanceof vscode.FileSystemError) || error.code !== 'FileNotFound') { throw error; } }
		if (current && !options.overwrite) { throw vscode.FileSystemError.FileExists(uri); }
		let text: string | undefined;
		try { text = new TextDecoder('utf-8', { fatal: true }).decode(content); } catch { /* Binary files use etags. */ }
		if (current?.document && text !== undefined && !text.includes('\0')) {
			const replica = new SharedDocument(current.document); replica.setText(text);
			await this.request(uri, { operation: 'write', fileId: current.fileId, document: replica.snapshot, ...options });
		} else {
			await this.request(uri, { operation: 'write', content: encode(content), ...(current ? { fileId: current.fileId, etag: current.etag } : {}), ...options });
		}
		this.changes.fire([{ type: vscode.FileChangeType.Changed, uri }]);
	}

	async createDirectory(uri: vscode.Uri): Promise<void> {
		await this.request(uri, { operation: 'mkdir' });
		this.changes.fire([{ type: vscode.FileChangeType.Created, uri }]);
	}

	async rename(oldUri: vscode.Uri, newUri: vscode.Uri, options: { overwrite: boolean }): Promise<void> {
		if (oldUri.authority !== newUri.authority) { throw vscode.FileSystemError.NoPermissions('Cross-workspace moves are not supported'); }
		// Destination replacement is deliberately a conflict; preserve both participants' files.
		await this.flushBelow(oldUri);
		const source = await this.request<FileRecord>(oldUri, { operation: 'stat' });
		await this.request(oldUri, { operation: 'rename', target: newUri.path, resourceId: source.resourceId, overwrite: options.overwrite });
		this.dropBelow(oldUri);
		this.changes.fire([{ type: vscode.FileChangeType.Deleted, uri: oldUri }, { type: vscode.FileChangeType.Created, uri: newUri }]);
	}

	async delete(uri: vscode.Uri, options: { recursive: boolean }): Promise<void> {
		await this.flushBelow(uri);
		const source = await this.request<FileRecord>(uri, { operation: 'stat' });
		await this.request(uri, { operation: 'delete', recursive: options.recursive, resourceId: source.resourceId });
		this.dropBelow(uri);
		this.changes.fire([{ type: vscode.FileChangeType.Deleted, uri }]);
	}

	private below(file: vscode.Uri, directory: vscode.Uri): boolean {
		return file.authority === directory.authority && (directory.path === '/' || file.path === directory.path || file.path.startsWith(directory.path + '/'));
	}
	private async flushBelow(uri: vscode.Uri): Promise<void> {
		for (const file of this.files.values()) { if (this.below(file.uri, uri)) { await this.flush(file); } }
	}
	private dropBelow(uri: vscode.Uri): void {
		for (const [key, file] of this.files) { if (this.below(file.uri, uri)) { clearTimeout(file.timer); this.files.delete(key); } }
	}

	private changed(event: vscode.TextDocumentChangeEvent): void {
		if (!event.contentChanges.length) { return; }
		const file = this.files.get(event.document.uri.toString());
		if (!file) { return; }
		const text = event.document.getText();
		if (file.applying?.text === text) {
			file.view.merge(file.applying.snapshot); file.applying = undefined;
			return;
		}
		if (text === file.replica.text) {
			// A clean working copy may reload through readFile before the explicit editor update.
			file.view.merge(file.replica.snapshot);
			return;
		}
		// Local operations branch from the text actually displayed, including while a remote edit is pending.
		file.view.setText(text); file.replica.merge(file.view.snapshot); file.version++;
		clearTimeout(file.timer);
		file.timer = setTimeout(() => { void this.flush(file).catch(error => this.report(file, error)); }, 100);
		void this.applyRemote(file).catch(error => this.report(file, error));
	}

	private async flush(file: OpenFile): Promise<void> {
		clearTimeout(file.timer);
		if (file.flight) { await file.flight; }
		if (file.sent === file.version) { return; }
		file.flight = (async () => {
			while (file.sent < file.version) {
				const version = file.version;
				await this.request(file.uri, { operation: 'write', fileId: file.replica.fileId, document: file.replica.snapshot, overwrite: true });
				file.sent = version; file.error = undefined;
			}
			await this.refreshFile(file);
		})().finally(() => { file.flight = undefined; });
		await file.flight;
	}

	private invalidate(client: AgoraClient, change: FileChange): void {
		if (this.disposed) { return; }
		const root = workspaceUri(client.config);
		if (change.operation !== 'poll') {
			const uri = root.with({ path: change.path });
			const type = change.operation === 'delete' || change.operation === 'rename' ? vscode.FileChangeType.Deleted
				: change.created || change.operation === 'mkdir' ? vscode.FileChangeType.Created : vscode.FileChangeType.Changed;
			const parent = uri.with({ path: uri.path.slice(0, uri.path.lastIndexOf('/')) || '/' });
			this.changes.fire([{ type, uri }, { type: vscode.FileChangeType.Changed, uri: parent }]);
			if (change.target) { this.changes.fire([{ type: vscode.FileChangeType.Created, uri: root.with({ path: change.target }) }]); }
		} else {
			for (const { uri } of this.watched.values()) {
				if (uri.authority === root.authority) { this.changes.fire([{ type: vscode.FileChangeType.Changed, uri }]); }
			}
		}
		for (const file of this.files.values()) {
			if (file.uri.authority === root.authority && (change.path === '/' || file.uri.path === change.path || file.uri.path.startsWith(change.path + '/'))) {
				void this.refreshFile(file).catch(error => this.report(file, error));
			}
		}
	}

	private async refreshFile(file: OpenFile): Promise<void> {
		const key = file.uri.toString();
		if (this.refreshing.has(key) || this.disposed) { return; }
		this.refreshing.add(key);
		try {
			const record = await this.request<FileRecord>(file.uri, { operation: 'read' });
			if (!record.document || record.fileId !== file.replica.fileId) { throw new Error('FILE_REPLACED'); }
			file.replica.merge(record.document); file.record = record;
			await this.applyRemote(file);
		} finally { this.refreshing.delete(key); }
	}

	private async applyRemote(file: OpenFile): Promise<void> {
		if (file.applying || this.disposed) { return; }
		const editor = vscode.window.visibleTextEditors.find(editor => editor.document.uri.toString() === file.uri.toString());
		if (!editor) { return; }
		const text = file.replica.text;
		const before = editor.document.getText();
		const snapshot = file.replica.snapshot;
		if (before === text) { file.view.merge(snapshot); return; }
		const edit = replacement(before, text);
		const applying = { text, snapshot };
		file.applying = applying;
		let applied = false;
		try {
			// TextEditor.edit rejects when the document changes before this versioned edit applies.
			applied = await editor.edit(builder => builder.replace(new vscode.Range(editor.document.positionAt(edit.start), editor.document.positionAt(edit.end)), edit.text),
				{ undoStopBefore: false, undoStopAfter: false });
		} finally {
			if (file.applying === applying) { file.applying = undefined; }
		}
		if (applied && editor.document.getText() !== file.replica.text) {
			void this.applyRemote(file).catch(error => this.report(file, error));
		}
	}

	private report(file: OpenFile, error: unknown): void {
		if (this.disposed || this.files.get(file.uri.toString()) !== file) { return; }
		const message = error instanceof Error ? error.message : String(error);
		if (file.error !== message) {
			file.error = message;
			void vscode.window.showErrorMessage(vscode.l10n.t('Agora could not synchronize {0}: {1}. Your local editor contents are retained.', file.uri.path, message));
		}
	}

	reconnect(origin: string): void {
		for (const [key, client] of [...this.clients]) {
			if (client.config.origin === origin) {
				client.dispose(); this.clients.delete(key);
				const root = workspaceUri(client.config);
				this.changes.fire([{ type: vscode.FileChangeType.Changed, uri: root }]);
				void this.client(root).request({ operation: 'stat', path: '/' }).catch(() => {});
			}
		}
		for (const file of this.files.values()) {
			if (config(file.uri).origin === origin) { void this.refreshFile(file).catch(error => this.report(file, error)); }
		}
	}

	dispose(): void {
		this.disposed = true; clearInterval(this.poll);
		for (const file of this.files.values()) { clearTimeout(file.timer); }
		for (const client of this.clients.values()) { client.dispose(); }
		for (const disposable of this.disposables) { disposable.dispose(); }
	}
}

export function activate(context: vscode.ExtensionContext): void {
	const provider = new WorkspaceFiles(context);
	context.subscriptions.push(provider, vscode.workspace.registerFileSystemProvider('agora', provider, { isCaseSensitive: true }),
		vscode.commands.registerCommand('agora.openWorkspace', async () => {
			const input = await vscode.window.showInputBox({ prompt: vscode.l10n.t('Agora HTTPS origin or shared agora:// workspace URI'), value: 'https://localhost:8443' });
			if (!input) { return; }
			try {
				let uri: vscode.Uri;
				if (input.startsWith('agora://')) { uri = workspaceUri(config(vscode.Uri.parse(input))); }
				else {
					const canvasId = await vscode.window.showInputBox({ prompt: vscode.l10n.t('Agora Canvas ID') });
					if (!canvasId) { return; }
					uri = workspaceUri(connection({ origin: input, canvasId: Number(canvasId) }));
				}
				await vscode.commands.executeCommand('vscode.openFolder', uri);
			} catch (error) { void vscode.window.showErrorMessage(String(error)); }
		}),
		vscode.commands.registerCommand('agora.setToken', async (options?: { origin: string; token?: string }) => {
			const folder = vscode.workspace.workspaceFolders?.find(folder => folder.uri.scheme === 'agora');
			const origin = options?.origin || (folder ? config(folder.uri).origin : await vscode.window.showInputBox({ prompt: vscode.l10n.t('Agora HTTPS Origin'), value: 'https://localhost:8443' }));
			if (!origin) { return; }
			const validated = connection({ origin, canvasId: 1 }).origin;
			const token = options?.token || await vscode.window.showInputBox({ password: true, ignoreFocusOut: true, prompt: vscode.l10n.t('Agora Access Token') });
			if (token) { await context.secrets.store(`agora.token:${validated}`, token); provider.reconnect(validated); }
		}),
		vscode.commands.registerCommand('agora.copyWorkspaceLink', async () => {
			const folder = vscode.workspace.workspaceFolders?.find(folder => folder.uri.scheme === 'agora');
			if (folder) { await vscode.env.clipboard.writeText(folder.uri.toString()); }
		}));
}
