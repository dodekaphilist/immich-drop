"""iOS Shortcut / script endpoints (SHORTCUT_ENABLED): bearer token auth and streaming uploads."""
import base64

import pytest

from app import app as m
from app import api_routes
from app.immich_client import UploadOutcome


@pytest.fixture()
def immich(monkeypatch):
    seen = []

    async def upload_asset(client, base, headers, *, file, size, filename, content_type, **kw):
        file.seek(0)
        seen.append({"filename": filename, "content_type": content_type, "size": size, "data": file.read()})
        return UploadOutcome(ok=True, status_code=201, asset_id=f"a{len(seen)}", status="created")

    async def album(client, base, headers, name, description=None, owner_id=""):
        return "album-1"

    async def add(client, base, headers, album_id, asset_id):
        seen.append({"album": album_id, "asset": asset_id})
        return True

    monkeypatch.setattr(m.immich_client, "upload_asset", upload_asset)
    monkeypatch.setattr(api_routes.immich_client, "find_or_create_album", album)
    monkeypatch.setattr(api_routes.immich_client, "add_to_album", add)
    return seen


@pytest.fixture()
def token(client, login):
    login("user-a")
    value = client.post("/api/me/shortcut").json()["token"]
    client.cookies.clear()
    return value


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


JPEG = b"\xff\xd8\xff\xe0" + b"0" * 40


def test_no_token_no_access(client, immich):
    assert client.post("/api/upload/base64", json={"data": "AAAA"}).status_code == 401
    assert client.post("/api/upload/base64", json={"data": "AAAA"}, headers=_auth("wrong")).status_code == 401


def test_revoked_token_stops_working(client, login, token, immich):
    login("user-a")
    client.delete("/api/me/shortcut")
    client.cookies.clear()
    assert client.post("/api/upload/base64", json={"data": base64.b64encode(JPEG).decode()}, headers=_auth(token)).status_code == 401


def test_base64_upload_detects_type_and_adds_album(client, token, immich):
    r = client.post("/api/upload/base64", json={"data": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode(), "album_name": "Trip"}, headers=_auth(token))
    assert r.status_code == 200 and r.json()["status"] == "success"
    assert immich[0]["filename"].endswith(".jpg") and immich[0]["data"] == JPEG and immich[0]["content_type"] == "image/jpeg"
    assert immich[1] == {"album": "album-1", "asset": "a1"}


def test_base64_garbage_is_a_client_error(client, token, immich):
    assert client.post("/api/upload/base64", json={"data": "!!notbase64!!"}, headers=_auth(token)).status_code == 400


def test_filename_cannot_inject_multipart_headers(client, token, immich):
    r = client.post("/api/upload/base64", json={"data": base64.b64encode(JPEG).decode(), "filename": 'a"\r\nX-Evil: 1.jpg'}, headers=_auth(token))
    assert r.status_code == 200 and "\r" not in immich[0]["filename"] and "\n" not in immich[0]["filename"]


def test_file_upload_streams_the_content(client, token, immich):
    r = client.post("/api/upload/file", files={"file": ("p.jpg", JPEG, "image/jpeg")}, headers=_auth(token))
    assert r.status_code == 200 and immich[0]["data"] == JPEG and immich[0]["size"] == len(JPEG)


def test_batch_upload_and_limits(client, token, immich, monkeypatch):
    files = [("files", (f"{i}.jpg", JPEG + bytes([i]), "image/jpeg")) for i in range(3)]
    body = client.post("/api/upload/batch", files=files, headers=_auth(token)).json()
    assert body["total"] == 3 and body["successful"] == 3
    monkeypatch.setattr(m.SETTINGS, "max_upload_size_mb", 1)
    big = client.post("/api/upload/file", files={"file": ("p.jpg", b"x" * (2 * 1024 * 1024), "image/jpeg")}, headers=_auth(token))
    assert big.status_code == 413


def test_urls_endpoint_vets_every_url(client, token, immich):
    body = client.post("/api/upload/urls", json={"urls": ["--batch-file=/etc/passwd", "http://127.0.0.1/x"]}, headers=_auth(token)).json()
    assert body["failed"] == 2 and body["successful"] == 0
