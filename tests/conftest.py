"""Shared fixtures: the real FastAPI app on a throw-away DATA_DIR, with no Immich server behind it."""
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The settings are read once when app.app is imported, so the environment must be ready before that.
_DATA = tempfile.mkdtemp(prefix="immich_drop_test_")
os.environ.update(
    IMMICH_BASE_URL="http://immich.invalid",
    PUBLIC_BASE_URL="http://testserver",
    SESSION_SECRET="test-session-secret",
    DATA_DIR=_DATA,
    IMMICH_API_KEY="test-server-key",
    CHUNKED_UPLOADS_ENABLED="true",
    SHORTCUT_ENABLED="true",
    TEST_CONNECTION_ENABLED="true",
)
os.environ.pop("IMMICH_ALBUM_NAME", None)

# Never read a developer's real .env
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import app as app_module  # noqa: E402
from app import db, job_manager  # noqa: E402


def session_cookie(user_id: str = "user-a", **extra) -> str:
    """A valid signed session cookie, as if the user had logged in through Immich."""
    data = {"accessToken": "t", "userId": user_id, "userEmail": f"{user_id}@example.test", "name": user_id, **extra}
    raw = base64.b64encode(json.dumps(data).encode("utf-8"))
    return TimestampSigner(os.environ["SESSION_SECRET"]).sign(raw).decode("utf-8")


@pytest.fixture(scope="session")
def app():
    return app_module.app


@pytest.fixture()
def client(app):
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def login(client):
    def _login(user_id: str = "user-a"):
        client.cookies.set("session", session_cookie(user_id))
        return client

    return _login


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Host names resolve to a public address, IP literals to themselves; nothing touches a real resolver."""
    import ipaddress
    import socket

    def getaddrinfo(host, *a, **k):
        try:
            ipaddress.ip_address(host)
            ip = host
        except ValueError:
            ip = "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def clean_state():
    """Every test starts with empty tables and no pending cancel flags."""
    conn = db.connect()
    for table in ("uploads", "invites", "upload_events", "platform_cookies", "user_keys", "shortcut_tokens"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    app_module._CANCELLED.clear()
    job_manager._jobs.clear()
    app_module._ping_cache.update({"at": 0.0, "ok": False})
    for limiter in (app_module._login_limit, app_module._login_ip_limit, app_module._invite_pw_limit, app_module._invite_pw_token_limit):
        limiter._hits.clear()
    yield


def make_invite(token: str = "tok1", owner: str = "user-a", **cols) -> str:
    """Insert an invite row directly."""
    values = {"token": token, "max_uses": -1, "owner_user_id": owner, "name": "t", **cols}
    conn = db.connect()
    conn.execute(
        f"INSERT INTO invites ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        tuple(values.values()),
    )
    conn.commit()
    conn.close()
    return token
