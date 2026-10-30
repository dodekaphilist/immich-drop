"""Cookies, QR, config and small helpers."""
import pytest

from app import app as m
from app import config as cfg
from app.immich_client import parse_error, to_immich_iso
from app.utils import detect_file_type


def test_cookie_roundtrip_is_per_user(client, login):
    login("user-a")
    assert client.post("/api/cookies", json={"platform": "instagram", "cookie_string": "sessionid=abc; csrftoken=x=y"}).status_code == 200
    assert [c["platform"] for c in client.get("/api/cookies").json()["items"]] == ["instagram"]
    login("user-b")
    assert client.get("/api/cookies").json()["items"] == []
    assert client.delete("/api/cookies/instagram").status_code == 404


def test_cookie_platform_is_validated(client, login):
    login()
    assert client.post("/api/cookies", json={"platform": "../../etc", "cookie_string": "a=b"}).status_code == 400
    assert client.post("/api/cookies", json={"platform": "instagram", "cookie_string": ""}).status_code == 400


def test_cookie_wrong_types_are_a_client_error(client, login):
    login()
    assert client.post("/api/cookies", json={"platform": 5, "cookie_string": ["a"]}).status_code == 400
    assert client.post("/api/cookies", json=["x"]).status_code == 400


def test_cookie_string_with_control_characters_is_refused(client, login):
    login()
    assert client.post("/api/cookies", json={"platform": "reddit", "cookie_string": "a=b\nevil.com"}).status_code == 400


def test_cookie_list_does_not_return_the_full_secret(client, login):
    login()
    secret = "sessionid=" + "s" * 80
    client.post("/api/cookies", json={"platform": "reddit", "cookie_string": secret})
    item = client.get("/api/cookies").json()["items"][0]
    assert secret not in str(item)


def test_qr_needs_login_and_rejects_oversized_text(client, login):
    assert client.get("/api/qr", params={"text": "x"}).status_code == 401
    login()
    assert client.get("/api/qr", params={"text": "a" * 9000}).status_code == 400
    assert client.get("/api/qr", params={"text": "a" * 1000}).status_code == 200


def test_qr_small_text_works(client, login):
    login()
    r = client.get("/api/qr", params={"text": "https://example.com/invite/abc"})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"


def test_config_requires_urls(monkeypatch):
    monkeypatch.delenv("IMMICH_BASE_URL")
    monkeypatch.delenv("PUBLIC_BASE_URL")
    with pytest.raises(cfg.ConfigError) as e:
        cfg.load_settings()
    assert "IMMICH_BASE_URL" in str(e.value) and "PUBLIC_BASE_URL" in str(e.value)


def test_config_base_path_and_api_suffix(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://immich.example.com/drop/")
    monkeypatch.setenv("IMMICH_BASE_URL", "http://immich:2283/")
    s = cfg.load_settings()
    assert s.base_path == "/drop" and s.normalized_base_url == "http://immich:2283/api"


@pytest.mark.parametrize("name", ["CHUNK_SIZE_MB", "DOWNLOAD_CONCURRENCY"])
def test_config_garbage_numbers_fall_back(monkeypatch, name):
    monkeypatch.setenv(name, "lots")
    cfg.load_settings()


@pytest.mark.parametrize("name,value,field,expected", [
    ("CHUNK_SIZE_MB", "0", "chunk_size_mb", 95), ("CHUNK_SIZE_MB", "-4", "chunk_size_mb", 95),
    ("CHUNK_SIZE_MB", "50", "chunk_size_mb", 50), ("DOWNLOAD_CONCURRENCY", "0", "download_concurrency", 1),
    ("GALLERY_DL_TIMEOUT", "-1", "gallery_dl_timeout", 300), ("MAX_UPLOAD_SIZE_MB", "junk", "max_upload_size_mb", 0),
    ("MAX_UPLOAD_SIZE_MB", "500", "max_upload_size_mb", 500),
])
def test_config_numbers_are_validated(monkeypatch, name, value, field, expected):
    monkeypatch.setenv(name, value)
    assert getattr(cfg.load_settings(), field) == expected


def test_album_reset_needs_login(client, login):
    assert client.post("/api/album/reset").status_code == 401
    assert login().post("/api/album/reset").status_code == 200


def test_ping_is_cached_and_hides_the_album_from_anonymous_callers(client, login, monkeypatch):
    calls = []

    async def ping():
        calls.append(1)
        return True

    monkeypatch.setattr(m, "immich_ping", ping)
    monkeypatch.setattr(m.SETTINGS, "album_name", "Drop")
    for _ in range(3):
        anon = client.post("/api/ping").json()
    assert calls == [1] and anon["ok"] is True and anon["album_name"] is None
    assert login().post("/api/ping").json()["album_name"] == "Drop"


def test_logout_is_post_only(client):
    assert client.get("/logout").status_code in (404, 405)
    assert client.post("/api/logout").json() == {"ok": True}


def test_rate_limiter_blocks_and_expires(monkeypatch):
    from app import ratelimit

    now = [1000.0]
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: now[0])
    rl = ratelimit.RateLimiter(limit=3, window=60)
    for _ in range(3):
        assert not rl.blocked("k")
        rl.fail("k")
    assert rl.blocked("k") and not rl.blocked("other")
    now[0] += 61
    assert not rl.blocked("k")
    rl.fail("k")
    rl.reset("k")
    assert not rl.blocked("k")


def test_sanitize_filename():
    assert m.sanitize_filename("a/b\\c\x00d.jpg") == "a_b_cd.jpg"
    assert m.sanitize_filename("") == "file" and m.sanitize_filename(None) == "file"
    assert m.sanitize_filename("Ünï 🎉.jpg") == "Ünï 🎉.jpg"


def test_detect_file_type_and_short_input():
    assert detect_file_type(b"\xff\xd8\xff" + b"0" * 20)[0] == ".jpg"
    assert detect_file_type(b"abc") == (None, None)


def test_immich_helpers():
    from datetime import datetime

    assert to_immich_iso(datetime(2024, 1, 2, 3, 4, 5)).endswith("+00:00")

    class R:
        status_code = 400
        text = "boom"

        def json(self):
            return {"message": ["a", "b"]}

    assert parse_error(R()) == "a; b"


def test_chunk_upload_roundtrip_limits(client, login, monkeypatch):
    """Parts of an invite upload are accepted for a valid link only."""
    r = client.post("/api/upload/chunk/init", json={"item_id": "a1", "session_id": "s1", "invite_token": "nope"})
    assert r.status_code == 403


def test_only_a_logged_in_page_can_reset_the_album_cache_via_websocket(client, login):
    import json

    m.ALBUM_IDS["x"] = "album"
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"session_id": "ab12"}))
    assert m.ALBUM_IDS == {"x": "album"}
    login()
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"session_id": "cd34"}))
    assert m.ALBUM_IDS == {}
