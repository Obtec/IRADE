import { marked } from 'marked';
import DOMPurify from 'dompurify';
import {
	AgoraError,
	type AgentApiProfile,
	type AgentHistoryTurn,
	type AgentQuotaWindow,
	type AgentStatusResult,
	type AgentTelemetry,
} from '../client.ts';
import type { ResearchWorkspace, Cell } from './workspace.ts';
import {
	clampAgentOptions,
	DEFAULT_AGENT_OPTIONS,
	loadAgentPreferences,
	saveAgentPreferences,
	sanitizeApiId,
	type AgentPreferences,
	type AgentReasoningEffort,
} from './agentPrefs.ts';
import {
	clearPersonalApiKey,
	getPersonalApiKey,
	hasPersonalApiKey,
	setPersonalApiKey,
} from './agentCredentials.ts';

interface ThreadEntry {
	id: string;
	role: 'user' | 'assistant' | 'error';
	content: string;
	api?: string;
	model?: string;
	limited?: boolean;
}

export interface AgentPanelContext {
	getWorkspace: () => ResearchWorkspace | undefined;
	getCells: () => Cell[];
	getSelectedId: () => string | undefined;
	isConnected: () => boolean;
	isDemo?: () => boolean;
	onLocalPrompt?: (message: string, selectedCell?: Cell) => Promise<{ answer: string }>;
	onLog: (message: string) => void;
	onNotify: (message: string, isError?: boolean) => void;
	onSelectCell?: (id: string) => void;
	onVisibilityChange?: () => void;
	onClosePanel?: () => void;
}

const AGENT_ERROR_LABELS: Record<string, string> = {
	AGENT_NOT_CONFIGURED: '에이전트 API 키가 설정되지 않았습니다. 개인 키를 입력하거나 서버 .env에 AGENT_API_KEY를 설정하세요.',
	AGENT_API_UNKNOWN: '선택한 API 프로필이 서버에 구성되어 있지 않습니다.',
	AGENT_API_KEY_INVALID: 'API 키 형식을 확인해주세요. 공백이나 줄바꿈 없이 입력해야 합니다.',
	AGENT_OPTIONS_INVALID: '에이전트 파라미터가 허용 범위를 벗어났습니다.',
	AGENT_UNAVAILABLE: '에이전트 데몬에 연결할 수 없습니다. 컨테이너 상태를 확인하세요.',
	AGENT_BUSY: '다른 에이전트 요청을 처리 중입니다. 잠시 후 다시 시도하세요.',
	AGENT_REQUEST_BUSY: '이미 실행 중인 에이전트 작업이 있습니다. 완료 후 다시 시도하세요.',
	AGENT_STATUS_BUSY: '한도 조회 요청이 잠시 혼잡합니다. 곧 다시 시도하세요.',
	AGENT_PROVIDER_BUSY: '모델 API 호출 한도(429)에 도달했습니다. 잔여 한도를 확인한 뒤 다시 시도하세요.',
	AGENT_PROVIDER_AUTH_FAILED: '모델 API 인증이 거부되었습니다. API 키와 모델 권한을 확인하세요.',
	AGENT_PROVIDER_TIMEOUT: '모델 API 응답 시간이 초과되었습니다.',
	AGENT_PROVIDER_UNAVAILABLE: '모델 API 엔드포인트에 연결할 수 없습니다.',
	AGENT_TIMEOUT: '에이전트 작업 시간 제한을 초과했습니다.',
	RUNTIME_EXECUTION_DENIED: '현재 권한으로는 AI 에이전트 쓰기 작업을 실행할 수 없습니다 (쓰기 그룹 권한 필요).',
	ITEM_WRITE_DENIED: '이 workspace에 쓰기 권한이 없습니다.',
	DISCONNECTED: 'Workspace 연결이 끊어졌습니다.',
};

function esc(value: string): string {
	return value.replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]!));
}

function renderAssistantMarkdown(markdown: string): string {
	const raw = marked.parse(markdown, { async: false, breaks: true, gfm: true });
	return DOMPurify.sanitize(typeof raw === 'string' ? raw : '');
}

function formatNumber(value: number | null | undefined): string {
	if (typeof value !== 'number' || !Number.isFinite(value)) { return '—'; }
	return Number.isInteger(value) ? value.toLocaleString('ko-KR') : value.toFixed(1);
}

function quotaRatio(win?: AgentQuotaWindow): number | null {
	if (!win || typeof win.limit !== 'number' || win.limit <= 0 || typeof win.remaining !== 'number') { return null; }
	return Math.max(0, Math.min(1, win.remaining / win.limit));
}

function quotaStateClass(ratio: number | null): string {
	if (ratio === null) { return 'unknown'; }
	if (ratio <= 0) { return 'exhausted'; }
	if (ratio <= 0.2) { return 'warn'; }
	return 'ok';
}

function formatTime(epochSeconds?: number | null): string {
	if (!epochSeconds || !Number.isFinite(epochSeconds)) { return '미관측'; }
	return new Date(epochSeconds * 1000).toLocaleTimeString('ko-KR', {
		hour: '2-digit',
		minute: '2-digit',
		second: '2-digit',
	});
}

export class AgentPanelController {
	private drawer: HTMLElement;
	private toggleButton: HTMLButtonElement | null;
	private closeButton: HTMLButtonElement;
	private apiSelect: HTMLSelectElement;
	private activeApiBadge: HTMLElement;
	private connectionNotice: HTMLElement;
	private credBadge: HTMLElement;
	private keyInput: HTMLInputElement;
	private keyApplyBtn: HTMLButtonElement;
	private keyClearBtn: HTMLButtonElement;
	private quotaScopeEl: HTMLElement;
	private quotaObservedEl: HTMLElement;
	private quotaRefreshBtn: HTMLButtonElement;
	private quotaGridEl: HTMLElement;
	private quotaNoteEl: HTMLElement;
	private reasoningSelect: HTMLSelectElement;
	private maxTokensInput: HTMLInputElement;
	private maxStepsInput: HTMLInputElement;
	private temperatureInput: HTMLInputElement;
	private topPInput: HTMLInputElement;
	private attachCheck: HTMLInputElement;
	private historyCheck: HTMLInputElement;
	private allowDeleteCheck: HTMLInputElement;
	private resetParamsBtn: HTMLButtonElement;
	private selectionSummaryEl: HTMLElement;
	private threadEl: HTMLElement;
	private progressEl: HTMLElement;
	private formEl: HTMLFormElement;
	private promptInput: HTMLTextAreaElement;
	private submitBtn: HTMLButtonElement;
	private clearHistoryBtn: HTMLButtonElement;

	private prefs: AgentPreferences = loadAgentPreferences();
	private apis: AgentApiProfile[] = [];
	private serverDefaults = { ...DEFAULT_AGENT_OPTIONS };
	private statusByScope = new Map<string, AgentStatusResult>();
	private telemetryByScope = new Map<string, AgentTelemetry>();
	private thread: ThreadEntry[] = [];
	private running = false;
	private refreshingStatus = false;
	private progressText = '';
	private countdownTimer?: ReturnType<typeof setInterval>;

	constructor(private readonly ctx: AgentPanelContext) {
		this.drawer = this.require('#agent-drawer');
		this.toggleButton = document.querySelector<HTMLButtonElement>('#agent-toggle');
		this.closeButton = this.require('#agent-close');
		this.apiSelect = this.require('#agent-api-select');
		this.activeApiBadge = this.require('#agent-active-api-badge');
		this.connectionNotice = this.require('#agent-connection-notice');
		this.credBadge = this.require('#agent-cred-badge');
		this.keyInput = this.require('#agent-api-key-input');
		this.keyApplyBtn = this.require('#agent-api-key-apply');
		this.keyClearBtn = this.require('#agent-api-key-clear');
		this.quotaScopeEl = this.require('#agent-quota-scope');
		this.quotaObservedEl = this.require('#agent-quota-observed');
		this.quotaRefreshBtn = this.require('#agent-refresh-status');
		this.quotaGridEl = this.require('#agent-quota-grid');
		this.quotaNoteEl = this.require('#agent-quota-note');
		this.reasoningSelect = this.require('#agent-reasoning');
		this.maxTokensInput = this.require('#agent-max-tokens');
		this.maxStepsInput = this.require('#agent-max-steps');
		this.temperatureInput = this.require('#agent-temperature');
		this.topPInput = this.require('#agent-top-p');
		this.attachCheck = this.require('#agent-attach-selected');
		this.historyCheck = this.require('#agent-include-history');
		this.allowDeleteCheck = this.require('#agent-allow-delete');
		this.resetParamsBtn = this.require('#agent-reset-params');
		this.selectionSummaryEl = this.require('#agent-selection-summary');
		this.threadEl = this.require('#agent-thread');
		this.progressEl = this.require('#agent-progress');
		this.formEl = this.require('#agent-form');
		this.promptInput = this.require('#agent-prompt');
		this.submitBtn = this.require('#agent-submit');
		this.clearHistoryBtn = this.require('#agent-clear-history');

		this.bindEvents();
		this.syncInputsFromPrefs();
		this.render();
		this.countdownTimer = setInterval(() => {
			if (!this.drawer.hidden) {
				this.renderQuota();
			}
		}, 1000);
	}

	private require<T extends Element>(selector: string): T {
		const el = document.querySelector<T>(selector);
		if (!el) { throw new Error(`Missing agent UI element: ${selector}`); }
		return el;
	}

	private activeApiId(): string {
		if (this.prefs.api) { return this.prefs.api; }
		if (this.apis.length) { return this.apis[0].id; }
		return 'k-exaone_v2.5';
	}

	private scopeKey(apiId = this.activeApiId(), personal = hasPersonalApiKey(apiId)): string {
		return `${apiId || '__default__'}::${personal ? 'personal' : 'server'}`;
	}

	private bindEvents(): void {
		this.toggleButton?.addEventListener('click', () => {
			if (this.drawer.hidden) { this.open(); } else {
				this.close();
				this.ctx.onClosePanel?.();
			}
		});
		this.closeButton.addEventListener('click', () => {
			this.close();
			this.ctx.onClosePanel?.();
		});
		this.threadEl.addEventListener('click', event => {
			const starter = (event.target as Element).closest<HTMLButtonElement>('[data-agent-starter]');
			if (!starter || this.running) { return; }
			const text = starter.dataset.agentStarter || '';
			if (!text.trim()) { return; }
			this.promptInput.value = text;
			this.formEl.requestSubmit();
		});
		this.apiSelect.addEventListener('change', () => {
			const nextApi = sanitizeApiId(this.apiSelect.value);
			this.prefs = saveAgentPreferences({ ...this.prefs, api: nextApi });
			this.keyInput.value = '';
			this.render();
			void this.refreshStatus().catch(() => {});
		});
		this.keyApplyBtn.addEventListener('click', () => {
			const apiId = this.activeApiId();
			const ok = setPersonalApiKey(apiId, this.keyInput.value);
			if (!ok) {
				this.ctx.onNotify('유효한 개인 API 키를 공백·줄바꿈 없이 입력해주세요.', true);
				return;
			}
			this.keyInput.value = '';
			this.ctx.onNotify(`${apiId} 개인 API 키를 현재 탭 세션에 적용했습니다.`);
			this.render();
			void this.refreshStatus().catch(() => {});
		});
		this.keyClearBtn.addEventListener('click', () => {
			const apiId = this.activeApiId();
			clearPersonalApiKey(apiId);
			this.keyInput.value = '';
			this.ctx.onNotify(`${apiId} 개인 키를 삭제하고 서버 기본 키로 전환했습니다.`);
			this.render();
			void this.refreshStatus().catch(() => {});
		});
		this.quotaRefreshBtn.addEventListener('click', () => {
			void this.refreshStatus(true);
		});

		const saveParams = (): void => {
			this.prefs = saveAgentPreferences({
				...this.prefs,
				options: clampAgentOptions({
					reasoning_effort: this.reasoningSelect.value as AgentReasoningEffort,
					max_tokens: Number(this.maxTokensInput.value),
					max_steps: Number(this.maxStepsInput.value),
					temperature: Number(this.temperatureInput.value),
					top_p: Number(this.topPInput.value),
				}, this.serverDefaults),
				attachSelection: this.attachCheck.checked,
				includeHistory: this.historyCheck.checked,
				allowDelete: this.allowDeleteCheck.checked,
			});
			this.syncInputsFromPrefs();
		};
		for (const control of [
			this.reasoningSelect,
			this.maxTokensInput,
			this.maxStepsInput,
			this.temperatureInput,
			this.topPInput,
			this.attachCheck,
			this.historyCheck,
			this.allowDeleteCheck,
		]) {
			control.addEventListener('change', saveParams);
		}
		this.resetParamsBtn.addEventListener('click', () => {
			this.prefs = saveAgentPreferences({
				...this.prefs,
				options: { ...this.serverDefaults },
				attachSelection: true,
				includeHistory: true,
				allowDelete: false,
			});
			this.syncInputsFromPrefs();
			this.ctx.onNotify('에이전트 파라미터를 기본값으로 복원했습니다.');
		});

		this.clearHistoryBtn.addEventListener('click', () => {
			this.thread = [];
			this.renderThread();
			this.ctx.onNotify('에이전트 대화 기록을 비웠습니다.');
		});

		this.promptInput.addEventListener('keydown', event => {
			if (event.isComposing || event.keyCode === 229) { return; }
			if (event.key === 'Enter' && (!event.shiftKey || event.ctrlKey || event.metaKey)) {
				event.preventDefault();
				this.formEl.requestSubmit();
			}
		});
		this.formEl.addEventListener('submit', event => {
			event.preventDefault();
			void this.runPrompt();
		});
	}

	private syncInputsFromPrefs(): void {
		const opts = this.prefs.options;
		this.reasoningSelect.value = opts.reasoning_effort;
		this.maxTokensInput.value = String(opts.max_tokens);
		this.maxStepsInput.value = String(opts.max_steps);
		this.temperatureInput.value = String(opts.temperature);
		this.topPInput.value = String(opts.top_p);
		this.attachCheck.checked = this.prefs.attachSelection;
		this.historyCheck.checked = this.prefs.includeHistory;
		this.allowDeleteCheck.checked = this.prefs.allowDelete;
	}

	open(options?: string | { initialPrompt?: string; focus?: boolean }): void {
		const normalized = typeof options === 'string'
			? { initialPrompt: options, focus: true }
			: (options ?? { focus: true });
		this.drawer.hidden = false;
		document.body.classList.add('agent-open');
		this.toggleButton?.setAttribute('aria-expanded', 'true');
		this.toggleButton?.classList.add('active');
		this.ctx.onVisibilityChange?.();
		if (normalized.initialPrompt !== undefined) {
			this.promptInput.value = normalized.initialPrompt;
		}
		this.render();
		if (this.ctx.isConnected() && this.ctx.getWorkspace()) {
			void this.refreshStatus().catch(() => {});
		}
		if (normalized.focus !== false) {
			requestAnimationFrame(() => this.promptInput.focus());
		}
	}

	close(): void {
		this.drawer.hidden = true;
		document.body.classList.remove('agent-open');
		this.toggleButton?.setAttribute('aria-expanded', 'false');
		this.toggleButton?.classList.remove('active');
		this.ctx.onVisibilityChange?.();
	}

	onWorkspaceChanged(): void {
		this.render();
		if (!this.drawer.hidden && this.ctx.isConnected() && this.ctx.getWorkspace()) {
			void this.refreshStatus().catch(() => {});
		}
	}

	getActiveApiLabel(): string {
		const id = this.activeApiId();
		const profile = this.apis.find(entry => entry.id === id);
		return profile?.label || profile?.model || id;
	}

	async runCellPrompt(
		cellId: string,
		message: string,
		history: AgentHistoryTurn[] = [],
		onProgress?: (chunk: string) => void,
	): Promise<{ answer: string; api: string; model?: string; limited?: boolean }> {
		const workspace = this.ctx.getWorkspace();
		if (!workspace || !this.ctx.isConnected()) {
			throw new Error('공유 Workspace에 연결한 뒤 AI 에이전트를 실행할 수 있습니다.');
		}
		if (!workspace.client.canWrite) {
			throw new Error('이 Workspace는 읽기 전용입니다. 쓰기 그룹 권한이 필요합니다.');
		}
		if (workspace.pending) {
			await workspace.flush();
		}
		const requestedApi = this.activeApiId();
		const personalKey = getPersonalApiKey(requestedApi);
		const scope = this.scopeKey(requestedApi, Boolean(personalKey));
		const options = clampAgentOptions(this.prefs.options, this.serverDefaults);
		try {
			const result = await workspace.client.executeAgent({
				operation: 'run',
				...(requestedApi ? { api: requestedApi } : {}),
				...(personalKey ? { api_key: personalKey } : {}),
				message,
				selected_ids: [cellId],
				history: history.slice(-12).map(entry => ({
					role: entry.role,
					content: entry.content.slice(0, 8000),
				})),
				allow_delete: this.prefs.allowDelete,
				options,
			}, onProgress);
			const resolvedApi = result.api || requestedApi;
			const resolvedScope = this.scopeKey(resolvedApi, Boolean(personalKey));
			if (result.telemetry) {
				this.telemetryByScope.set(resolvedScope, result.telemetry);
			}
			const answer = result.answer || result.message || '작업을 완료했습니다.';
			this.ctx.onLog(`AI 에이전트(${result.model || resolvedApi})가 대화 셀에 응답했습니다.`);
			if (!this.drawer.hidden) {
				this.render();
			}
			return {
				answer,
				api: resolvedApi,
				model: result.model,
				limited: result.limited,
			};
		} catch (error) {
			this.recordErrorTelemetry(scope, error);
			if (!this.drawer.hidden) {
				this.render();
			}
			throw new Error(this.describeError(error));
		}
	}

	onSelectionChanged(): void {
		if (!this.drawer.hidden) {
			this.renderSelectionSummary();
		}
	}

	async refreshStatus(manual = false): Promise<void> {
		const workspace = this.ctx.getWorkspace();
		if (!workspace || !this.ctx.isConnected() || this.refreshingStatus) { return; }
		const requestedApi = this.prefs.api || undefined;
		const personalKey = getPersonalApiKey(this.activeApiId());
		const targetScope = this.scopeKey(this.activeApiId(), Boolean(personalKey));
		this.refreshingStatus = true;
		this.quotaRefreshBtn.disabled = true;
		try {
			const status = await workspace.client.executeAgent({
				operation: 'status',
				...(requestedApi ? { api: requestedApi } : {}),
				...(personalKey ? { api_key: personalKey } : {}),
			});
			if (Array.isArray(status.apis) && status.apis.length) {
				this.apis = status.apis;
				if (!this.prefs.api && status.api) {
					this.prefs = saveAgentPreferences({ ...this.prefs, api: status.api });
				}
			}
			if (status.defaults) {
				this.serverDefaults = clampAgentOptions(status.defaults);
			}
			const resolvedApi = status.api || this.activeApiId();
			const scope = this.scopeKey(resolvedApi, status.credential_source === 'personal');
			this.statusByScope.set(scope, status);
			if (status.telemetry) {
				this.telemetryByScope.set(scope, status.telemetry);
			} else if (scope !== targetScope) {
				this.statusByScope.set(targetScope, status);
			}
			if (manual) {
				this.ctx.onNotify(`${resolvedApi} 상태 및 잔여 한도를 조회했습니다.`);
			}
		} catch (error) {
			this.recordErrorTelemetry(targetScope, error);
			if (manual) {
				this.ctx.onNotify(this.describeError(error), true);
			}
		} finally {
			this.refreshingStatus = false;
			this.quotaRefreshBtn.disabled = false;
			this.render();
		}
	}

	private recordErrorTelemetry(scope: string, error: unknown): void {
		if (error instanceof AgoraError && error.payload && typeof error.payload.telemetry === 'object' && error.payload.telemetry) {
			this.telemetryByScope.set(scope, error.payload.telemetry as AgentTelemetry);
		}
	}

	private describeError(error: unknown): string {
		if (error instanceof AgoraError) {
			if (error.serverMessage) { return error.serverMessage; }
			return AGENT_ERROR_LABELS[error.code] || `에이전트 오류 (${error.code})`;
		}
		return error instanceof Error ? error.message : String(error);
	}

	private async runPrompt(): Promise<void> {
		const message = this.promptInput.value.trim();
		if (!message || this.running) { return; }

		const workspace = this.ctx.getWorkspace();
		if (!workspace || !this.ctx.isConnected()) {
			if (!this.ctx.onLocalPrompt) {
				this.ctx.onNotify('공유 Workspace에 연결한 뒤 AI 에이전트를 실행할 수 있습니다.', true);
				return;
			}
			const selectedId = this.ctx.getSelectedId();
			const selectedCell = selectedId ? this.ctx.getCells().find(item => item.id === selectedId) : undefined;
			this.thread.push({ id: crypto.randomUUID(), role: 'user', content: message, api: 'local-assistant' });
			this.promptInput.value = '';
			this.running = true;
			this.progressText = '워크스페이스 맥락을 분석하는 중…';
			this.render();
			try {
				const localRes = await this.ctx.onLocalPrompt(message, this.prefs.attachSelection ? selectedCell : undefined);
				this.thread.push({
					id: crypto.randomUUID(),
					role: 'assistant',
					content: localRes.answer,
					api: 'local-assistant',
					model: '로컬 체험 어시스턴트',
				});
			} catch (error) {
				const errMsg = this.describeError(error);
				this.thread.push({ id: crypto.randomUUID(), role: 'error', content: errMsg, api: 'local-assistant' });
				this.ctx.onNotify(errMsg, true);
			} finally {
				this.running = false;
				this.progressText = '';
				this.render();
			}
			return;
		}

		if (!workspace.client.canWrite) {
			this.ctx.onNotify('이 Workspace는 읽기 전용입니다. 쓰기 그룹 권한이 필요합니다.', true);
			return;
		}

		if (workspace.pending) {
			await workspace.flush();
		}

		const requestedApi = this.activeApiId();
		const personalKey = getPersonalApiKey(requestedApi);
		const scope = this.scopeKey(requestedApi, Boolean(personalKey));
		const options = clampAgentOptions({
			reasoning_effort: this.reasoningSelect.value as AgentReasoningEffort,
			max_tokens: Number(this.maxTokensInput.value),
			max_steps: Number(this.maxStepsInput.value),
			temperature: Number(this.temperatureInput.value),
			top_p: Number(this.topPInput.value),
		}, this.serverDefaults);
		this.prefs = saveAgentPreferences({
			...this.prefs,
			api: sanitizeApiId(this.apiSelect.value) || requestedApi,
			options,
			attachSelection: this.attachCheck.checked,
			includeHistory: this.historyCheck.checked,
			allowDelete: this.allowDeleteCheck.checked,
		});

		const selectedId = this.ctx.getSelectedId();
		const selectedIds = this.prefs.attachSelection && selectedId ? [selectedId] : [];
		const history: AgentHistoryTurn[] = this.prefs.includeHistory
			? this.thread
				.filter((entry): entry is ThreadEntry & { role: 'user' | 'assistant' } => entry.role === 'user' || entry.role === 'assistant')
				.slice(-12)
				.map(entry => ({ role: entry.role, content: entry.content.slice(0, 8000) }))
			: [];

		this.thread.push({ id: crypto.randomUUID(), role: 'user', content: message, api: requestedApi });
		this.promptInput.value = '';
		this.running = true;
		this.progressText = '에이전트 요청을 전송했습니다…';
		this.render();

		try {
			const result = await workspace.client.executeAgent({
				operation: 'run',
				...(requestedApi ? { api: requestedApi } : {}),
				...(personalKey ? { api_key: personalKey } : {}),
				message,
				selected_ids: selectedIds,
				history,
				allow_delete: this.prefs.allowDelete,
				options,
			}, chunk => {
				this.progressText = (this.progressText ? `${this.progressText}\n` : '') + chunk.trimEnd();
				if (this.progressText.length > 2400) {
					this.progressText = this.progressText.slice(-2400);
				}
				this.renderProgress();
			});

			const resolvedApi = result.api || requestedApi;
			const resolvedScope = this.scopeKey(resolvedApi, Boolean(personalKey));
			if (result.telemetry) {
				this.telemetryByScope.set(resolvedScope, result.telemetry);
			}
			const answer = result.answer || result.message || '작업을 완료했습니다.';
			this.thread.push({
				id: crypto.randomUUID(),
				role: 'assistant',
				content: answer,
				api: resolvedApi,
				model: result.model,
				limited: result.limited,
			});
			this.ctx.onLog(`AI 에이전트(${result.model || resolvedApi})가 작업을 완료했습니다.`);
		} catch (error) {
			this.recordErrorTelemetry(scope, error);
			const errMsg = this.describeError(error);
			this.thread.push({ id: crypto.randomUUID(), role: 'error', content: errMsg, api: requestedApi });
			this.ctx.onNotify(errMsg, true);
		} finally {
			this.running = false;
			this.progressText = '';
			this.render();
		}
	}

	dispose(): void {
		if (this.countdownTimer) {
			clearInterval(this.countdownTimer);
		}
	}

	render(): void {
		this.renderApiSelector();
		this.renderConnectionAndCredentialState();
		this.renderQuota();
		this.renderSelectionSummary();
		this.renderProgress();
		this.renderThread();
	}

	private renderApiSelector(): void {
		const activeApi = this.activeApiId();
		const profiles: AgentApiProfile[] = this.apis.length
			? this.apis
			: [{ id: activeApi, label: `EXAONE 2.5 (${activeApi})`, model: activeApi, default_configured: undefined }];
		const optionsHtml = profiles.map(profile =>
			`<option value="${esc(profile.id)}" ${profile.id === activeApi ? 'selected' : ''}>${esc(profile.label || profile.model || profile.id)}</option>`,
		).join('');
		if (this.apiSelect.innerHTML !== optionsHtml) {
			this.apiSelect.innerHTML = optionsHtml;
		}
		this.apiSelect.value = profiles.some(p => p.id === activeApi) ? activeApi : profiles[0].id;
		const currentProfile = profiles.find(p => p.id === this.apiSelect.value) || profiles[0];
		const personal = hasPersonalApiKey(currentProfile.id);
		this.activeApiBadge.textContent = `${currentProfile.id} · ${personal ? '개인 키' : '서버 키'}`;
	}

	private renderConnectionAndCredentialState(): void {
		const workspace = this.ctx.getWorkspace();
		const connected = this.ctx.isConnected() && Boolean(workspace);
		const isLocalAssist = !connected && Boolean(this.ctx.onLocalPrompt);
		const canWrite = Boolean(workspace?.client.canWrite);
		const apiId = this.activeApiId();
		const scope = this.scopeKey(apiId);
		const status = this.statusByScope.get(scope);
		const personal = hasPersonalApiKey(apiId);

		if (!connected) {
			this.connectionNotice.hidden = false;
			this.connectionNotice.className = isLocalAssist ? 'agent-notice info' : 'agent-notice warn';
			this.connectionNotice.textContent = isLocalAssist
				? '로컬 어시스턴트 모드 · 실시간 EXAONE MCP 연동은 ORCID Workspace 연결 후 활성화됩니다.'
				: '실시간 AI 에이전트 실행과 한도 조회는 상단 "열기 ↗"로 공유 Workspace에 연결한 뒤 사용할 수 있습니다.';
		} else if (!canWrite) {
			this.connectionNotice.hidden = false;
			this.connectionNotice.className = 'agent-notice warn';
			this.connectionNotice.textContent = '현재 읽기 전용 권한입니다. 잔여 한도 조회는 가능하지만 셀 생성·수정 에이전트 실행에는 쓰기 권한이 필요합니다.';
		} else if (status && status.configured === false) {
			this.connectionNotice.hidden = false;
			this.connectionNotice.className = 'agent-notice error';
			this.connectionNotice.textContent = `${apiId} API 키가 없습니다. 아래 개인 API 키를 입력하거나 서버 .env에 AGENT_API_KEY를 설정하세요.`;
		} else {
			this.connectionNotice.hidden = true;
			this.connectionNotice.textContent = '';
		}

		this.credBadge.className = `agent-cred-badge ${personal ? 'personal' : status?.default_configured === false ? 'missing' : 'server'}`;
		this.credBadge.textContent = personal
			? '개인 키 적용됨 (세션 전용)'
			: status?.default_configured === false
				? '키 미설정'
				: '서버 기본 키 사용 중';
		this.keyClearBtn.disabled = !personal;
		this.quotaRefreshBtn.disabled = !connected || this.refreshingStatus;
		this.submitBtn.disabled = (connected ? !canWrite : !isLocalAssist) || this.running;
		this.submitBtn.textContent = this.running ? '작업 중…' : '실행 ↑';
	}

	private renderQuota(): void {
		const apiId = this.activeApiId();
		const personal = hasPersonalApiKey(apiId);
		const scope = this.scopeKey(apiId, personal);
		const telemetry = this.telemetryByScope.get(scope);
		this.quotaScopeEl.textContent = `${apiId} · ${personal ? '개인 키' : '서버 기본 키'}`;
		this.quotaObservedEl.textContent = telemetry?.observed_at ? `관측 ${formatTime(telemetry.observed_at)}` : '미조회';

		const limits = telemetry?.limits || {};
		const windows: Array<{ key: 'second' | 'minute' | 'day'; label: string }> = [
			{ key: 'second', label: '초당' },
			{ key: 'minute', label: '분당' },
			{ key: 'day', label: '일일' },
		];
		this.quotaGridEl.innerHTML = windows.map(({ key, label }) => {
			const win = limits[key];
			const ratio = quotaRatio(win);
			const cls = quotaStateClass(ratio);
			const pct = ratio === null ? 0 : Math.round(ratio * 100);
			const value = win && (win.remaining != null || win.limit != null)
				? `${formatNumber(win.remaining)} / ${formatNumber(win.limit)}`
				: '— / —';
			return `<div class="agent-quota-card ${cls}" data-quota-window="${key}"><div class="agent-quota-head"><span>${label}</span><b>${esc(value)}</b></div><div class="agent-quota-bar"><span style="width:${pct}%"></span></div></div>`;
		}).join('');

		const retryAt = telemetry?.retry_at;
		if (typeof retryAt === 'number' && retryAt > Date.now() / 1000) {
			const left = Math.ceil(retryAt - Date.now() / 1000);
			this.quotaNoteEl.textContent = `호출 제한 대기 중 · ${left}초 후 재시도 가능 (API: ${apiId})`;
			this.quotaNoteEl.className = 'agent-quota-note warn';
		} else if (!telemetry?.limits_observed_at) {
			this.quotaNoteEl.textContent = '이 API·키 조합의 모델 응답 헤더가 아직 관측되지 않았습니다. 요청 실행 후 초당·분당·일일 잔여/한도가 갱신됩니다.';
			this.quotaNoteEl.className = 'agent-quota-note';
		} else {
			this.quotaNoteEl.textContent = `${apiId} (${personal ? '개인 키' : '서버 기본 키'}) 기준 최근 응답 헤더를 표시 중입니다.`;
			this.quotaNoteEl.className = 'agent-quota-note';
		}
	}

	private renderSelectionSummary(): void {
		const selectedId = this.ctx.getSelectedId();
		const cell = selectedId ? this.ctx.getCells().find(item => item.id === selectedId) : undefined;
		this.selectionSummaryEl.textContent = cell
			? `대상: ${cell.title} (#${cell.id.slice(0, 10)})`
			: '선택된 셀 없음 (필요 시 에이전트가 검색·생성)';
	}

	private renderProgress(): void {
		if (!this.running || !this.progressText) {
			this.progressEl.hidden = true;
			this.progressEl.textContent = '';
			return;
		}
		this.progressEl.hidden = false;
		this.progressEl.textContent = this.progressText;
	}

	private renderThread(): void {
		if (!this.thread.length) {
			this.threadEl.innerHTML = `
				<div class="agent-welcome">
					<div class="agent-welcome-badge">✦ IRADE Research Agent</div>
					<strong>오른쪽 패널에서 바로 연구를 지시하세요.</strong>
					<p>코드·형식 증명 생성, 선택한 셀 설명 및 리팩터링, 실험 결과 요약을 대화형으로 수행합니다.</p>
					<div class="agent-starter-grid">
						<button type="button" class="agent-starter-chip" data-agent-starter="선택한 셀의 내용을 분석하고 개선점을 설명해 줘.">✨ 현재 셀 설명 · 분석</button>
						<button type="button" class="agent-starter-chip" data-agent-starter="새 Lean 4 정리 셀을 생성하고 증명 초안을 작성해 줘.">∀ Lean 4 정리 생성</button>
						<button type="button" class="agent-starter-chip" data-agent-starter="수치 검증용 Python 코드 셀을 추가해 줘.">λ Python 실험 추가</button>
						<button type="button" class="agent-starter-chip" data-agent-starter="현재 Workspace 내용을 요약하는 Markdown 노트를 생성해 줘.">≡ 연구 요약 노트</button>
					</div>
				</div>`;
			return;
		}
		this.threadEl.innerHTML = this.thread.map(entry => {
			if (entry.role === 'user') {
				return `<div class="agent-msg user"><div class="agent-msg-meta">나</div><div class="agent-msg-body">${esc(entry.content)}</div></div>`;
			}
			if (entry.role === 'error') {
				return `<div class="agent-msg error"><div class="agent-msg-meta">오류</div><div class="agent-msg-body">${esc(entry.content)}</div></div>`;
			}
			const label = `✦ ${esc(entry.model || entry.api || 'EXAONE 2.5')}${entry.limited ? ' · 토큰 상한' : ''}`;
			return `<div class="agent-msg assistant"><div class="agent-msg-meta">${label}</div><div class="agent-msg-body agent-markdown">${renderAssistantMarkdown(entry.content)}</div></div>`;
		}).join('');
		this.threadEl.scrollTop = this.threadEl.scrollHeight;
	}
}
