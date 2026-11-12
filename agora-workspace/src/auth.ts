import * as vscode from 'vscode';

interface LoginResult { accessToken?: string; mfaRequired?: boolean; challengeToken?: string; message?: string }

export function defaultOrigin(context: vscode.ExtensionContext): string {
	const saved = context.globalState.get<string>('agora.lastOrigin');
	if (saved) { return saved; }
	return context.extensionUri.scheme === 'https' ? `https://${context.extensionUri.authority}` : 'https://localhost:8443';
}

async function loginRequest(origin: string, path: string, body: object): Promise<LoginResult> {
	const response = await fetch(origin + path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body), redirect: 'error', signal: AbortSignal.timeout(15000) });
	const result = await response.json() as LoginResult;
	if (!response.ok) { throw new Error(result.message || vscode.l10n.t('Agora sign-in failed ({0})', response.status)); }
	return result;
}

/** Normal account authentication; passwords are never stored in the editor. */
export async function signIn(context: vscode.ExtensionContext, origin: string): Promise<string | undefined> {
	const email = await vscode.window.showInputBox({ prompt: vscode.l10n.t('Sign in to {0}: Email', origin), ignoreFocusOut: true });
	if (!email) { return; }
	const password = await vscode.window.showInputBox({ prompt: vscode.l10n.t('Agora Password'), password: true, ignoreFocusOut: true });
	if (!password) { return; }
	let result = await loginRequest(origin, '/api/auth/login', { email: email.trim(), password });
	if (result.mfaRequired && result.challengeToken) {
		const code = await vscode.window.showInputBox({ prompt: vscode.l10n.t('Authenticator or Recovery Code'), password: true, ignoreFocusOut: true });
		if (!code) { return; }
		result = await loginRequest(origin, '/api/auth/mfa/verify', { challengeToken: result.challengeToken, code: code.trim() });
	}
	if (!result.accessToken) { throw new Error(vscode.l10n.t('Agora did not return an access token')); }
	await context.secrets.store(`agora.token:${origin}`, result.accessToken);
	await context.globalState.update('agora.lastOrigin', origin);
	return result.accessToken;
}

export async function accessToken(context: vscode.ExtensionContext, origin: string): Promise<string | undefined> {
	return await context.secrets.get(`agora.token:${origin}`) || await signIn(context, origin);
}
