import { fetchJson } from './config.svelte.js';

/** State of the Immich API key in use: own (saved by the user), server (IMMICH_API_KEY) and status (ok, missing, invalid, unknown). */
export const apiKey = $state({ loaded: false, own: false, server: false, status: 'unknown', immichUrl: '' });

export async function refreshKey() {
  try {
    const { ok, body } = await fetchJson('/api/me/key');
    if (ok) {
      Object.assign(apiKey, {
        loaded: true,
        own: !!body.own,
        server: !!body.server,
        status: body.status || 'unknown',
        immichUrl: body.immichUrl || '',
      });
    }
  } catch {}
}
