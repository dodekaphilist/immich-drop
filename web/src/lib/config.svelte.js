import { u } from './paths.js';

// Public server config (/api/config), loaded once and shared by all pages.
export const config = $state({
  loaded: false,
  version: '',
  test_connection_enabled: false,
  social_media_uploads: true,
  chunked_uploads_enabled: false,
  chunk_size_mb: 95,
  default_album: '',
});

export async function loadConfig() {
  if (config.loaded) return;
  try {
    const r = await fetch(u('/api/config'));
    if (r.ok) {
      const j = await r.json();
      Object.assign(config, {
        version: j.version ?? '',
        test_connection_enabled: !!j.test_connection_enabled,
        social_media_uploads: j.social_media_uploads !== false,
        chunked_uploads_enabled: !!j.chunked_uploads_enabled,
        default_album: j.default_album || '',
      });
      const n = parseInt(j.chunk_size_mb, 10);
      if (!Number.isNaN(n) && n > 0) config.chunk_size_mb = n;
    }
  } catch {}
  config.loaded = true;
}

// Album list for the album picker, shared by all dialogs. `ok` is false while the server cannot list albums.
export const albumStore = $state({ loaded: false, ok: false, items: [] });

const ALBUM_MAX_AGE_MS = 60_000;
let albumsAt = 0;
let albumsInflight = null;

/**
 * Loads /api/albums into `albumStore`. Cheap to call often: a list younger than a minute is reused,
 * and the store is only touched when the list actually changed, so open dropdowns do not flicker.
 */
export function loadAlbums() {
  if (albumStore.loaded && Date.now() - albumsAt < ALBUM_MAX_AGE_MS) return Promise.resolve();
  albumsInflight ??= (async () => {
    try {
      const { status, body } = await fetchJson('/api/albums');
      if (status === 502) connection.status = 'down';
      if (Array.isArray(body)) {
        connection.status = 'ok';
        const items = body.map((a) => ({ id: a.id, name: a.albumName || a.title || a.id }));
        if (JSON.stringify(items) !== JSON.stringify(albumStore.items)) albumStore.items = items;
        albumStore.ok = true;
        albumsAt = Date.now(); // a failed load is not remembered, so the next call retries
      }
    } catch {}
    albumStore.loaded = true;
    albumsInflight = null;
  })();
  return albumsInflight;
}

// Reachability of the Immich server: 'unknown' until the first answer, then 'ok' or 'down'.
// Fed by the /api/ping poll and by the album list, whichever answers first.
export const connection = $state({ status: 'unknown', host: '' });

const CONNECTION_INTERVAL_MS = 15_000;

async function checkConnection() {
  try {
    const ping = await (await fetch(u('/api/ping'), { method: 'POST' })).json();
    connection.status = ping.ok ? 'ok' : 'down';
    connection.host = ping.base_url ? new URL(ping.base_url).host : '';
  } catch {
    connection.status = 'down';
  }
  if (connection.status === 'ok' && albumStore.loaded && !albumStore.ok) loadAlbums(); // the server is back: retry the albums
}

/** Checks the connection now and then every 15 s while the tab is visible; returns a function that stops it. */
export function watchConnection() {
  let timer;
  const tick = () => {
    if (!document.hidden) checkConnection();
  };
  loadConfig().then(() => {
    if (!config.test_connection_enabled) return;
    tick();
    timer = setInterval(tick, CONNECTION_INTERVAL_MS);
  });
  document.addEventListener('visibilitychange', tick);
  return () => {
    clearInterval(timer);
    document.removeEventListener('visibilitychange', tick);
  };
}

export async function fetchJson(url, options) {
  const r = await fetch(u(url), options);
  const body = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, body };
}

export function jsonRequest(url, payload, method = 'POST') {
  return fetchJson(url, {
    method,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function formatBytes(bytes) {
  const b = Number(bytes || 0);
  if (!b) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(b) / Math.log(1024));
  const value = b / Math.pow(1024, i);
  return `${value.toFixed(value >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}
