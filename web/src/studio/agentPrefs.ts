import type { AgentOptions } from '../client.ts';

export type AgentReasoningEffort = 'low' | 'high' | 'max';

export interface AgentPreferences {
	api: string;
	options: Required<AgentOptions>;
	attachSelection: boolean;
	includeHistory: boolean;
	allowDelete: boolean;
}

const STORAGE_KEY = 'irade.agent.preferences.v1';

export const DEFAULT_AGENT_OPTIONS: Required<AgentOptions> = {
	reasoning_effort: 'low',
	max_tokens: 8192,
	max_steps: 8,
	temperature: 1,
	top_p: 0.95,
};

export const DEFAULT_AGENT_PREFERENCES: AgentPreferences = {
	api: '',
	options: { ...DEFAULT_AGENT_OPTIONS },
	attachSelection: true,
	includeHistory: true,
	allowDelete: false,
};

function clampInt(value: unknown, fallback: number, low: number, high: number): number {
	const parsed = typeof value === 'number' ? value : Number(value);
	if (!Number.isFinite(parsed)) { return fallback; }
	return Math.min(high, Math.max(low, Math.round(parsed)));
}

function clampFloat(value: unknown, fallback: number, low: number, high: number): number {
	const parsed = typeof value === 'number' ? value : Number(value);
	if (!Number.isFinite(parsed)) { return fallback; }
	return Math.min(high, Math.max(low, Math.round(parsed * 100) / 100));
}

export function sanitizeApiId(value: unknown): string {
	if (typeof value !== 'string') { return ''; }
	const trimmed = value.trim();
	return /^[A-Za-z0-9._-]{1,64}$/.test(trimmed) ? trimmed : '';
}

export function clampAgentOptions(
	raw?: Partial<AgentOptions> | null,
	defaults: Required<AgentOptions> = DEFAULT_AGENT_OPTIONS,
): Required<AgentOptions> {
	const effort = raw?.reasoning_effort;
	return {
		reasoning_effort: effort === 'low' || effort === 'high' || effort === 'max' ? effort : defaults.reasoning_effort,
		max_tokens: clampInt(raw?.max_tokens, defaults.max_tokens, 512, 32768),
		max_steps: clampInt(raw?.max_steps, defaults.max_steps, 1, 12),
		temperature: clampFloat(raw?.temperature, defaults.temperature, 0, 2),
		top_p: clampFloat(raw?.top_p, defaults.top_p, 0.05, 1),
	};
}

export function loadAgentPreferences(storage?: Pick<Storage, 'getItem'>): AgentPreferences {
	const target = storage ?? (typeof localStorage !== 'undefined' ? localStorage : undefined);
	if (!target) { return structuredClone(DEFAULT_AGENT_PREFERENCES); }
	try {
		const raw = target.getItem(STORAGE_KEY);
		if (!raw) { return structuredClone(DEFAULT_AGENT_PREFERENCES); }
		const parsed = JSON.parse(raw) as Partial<AgentPreferences>;
		return {
			api: sanitizeApiId(parsed.api),
			options: clampAgentOptions(parsed.options),
			attachSelection: parsed.attachSelection !== false,
			includeHistory: parsed.includeHistory !== false,
			allowDelete: parsed.allowDelete === true,
		};
	} catch {
		return structuredClone(DEFAULT_AGENT_PREFERENCES);
	}
}

export function saveAgentPreferences(
	prefs: AgentPreferences,
	storage?: Pick<Storage, 'setItem'>,
): AgentPreferences {
	const normalized: AgentPreferences = {
		api: sanitizeApiId(prefs.api),
		options: clampAgentOptions(prefs.options),
		attachSelection: prefs.attachSelection,
		includeHistory: prefs.includeHistory,
		allowDelete: prefs.allowDelete,
	};
	const target = storage ?? (typeof localStorage !== 'undefined' ? localStorage : undefined);
	try { target?.setItem(STORAGE_KEY, JSON.stringify(normalized)); } catch { /* ignore quota failures */ }
	return normalized;
}
