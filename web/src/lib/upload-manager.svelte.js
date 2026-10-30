import { config } from './config.svelte.js';
import { errorText, t } from './i18n.svelte.js';
import { u } from './paths.js';

// Status precedence: never regress (e.g. uploading -> done must not go back to uploading).
// 'sending' is the browser->server transfer; 'checking'/'uploading' arrive via WebSocket.
const STATUS_ORDER = { queued: 0, sending: 1, checking: 2, uploading: 3, duplicate: 4, done: 4, cancelled: 5, error: 5 };
const FINAL_STATES = new Set(['done', 'duplicate', 'cancelled', 'error']);
const ACCEPT = /\.(jpe?g|png|heic|heif|webp|gif|tiff|bmp|mp4|mov|m4v|avi|mkv)$/i;

// One progress bar over all phases. Every phase reports 0-100 on its own, which made the bar jump back
// each time; here each phase gets its own slice of a single bar that only ever moves forward.
const SLICE = { sending: [0, 50], checking: [50, 55], uploading: [55, 100] };

function overall(status, pct) {
  const slice = SLICE[status];
  if (!slice) return 0;
  const p = Math.min(100, Math.max(0, pct || 0));
  return Math.round(slice[0] + ((slice[1] - slice[0]) * p) / 100);
}

function randomId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  const buf = new Uint8Array(16);
  crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('');
}

function getDeviceId() {
  try {
    let id = localStorage.getItem('immich_drop_device_id');
    if (!id) {
      id = randomId();
      localStorage.setItem('immich_drop_device_id', id);
    }
    return id;
  } catch {
    return 'anon';
  }
}

// Stable per-browser fingerprint: stored id + UA/screen/timezone
function computeFingerprint() {
  try {
    const id = getDeviceId();
    const raw = [
      id,
      navigator.userAgent || '',
      navigator.language || '',
      navigator.platform || '',
      Intl.DateTimeFormat().resolvedOptions().timeZone || '',
      screen ? `${screen.width}x${screen.height}x${screen.colorDepth}` : '',
    ].join('|');
    let h = 0;
    for (let i = 0; i < raw.length; i++) {
      h = (h << 5) - h + raw.charCodeAt(i);
      h |= 0;
    }
    return `${id}:${Math.abs(h)}`;
  } catch {
    return getDeviceId();
  }
}

// POST a FormData via XHR so the browser->server transfer reports progress (fetch can't).
// `onXhr` hands out the request so it can be aborted.
function postForm(url, form, onProgress, onXhr, headers = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    onXhr?.(xhr);
    xhr.open('POST', url);
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
    xhr.responseType = 'json';
    if (xhr.upload && onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded * 100) / e.total));
      };
    }
    xhr.onload = () => resolve({ ok: xhr.status >= 200 && xhr.status < 300, body: xhr.response || {} });
    xhr.onerror = () => reject(new Error(t('upload.network')));
    xhr.onabort = () => reject(new DOMException('aborted', 'AbortError'));
    xhr.send(form);
  });
}

export class UploadManager {
  items = $state([]);
  rejected = $state(0); // files of the last drop that were not photos or videos
  counts = $derived.by(() => {
    const c = { queued: 0, uploading: 0, done: 0, duplicate: 0, error: 0 };
    for (const it of this.items) {
      if (it.status === 'queued') c.queued++;
      else if (['sending', 'checking', 'uploading'].includes(it.status)) c.uploading++;
      else if (it.status in c) c[it.status]++;
    }
    return c;
  });

  #sessionId = randomId();
  #fingerprint = computeFingerprint();
  #socket = null;
  #closed = false;
  #inflight = 0; // uploads running right now, over all runs of the queue
  #running = new Map(); // item id -> { xhr, controller } of the request currently carrying the file

  /** @param {{ inviteToken?: string|null }} opts */
  constructor({ inviteToken = null } = {}) {
    this.inviteToken = inviteToken;
  }

  start() {
    this.#closed = false;
    this.#openSocket();
  }

  stop() {
    this.#closed = true;
    this.#socket?.close();
  }

  #openSocket() {
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss' : 'ws') + '://' + location.host + u('/ws'));
    this.#socket = ws;
    ws.onopen = () => ws.send(JSON.stringify({ session_id: this.#sessionId }));
    ws.onmessage = (evt) => {
      const { item_id, status, progress } = JSON.parse(evt.data);
      const it = this.items.find((x) => x.id === item_id);
      if (!it || FINAL_STATES.has(it.status)) return; // ignore late/regressive updates
      const cur = STATUS_ORDER[it.status] ?? 0;
      const inc = STATUS_ORDER[status] ?? 0;
      if (inc < cur) return;
      this.#setPhase(it, status, typeof progress === 'number' ? (inc === cur ? Math.max(it.pct, progress) : progress) : it.pct);
    };
    ws.onclose = () => {
      if (!this.#closed) setTimeout(() => this.#openSocket(), 2000);
    };
  }

  /** Move an item to a phase; the bar only moves forward. */
  #setPhase(item, status, pct = 0) {
    item.status = status;
    item.pct = pct;
    if (FINAL_STATES.has(status)) {
      if (status === 'done' || status === 'duplicate') item.progress = 100;
    } else {
      item.progress = Math.max(item.progress, overall(status, pct));
    }
  }

  addFiles(files) {
    const all = Array.from(files || []);
    const accepted = all.filter((f) => /^(image|video)\//.test(f.type) || ACCEPT.test(f.name));
    this.rejected = all.length - accepted.length;
    for (const file of accepted) {
      this.items.unshift({ id: randomId(), file, name: file.name, size: file.size, status: 'queued', pct: 0, progress: 0, message: '', cancelling: false });
    }
    this.#runQueue();
  }

  clearFinished() {
    this.items = this.items.filter((i) => !['done', 'duplicate', 'cancelled'].includes(i.status));
    this.#resetAlbumCache();
  }

  /** Take one entry off the list (a running upload is cancelled first). */
  remove(id) {
    this.cancel(id);
    this.items = this.items.filter((i) => i.id !== id);
  }

  clearAll() {
    for (const it of this.items) if (!FINAL_STATES.has(it.status)) this.cancel(it.id);
    this.items = [];
    this.#resetAlbumCache();
  }

  /** Refresh the server's album cache so a renamed album triggers a new one (only logged-in pages have one). */
  #resetAlbumCache() {
    if (!this.inviteToken) fetch(u('/api/album/reset'), { method: 'POST' }).catch(() => {});
  }

  /**
   * Cancel an upload. Queued items just never start. While the file is still travelling to this server the
   * request is aborted. Once the server forwards it to Immich, the server is asked to stop; the item then
   * ends as cancelled, or as done if the upload had already completed by then.
   */
  cancel(id) {
    const it = this.items.find((x) => x.id === id);
    if (!it || FINAL_STATES.has(it.status) || it.cancelling) return;
    if (it.status === 'queued') {
      it.status = 'cancelled';
      return;
    }
    it.cancelling = true;
    if (it.status === 'sending') {
      const r = this.#running.get(id);
      r?.xhr?.abort();
      r?.controller?.abort();
    }
    fetch(u('/api/upload/cancel'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...this.#authHeaders() },
      body: JSON.stringify({ session_id: this.#sessionId, item_id: id, invite_token: this.inviteToken || '' }),
    }).catch(() => {});
  }

  #runQueue() {
    // One counter for the whole manager: however many times files were added, at most 3 uploads run at once
    while (this.#inflight < 3) {
      const next = this.items.find((i) => i.status === 'queued');
      if (!next) return;
      this.#start(next);
    }
  }

  async #start(next) {
    this.#setPhase(next, 'sending', 0);
    this.#inflight++;
    try {
      if (config.chunked_uploads_enabled && next.file.size > config.chunk_size_mb * 1024 * 1024) {
        await this.#uploadChunked(next);
      } else {
        await this.#uploadWhole(next);
      }
    } catch (err) {
      if (next.cancelling || err?.name === 'AbortError') {
        next.status = 'cancelled';
      } else {
        next.status = 'error';
        next.message = errorText(String(err?.message || err));
      }
    } finally {
      this.#running.delete(next.id);
      this.#inflight--;
      setTimeout(() => this.#runQueue(), 50);
    }
  }

  /** The invite token as a header, so the server can check it before it reads the file. */
  #authHeaders() {
    return this.inviteToken ? { 'X-Invite-Token': this.inviteToken } : {};
  }

  #finish(item, ok, body) {
    if (body?.status === 'cancelled') {
      item.status = 'cancelled';
      return;
    }
    if (!ok) {
      if (item.status !== 'error' && item.status !== 'cancelled') {
        item.status = 'error';
        item.message = errorText(body.error) || t('upload.failed');
      }
      return;
    }
    const isDuplicate = /duplicate/i.test(body && body.status ? String(body.status) : '');
    this.#setPhase(item, isDuplicate ? 'duplicate' : 'done', 100);
  }

  async #uploadWhole(item) {
    const form = new FormData();
    form.append('file', item.file);
    form.append('item_id', item.id);
    form.append('session_id', this.#sessionId);
    form.append('last_modified', item.file.lastModified || '');
    if (this.inviteToken) form.append('invite_token', this.inviteToken);
    form.append('fingerprint', this.#fingerprint);
    const res = await postForm(
      u('/api/upload'),
      form,
      (pct) => {
        // only drive the bar while still sending; WS updates own the item afterwards
        if (item.status === 'sending') this.#setPhase(item, 'sending', pct);
      },
      (xhr) => this.#running.set(item.id, { xhr }),
      this.#authHeaders(),
    );
    this.#finish(item, res.ok, res.body || {});
  }

  /** Send one part; a network error or a 5xx answer is retried twice, a refusal (4xx) is final. */
  async #sendChunk(fd, item) {
    let lastError;
    for (let attempt = 0; attempt < 3; attempt++) {
      if (item.cancelling) throw new DOMException('cancelled', 'AbortError');
      const controller = new AbortController();
      this.#running.set(item.id, { controller });
      let r;
      try {
        r = await fetch(u('/api/upload/chunk'), { method: 'POST', body: fd, headers: this.#authHeaders(), signal: controller.signal });
      } catch (e) {
        if (e?.name === 'AbortError') throw e;
        lastError = e;
      }
      if (r) {
        if (r.ok) return;
        const j = await r.json().catch(() => ({}));
        const err = new Error(errorText(j.error, `Chunk failed (${r.status})`));
        if (r.status < 500) throw err;
        lastError = err;
      }
      await new Promise((res) => setTimeout(res, 1000 * (attempt + 1)));
    }
    throw lastError ?? new Error(t('upload.network'));
  }

  async #uploadChunked(item) {
    const chunkBytes = Math.max(1, config.chunk_size_mb | 0) * 1024 * 1024;
    const total = Math.ceil(item.file.size / chunkBytes) || 1;
    const headers = { 'Content-Type': 'application/json', Accept: 'application/json', ...this.#authHeaders() };
    const common = {
      item_id: item.id,
      session_id: this.#sessionId,
      last_modified: item.file.lastModified || '',
      invite_token: this.inviteToken || '',
      content_type: item.file.type || 'application/octet-stream',
      fingerprint: this.#fingerprint,
    };
    const init = await fetch(u('/api/upload/chunk/init'), {
      method: 'POST',
      headers,
      body: JSON.stringify({ ...common, name: item.file.name, size: item.file.size }),
    });
    if (!init.ok) {
      const j = await init.json().catch(() => ({}));
      throw new Error(errorText(j.error, t('upload.failed')));
    }
    for (let i = 0; i < total; i++) {
      if (item.cancelling) throw new DOMException('cancelled', 'AbortError');
      const start = i * chunkBytes;
      const blob = item.file.slice(start, Math.min(item.file.size, start + chunkBytes));
      const fd = new FormData();
      fd.append('item_id', item.id);
      fd.append('session_id', this.#sessionId);
      fd.append('chunk_index', String(i));
      fd.append('total_chunks', String(total));
      if (this.inviteToken) fd.append('invite_token', this.inviteToken);
      fd.append('fingerprint', this.#fingerprint);
      fd.append('chunk', blob, `${item.file.name}.part${i}`);
      await this.#sendChunk(fd, item);
      this.#setPhase(item, 'sending', Math.round(((i + 1) / total) * 100));
    }
    if (item.cancelling) throw new DOMException('cancelled', 'AbortError');
    // from here on the server forwards the file to Immich and reports progress over the WebSocket
    this.#running.delete(item.id);
    const rc = await fetch(u('/api/upload/chunk/complete'), {
      method: 'POST',
      headers,
      body: JSON.stringify({ ...common, name: item.file.name, total_chunks: total }),
    });
    const body = await rc.json().catch(() => ({}));
    this.#finish(item, rc.ok, body);
  }
}
