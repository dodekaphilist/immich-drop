"""
Config loader for the Immich Drop Uploader (Python).
Reads ONLY from .env; there is NO runtime mutation from the UI.
"""

from __future__ import annotations
import ipaddress
import os
from urllib.parse import urlparse
from dataclasses import dataclass
import secrets
from dotenv import load_dotenv


class ConfigError(Exception):
    """The configuration is incomplete or invalid; the app must not start."""


@dataclass
class Settings:
    """App settings loaded from environment variables (.env)."""
    immich_base_url: str
    immich_api_key: str
    album_name: str = ""
    public_base_url: str = ""
    base_path: str = ""
    data_dir: str = "/data"
    session_secret: str = ""
    session_secret_generated: bool = False
    log_level: str = "INFO"
    chunked_uploads_enabled: bool = False
    chunk_size_mb: int = 95
    gallery_dl_sleep_request: str = "10-25"
    gallery_dl_sleep: str = "5-15"
    gallery_dl_timeout: int = 300
    download_concurrency: int = 1
    instagram_ytdlp_fallback: bool = False
    social_media_uploads: bool = True
    test_connection_enabled: bool = True
    test_connection_show_hostname: bool = True
    shortcut_enabled: bool = False

    @property
    def immich_web_url(self) -> str:
        """Address of the Immich web UI for links in the UI; empty if IMMICH_BASE_URL looks internal.

        Internal means localhost, an IP address, or a name without a dot (e.g. the Docker service "immich_server"):
        users could not open those from their browser.
        """
        url = self.immich_base_url.strip().rstrip("/")
        url = url[:-4] if url.endswith("/api") else url
        host = (urlparse(url).hostname or "").lower()
        if "." not in host or host == "localhost":
            return ""
        try:
            ipaddress.ip_address(host)
            return ""
        except ValueError:
            return url

    @property
    def state_db(self) -> str:
        return os.path.join(self.data_dir, "state.db")

    @property
    def normalized_base_url(self) -> str:
        """Immich API URL without a trailing slash. "/api" is added when the server URL is given without it."""
        url = self.immich_base_url.strip().rstrip("/")
        return url if url.endswith("/api") else f"{url}/api"

def load_settings() -> Settings:
    """Load settings from .env, applying defaults when absent."""
    # Load environment variables from .env once here so importers don't have to
    try:
        load_dotenv()
    except Exception:
        pass
    def is_http_url(value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)

    # Without these the app can not do its job, so refuse to start instead of failing later
    problems: list[str] = []
    base = os.getenv("IMMICH_BASE_URL", "").strip()
    if not base:
        problems.append("IMMICH_BASE_URL is not set: the address of your Immich server, e.g. https://immich.example.com")
    elif not is_http_url(base):
        problems.append(f"IMMICH_BASE_URL must be a full http(s) URL, e.g. https://immich.example.com (got {base!r})")
    # Optional: one key for everyone; without it every user saves an API key of their own in the app
    api_key = os.getenv("IMMICH_API_KEY", "").strip()
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not public_base_url:
        problems.append("PUBLIC_BASE_URL is not set: the public address of this app, e.g. https://drop.example.com or https://immich.example.com/drop (used for upload links and SSO)")
    elif not is_http_url(public_base_url):
        problems.append(f"PUBLIC_BASE_URL must be a full http(s) URL, e.g. https://drop.example.com (got {public_base_url!r})")
    if problems:
        raise ConfigError("\n".join(f"  - {p}" for p in problems))
    album_name = os.getenv("IMMICH_ALBUM_NAME", "")
    # Safe defaults
    def as_bool(v: str, default: bool = False) -> bool:
        if v is None:
            return default
        return str(v).strip().lower() in {"1","true","yes","on"}
    # Subfolder the app is reachable under (e.g. "/drop" for https://immich.example.com/drop).
    # Taken from the path part of PUBLIC_BASE_URL; empty = served at the root.
    base_path = (urlparse(public_base_url.strip()).path or "").strip().strip("/")
    base_path = f"/{base_path}" if base_path else ""
    # One directory holds everything the app stores: state.db, chunks/ and cookies/
    data_dir = os.getenv("DATA_DIR", "").strip() or "/data"
    session_secret = os.getenv("SESSION_SECRET", "").strip()
    session_secret_generated = not session_secret
    if session_secret_generated:
        session_secret = secrets.token_hex(32)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    chunked_uploads_enabled = as_bool(os.getenv("CHUNKED_UPLOADS_ENABLED", "false"), False)
    try:
        chunk_size_mb = int(os.getenv("CHUNK_SIZE_MB", "95"))
    except ValueError:
        chunk_size_mb = 95
    gallery_dl_sleep_request = os.getenv("GALLERY_DL_SLEEP_REQUEST", "10-25")
    gallery_dl_sleep = os.getenv("GALLERY_DL_SLEEP", "5-15")
    try:
        gallery_dl_timeout = int(os.getenv("GALLERY_DL_TIMEOUT", "300"))
    except ValueError:
        gallery_dl_timeout = 300
    try:
        download_concurrency = int(os.getenv("DOWNLOAD_CONCURRENCY", "1"))
    except ValueError:
        download_concurrency = 1
    instagram_ytdlp_fallback = as_bool(os.getenv("INSTAGRAM_YTDLP_FALLBACK", "false"), False)
    social_media_uploads = as_bool(os.getenv("SOCIAL_MEDIA_UPLOADS_ENABLED", "true"), True)
    test_connection_enabled = as_bool(os.getenv("TEST_CONNECTION_ENABLED", "true"), True)
    shortcut_enabled = as_bool(os.getenv("SHORTCUT_ENABLED", "false"), False)
    test_connection_show_hostname = as_bool(os.getenv("TEST_CONNECTION_SHOW_HOSTNAME", "true"), True)
    return Settings(
        immich_base_url=base,
        immich_api_key=api_key,
        album_name=album_name,
        public_base_url=public_base_url,
        base_path=base_path,
        data_dir=data_dir,
        session_secret=session_secret,
        session_secret_generated=session_secret_generated,
        log_level=log_level,
        chunked_uploads_enabled=chunked_uploads_enabled,
        chunk_size_mb=chunk_size_mb,
        gallery_dl_sleep_request=gallery_dl_sleep_request,
        gallery_dl_sleep=gallery_dl_sleep,
        gallery_dl_timeout=gallery_dl_timeout,
        download_concurrency=download_concurrency,
        instagram_ytdlp_fallback=instagram_ytdlp_fallback,
        social_media_uploads=social_media_uploads,
        test_connection_enabled=test_connection_enabled,
        test_connection_show_hostname=test_connection_show_hostname,
        shortcut_enabled=shortcut_enabled,
    )
