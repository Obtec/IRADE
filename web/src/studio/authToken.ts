// Stateless login session in an HttpOnly cookie. The server sets IRADE_SESSION (the signed 24 h JWT,
// unreadable by scripts) and IRADE_SESSION_HINT (non-secret "a session exists" flag). Same-origin
// requests carry the cookie automatically, so the page never stores the token itself.
export const SESSION = 'cookie-session';
const legacyKey = (): string => `irade.orcid.token:${location.origin}`;

export function hasSession(): boolean {
  return document.cookie.split(';').some(c => c.trim() === 'IRADE_SESSION_HINT=1');
}

/** Authorization header only for an explicit in-memory JWT; the cookie session needs none. */
export function bearer(token: string | undefined): Record<string, string> {
  return token && token !== SESSION ? { Authorization: `Bearer ${token}` } : {};
}

/** Exchange a freshly issued login JWT for the HttpOnly session cookie. */
export async function establishSession(accessToken: string): Promise<void> {
  const r = await fetch('/api/irade/auth/session-cookie', { method: 'POST', credentials: 'same-origin', headers: { Authorization: `Bearer ${accessToken}` } });
  if (!r.ok) { throw new Error('로그인 세션을 만들지 못했습니다. 다시 로그인하세요.'); }
}

export async function endSession(): Promise<void> {
  try { await fetch('/api/irade/auth/logout', { method: 'POST', credentials: 'same-origin' }); } catch { /* offline: cookie expires on its own */ }
  clearLegacy();
}

/** One-time migration of tokens that older builds kept in web storage. */
export async function migrateLegacyToken(): Promise<boolean> {
  let token: string | null = null;
  try { token = localStorage.getItem(legacyKey()) || sessionStorage.getItem(legacyKey()); } catch { return false; }
  if (!token) { return false; }
  clearLegacy();
  try { await establishSession(token); return true; } catch { return false; }
}

function clearLegacy(): void { try { localStorage.removeItem(legacyKey()); sessionStorage.removeItem(legacyKey()); } catch { /* ignore */ } }
