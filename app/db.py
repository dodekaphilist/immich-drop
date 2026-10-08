"""
Central SQLite access for immich-drop.
All connections go through connect(); all tables and migrations are created
once at startup via init_db() instead of ad hoc DDL in request handlers.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger("immich_drop.db")

_DB_PATH: str = ""


def configure(path: str) -> None:
    global _DB_PATH
    _DB_PATH = path


def connect() -> sqlite3.Connection:
    """Return a new connection to the state DB (short-lived, caller closes)."""
    return sqlite3.connect(_DB_PATH, timeout=10)


def _migrate_uploads_owner(cur: sqlite3.Cursor) -> None:
    """Scope the duplicate cache per Immich account: UNIQUE(checksum) becomes UNIQUE(owner_id, checksum)."""
    cols = [r[1] for r in cur.execute("PRAGMA table_info(uploads)").fetchall()]
    if cols and "owner_id" not in cols:
        cur.execute("ALTER TABLE uploads RENAME TO uploads_old")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT NOT NULL DEFAULT '',
            checksum TEXT,
            filename TEXT,
            size INTEGER,
            device_asset_id TEXT,
            immich_asset_id TEXT,
            created_at TEXT,
            inserted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (owner_id, checksum)
        );
        """
    )
    if cols and "owner_id" not in cols:
        cur.execute(
            "INSERT OR IGNORE INTO uploads (checksum, filename, size, device_asset_id, immich_asset_id, created_at, inserted_at) "
            "SELECT checksum, filename, size, device_asset_id, immich_asset_id, created_at, inserted_at FROM uploads_old"
        )
        cur.execute("DROP TABLE uploads_old")


def _migrate_cookies_owner(cur: sqlite3.Cursor) -> None:
    """Cookies belong to a user: UNIQUE(platform) becomes UNIQUE(user_id, platform).

    Cookies saved before stay with user_id '' (the iOS Shortcut and other requests without a login use those).
    """
    cols = [r[1] for r in cur.execute("PRAGMA table_info(platform_cookies)").fetchall()]
    old = bool(cols) and "user_id" not in cols
    if old:
        cur.execute("ALTER TABLE platform_cookies RENAME TO platform_cookies_old")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_cookies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL,
            cookie_string TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, platform)
        );
        """
    )
    if old:
        cur.execute(
            "INSERT OR IGNORE INTO platform_cookies (platform, cookie_string, created_at, updated_at) "
            "SELECT platform, cookie_string, created_at, updated_at FROM platform_cookies_old"
        )
        cur.execute("DROP TABLE platform_cookies_old")


def init_db() -> None:
    """Create all tables and run best-effort column migrations (idempotent)."""
    conn = connect()
    try:
        cur = conn.cursor()
        _migrate_uploads_owner(cur)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS shortcut_tokens (
                user_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_keys (
                user_id TEXT PRIMARY KEY,
                api_key TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invites (
                token TEXT PRIMARY KEY,
                album_id TEXT,
                album_name TEXT,
                max_uses INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                expires_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        # Best-effort column migrations for older databases
        for ddl in (
            "ALTER TABLE invites ADD COLUMN claimed INTEGER DEFAULT 0",
            "ALTER TABLE invites ADD COLUMN claimed_at TEXT",
            "ALTER TABLE invites ADD COLUMN claimed_by_session TEXT",
            "ALTER TABLE invites ADD COLUMN password_hash TEXT",
            "ALTER TABLE invites ADD COLUMN owner_user_id TEXT",
            "ALTER TABLE invites ADD COLUMN owner_email TEXT",
            "ALTER TABLE invites ADD COLUMN owner_name TEXT",
            "ALTER TABLE invites ADD COLUMN name TEXT",
            "ALTER TABLE invites ADD COLUMN disabled INTEGER DEFAULT 0",
        ):
            try:
                cur.execute(ddl)
            except sqlite3.OperationalError:
                pass
        _migrate_cookies_owner(cur)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT,
                uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                ip TEXT,
                user_agent TEXT,
                fingerprint TEXT,
                filename TEXT,
                size INTEGER,
                checksum TEXT,
                immich_asset_id TEXT
            );
            """
        )
        conn.commit()
    except Exception as e:
        logger.exception("Failed to initialize state DB: %s", e)
    finally:
        conn.close()
