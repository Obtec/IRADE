/* SecretStorage embedder: survive navigation within this tab, never put tokens in URLs. */
globalThis.__iradeSecretStorage = {
	type: 'persisted',
	async get(key) { return sessionStorage.getItem(`irade.secret:${key}`) ?? undefined; },
	async set(key, value) { sessionStorage.setItem(`irade.secret:${key}`, value); },
	async delete(key) { sessionStorage.removeItem(`irade.secret:${key}`); },
	async keys() {
		return Object.keys(sessionStorage).filter(key => key.startsWith('irade.secret:')).map(key => key.slice('irade.secret:'.length));
	},
};
