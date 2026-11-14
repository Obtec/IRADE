const STORAGE_KEY = 'irade.agent.personalKeys.v1';

function readMap(storage?: Pick<Storage, 'getItem'>): Record<string, string> {
	const target = storage ?? (typeof sessionStorage !== 'undefined' ? sessionStorage : undefined);
	if (!target) { return {}; }
	try {
		const raw = target.getItem(STORAGE_KEY);
		if (!raw) { return {}; }
		const parsed = JSON.parse(raw) as Record<string, unknown>;
		if (!parsed || typeof parsed !== 'object') { return {}; }
		const clean: Record<string, string> = {};
		for (const [api, key] of Object.entries(parsed)) {
			if (typeof key === 'string' && key.trim()) {
				clean[api] = key;
			}
		}
		return clean;
	} catch {
		return {};
	}
}

function writeMap(map: Record<string, string>, storage?: Pick<Storage, 'setItem' | 'removeItem'>): void {
	const target = storage ?? (typeof sessionStorage !== 'undefined' ? sessionStorage : undefined);
	if (!target) { return; }
	try {
		if (!Object.keys(map).length) {
			target.removeItem(STORAGE_KEY);
		} else {
			target.setItem(STORAGE_KEY, JSON.stringify(map));
		}
	} catch { /* ignore storage errors */ }
}

export function normalizeApiKey(raw: string): string {
	return raw.trim();
}

export function validPersonalApiKey(raw: string): boolean {
	const value = raw.trim();
	return Boolean(value) && value.length <= 512 && !/[\r\n\t\s]/.test(value);
}

export function getPersonalApiKey(apiId: string, storage?: Pick<Storage, 'getItem'>): string | undefined {
	const scope = apiId || '__default__';
	return readMap(storage)[scope];
}

export function setPersonalApiKey(
	apiId: string,
	rawKey: string,
	storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>,
): boolean {
	const trimmed = normalizeApiKey(rawKey);
	if (!validPersonalApiKey(trimmed)) { return false; }
	const scope = apiId || '__default__';
	const map = readMap(storage);
	map[scope] = trimmed;
	writeMap(map, storage);
	return true;
}

export function clearPersonalApiKey(
	apiId: string,
	storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>,
): void {
	const scope = apiId || '__default__';
	const map = readMap(storage);
	delete map[scope];
	writeMap(map, storage);
}

export function hasPersonalApiKey(apiId: string, storage?: Pick<Storage, 'getItem'>): boolean {
	return Boolean(getPersonalApiKey(apiId, storage));
}

export function configuredApiIds(storage?: Pick<Storage, 'getItem'>): string[] {
	return Object.keys(readMap(storage));
}
