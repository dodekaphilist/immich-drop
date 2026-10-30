"""
Immich Drop Uploader – Backend (FastAPI, simplified)
----------------------------------------------------
- Serves static frontend (no settings UI)
- Uploads to Immich using values from .env ONLY
- Duplicate checks (local SHA-1 DB + optional Immich bulk-check)
- WebSocket progress per session
- Ephemeral "Connected" banner via /api/ping
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import hashlib
import hmac
import os
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import logging
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState
from starlette.middleware.sessions import SessionMiddleware
from PIL import Image, ExifTags
try:
    import qrcode
except Exception:
    qrcode = None

from app.config import ConfigError, Settings, load_settings
from app import db, immich_client
from app.immich_client import to_immich_iso
from app.job_manager import cleanup_expired
from app.ratelimit import RateLimiter
from version import VERSION


def _utcnow() -> datetime:
    """Current UTC time as a naive datetime (the format stored in the database)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _z(timestamp: Optional[str]) -> Optional[str]:
    """Mark a stored naive-UTC timestamp as UTC for API clients, so browsers do not read it as local time."""
    return f"{timestamp}Z" if timestamp and not timestamp.endswith("Z") else timestamp


# ---- Lifespan for shared resources ----
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle - initialize and cleanup shared resources."""
    logger.info(f"Starting immich-drop v{VERSION}")
    # Startup: create shared httpx client for connection pooling
    app.state.httpx_client = httpx.AsyncClient(timeout=30.0)

    async def _job_cleanup_loop():
        while True:
            await asyncio.sleep(60)
            with suppress(Exception):
                cleanup_expired()
            with suppress(Exception):
                cleanup_stale_chunks()

    cleanup_task = asyncio.create_task(_job_cleanup_loop())
    yield
    # Shutdown: stop background work and close the shared client
    cleanup_task.cancel()
    with suppress(asyncio.CancelledError):
        await cleanup_task
    await app.state.httpx_client.aclose()


# ---- App & static ----
app = FastAPI(title="Immich Drop Uploader (Python)", lifespan=lifespan)
# Wildcard origins must not be combined with credentials; the frontend is
# same-origin and API clients (iOS Shortcuts) do not use cookies.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global settings (read-only at runtime)
try:
    SETTINGS: Settings = load_settings()
except ConfigError as exc:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("immich_drop").critical("Not starting: the configuration is incomplete.\n%s", exc)
    raise SystemExit(1)

# Basic logging setup using settings
logging.basicConfig(level=SETTINGS.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Third-party libraries log large volumes at DEBUG; keep them at WARNING
# regardless of the app LOG_LEVEL. httpx stays at INFO: its one-line-per-request
# entries are the only record of individual Immich API calls.
for _noisy in ("PIL", "python_multipart", "httpcore", "urllib3", "websockets", "charset_normalizer"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.INFO)
logger = logging.getLogger("immich_drop")
if SETTINGS.session_secret_generated:
    logger.warning("SESSION_SECRET is not set: a random one is used, so all logins end whenever the app restarts")
if not SETTINGS.immich_base_url.rstrip("/").endswith("/api"):
    logger.info("Using the Immich API at %s", SETTINGS.normalized_base_url)

# Cookie-based session for short-lived auth token storage (no persistence).
# Scoped to the subfolder when there is one, so it is not sent to other apps on the same host.
app.add_middleware(
    SessionMiddleware,
    secret_key=SETTINGS.session_secret,
    same_site="lax",
    path=SETTINGS.base_path or "/",
    https_only=SETTINGS.public_base_url.lower().startswith("https://"),
)


class BasePathMiddleware:
    """Serve the app under a subfolder such as /drop.

    A reverse proxy may forward the prefix (/drop/login) or strip it (/login). Either way the routes see the
    unprefixed path: the prefix is removed here when present and left alone when it is not.
    (Not done via root_path: Starlette's static mounts then fail to find files once the proxy has stripped it.)
    """

    def __init__(self, app, base_path: str) -> None:
        self.app = app
        self.base_path = base_path
        self._raw = base_path.encode()

    async def __call__(self, scope, receive, send):
        if self.base_path and scope["type"] in ("http", "websocket"):
            path = scope["path"]
            if path == self.base_path or path.startswith(self.base_path + "/"):
                scope = {**scope, "path": path[len(self.base_path):] or "/"}
                raw = scope.get("raw_path")
                if raw and (raw == self._raw or raw.startswith(self._raw + b"/") or raw.startswith(self._raw + b"?")):
                    scope["raw_path"] = raw[len(self._raw):] or b"/"
        await self.app(scope, receive, send)


# Added last so it wraps everything else
app.add_middleware(BasePathMiddleware, base_path=SETTINGS.base_path)


def _u(path: str) -> str:
    """Absolute path for a Location header or link, including the subfolder."""
    return f"{SETTINGS.base_path}{path}"

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
# Svelte app (built in the Docker image): hashed assets are referenced as /_app/*
_APP_ASSETS = os.path.join(FRONTEND_DIR, "app", "_app")
if os.path.isdir(_APP_ASSETS):
    app.mount("/_app", StaticFiles(directory=_APP_ASSETS), name="app_assets")

# Include URL/batch upload routes
from .api_routes import create_api_routes
api_router = create_api_routes(SETTINGS, lambda user_id: key_for_user(user_id))
app.include_router(api_router)

# Chunk upload storage
CHUNK_ROOT = os.path.join(SETTINGS.data_dir, "chunks")
try:
    os.makedirs(CHUNK_ROOT, exist_ok=True)
except Exception:
    pass
_CHUNK_ROOT_RESOLVED = Path(CHUNK_ROOT).resolve()
_CHUNK_ROOT_PREFIX = str(_CHUNK_ROOT_RESOLVED) + os.sep
# Age at which abandoned chunk directories are swept
CHUNK_TTL_SECONDS = 6 * 60 * 60

# Album cache
ALBUM_IDS: Dict[str, str] = {}  # default album id per Immich account ("" = server key)
# Lock to prevent concurrent album creation race conditions
_album_lock = asyncio.Lock()

def reset_album_cache() -> None:
    """Invalidate the cached Immich album id so next use re-resolves it."""
    ALBUM_IDS.clear()

# ---------- DB (local dedupe cache) ----------
# All tables (uploads, invites, platform_cookies, upload_events) are created
# once at startup in db.init_db().

with suppress(Exception):
    os.makedirs(SETTINGS.data_dir, exist_ok=True)
db.configure(SETTINGS.state_db)
db.init_db()

def db_lookup_checksum(checksum: str, owner: str = "") -> Optional[dict]:
    """Return a record for the given checksum if this Immich account uploaded it before (None if not)."""
    conn = db.connect()
    cur = conn.cursor()
    cur.execute("SELECT checksum, immich_asset_id FROM uploads WHERE owner_id = ? AND checksum = ?", (owner, checksum))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"checksum": row[0], "immich_asset_id": row[1]}
    return None

def db_forget_checksum(checksum: str, owner: str = "") -> None:
    """Drop a cached checksum that Immich no longer knows (the asset was deleted there)."""
    conn = db.connect()
    conn.execute("DELETE FROM uploads WHERE owner_id = ? AND checksum = ?", (owner, checksum))
    conn.commit()
    conn.close()

def db_insert_upload(checksum: str, filename: str, size: int, device_asset_id: str, immich_asset_id: Optional[str], created_at: str, owner: str = "") -> None:
    """Insert a newly-uploaded asset into the local cache (a known checksum gets its asset id refreshed)."""
    conn = db.connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO uploads (owner_id, checksum, filename, size, device_asset_id, immich_asset_id, created_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(owner_id, checksum) DO UPDATE SET immich_asset_id = excluded.immich_asset_id",
        (owner, checksum, filename, size, device_asset_id, immich_asset_id, created_at)
    )
    conn.commit()
    conn.close()

# ---------- WebSocket hub ----------

class SessionHub:
    """Holds WebSocket connections per session and broadcasts progress updates."""
    def __init__(self) -> None:
        self.sessions: Dict[str, List[WebSocket]] = {}

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        """Register a newly accepted WebSocket under the given session id."""
        self.sessions.setdefault(session_id, []).append(ws)

    def _cleanup_closed(self, session_id: str) -> None:
        """Drop closed sockets and cleanup empty session buckets."""
        if session_id not in self.sessions:
            return
        self.sessions[session_id] = [w for w in self.sessions[session_id] if w.client_state == WebSocketState.CONNECTED]
        if not self.sessions[session_id]:
            del self.sessions[session_id]

    async def send(self, session_id: str, payload: dict) -> None:
        """Broadcast a JSON payload to all sockets for one session."""
        conns = self.sessions.get(session_id, [])
        for ws in list(conns):
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                try:
                    await ws.close()
                except Exception:
                    pass
        self._cleanup_closed(session_id)

    async def disconnect(self, session_id: str, ws: WebSocket) -> None:
        """Remove a socket from the hub and close it (best-effort)."""
        if session_id in self.sessions and ws in self.sessions[session_id]:
            self.sessions[session_id].remove(ws)
            self._cleanup_closed(session_id)
        # Only try to close if the connection is still open
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.close()
            except Exception:
                pass

hub = SessionHub()

# ---------- Helpers ----------

_ID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F\-]{0,63}$")


def sanitize_filename(name: Optional[str]) -> str:
    """Return a minimally sanitized filename that preserves the original name.

    - Removes control characters (\x00-\x1F, \x7F)
    - Replaces path separators ('/' and '\\') with underscore
    - Falls back to 'file' if empty
    Other Unicode characters and spaces are preserved.
    """
    if not name:
        return "file"
    cleaned_chars = []
    for ch in str(name):
        o = ord(ch)
        if o < 32 or o == 127:
            continue
        if ch in ('/', '\\'):
            cleaned_chars.append('_')
        else:
            cleaned_chars.append(ch)
    cleaned = ''.join(cleaned_chars).strip()
    return cleaned or "file"

def read_exif_datetimes(file):
    """
    Extract EXIF DateTimeOriginal / ModifyDate values from a seekable binary file when possible.
    Returns (created, modified) as datetime or (None, None) on failure. The file is left at its start.
    """
    created = modified = None
    try:
        file.seek(0)
        with Image.open(file) as im:
            exif = getattr(im, "_getexif", lambda: None)() or {}
            if exif:
                tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
                dt_original = tags.get("DateTimeOriginal") or tags.get("CreateDate")
                dt_modified = tags.get("ModifyDate") or dt_original
                def parse_dt(s: str):
                    try:
                        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
                    except Exception:
                        return None
                if isinstance(dt_original, str):
                    created = parse_dt(dt_original)
                if isinstance(dt_modified, str):
                    modified = parse_dt(dt_modified)
    except Exception:
        pass
    finally:
        with suppress(Exception):
            file.seek(0)
    return created, modified

def immich_headers(request: Optional[Request] = None) -> dict:
    """Headers for Immich API calls using either session access token or API key."""
    headers = {"Accept": "application/json"}
    token = None
    try:
        if request is not None:
            token = request.session.get("accessToken")
    except Exception:
        token = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif SETTINGS.immich_api_key:
        headers["x-api-key"] = SETTINGS.immich_api_key
    return headers

def stored_api_key(user_id: str) -> Optional[str]:
    """The API key a user saved in the app (None if there is none)."""
    if not user_id:
        return None
    try:
        conn = db.connect()
        row = conn.execute("SELECT api_key FROM user_keys WHERE user_id = ?", (user_id,)).fetchone()
        conn.close()
    except Exception as e:
        logger.exception("API key lookup failed: %s", e)
        return None
    return row[0] if row else None

def key_headers(user_id: str) -> tuple[Optional[dict], str]:
    """Headers for acting as an Immich account without a login: IMMICH_API_KEY if set, else the user's own key.

    Returns (headers or None if no key exists, dedupe owner). The owner is the user id when the user's own key is
    used and "" for the server key, so the local duplicate cache never mixes libraries.
    """
    if SETTINGS.immich_api_key:
        return {"Accept": "application/json", "x-api-key": SETTINGS.immich_api_key}, ""
    own = stored_api_key(user_id)
    if own:
        return {"Accept": "application/json", "x-api-key": own}, user_id
    return None, ""

def key_for_user(user_id: str) -> Optional[str]:
    """API key for work done on behalf of a user (downloads, shortcut): IMMICH_API_KEY, else their own."""
    return SETTINGS.immich_api_key or stored_api_key(user_id) or None

def invite_owner_id(invite_token: str) -> Optional[str]:
    """Owner (Immich user id) of a link; None if the link does not exist."""
    try:
        conn = db.connect()
        row = conn.execute("SELECT COALESCE(owner_user_id,'') FROM invites WHERE token = ?", (invite_token,)).fetchone()
        conn.close()
    except Exception as e:
        logger.exception("Invite owner lookup failed: %s", e)
        return None
    return row[0] if row else None

async def get_or_create_album(request: Optional[Request] = None, album_name_override: Optional[str] = None, headers: Optional[dict] = None, owner: Optional[str] = None) -> Optional[str]:
    """Get existing album by name or create a new one. Returns album ID or None.

    Uses a lock to prevent race conditions when multiple concurrent uploads
    try to create the same album simultaneously. The default album is cached per Immich account.
    """
    album_name = album_name_override if album_name_override is not None else SETTINGS.album_name
    # Skip if no album name configured
    if not album_name:
        return None
    if owner is None:
        owner = str(request.session.get("userId") or "") if request is not None else ""
    use_cache = album_name_override is None
    # Return cached album ID if already fetched and using default settings name
    if use_cache and ALBUM_IDS.get(owner):
        return ALBUM_IDS[owner]

    # Use lock to prevent concurrent album creation race conditions
    async with _album_lock:
        # Double-check cache after acquiring lock (another request may have set it)
        if use_cache and ALBUM_IDS.get(owner):
            return ALBUM_IDS[owner]

        found_id = await immich_client.find_or_create_album(
            app.state.httpx_client,
            SETTINGS.normalized_base_url,
            headers or immich_headers(request),
            album_name,
            description="Auto-created album for Immich Drop uploads",
            owner_id=owner,
        )
        if found_id and use_cache:
            ALBUM_IDS[owner] = found_id
            logger.info("Resolved album '%s' to ID: %s", album_name, found_id)
        return found_id

async def add_asset_to_album(asset_id: str, request: Optional[Request] = None, album_id_override: Optional[str] = None, album_name_override: Optional[str] = None, headers_override: Optional[dict] = None, owner: Optional[str] = None) -> bool:
    """Add an asset to the configured album. Returns True on success."""
    album_id = album_id_override
    if not album_id:
        album_id = await get_or_create_album(request=request, album_name_override=album_name_override, headers=headers_override, owner=owner)
    if not album_id or not asset_id:
        return False
    return await immich_client.add_to_album(
        app.state.httpx_client, SETTINGS.normalized_base_url, headers_override or immich_headers(request), album_id, asset_id
    )

async def immich_ping() -> bool:
    """Best-effort reachability check against a few Immich endpoints."""
    return await immich_client.ping(app.state.httpx_client, SETTINGS.normalized_base_url, immich_headers())

async def immich_bulk_check(checks: List[dict], headers: dict) -> Dict[str, dict]:
    """Try Immich bulk upload check; return map id->result (or empty on failure)."""
    return await immich_client.bulk_upload_check(
        app.state.httpx_client, SETTINGS.normalized_base_url, headers, checks
    )

async def send_progress(session_id: str, item_id: str, status: str, progress: int = 0, message: str = "", response_id: Optional[str] = None) -> None:
    """Push a progress update over WebSocket for one queue item."""
    await hub.send(session_id, {
        "item_id": item_id,
        "status": status,
        "progress": progress,
        "message": message,
        "responseId": response_id,
    })

# ---------- Routes ----------

_SPA_HTML: Optional[str] = None

def _spa() -> HTMLResponse:
    """Serve the Svelte app shell; the client router renders the actual page.

    The UI is built once with root-relative URLs. Under a subfolder they are rewritten here, and the router's
    `base` is set, so the same build works at the root and at e.g. /drop.
    """
    global _SPA_HTML
    if _SPA_HTML is None:
        with open(os.path.join(FRONTEND_DIR, "app", "index.html"), encoding="utf-8") as f:
            html = f.read()
        base = SETTINGS.base_path
        if base:
            html = (
                html.replace('"/_app/', f'"{base}/_app/')
                .replace('"/static/', f'"{base}/static/')
                .replace('base: ""', f'base: "{base}"')
            )
        _SPA_HTML = html
    return HTMLResponse(_SPA_HTML, headers={"Cache-Control": "no-store"})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Owner upload page. Requires login."""
    if not request.session.get("accessToken"):
        return RedirectResponse(url=_u("/login"))
    return _spa()

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Login page; already logged-in users go straight to the uploader."""
    if request.session.get("accessToken"):
        return RedirectResponse(url=_u("/"))
    return _spa()


try:
    with open(os.path.join(FRONTEND_DIR, "favicon.png"), "rb") as _f:
        _FAVICON_BYTES: Optional[bytes] = _f.read()
except Exception:
    _FAVICON_BYTES = None

@app.get("/favicon.ico")
async def favicon() -> Response:
    """Serve favicon from /static/favicon.png if present (avoids 404 noise)."""
    if _FAVICON_BYTES:
        return Response(content=_FAVICON_BYTES, media_type="image/png")
    return Response(status_code=204)

_PING_TTL = 5.0
_ping_cache: dict = {"at": 0.0, "ok": False}

@app.post("/api/ping")
async def api_ping(request: Request) -> dict:
    """Connectivity test endpoint used by the UI to display a temporary banner."""
    if not SETTINGS.test_connection_enabled:
        return {"ok": False, "disabled": True}
    # Anonymous callers share one short-lived answer, so this can not be used to hammer Immich
    now = time.monotonic()
    if now - _ping_cache["at"] >= _PING_TTL:
        _ping_cache.update({"at": now, "ok": await immich_ping()})
    return {
        "ok": _ping_cache["ok"],
        "base_url": SETTINGS.normalized_base_url if SETTINGS.test_connection_show_hostname else None,
        "album_name": (SETTINGS.album_name or None) if request.session.get("accessToken") else None,
    }

@app.get("/api/config")
async def api_config() -> dict:
    """Expose minimal public configuration flags for the frontend."""
    return {
        "chunked_uploads_enabled": SETTINGS.chunked_uploads_enabled,
        "chunk_size_mb": SETTINGS.chunk_size_mb,
        "version": VERSION,
        "social_media_uploads": SETTINGS.social_media_uploads,
        "test_connection_enabled": SETTINGS.test_connection_enabled,
        "default_album": SETTINGS.album_name,
    }

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """WebSocket endpoint for pushing per-item upload progress."""
    await ws.accept()
    try:
        init = await ws.receive_text()
        data = json.loads(init)
        session_id = data.get("session_id") or "default"
    except Exception:
        session_id = "default"
    # If this is the first socket for a (possibly new) session, reset album cache
    # so a freshly opened page can rotate the drop album by renaming the old one.
    if session_id not in hub.sessions and ws.session.get("accessToken"):
        reset_album_cache()
    await hub.connect(session_id, ws)

    # keepalive to avoid proxy idle timeouts
    try:
        while True:
            msg_task = asyncio.create_task(ws.receive_text())
            keep_task = asyncio.create_task(asyncio.sleep(30))
            done, pending = await asyncio.wait({msg_task, keep_task}, return_when=asyncio.FIRST_COMPLETED)
            if msg_task in done:
                try:
                    _ = msg_task.result()
                except RuntimeError:
                    # Client disconnected during receive
                    break
            else:
                await ws.send_text('{"type":"ping"}')
            for t in pending:
                t.cancel()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await hub.disconnect(session_id, ws)

# ---------- Upload core (shared by whole-file and chunked paths) ----------

def check_invite_for_upload(request: Request, invite_token: str, session_id: str, claim: bool = True):
    """Validate a locally-created invite token for an upload attempt.

    Returns (error, album_id, album_name, one_time) where error is
    (ws_message, error_key, http_status) or None when the invite is usable.
    With claim=False nothing is changed (a one-time link is not claimed yet); the upload path validates first,
    then claims once the file is known to be new.
    """
    def fail(msg: str, key: str, status: int = 403):
        return (msg, key, status), None, None, False

    try:
        conn = db.connect()
        cur = conn.cursor()
        cur.execute("SELECT token, album_id, album_name, max_uses, used_count, expires_at, COALESCE(claimed,0), claimed_by_session, password_hash, COALESCE(disabled,0) FROM invites WHERE token = ?", (invite_token,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        logger.exception("Invite lookup error: %s", e)
        row = None
    if not row:
        return fail("Invalid invite token", "invalid_invite")
    _, album_id, album_name, max_uses, used_count, expires_at, claimed, claimed_by_session, password_hash, disabled = row
    if int(disabled or 0) == 1:
        return fail("Invite disabled", "invite_disabled")
    # If invite requires password, ensure this session is authorized
    if password_hash:
        ia = request.session.get("inviteAuth") or {}
        if not ia.get(invite_token):
            return fail("Password required", "invite_password_required")
    # Expiry check
    if expires_at:
        try:
            if _utcnow() > datetime.fromisoformat(expires_at):
                return fail("Invite expired", "invite_expired")
        except ValueError:
            pass
    try:
        max_uses_int = int(max_uses) if max_uses is not None else -1
    except (TypeError, ValueError):
        max_uses_int = -1
    one_time = max_uses_int == 1
    if one_time:
        if claimed:
            # Allow same session to continue; block different sessions
            if claimed_by_session and claimed_by_session != session_id:
                return fail("Invite already used", "invite_claimed")
        elif claim:
            # Atomically claim the one-time invite to prevent concurrent use
            try:
                connc = db.connect()
                cur_c = connc.execute(
                    "UPDATE invites SET claimed = 1, claimed_at = CURRENT_TIMESTAMP, claimed_by_session = ? WHERE token = ? AND (claimed IS NULL OR claimed = 0)",
                    (session_id, invite_token),
                )
                connc.commit()
                changed = cur_c.rowcount
                connc.close()
            except Exception as e:
                logger.exception("Invite claim failed: %s", e)
                return fail("Invite claim failed", "invite_claim_failed", 500)
            if changed == 0:
                # Someone else just claimed; re-check owner
                try:
                    conn2 = db.connect()
                    owner_row = conn2.execute("SELECT claimed_by_session FROM invites WHERE token = ?", (invite_token,)).fetchone()
                    conn2.close()
                    owner = owner_row[0] if owner_row else None
                except Exception:
                    owner = None
                if not owner or owner != session_id:
                    return fail("Invite already used", "invite_claimed")
    elif max_uses_int >= 0 and (used_count or 0) >= max_uses_int:
        return fail("Invite already used up", "invite_exhausted")
    return None, album_id, album_name, one_time


def reserve_invite_use(invite_token: str) -> bool:
    """Atomically take one use of a multi-use link; False when none is left (no over-use under concurrency)."""
    conn = db.connect()
    cur = conn.execute(
        "UPDATE invites SET used_count = COALESCE(used_count, 0) + 1 WHERE token = ? AND (max_uses IS NULL OR max_uses < 0 OR COALESCE(used_count, 0) < max_uses)",
        (invite_token,),
    )
    conn.commit()
    ok = cur.rowcount == 1
    conn.close()
    return ok


def release_invite_use(invite_token: str, one_time: bool) -> None:
    """Undo the claim/reservation when the upload did not happen, so a failed attempt does not burn the link."""
    try:
        conn = db.connect()
        if one_time:
            conn.execute(
                "UPDATE invites SET claimed = 0, claimed_at = NULL, claimed_by_session = NULL WHERE token = ? AND COALESCE(used_count, 0) = 0",
                (invite_token,),
            )
        else:
            conn.execute("UPDATE invites SET used_count = MAX(COALESCE(used_count, 0) - 1, 0) WHERE token = ?", (invite_token,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("Failed to release invite use: %s", e)


def finish_invite_use(invite_token: str, one_time: bool) -> None:
    """A one-time link counts as used once its first upload succeeded (multi-use links were counted on reserve)."""
    if not one_time:
        return
    try:
        conn = db.connect()
        conn.execute("UPDATE invites SET used_count = 1 WHERE token = ?", (invite_token,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("Failed to record invite usage: %s", e)

def log_upload_event(request: Request, invite_token: Optional[str], fingerprint: Optional[str], filename: str, size: int, checksum: str, asset_id: Optional[str]) -> None:
    """Record uploader identity and file metadata (best-effort)."""
    try:
        ip = None
        try:
            ip = (request.client.host if request and request.client else None) or request.headers.get('x-forwarded-for')
        except Exception:
            ip = None
        ua = request.headers.get('user-agent', '') if request else ''
        conn = db.connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO upload_events (token, ip, user_agent, fingerprint, filename, size, checksum, immich_asset_id) VALUES (?,?,?,?,?,?,?,?)",
            (invite_token or '', ip, ua, fingerprint or '', filename, size, checksum, asset_id or None)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

# Items the client asked to cancel: (session_id, item_id) -> time. Cancelling only needs those two client-made
# random ids, the same capability the WebSocket progress uses.
_CANCELLED: Dict[tuple, float] = {}
_CANCELLED_MAX = 10_000

def _upload_cancelled(session_id: str, item_id: str) -> bool:
    return (session_id, item_id) in _CANCELLED

def _duplicate_response(invite_token: Optional[str], asset_id: Optional[str]) -> Optional[str]:
    """Guests learn that a file is a duplicate, not which asset it is."""
    return None if invite_token else asset_id

async def process_upload(
    request: Request,
    *,
    file,
    size: int,
    orig_name: str,
    content_type: Optional[str],
    item_id: str,
    session_id: str,
    last_modified: Optional[int],
    invite_token: Optional[str],
    fingerprint: Optional[str],
    album_name: Optional[str] = None,
) -> JSONResponse:
    """Dedupe-check and forward one file (a seekable binary file, never held in memory) to Immich, with progress via WS."""
    if denied := _upload_not_allowed(request, invite_token):
        return denied
    if _upload_cancelled(session_id, item_id):
        _CANCELLED.pop((session_id, item_id), None)
        await send_progress(session_id, item_id, "cancelled", 100)
        return JSONResponse({"status": "cancelled"}, status_code=200)

    # The link comes first: nothing (not even a duplicate answer) is given out for a link that is not usable
    target_album_id: Optional[str] = None
    target_album_name: Optional[str] = None
    one_time = False
    if invite_token:
        error, target_album_id, target_album_name, one_time = check_invite_for_upload(request, invite_token, session_id, claim=False)
        if error:
            msg, key, http_status = error
            await send_progress(session_id, item_id, "error", 100, msg)
            return JSONResponse({"error": key}, status_code=http_status)

    # Whose Immich account receives the file: a link's owner (via their own or the server key), or the logged-in user
    if invite_token:
        link_owner = invite_owner_id(invite_token)
        if link_owner is None:
            await send_progress(session_id, item_id, "error", 100, "Invalid invite token")
            return JSONResponse({"error": "invalid_invite"}, status_code=403)
        upload_headers, owner = key_headers(link_owner)
        if upload_headers is None:
            logger.warning("Upload via link %s...: its owner has no API key and IMMICH_API_KEY is not set", invite_token[:6])
            await send_progress(session_id, item_id, "error", 100, "Uploads are not available for this link")
            return JSONResponse({"error": "no_api_key"}, status_code=503)
    else:
        upload_headers, owner = immich_headers(request), str(request.session.get("userId") or "")

    checksum = await asyncio.to_thread(immich_client.sha1_file, file)
    exif_created, exif_modified = await asyncio.to_thread(read_exif_datetimes, file)
    created_at = exif_created or (datetime.fromtimestamp(last_modified / 1000, tz=timezone.utc) if last_modified else datetime.now(timezone.utc))
    modified_at = exif_modified or created_at
    created_iso = to_immich_iso(created_at)

    # Local dedupe key only; Immich v3 no longer accepts deviceAssetId/deviceId
    device_asset_id = f"{orig_name}-{last_modified or 0}-{size}"

    # Immich decides. The local cache is only a fallback for when Immich does not answer: a file deleted in Immich
    # since must be uploadable again.
    cached = db_lookup_checksum(checksum, owner)
    await send_progress(session_id, item_id, "checking", 2, "Checking duplicates…")
    bulk = await immich_bulk_check([{"id": item_id, "checksum": checksum}], upload_headers)
    verdict = bulk.get(item_id)
    if verdict is not None and verdict.get("action") == "reject" and verdict.get("reason") == "duplicate":
        asset_id = verdict.get("assetId")
        db_insert_upload(checksum, orig_name, size, device_asset_id, asset_id, created_iso, owner)
        await send_progress(session_id, item_id, "duplicate", 100, "Duplicate (server)", _duplicate_response(invite_token, asset_id))
        return JSONResponse({"status": "duplicate", "id": _duplicate_response(invite_token, asset_id)}, status_code=200)
    if verdict is None and cached:
        await send_progress(session_id, item_id, "duplicate", 100, "Duplicate (by checksum - local cache)")
        return JSONResponse({"status": "duplicate", "id": None}, status_code=200)
    if verdict is not None and cached:
        db_forget_checksum(checksum, owner)  # Immich no longer has it

    # Now that the file is new: claim a one-time link / take one use of a multi-use link
    if invite_token:
        error, target_album_id, target_album_name, one_time = check_invite_for_upload(request, invite_token, session_id, claim=True)
        if not error and not one_time and not reserve_invite_use(invite_token):
            error = ("Invite already used up", "invite_exhausted", 403)
        if error:
            msg, key, http_status = error
            await send_progress(session_id, item_id, "error", 100, msg)
            return JSONResponse({"error": key}, status_code=http_status)

    safe_name = sanitize_filename(orig_name)
    await send_progress(session_id, item_id, "uploading", 0, "Uploading…")

    def on_progress(pct: int) -> None:
        asyncio.create_task(send_progress(session_id, item_id, "uploading", pct))

    outcome = await immich_client.upload_asset(
        app.state.httpx_client,
        SETTINGS.normalized_base_url,
        upload_headers,
        file=file,
        size=size,
        filename=safe_name,
        content_type=content_type or "application/octet-stream",
        checksum=checksum,
        created_at=created_at,
        modified_at=modified_at,
        progress=on_progress,
        should_cancel=lambda: _upload_cancelled(session_id, item_id),
        timeout=300.0,
    )
    if outcome.cancelled:
        if invite_token:
            release_invite_use(invite_token, one_time)
        _CANCELLED.pop((session_id, item_id), None)
        await send_progress(session_id, item_id, "cancelled", 100)
        return JSONResponse({"status": "cancelled"}, status_code=200)
    if not outcome.ok:
        if invite_token:
            release_invite_use(invite_token, one_time)
        if outcome.status_code == 0:
            logger.error("upload failed (session=%s item=%s): %s", session_id, item_id, outcome.error)
            await send_progress(session_id, item_id, "error", 100, "upload failed")
            return JSONResponse({"error": "upload failed"}, status_code=500)
        msg = outcome.error or "upload failed"
        await send_progress(session_id, item_id, "error", 100, msg)
        return JSONResponse({"error": msg}, status_code=400)

    asset_id = outcome.asset_id
    status = outcome.status or "created"
    db_insert_upload(checksum, orig_name, size, device_asset_id, asset_id, created_iso, owner)

    # Add to album if configured (invite overrides .env)
    if asset_id:
        if invite_token:
            # Only add if invite specified an album; do not fallback to env default
            if target_album_id or target_album_name:
                if await add_asset_to_album(asset_id, request=request, album_id_override=target_album_id, album_name_override=target_album_name, headers_override=upload_headers, owner=owner):
                    status += f" (added to album '{target_album_name or target_album_id}')"
        elif album_name or SETTINGS.album_name:
            # Logged-in uploads: album chosen on the upload page, else the .env default
            target = (album_name or "").strip() or SETTINGS.album_name
            if await add_asset_to_album(asset_id, request=request, album_name_override=target, headers_override=upload_headers, owner=owner):
                status += f" (added to album '{target}')"

    await send_progress(session_id, item_id, "duplicate" if outcome.status == "duplicate" else "done", 100, status, asset_id if not invite_token else None)

    if invite_token:
        finish_invite_use(invite_token, one_time)
    log_upload_event(request, invite_token, fingerprint, orig_name, size, checksum, asset_id)
    return JSONResponse({"id": asset_id if not invite_token else None, "status": status}, status_code=200)

@app.post("/api/upload/cancel")
async def api_upload_cancel(request: Request) -> JSONResponse:
    """Abort an upload that is still queued, being received, or being forwarded to Immich."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    session_id = str((data or {}).get("session_id") or "")
    item_id = str((data or {}).get("item_id") or "")
    if not session_id or not item_id:
        return JSONResponse({"error": "missing_ids"}, status_code=400)
    if denied := _upload_not_allowed(request, (data or {}).get("invite_token")):
        return denied
    now = time.time()
    for key in [k for k, t in _CANCELLED.items() if now - t > 3600]:
        _CANCELLED.pop(key, None)  # flags of items that finished long ago
    if len(_CANCELLED) < _CANCELLED_MAX:
        _CANCELLED[(session_id, item_id)] = now
    # partly received chunks are of no use any more
    with suppress(Exception):
        shutil.rmtree(_chunk_dir(session_id, item_id), ignore_errors=True)
    return JSONResponse({"ok": True})

def _to_int(value) -> Optional[int]:
    """Int from a form value; None for empty or non-numeric text."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None

def _body_too_large(request: Request, limit: int) -> Optional[JSONResponse]:
    """413 when the announced body is larger than limit bytes (0 = no limit). Checked before the body is read."""
    if limit <= 0:
        return None
    declared = _to_int(request.headers.get("content-length"))
    if declared is not None and declared > limit:
        return JSONResponse({"error": "file_too_large"}, status_code=413)
    return None

_MULTIPART_OVERHEAD = 1024 * 1024  # form fields and boundaries around the file

async def _read_form(request: Request):
    """Parse the multipart body; None if it is malformed."""
    try:
        return await request.form()
    except Exception:
        return None

def _header_token(request: Request) -> Optional[str]:
    """The invite token as the browser sends it in a header, so it can be checked before the body is read."""
    return (request.headers.get("x-invite-token") or "").strip() or None

@app.post("/api/upload")
async def api_upload(request: Request):
    """Receive a file, check duplicates, forward to Immich; stream progress via WS."""
    header_token = _header_token(request)
    # Login or a known invite token first: an anonymous caller's body is never read
    if denied := _upload_not_allowed(request, header_token):
        return denied
    limit = SETTINGS.max_upload_size_mb * 1024 * 1024
    if too_big := _body_too_large(request, limit + _MULTIPART_OVERHEAD if limit else 0):
        return too_big
    form = await _read_form(request)
    if form is None:
        return JSONResponse({"error": "invalid_form"}, status_code=400)
    try:
        upload = form.get("file")
        item_id = str(form.get("item_id") or "")
        session_id = str(form.get("session_id") or "")
        if upload is None or isinstance(upload, str) or not item_id or not session_id:
            return JSONResponse({"error": "invalid_form"}, status_code=400)
        invite_token = header_token or (str(form.get("invite_token") or "").strip() or None)
        file_size = upload.size if upload.size is not None else 0
        return await process_upload(
            request,
            file=upload.file,
            size=file_size,
            orig_name=upload.filename or "file",
            content_type=upload.content_type,
            item_id=item_id,
            session_id=session_id,
            last_modified=_to_int(form.get("last_modified")),
            invite_token=invite_token,
            fingerprint=str(form.get("fingerprint") or "") or None,
            album_name=str(form.get("album_name") or "") or None,
        )
    finally:
        await form.close()

# --------- Chunked upload endpoints ---------

def _chunk_dir(session_id: str, item_id: str) -> str:
    """Build a chunk-storage path that is provably inside CHUNK_ROOT.

    Each id is validated against `_ID_RE` directly at the call site (rather
    than only via `_validate_id`) so static analyzers see the regex match as
    a barrier guard before the path join.
    """
    sid = (session_id or "").strip()
    iid = (item_id or "").strip()
    if not _ID_RE.match(sid) or not _ID_RE.match(iid):
        raise HTTPException(status_code=400, detail="invalid id")
    path = os.path.normpath(os.path.join(_CHUNK_ROOT_PREFIX, sid, iid))
    if not path.startswith(_CHUNK_ROOT_PREFIX):
        raise HTTPException(status_code=400, detail="invalid id")
    return path


def _upload_not_allowed(request: Request, invite_token: Optional[str]) -> Optional[JSONResponse]:
    """Uploads need a known invite token or a logged-in session; never the bare server API key."""
    if request.session.get("accessToken"):
        return None
    if not invite_token:
        return JSONResponse({"error": "login_required"}, status_code=401)
    if invite_owner_id(str(invite_token)) is None:
        return JSONResponse({"error": "invalid_invite"}, status_code=403)
    return None


async def _guard_chunked_upload(request: Request, invite_token: Optional[str], session_id: str) -> Optional[JSONResponse]:
    """Reject chunk writes before any bytes hit disk.

    Same rules as a finished upload (password, expiry, usage, one-time ownership), but read-only: one-time
    invites are still claimed at completion.
    """
    if not SETTINGS.chunked_uploads_enabled:
        return JSONResponse({"error": "chunked_uploads_disabled"}, status_code=403)
    if denied := _upload_not_allowed(request, invite_token):
        return denied
    if invite_token and not request.session.get("accessToken"):
        error, _, _, _ = check_invite_for_upload(request, invite_token, session_id, claim=False)
        if error:
            _, key, http_status = error
            return JSONResponse({"error": key}, status_code=http_status)
    return None


def cleanup_stale_chunks() -> int:
    """Delete chunk directories older than CHUNK_TTL_SECONDS."""
    removed = 0
    cutoff = time.time() - CHUNK_TTL_SECONDS
    for session_dir in Path(CHUNK_ROOT).glob("*"):
        if not session_dir.is_dir():
            continue
        for item_dir in session_dir.glob("*"):
            if not item_dir.is_dir():
                continue
            with suppress(Exception):
                if item_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(item_dir)
                    removed += 1
        with suppress(Exception):
            session_dir.rmdir()  # only succeeds when empty
    if removed:
        logger.info("Removed %d stale chunk director%s", removed, "y" if removed == 1 else "ies")
    return removed

MAX_CHUNKS = 100_000

def _max_chunk_bytes() -> int:
    return (SETTINGS.chunk_size_mb + 1) * 1024 * 1024

@app.post("/api/upload/chunk/init")
async def api_upload_chunk_init(request: Request) -> JSONResponse:
    """Initialize a chunked upload; creates a temp directory for incoming parts."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    data = data if isinstance(data, dict) else {}
    item_id = data.get("item_id")
    session_id = data.get("session_id")
    if not item_id or not session_id:
        return JSONResponse({"error": "missing_ids"}, status_code=400)
    denied = await _guard_chunked_upload(request, data.get("invite_token") or _header_token(request), str(session_id))
    if denied:
        return denied
    limit = SETTINGS.max_upload_size_mb * 1024 * 1024
    size = _to_int(data.get("size"))
    if limit and size is not None and size > limit:
        return JSONResponse({"error": "file_too_large"}, status_code=413)
    d = _chunk_dir(session_id, item_id)
    try:
        os.makedirs(d, exist_ok=True)
        # Write manifest for later use
        meta = {
            "name": data.get("name"),
            "size": size,
            "last_modified": data.get("last_modified"),
            "content_type": data.get("content_type") or "application/octet-stream",
            "created_at": _utcnow().isoformat(),
        }
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
    except Exception as e:
        logger.exception("Chunk init failed: %s", e)
        return JSONResponse({"error": "init_failed"}, status_code=500)
    return JSONResponse({"ok": True})

@app.post("/api/upload/chunk")
async def api_upload_chunk(request: Request) -> JSONResponse:
    """Receive a single chunk; write to disk under chunk directory."""
    header_token = _header_token(request)
    # Login or a known invite token first: an anonymous caller's body is never read
    if denied := _upload_not_allowed(request, header_token):
        return denied
    if too_big := _body_too_large(request, _max_chunk_bytes() + _MULTIPART_OVERHEAD):
        return too_big
    form = await _read_form(request)
    if form is None:
        return JSONResponse({"error": "invalid_form"}, status_code=400)
    try:
        chunk = form.get("chunk")
        item_id = str(form.get("item_id") or "")
        session_id = str(form.get("session_id") or "")
        chunk_index = _to_int(form.get("chunk_index"))
        total_chunks = _to_int(form.get("total_chunks"))
        invite_token = header_token or (str(form.get("invite_token") or "").strip() or None)
        if chunk is None or isinstance(chunk, str) or not item_id or not session_id:
            return JSONResponse({"error": "invalid_form"}, status_code=400)
        if chunk_index is None or total_chunks is None or not (0 < total_chunks <= MAX_CHUNKS) or not (0 <= chunk_index < total_chunks):
            return JSONResponse({"error": "invalid_chunk"}, status_code=400)
        denied = await _guard_chunked_upload(request, invite_token, session_id)
        if denied:
            return denied
        limit = SETTINGS.max_upload_size_mb * 1024 * 1024
        if limit and (total_chunks - 1) * SETTINGS.chunk_size_mb * 1024 * 1024 > limit:
            return JSONResponse({"error": "file_too_large"}, status_code=413)
        d = _chunk_dir(session_id, item_id)
        part_path = os.path.join(d, f"part_{chunk_index:06d}")
        try:
            os.makedirs(d, exist_ok=True)
            meta_path = os.path.join(d, "meta.json")
            meta = {}
            if os.path.exists(meta_path):
                with suppress(Exception):
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
            meta["total_chunks"] = total_chunks
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f)
            written = 0
            with open(part_path, "wb") as out:
                while True:
                    block = await chunk.read(1024 * 1024)
                    if not block:
                        break
                    written += len(block)
                    if written > _max_chunk_bytes():
                        break
                    out.write(block)
            if written > _max_chunk_bytes():
                os.remove(part_path)
                return JSONResponse({"error": "file_too_large"}, status_code=413)
        except Exception as e:
            logger.exception("Chunk write failed: %s", e)
            return JSONResponse({"error": "chunk_write_failed"}, status_code=500)
        return JSONResponse({"ok": True})
    finally:
        await form.close()

# (session, item) pairs whose parts are being assembled right now: a second "complete" must not run in parallel
_ASSEMBLING: set = set()

@app.post("/api/upload/chunk/complete")
async def api_upload_chunk_complete(request: Request) -> JSONResponse:
    """Assemble all parts and run the regular upload flow to Immich."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    data = data if isinstance(data, dict) else {}
    item_id = data.get("item_id")
    session_id = data.get("session_id")
    name = data.get("name")
    last_modified = _to_int(data.get("last_modified"))
    invite_token = data.get("invite_token") or _header_token(request)
    fingerprint = data.get("fingerprint")
    album_name = data.get("album_name")
    content_type = data.get("content_type") or "application/octet-stream"
    if not item_id or not session_id:
        return JSONResponse({"error": "missing_ids"}, status_code=400)
    denied = await _guard_chunked_upload(request, invite_token, str(session_id))
    if denied:
        return denied
    d = _chunk_dir(session_id, item_id)
    meta_path = os.path.join(d, "meta.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        meta = {}
    total_chunks = _to_int(meta.get("total_chunks") or data.get("total_chunks") or 0)
    if not total_chunks or not (0 < total_chunks <= MAX_CHUNKS):
        return JSONResponse({"error": "missing_total"}, status_code=400)
    # Prefer the name captured at init if request did not include it
    name = name or meta.get("name") or "upload.bin"

    # Verify all parts exist before consuming any (missing part => client can retry)
    for i in range(total_chunks):
        if not os.path.exists(os.path.join(d, f"part_{i:06d}")):
            return JSONResponse({"error": "missing_part", "index": i}, status_code=400)

    key = (str(session_id), str(item_id))
    if key in _ASSEMBLING:
        return JSONResponse({"error": "already_completing"}, status_code=409)
    _ASSEMBLING.add(key)
    try:
        # Assemble by streaming parts into one file; the upload then reads that file in blocks too
        assembled_path = os.path.join(d, "assembled.bin")
        try:
            with open(assembled_path, "wb") as out:
                for i in range(total_chunks):
                    with open(os.path.join(d, f"part_{i:06d}"), "rb") as f:
                        shutil.copyfileobj(f, out, 1024 * 1024)
        except Exception as e:
            logger.exception("Assemble failed: %s", e)
            shutil.rmtree(d, ignore_errors=True)
            return JSONResponse({"error": "assemble_failed"}, status_code=500)
        limit = SETTINGS.max_upload_size_mb * 1024 * 1024
        total_size = os.path.getsize(assembled_path)
        if limit and total_size > limit:
            shutil.rmtree(d, ignore_errors=True)
            return JSONResponse({"error": "file_too_large"}, status_code=413)
        try:
            with open(assembled_path, "rb") as assembled:
                return await process_upload(
                    request,
                    file=assembled,
                    size=total_size,
                    orig_name=name,
                    content_type=content_type,
                    item_id=str(item_id),
                    session_id=str(session_id),
                    last_modified=last_modified,
                    invite_token=invite_token,
                    fingerprint=fingerprint,
                    album_name=album_name,
                )
        finally:
            shutil.rmtree(d, ignore_errors=True)
    finally:
        _ASSEMBLING.discard(key)

@app.post("/api/album/reset")
async def api_album_reset(request: Request) -> JSONResponse:
    """Explicit trigger from the UI to clear cached album id."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    reset_album_cache()
    return JSONResponse({"ok": True})

# ---------- Auth & Albums & Invites APIs ----------

_AUTH_OPTIONS_TTL = 60.0
_auth_options_cache: dict = {"at": 0.0, "value": None}

async def _fetch_auth_options() -> dict:
    """Ask Immich which login methods are enabled (cached briefly)."""
    now = time.monotonic()
    cached = _auth_options_cache["value"]
    if cached is not None and now - _auth_options_cache["at"] < _AUTH_OPTIONS_TTL:
        return cached
    opts = {"password": True, "oauth": False, "autoLaunch": False, "oauthButtonText": None, "message": None}
    base = SETTINGS.normalized_base_url
    headers = {"Accept": "application/json"}
    client = app.state.httpx_client
    try:
        rf = await client.get(f"{base}/server/features", headers=headers, timeout=10.0)
        rf.raise_for_status()
        f = rf.json()
        opts["password"] = bool(f.get("passwordLogin", True))
        opts["oauth"] = bool(f.get("oauth", False))
        opts["autoLaunch"] = bool(f.get("oauthAutoLaunch", False)) and opts["oauth"]
        try:
            rc = await client.get(f"{base}/server/config", headers=headers, timeout=10.0)
            if rc.is_success:
                c = rc.json()
                opts["oauthButtonText"] = c.get("oauthButtonText") or None
                opts["message"] = c.get("loginPageMessage") or None
        except Exception:
            logger.debug("Fetching /server/config failed", exc_info=True)
    except Exception as e:
        # Immich unreachable: keep the safe default (password only) and don't cache it.
        logger.warning("Could not fetch Immich login options: %s", e)
        return opts
    _auth_options_cache.update({"at": now, "value": opts})
    return opts

@app.get("/api/auth/options")
async def api_auth_options() -> dict:
    """Public: which login methods Immich currently offers."""
    return await _fetch_auth_options()

_login_limit = RateLimiter(limit=10, window=300)
_login_ip_limit = RateLimiter(limit=100, window=300)

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""

@app.post("/api/login")
async def api_login(request: Request) -> JSONResponse:
    """Authenticate against Immich using email/password; store token in session."""
    if not (await _fetch_auth_options())["password"]:
        return JSONResponse({"error": "password_login_disabled"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    email = (body or {}).get("email")
    password = (body or {}).get("password")
    if not isinstance(email, str) or not isinstance(password, str) or not email or not password:
        return JSONResponse({"error": "missing_credentials"}, status_code=400)
    email_key = f"{_client_ip(request)}|{email.strip().lower()}"
    if _login_limit.blocked(email_key) or _login_ip_limit.blocked(_client_ip(request)):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429)
    try:
        # Throwaway client: httpx keeps response cookies on the client that made the request, and the shared
        # client must never carry a user's Immich session cookie into later requests (e.g. API key checks).
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{SETTINGS.normalized_base_url}/auth/login", headers={"Content-Type": "application/json", "Accept": "application/json"}, json={"email": email, "password": password}, timeout=15.0)
    except Exception as e:
        logger.exception("Login request failed: %s", e)
        return JSONResponse({"error": "login_failed"}, status_code=502)
    if r.status_code not in (200, 201):
        logger.warning("Auth rejected: %s - %s", r.status_code, r.text)
        _login_limit.fail(email_key)
        _login_ip_limit.fail(_client_ip(request))
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    _login_limit.reset(email_key)
    data = r.json() if r.content else {}
    token = data.get("accessToken")
    if not token:
        logger.warning("Auth response missing accessToken")
        return JSONResponse({"error": "invalid_response"}, status_code=502)
    # Store only token and basic info in cookie session
    request.session.update({
        "accessToken": token,
        "userEmail": data.get("userEmail"),
        "userId": data.get("userId"),
        "name": data.get("name"),
        "isAdmin": data.get("isAdmin", False),
    })
    logger.info("User %s logged in", data.get("userEmail"))
    return JSONResponse({"ok": True, **{k: data.get(k) for k in ("userEmail","userId","name","isAdmin")}})

def _oauth_redirect_uri(request: Request) -> str:
    base = (SETTINGS.public_base_url or str(request.base_url)).rstrip("/")
    return f"{base}/oauth/callback"

@app.get("/oauth/start")
async def oauth_start(request: Request) -> RedirectResponse:
    """Begin Immich OAuth/OIDC login (PKCE); redirects the browser to the identity provider."""
    if not (await _fetch_auth_options())["oauth"]:
        return RedirectResponse(url=_u("/login?error=oauth_disabled"))
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    try:
        # Throwaway client: httpx keeps response cookies on the client that made the request.
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{SETTINGS.normalized_base_url}/oauth/authorize",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json={"redirectUri": _oauth_redirect_uri(request), "state": state, "codeChallenge": challenge},
            )
    except Exception as e:
        logger.exception("OAuth authorize request failed: %s", e)
        return RedirectResponse(url=_u("/login?error=oauth_failed"))
    url = r.json().get("url") if r.status_code in (200, 201) and r.content else None
    if not url:
        logger.warning("OAuth authorize rejected: %s - %s", r.status_code, r.text)
        return RedirectResponse(url=_u("/login?error=oauth_failed"))
    request.session["oauth"] = {"state": state, "verifier": verifier}
    return RedirectResponse(url=url)

@app.get("/oauth/callback")
async def oauth_callback(request: Request) -> RedirectResponse:
    """Finish Immich OAuth login: exchange the callback URL for an Immich access token."""
    pending = request.session.pop("oauth", None)
    received_state = request.query_params.get("state")
    if not pending or not received_state or not secrets.compare_digest(received_state, pending["state"]):
        logger.warning("OAuth callback with missing or mismatched state")
        return RedirectResponse(url=_u("/login?error=oauth_failed"))
    # Behind a proxy the request scheme/host may differ; rebuild from the registered redirect URI.
    callback_url = f"{_oauth_redirect_uri(request)}?{request.url.query}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{SETTINGS.normalized_base_url}/oauth/callback",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json={"url": callback_url, "state": pending["state"], "codeVerifier": pending["verifier"]},
            )
    except Exception as e:
        logger.exception("OAuth callback request failed: %s", e)
        return RedirectResponse(url=_u("/login?error=oauth_failed"))
    data = r.json() if r.content else {}
    token = data.get("accessToken") if r.status_code in (200, 201) else None
    if not token:
        logger.warning("OAuth login rejected: %s - %s", r.status_code, r.text)
        return RedirectResponse(url=_u("/login?error=oauth_failed"))
    request.session.update({
        "accessToken": token,
        "userEmail": data.get("userEmail"),
        "userId": data.get("userId"),
        "name": data.get("name"),
        "isAdmin": data.get("isAdmin", False),
    })
    logger.info("User %s logged in via OAuth", data.get("userEmail"))
    return RedirectResponse(url=_u("/"))

@app.post("/api/logout")
async def api_logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}

@app.get("/api/me/key")
async def api_my_key(request: Request) -> JSONResponse:
    """State of the key in use: own (saved by the user), server (IMMICH_API_KEY), and its status: ok, missing, invalid, unknown."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    own = stored_api_key(str(request.session.get("userId") or ""))
    key = SETTINGS.immich_api_key or own
    status = "missing"
    if key:
        status = "ok"
        try:
            r = await app.state.httpx_client.get(f"{SETTINGS.normalized_base_url}/users/me", headers={"Accept": "application/json", "x-api-key": key}, timeout=10.0)
            if r.status_code == 401:
                status = "invalid"
        except Exception:
            status = "unknown"
    return JSONResponse({"own": bool(own), "server": bool(SETTINGS.immich_api_key), "status": status, "immichUrl": SETTINGS.immich_web_url})

@app.put("/api/me/key")
async def api_my_key_set(request: Request) -> JSONResponse:
    """Save the user's own Immich API key; it must belong to the account that is logged in."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if SETTINGS.immich_api_key:
        return JSONResponse({"error": "key_managed_by_env"}, status_code=409)
    key = str((body or {}).get("apiKey") or "").strip()
    if not key:
        return JSONResponse({"error": "missing_key"}, status_code=400)
    user_id = str(request.session.get("userId") or "")
    # GET /users/me needs the user.read permission; it tells whose key this is
    try:
        r = await app.state.httpx_client.get(f"{SETTINGS.normalized_base_url}/users/me", headers={"Accept": "application/json", "x-api-key": key}, timeout=10.0)
    except Exception as e:
        logger.exception("API key check failed: %s", e)
        return JSONResponse({"error": "request_failed"}, status_code=502)
    if r.status_code == 403:
        return JSONResponse({"error": "key_missing_permission"}, status_code=400)
    if r.status_code == 401:
        return JSONResponse({"error": "key_invalid"}, status_code=400)
    if r.status_code != 200:
        logger.warning("API key check: Immich answered %s - %s", r.status_code, r.text[:200])
        return JSONResponse({"error": "immich_error", "status": r.status_code}, status_code=502)
    if str(r.json().get("id") or "") != user_id:
        return JSONResponse({"error": "key_other_account"}, status_code=400)
    conn = db.connect()
    conn.execute(
        "INSERT INTO user_keys (user_id, api_key) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET api_key = excluded.api_key, updated_at = CURRENT_TIMESTAMP",
        (user_id, key),
    )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})

@app.delete("/api/me/key")
async def api_my_key_delete(request: Request) -> JSONResponse:
    """Remove the user's saved API key."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    conn = db.connect()
    conn.execute("DELETE FROM user_keys WHERE user_id = ?", (str(request.session.get("userId") or ""),))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})

@app.get("/api/me/shortcut")
async def api_my_shortcut(request: Request) -> JSONResponse:
    """Whether the iOS Shortcut is enabled on the server, and whether the user has a token."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    conn = db.connect()
    row = conn.execute("SELECT 1 FROM shortcut_tokens WHERE user_id = ?", (str(request.session.get("userId") or ""),)).fetchone()
    conn.close()
    return JSONResponse({"enabled": SETTINGS.shortcut_enabled, "exists": bool(row)})

SHORTCUT_FILE = os.path.join(os.path.dirname(__file__), "assets", "Immich-Drop.shortcut")

@app.get("/api/me/shortcut/file")
async def api_my_shortcut_file(request: Request) -> Response:
    """Download the pre-built iOS Shortcut (contains no server URL or token)."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not SETTINGS.shortcut_enabled:
        return JSONResponse({"error": "shortcut_disabled"}, status_code=404)
    return FileResponse(SHORTCUT_FILE, media_type="application/octet-stream", filename="Immich-Drop.shortcut")

@app.post("/api/me/shortcut")
async def api_my_shortcut_create(request: Request) -> JSONResponse:
    """Create (or replace) the user's shortcut token. It is shown once; only a hash is stored."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not SETTINGS.shortcut_enabled:
        return JSONResponse({"error": "shortcut_disabled"}, status_code=404)
    token = secrets.token_urlsafe(32)
    conn = db.connect()
    conn.execute(
        "INSERT INTO shortcut_tokens (user_id, token_hash) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET token_hash = excluded.token_hash, created_at = CURRENT_TIMESTAMP",
        (str(request.session.get("userId") or ""), hashlib.sha256(token.encode("utf-8")).hexdigest()),
    )
    conn.commit()
    conn.close()
    return JSONResponse({"token": token})

@app.delete("/api/me/shortcut")
async def api_my_shortcut_delete(request: Request) -> JSONResponse:
    """Revoke the user's shortcut token."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    conn = db.connect()
    conn.execute("DELETE FROM shortcut_tokens WHERE user_id = ?", (str(request.session.get("userId") or ""),))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})

@app.get("/api/albums")
async def api_albums(request: Request) -> JSONResponse:
    """Return list of albums if authorized; logs on 401/403."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        # Use shared httpx client from app state
        client = app.state.httpx_client
        r = await client.get(f"{SETTINGS.normalized_base_url}/albums", headers=immich_headers(request), timeout=10.0)
    except Exception as e:
        logger.exception("Albums request failed: %s", e)
        return JSONResponse({"error": "request_failed"}, status_code=502)
    if r.status_code == 200:
        return JSONResponse(r.json())
    if r.status_code in (401, 403):
        logger.warning("Album list not allowed: %s - %s", r.status_code, r.text)
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({"error": "unexpected_status", "status": r.status_code}, status_code=502)

@app.post("/api/albums")
async def api_albums_create(request: Request) -> JSONResponse:
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    name = (body or {}).get("name")
    if not name:
        return JSONResponse({"error": "missing_name"}, status_code=400)
    try:
        # Use shared httpx client from app state
        client = app.state.httpx_client
        r = await client.post(f"{SETTINGS.normalized_base_url}/albums", headers={**immich_headers(request), "Content-Type": "application/json"}, json={"albumName": name}, timeout=10.0)
    except Exception as e:
        logger.exception("Create album failed: %s", e)
        return JSONResponse({"error": "request_failed"}, status_code=502)
    if r.status_code in (200, 201):
        return JSONResponse(r.json(), status_code=201)
    if r.status_code in (401, 403):
        logger.warning("Create album forbidden: %s - %s", r.status_code, r.text)
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({"error": "unexpected_status", "status": r.status_code, "body": r.text}, status_code=502)

# ---------- Invites (one-time/expiring links) ----------
# Tables are created at startup by db.init_db().

def hash_password(pw: str) -> str:
    """PBKDF2-SHA256 hash for invite passwords."""
    if not pw:
        return ""
    salt = os.urandom(16)
    iterations = 200_000
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt, iterations)
    return f"pbkdf2_sha256${iterations}${binascii.hexlify(salt).decode()}${binascii.hexlify(dk).decode()}"

def verify_password(stored: str, pw: Optional[str]) -> bool:
    """Verify a password against a stored pbkdf2_sha256 hash."""
    if not pw:
        return False
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != 'pbkdf2_sha256':
            return False
        iterations = int(iter_s)
        salt = binascii.unhexlify(salt_hex)
        dk = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt, iterations)
        return hmac.compare_digest(binascii.hexlify(dk).decode(), hash_hex)
    except Exception:
        return False

MAX_INVITE_USES = 1_000_000
MAX_EXPIRY_DAYS = 3650

def _parse_int(value) -> Optional[int]:
    """Whole number from an int or numeric string; None for anything else (bools, floats, text)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,9}", value.strip()):
        return int(value.strip())
    return None

def _valid_max_uses(value) -> Optional[int]:
    """-1 (unlimited) or 1..MAX_INVITE_USES; None if the value makes no sense."""
    n = _parse_int(value)
    if n is None or n == 0 or n < -1 or n > MAX_INVITE_USES:
        return None
    return n

def _expiry_from_days(value) -> Optional[str]:
    """Expiry timestamp for 1..MAX_EXPIRY_DAYS days from now; None if the value makes no sense."""
    n = _parse_int(value)
    if n is None or n < 1 or n > MAX_EXPIRY_DAYS:
        return None
    return (_utcnow() + timedelta(days=n)).replace(microsecond=0).isoformat()

def _valid_expires_at(value) -> Optional[str]:
    """The ISO timestamp as UTC without timezone, if it is in the future and within MAX_EXPIRY_DAYS."""
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    now = _utcnow()
    if dt <= now or dt > now + timedelta(days=MAX_EXPIRY_DAYS):
        return None
    return dt.replace(microsecond=0).isoformat()

@app.post("/api/invites")
async def api_invites_create(request: Request) -> JSONResponse:
    """Create an invite link for uploads with optional expiry and max uses."""
    # Require a logged-in session to create invites
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if key_headers(str(request.session.get("userId") or ""))[0] is None:
        return JSONResponse({"error": "no_api_key"}, status_code=409)
    album_id = (body or {}).get("albumId")
    album_name = (body or {}).get("albumName")
    max_uses = _valid_max_uses((body or {}).get("maxUses", 1))
    if max_uses is None:
        return JSONResponse({"error": "invalid_max_uses"}, status_code=400)
    invite_password = (body or {}).get("password")
    expires_days = (body or {}).get("expiresDays")
    link_name_in = str((body or {}).get("name") or "").strip()[:120]
    expires_at = None
    if expires_days not in (None, ""):
        expires_at = _expiry_from_days(expires_days)
        if expires_at is None:
            return JSONResponse({"error": "invalid_expiry"}, status_code=400)
    # Allow blank album for invites (no album association)
    if not album_name and SETTINGS.album_name and not album_id and album_name is not None:
        album_name = SETTINGS.album_name
    # If only album_name provided, resolve or create now to fix to an ID
    resolved_album_id = None
    if not album_id and album_name:
        resolved_album_id = await get_or_create_album(request=request, album_name_override=album_name)
    else:
        resolved_album_id = album_id
    # Generate token
    import uuid
    token = uuid.uuid4().hex
    pw_hash = hash_password(invite_password or "") if (invite_password and str(invite_password).strip()) else None
    # Owner info from session
    owner_user_id = str(request.session.get("userId") or "")
    owner_email = str(request.session.get("userEmail") or "")
    owner_name = str(request.session.get("name") or "")
    # Friendly name: default to album + creation timestamp if not provided in future updates
    # Here we set a default immediately
    now_tag = _utcnow().strftime("%Y-%m-%d %H:%M")
    default_link_name = link_name_in or f"{album_name or 'Link'} {now_tag}"
    try:
        conn = db.connect()
        cur = conn.cursor()
        if pw_hash:
            cur.execute(
                "INSERT INTO invites (token, album_id, album_name, max_uses, expires_at, password_hash, owner_user_id, owner_email, owner_name, name) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (token, resolved_album_id, album_name, max_uses, expires_at, pw_hash, owner_user_id, owner_email, owner_name, default_link_name)
            )
        else:
            cur.execute(
                "INSERT INTO invites (token, album_id, album_name, max_uses, expires_at, owner_user_id, owner_email, owner_name, name) VALUES (?,?,?,?,?,?,?,?,?)",
                (token, resolved_album_id, album_name, max_uses, expires_at, owner_user_id, owner_email, owner_name, default_link_name)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("Failed to create invite: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    # Build absolute URL using PUBLIC_BASE_URL if set, else request base
    try:
        base_url = SETTINGS.public_base_url.strip().rstrip('/') if SETTINGS.public_base_url else str(request.base_url).rstrip('/')
    except Exception:
        base_url = str(request.base_url).rstrip('/')
    absolute = f"{base_url}/invite/{token}"
    return JSONResponse({
        "ok": True,
        "token": token,
        "url": f"/invite/{token}",
        "absoluteUrl": absolute,
        "albumId": resolved_album_id,
        "albumName": album_name,
        "maxUses": max_uses,
        "expiresAt": _z(expires_at),
        "name": default_link_name
    })

@app.get("/api/invites")
async def api_invites_list(request: Request) -> JSONResponse:
    """List invites owned by the logged-in user, with optional q/sort filters."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    owner_user_id = str(request.session.get("userId") or "")
    q = (request.query_params.get("q") or "").strip()
    sort = (request.query_params.get("sort") or "-created").strip()
    # Map sort tokens to SQL
    sort_sql = "created_at DESC"
    if sort in ("created", "+created"):
        sort_sql = "created_at ASC"
    elif sort in ("-created",):
        sort_sql = "created_at DESC"
    elif sort in ("expires", "+expires"):
        sort_sql = "expires_at ASC"
    elif sort in ("-expires",):
        sort_sql = "expires_at DESC"
    elif sort in ("name", "+name"):
        sort_sql = "name ASC"
    elif sort in ("-name",):
        sort_sql = "name DESC"
    try:
        conn = db.connect()
        cur = conn.cursor()
        if q:
            like = f"%{q}%"
            cur.execute(
                """
                SELECT token, name, album_id, album_name, max_uses, used_count, expires_at, COALESCE(claimed,0), COALESCE(disabled,0), created_at, (COALESCE(password_hash,'') != '')
                FROM invites
                WHERE owner_user_id = ? AND (
                    COALESCE(name,'') LIKE ? OR COALESCE(album_name,'') LIKE ? OR token LIKE ?
                )
                ORDER BY """ + sort_sql,
                (owner_user_id, like, like, like)
            )
        else:
            cur.execute(
                f"SELECT token, name, album_id, album_name, max_uses, used_count, expires_at, COALESCE(claimed,0), COALESCE(disabled,0), created_at, (COALESCE(password_hash,'') != '') FROM invites WHERE owner_user_id = ? ORDER BY {sort_sql}",
                (owner_user_id,)
            )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.exception("List invites failed: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    items = []
    now = _utcnow()
    for (token, name, album_id, album_name, max_uses, used_count, expires_at, claimed, disabled, created_at, has_password) in rows:
        try:
            max_uses_int = int(max_uses) if max_uses is not None else -1
        except Exception:
            max_uses_int = -1
        remaining = None
        try:
            if max_uses_int >= 0:
                remaining = int(max_uses_int) - int(used_count or 0)
        except Exception:
            remaining = None
        expired = False
        if expires_at:
            try:
                expired = now > datetime.fromisoformat(expires_at)
            except Exception:
                expired = False
        inactive_reason = None
        active = True
        if (max_uses_int == 1 and claimed) or (remaining is not None and remaining <= 0):
            active = False
            inactive_reason = "claimed" if max_uses_int == 1 else "exhausted"
        if expired:
            active = False
            inactive_reason = inactive_reason or "expired"
        try:
            if int(disabled) == 1:
                active = False
                inactive_reason = "disabled"
        except Exception:
            pass
        items.append({
            "token": token,
            "name": name,
            "albumId": album_id,
            "albumName": album_name,
            "maxUses": max_uses,
            "used": used_count or 0,
            "remaining": remaining,
            "expiresAt": _z(expires_at),
            "active": active,
            "inactiveReason": inactive_reason,
            "createdAt": created_at,
            "passwordRequired": bool(has_password),
        })
    return JSONResponse({"items": items})

@app.patch("/api/invite/{token}")
async def api_invite_update(token: str, request: Request) -> JSONResponse:
    """Update invite fields: name, disabled, maxUses, expiresAt/expiresDays, password, resetUsage."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    owner_user_id = str(request.session.get("userId") or "")
    # Build dynamic update
    fields = []
    params = []
    # Name
    if "name" in (body or {}):
        fields.append("name = ?")
        params.append(str((body or {}).get("name") or "").strip()[:120])
    # Disabled toggle
    if "disabled" in (body or {}):
        try:
            disabled = 1 if (bool((body or {}).get("disabled")) is True) else 0
        except Exception:
            disabled = 0
        fields.append("disabled = ?")
        params.append(disabled)
    # Max uses
    if "maxUses" in (body or {}):
        mu = _valid_max_uses((body or {}).get("maxUses"))
        if mu is None:
            return JSONResponse({"error": "invalid_max_uses"}, status_code=400)
        fields.append("max_uses = ?")
        params.append(mu)
    # Expiration
    if "expiresAt" in (body or {}) or "expiresDays" in (body or {}):
        expires_at = None
        if (body or {}).get("expiresAt"):
            expires_at = _valid_expires_at((body or {}).get("expiresAt"))
            if expires_at is None:
                return JSONResponse({"error": "invalid_expiry"}, status_code=400)
        elif (body or {}).get("expiresDays") not in (None, ""):
            expires_at = _expiry_from_days((body or {}).get("expiresDays"))
            if expires_at is None:
                return JSONResponse({"error": "invalid_expiry"}, status_code=400)
        fields.append("expires_at = ?")
        params.append(expires_at)
    # Password
    if "password" in (body or {}):
        pw = str((body or {}).get("password") or "").strip()
        if pw:
            fields.append("password_hash = ?")
            params.append(hash_password(pw))
        else:
            fields.append("password_hash = NULL")
    # Reset usage
    reset_usage = bool((body or {}).get("resetUsage"))
    try:
        if fields or reset_usage:
            conn = db.connect()
            cur = conn.cursor()
            if fields:
                cur.execute(
                    f"UPDATE invites SET {', '.join(fields)} WHERE token = ? AND owner_user_id = ?",
                    (*params, token, owner_user_id)
                )
            if reset_usage:
                cur.execute("UPDATE invites SET used_count = 0, claimed = 0, claimed_at = NULL, claimed_by_session = NULL WHERE token = ? AND owner_user_id = ?", (token, owner_user_id))
            conn.commit()
            updated = conn.total_changes
            conn.close()
        else:
            updated = 0
    except Exception as e:
        logger.exception("Invite update failed: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    if updated == 0:
        return JSONResponse({"ok": False, "updated": 0}, status_code=404)
    return JSONResponse({"ok": True, "updated": updated})

MAX_BULK_TOKENS = 500

def _token_list(body) -> list:
    """The "tokens" of a bulk request as a list of strings (at most MAX_BULK_TOKENS); empty if malformed."""
    tokens = (body or {}).get("tokens") if isinstance(body, dict) else None
    if not isinstance(tokens, list):
        return []
    return [t for t in tokens if isinstance(t, str)][:MAX_BULK_TOKENS]

@app.post("/api/invites/bulk")
async def api_invites_bulk(request: Request) -> JSONResponse:
    """Bulk enable/disable invites owned by current user. Body: {tokens:[], action:'disable'|'enable'}"""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    tokens = _token_list(body)
    action = str((body or {}).get("action") or "disable").lower().strip()
    if not tokens:
        return JSONResponse({"error": "missing_tokens"}, status_code=400)
    val = 1 if action == "disable" else 0
    owner_user_id = str(request.session.get("userId") or "")
    try:
        conn = db.connect()
        cur = conn.cursor()
        # Build query with correct number of placeholders
        placeholders = ",".join(["?"] * len(tokens))
        cur.execute(
            f"UPDATE invites SET disabled = ? WHERE owner_user_id = ? AND token IN ({placeholders})",
            (val, owner_user_id, *tokens)
        )
        conn.commit()
        changed = cur.rowcount
        conn.close()
    except Exception as e:
        logger.exception("Bulk update failed: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    return JSONResponse({"ok": True, "updated": changed})

@app.post("/api/invites/delete")
async def api_invites_delete(request: Request) -> JSONResponse:
    """Hard delete invites owned by the current user and their upload logs.

    Body: { tokens: ["...", ...] }
    """
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    tokens = _token_list(body)
    if not tokens:
        return JSONResponse({"error": "missing_tokens"}, status_code=400)
    owner_user_id = str(request.session.get("userId") or "")
    try:
        conn = db.connect()
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(tokens))
        # Only links of this user: their upload events go first (no orphan rows), then the links
        owned = [r[0] for r in cur.execute(
            f"SELECT token FROM invites WHERE owner_user_id = ? AND token IN ({placeholders})",
            (owner_user_id, *tokens),
        ).fetchall()]
        if owned:
            owned_marks = ",".join(["?"] * len(owned))
            cur.execute(f"DELETE FROM upload_events WHERE token IN ({owned_marks})", tuple(owned))
            cur.execute(f"DELETE FROM invites WHERE owner_user_id = ? AND token IN ({owned_marks})", (owner_user_id, *owned))
        conn.commit()
        changed = len(owned)
        conn.close()
    except Exception as e:
        logger.exception("Bulk delete failed: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    return JSONResponse({"ok": True, "deleted": changed})

@app.get("/api/invite/{token}/uploads")
async def api_invite_uploads(token: str, request: Request) -> JSONResponse:
    """Return upload events for a given token (owner-only)."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    owner_user_id = str(request.session.get("userId") or "")
    try:
        conn = db.connect()
        cur = conn.cursor()
        # Verify ownership
        cur.execute("SELECT 1 FROM invites WHERE token = ? AND owner_user_id = ?", (token, owner_user_id))
        row = cur.fetchone()
        if not row:
            conn.close()
            return JSONResponse({"error": "forbidden"}, status_code=403)
        cur.execute("SELECT uploaded_at, ip, user_agent, fingerprint, filename, size, checksum, immich_asset_id FROM upload_events WHERE token = ? ORDER BY uploaded_at DESC LIMIT 500", (token,))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.exception("Fetch uploads failed: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    items = []
    for uploaded_at, ip, ua, fp, filename, size, checksum, asset_id in rows:
        items.append({
            "uploadedAt": uploaded_at,
            "ip": ip,
            "userAgent": ua,
            "fingerprint": fp,
            "filename": filename,
            "size": size,
            "checksum": checksum,
            "assetId": asset_id,
        })
    return JSONResponse({"items": items})

@app.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(token: str, request: Request) -> HTMLResponse:
    return _spa()

@app.get("/api/invite/{token}")
async def api_invite_info(token: str, request: Request) -> JSONResponse:
    try:
        conn = db.connect()
        cur = conn.cursor()
        cur.execute("SELECT token, album_id, album_name, max_uses, used_count, expires_at, COALESCE(claimed,0), claimed_at, password_hash, COALESCE(disabled,0), name FROM invites WHERE token = ?", (token,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        logger.exception("Invite info error: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    if not row:
        return JSONResponse({"error": "not_found"}, status_code=404)
    _, album_id, album_name, max_uses, used_count, expires_at, claimed, claimed_at, password_hash, disabled, link_name = row
    
    # If we have an album_id but no album_name, try to fetch it from Immich
    if album_id and not album_name:
        try:
            client = app.state.httpx_client
            r = await client.get(f"{SETTINGS.normalized_base_url}/albums", headers=immich_headers(request), timeout=10.0)
            if r.status_code == 200:
                albums = r.json()
                for album in albums:
                    if album.get("id") == album_id:
                        album_name = album.get("albumName")
                        break
        except Exception as e:
            logger.exception("Error fetching album name: %s", e)
    
    # compute remaining
    remaining = None
    try:
        if max_uses is not None and int(max_uses) >= 0:
            remaining = int(max_uses) - int(used_count or 0)
    except Exception:
        remaining = None
    # compute state flags
    one_time = False
    try:
        one_time = int(max_uses) == 1 if max_uses is not None else False
    except Exception:
        pass
    expired = False
    if expires_at:
        try:
            expired = _utcnow() > datetime.fromisoformat(expires_at)
        except Exception:
            pass
    deactivated = False
    reason = None
    if one_time and claimed:
        deactivated = True
        reason = "claimed"
    elif remaining is not None and remaining <= 0:
        deactivated = True
        reason = "exhausted"
    if expired:
        deactivated = True
        reason = reason or "expired"
    # Admin disabled flag
    try:
        if int(disabled) == 1:
            deactivated = True
            reason = "disabled"
    except Exception:
        pass
    active = not deactivated
    # Password requirement + authorization state
    password_required = bool(password_hash)
    authorized = False
    try:
        ia = request.session.get("inviteAuth") or {}
        authorized = ia.get(token, False)
    except Exception:
        pass
    return JSONResponse({
        "token": token,
        "albumId": album_id,
        "albumName": album_name,
        "name": link_name,
        "maxUses": max_uses,
        "used": used_count or 0,
        "remaining": remaining,
        "expiresAt": _z(expires_at),
        "oneTime": one_time,
        "claimed": bool(claimed),
        "claimedAt": claimed_at,
        "expired": expired,
        "active": active,
        "inactiveReason": (None if active else (reason or "inactive")),
        "passwordRequired": password_required,
        "authorized": authorized,
    })

_invite_pw_limit = RateLimiter(limit=10, window=600)
_invite_pw_token_limit = RateLimiter(limit=50, window=600)

@app.post("/api/invite/{token}/auth")
async def api_invite_auth(token: str, request: Request) -> JSONResponse:
    """Validate a password for an invite token, and mark this session authorized if valid."""
    ip_key = f"{_client_ip(request)}|{token}"
    if _invite_pw_limit.blocked(ip_key) or _invite_pw_token_limit.blocked(token):
        return JSONResponse({"error": "too_many_attempts"}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        body = None
    provided = (body or {}).get("password") if isinstance(body, dict) else None
    try:
        conn = db.connect()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM invites WHERE token = ?", (token,))
        row = cur.fetchone()
        conn.close()
    except Exception as e:
        logger.exception("Invite auth lookup error: %s", e)
        return JSONResponse({"error": "db_error"}, status_code=500)
    if not row:
        return JSONResponse({"error": "not_found"}, status_code=404)
    password_hash = row[0]
    if not password_hash:
        # No password required; mark as authorized to simplify client flow
        ia = request.session.get("inviteAuth") or {}
        ia[token] = True
        request.session["inviteAuth"] = ia
        return JSONResponse({"ok": True, "authorized": True})
    if not verify_password(password_hash, provided if isinstance(provided, str) else None):
        _invite_pw_limit.fail(ip_key)
        _invite_pw_token_limit.fail(token)
        return JSONResponse({"error": "invalid_password"}, status_code=403)
    ia = request.session.get("inviteAuth") or {}
    ia[token] = True
    request.session["inviteAuth"] = ia
    return JSONResponse({"ok": True, "authorized": True})

@app.get("/api/qr", response_model=None)
async def api_qr(request: Request):
    """Generate a QR code PNG for a given text (query param 'text')."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    text = request.query_params.get("text")
    if not text:
        return JSONResponse({"error": "missing_text"}, status_code=400)
    if len(text) > 1000:
        return JSONResponse({"error": "text_too_long"}, status_code=400)
    if qrcode is None:
        logger.warning("qrcode library not installed; cannot generate QR")
        return JSONResponse({"error": "qr_not_available"}, status_code=501)
    try:
        img = qrcode.make(text)
    except Exception:
        return JSONResponse({"error": "qr_failed"}, status_code=400)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")

# ---------- Platform Cookies API ----------

from .cookie_manager import (
    db_list_cookies,
    db_upsert_cookie,
    db_delete_cookie,
    is_cookie_stale,
    PLATFORM_DOMAINS,
)

@app.get("/api/cookies")
async def api_cookies_list(request: Request) -> JSONResponse:
    """List the logged-in user's platform cookies."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cookies = db_list_cookies(SETTINGS.state_db, str(request.session.get("userId") or ""))
    # The secret itself never leaves the server: only a short preview is returned
    for c in cookies:
        secret = c.pop("cookie_string", "") or ""
        c["cookie_preview"] = secret[:40] + "..." if len(secret) > 40 else secret
        c["is_stale"] = is_cookie_stale(c.get("updated_at", ""))
    return JSONResponse({"items": cookies, "platforms": list(PLATFORM_DOMAINS.keys())})

@app.post("/api/cookies")
async def api_cookies_upsert(request: Request) -> JSONResponse:
    """Create or update a platform cookie of the logged-in user."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    body = body if isinstance(body, dict) else {}
    platform = body.get("platform", "")
    cookie_string = body.get("cookie_string", "")
    if not isinstance(platform, str) or not isinstance(cookie_string, str):
        return JSONResponse({"error": "invalid_type"}, status_code=400)
    platform = platform.strip().lower()
    cookie_string = cookie_string.strip()
    if len(cookie_string) > 16_000 or re.search(r"[\x00-\x1f\x7f]", cookie_string):
        return JSONResponse({"error": "invalid_cookie_string"}, status_code=400)
    if not platform:
        return JSONResponse({"error": "missing_platform"}, status_code=400)
    if not cookie_string:
        return JSONResponse({"error": "missing_cookie_string"}, status_code=400)
    if platform not in PLATFORM_DOMAINS:
        return JSONResponse({"error": "unsupported_platform", "supported": list(PLATFORM_DOMAINS.keys())}, status_code=400)
    success = db_upsert_cookie(SETTINGS.state_db, platform, cookie_string, str(request.session.get("userId") or ""))
    if success:
        return JSONResponse({"ok": True, "platform": platform})
    return JSONResponse({"error": "save_failed"}, status_code=500)

@app.delete("/api/cookies/{platform}")
async def api_cookies_delete(request: Request, platform: str) -> JSONResponse:
    """Delete a platform cookie of the logged-in user."""
    if not request.session.get("accessToken"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    platform = platform.strip().lower()
    deleted = db_delete_cookie(SETTINGS.state_db, platform, str(request.session.get("userId") or ""))
    if deleted:
        return JSONResponse({"ok": True, "deleted": platform})
    return JSONResponse({"error": "not_found"}, status_code=404)
