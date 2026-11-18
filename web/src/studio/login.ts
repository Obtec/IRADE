import { initParticleField } from './particleField.ts';

interface Login { accessToken?: string; mfaRequired?: boolean; challengeToken?: string }
interface Profile { orcid: string; issuer: string }
initParticleField('#login-particles', { variant: 'login' });
const get = <T extends HTMLElement = HTMLElement>(selector: string): T => document.querySelector<T>(selector)!;
const origin = location.origin;
const params = new URL(location.href).searchParams;
const target = Number(params.get('workspace'));
const workspace = Number.isSafeInteger(target) && target > 0 ? target : undefined;
const button = get<HTMLButtonElement>('#orcid-login');
let challenge: string | undefined;
let linkToken: string | undefined;
let completing: Profile | undefined;
const start = new URL('/api/irade/auth/orcid/start', origin);
if (workspace) { start.searchParams.set('workspace', String(workspace)); }
const authEndpoint = (suffix: string): string => `${location.pathname.startsWith('/irade') ? '/irade/api/auth' : '/api/auth'}/${suffix}`;
function fail(error: unknown): void { const element = get('#login-error'); element.textContent = error instanceof Error ? error.message : String(error); element.hidden = false; }
async function request<T>(path: string, body?: object, token?: string): Promise<T> {
  const response = await fetch(path, { method: body ? 'POST' : 'GET', credentials: 'same-origin', headers: { ...(body ? { 'Content-Type':'application/json' } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(25000), redirect: 'error' });
  const result = await response.json();
  if (!response.ok) { throw new Error(result.message || '로그인을 완료하지 못했습니다. 다시 시도하세요.'); }
  return result as T;
}
function finish(result: Login): void {
  if (!result.accessToken) { throw new Error('로그인 토큰을 받지 못했습니다.'); }
  sessionStorage.setItem(`irade.orcid.token:${origin}`, result.accessToken);
  const url = new URL('/irade/studio', origin);
  if (workspace) { url.searchParams.set('workspace', String(workspace)); } else { url.searchParams.set('workspaces', '1'); }
  location.replace(String(url));
}
async function link(token: string): Promise<void> {
  const response = await request<{ authorizationUrl: string }>('/api/irade/auth/orcid/link', { workspace }, token);
  const url = new URL(response.authorizationUrl);
  if (!['https://orcid.org','https://sandbox.orcid.org'].includes(url.origin) || url.pathname !== '/oauth/authorize') { throw new Error('ORCID 인증 주소가 올바르지 않습니다.'); }
  location.assign(String(url));
}
function verify(result: Login): boolean {
  if (!result.mfaRequired || !result.challengeToken) { return false; }
  challenge = result.challengeToken; get('#mfa-form').hidden = false; get<HTMLDetailsElement>('#link-existing').open = false;
  get('#login-status').textContent = '계정의 2단계 인증을 완료하세요.'; get<HTMLInputElement>('#mfa-form input').focus(); return true;
}
get('#mfa-form').addEventListener('submit', event => {
  event.preventDefault(); const submit = get<HTMLButtonElement>('#mfa-form button'); submit.disabled = true;
  void (async () => { try {
    const data = new FormData(event.target as HTMLFormElement);
    const result = await request<Login>(authEndpoint('mfa/verify'), { challengeToken: challenge, code: String(data.get('code')).trim() });
    if (completing) { finish(result); } else if (result.accessToken) { await link(result.accessToken); } else { throw new Error('인증 응답을 확인할 수 없습니다.'); }
  } catch (error) { fail(error); } finally { submit.disabled = false; } })();
});
get('#link-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.target as HTMLFormElement; const submit = form.querySelector('button')!; submit.disabled = true; get('#login-error').hidden = true;
  void (async () => { try {
    const data = new FormData(form);
    const result = await request<Login>(authEndpoint('login'), { email: String(data.get('email')).trim(), password: data.get('password') });
    (form.elements.namedItem('password') as HTMLInputElement).value = '';
    completing = undefined;
    if (verify(result)) { return; }
    if (!result.accessToken) { throw new Error('계정을 확인할 수 없습니다.'); }
    linkToken = result.accessToken; await link(linkToken);
  } catch (error) { fail(error); } finally { submit.disabled = false; } })();
});
button.addEventListener('click', () => location.assign(String(start)));
void (async () => {
  try {
    const status = await request<{ available: boolean; issuer: string }>('/api/irade/auth/orcid/status');
    button.disabled = !status.available;
    get('#login-status').textContent = status.available ? `${status.issuer === 'https://sandbox.orcid.org' ? 'ORCID Sandbox 테스트 환경 · ' : ''}첫 로그인 시 인증된 ORCID로 연구자 계정이 생성됩니다.` : 'ORCID 로그인 연결이 준비 중입니다. 서버에 ORCID 클라이언트 설정이 필요합니다.';
    get<HTMLFormElement>('#link-form').querySelector<HTMLButtonElement>('button')!.disabled = !status.available;
    const outcome = params.get('orcid');
    if (outcome) {
      // Remove provider outcome markers from the address before completing the cookie-bound handoff.
      const clean = new URL(location.href); clean.searchParams.delete('orcid'); history.replaceState(null,'',clean);
      if (outcome === 'cancelled') { fail('ORCID 인증을 취소했습니다. 준비되면 다시 로그인하세요.'); }
      else if (outcome !== 'success') { fail('ORCID 인증 요청이 만료되었거나 로그인에 실패했습니다. 다시 시도하세요.'); }
      else {
        const response = await request<{ login: Login; orcid: string; issuer: string }>('/api/irade/auth/session', {});
        completing = response;
        if (!verify(response.login)) { finish(response.login); }
      }
    }
  } catch (error) { fail(error); }
})();
