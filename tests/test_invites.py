"""Invite links, uploads (whole and chunked), duplicate handling and ownership."""
import hashlib

import pytest

from app import app as m
from app import db
from app.immich_client import UploadOutcome

from .conftest import make_invite


def _sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def _upload(client, token, content=b"A" * 64, name="a.jpg", item="a1", last_modified="1700000000000", session="s1", header=True):
    data = {"item_id": item, "session_id": session, "last_modified": last_modified}
    headers = {}
    if header:
        headers["X-Invite-Token"] = token
    else:
        data["invite_token"] = token
    return client.post("/api/upload", data=data, files={"file": (name, content, "image/jpeg")}, headers=headers)


@pytest.fixture()
def fake_immich(monkeypatch):
    """Stands in for Immich: `bulk` is what the duplicate check answers, `upload_ok` whether uploads work."""
    state = {"upload": 0, "bulk": 0, "bulk_result": {}, "upload_ok": True, "last_headers": None}

    async def upload_asset(client, base, headers, *, file, size, **kw):
        state["upload"] += 1
        state["last_headers"] = headers
        file.seek(0)
        assert len(file.read()) == size
        if not state["upload_ok"]:
            return UploadOutcome(ok=False, status_code=500, error="immich down")
        return UploadOutcome(ok=True, status_code=201, asset_id=f"asset-{state['upload']}", status="created")

    async def bulk(checks, headers):
        state["bulk"] += 1
        verdict = state["bulk_result"].get("*")
        return {c["id"]: {**verdict, "id": c["id"]} for c in checks} if verdict else {}

    monkeypatch.setattr(m.immich_client, "upload_asset", upload_asset)
    monkeypatch.setattr(m, "immich_bulk_check", bulk)
    return state


def _duplicate_verdict(asset_id="server-asset"):
    return {"*": {"action": "reject", "reason": "duplicate", "assetId": asset_id}}


# ---- validation order ---------------------------------------------------------------------------

def test_unknown_token_is_rejected(client, fake_immich):
    assert _upload(client, "nope").status_code == 403
    assert fake_immich["upload"] == 0


def test_valid_invite_uploads_and_counts_use(client, fake_immich):
    make_invite("t-ok")
    r = _upload(client, "t-ok")
    assert r.status_code == 200 and r.json()["status"] == "created"
    conn = db.connect()
    used = conn.execute("SELECT used_count FROM invites WHERE token='t-ok'").fetchone()[0]
    conn.close()
    assert used == 1


def test_expired_disabled_exhausted_links_refuse_uploads(client, fake_immich):
    make_invite("t-exp", expires_at="2000-01-01T00:00:00")
    make_invite("t-dis", disabled=1)
    make_invite("t-max", max_uses=2, used_count=2)
    for tok in ("t-exp", "t-dis", "t-max"):
        assert _upload(client, tok).status_code == 403, tok
    assert fake_immich["upload"] == 0


def test_disabled_link_does_not_answer_duplicate_queries(client, fake_immich):
    make_invite("t-dis2", disabled=1)
    content = b"known-file" * 10
    conn = db.connect()
    conn.execute("INSERT INTO uploads (owner_id, checksum, filename, size, device_asset_id) VALUES ('', ?, 'x.jpg', 100, 'd')", (_sha1(content),))
    conn.commit()
    conn.close()
    fake_immich["bulk_result"] = _duplicate_verdict()
    r = _upload(client, "t-dis2", content=content)
    assert r.status_code == 403 and fake_immich["bulk"] == 0


def test_bogus_token_does_no_work_before_validation(client, fake_immich, monkeypatch):
    hashed = []
    real = m.immich_client.sha1_file
    monkeypatch.setattr(m.immich_client, "sha1_file", lambda f: hashed.append(1) or real(f))
    assert _upload(client, "not-a-token", content=b"Z" * 5000).status_code == 403
    assert hashed == []


def test_guests_must_send_the_token_as_a_header(client, fake_immich):
    make_invite("t-form")
    r = _upload(client, "t-form", header=False)
    assert r.status_code == 401 and fake_immich["upload"] == 0


def test_token_header_is_checked_before_the_body_is_read(client, fake_immich, monkeypatch):
    parsed = []
    real = m._read_form

    async def spy(request):
        parsed.append(1)
        return await real(request)

    monkeypatch.setattr(m, "_read_form", spy)
    assert _upload(client, "not-a-token", header=True).status_code == 403
    assert client.post("/api/upload", data={"item_id": "a", "session_id": "b"}, files={"file": ("a.jpg", b"x")}).status_code == 401
    assert parsed == []
    make_invite("t-h")
    assert _upload(client, "t-h", header=True).status_code == 200 and parsed == [1]


def test_upload_size_limit_is_enforced_before_reading(client, fake_immich, monkeypatch):
    monkeypatch.setattr(m.SETTINGS, "max_upload_size_mb", 1)
    make_invite("t-big")
    r = _upload(client, "t-big", content=b"x" * (3 * 1024 * 1024), header=True)
    assert r.status_code == 413 and fake_immich["upload"] == 0
    assert _upload(client, "t-big", content=b"x" * 1000, header=True).status_code == 200


# ---- one-time and multi-use links --------------------------------------------------------------

def test_one_time_link_is_claimed_by_first_session_only(client, fake_immich):
    make_invite("t-once", max_uses=1)
    assert _upload(client, "t-once", item="a1").status_code == 200
    r = _upload(client, "t-once", content=b"B" * 64, item="a2", session="other")
    assert r.status_code == 403 and r.json()["error"] == "invite_claimed"


def test_failed_upload_does_not_burn_a_one_time_link(client, fake_immich):
    make_invite("t-burn", max_uses=1)
    fake_immich["upload_ok"] = False
    assert _upload(client, "t-burn", session="first").status_code == 400
    fake_immich["upload_ok"] = True
    assert _upload(client, "t-burn", session="second", item="a2").status_code == 200


def test_failed_upload_does_not_use_up_a_multi_use_link(client, fake_immich):
    make_invite("t-multi", max_uses=1 + 1)
    fake_immich["upload_ok"] = False
    _upload(client, "t-multi")
    conn = db.connect()
    assert conn.execute("SELECT used_count FROM invites WHERE token='t-multi'").fetchone()[0] == 0
    conn.close()


def test_multi_use_link_can_not_be_over_used(client, fake_immich):
    make_invite("t-two", max_uses=2)
    assert [m.reserve_invite_use("t-two") for _ in range(3)] == [True, True, False]
    m.release_invite_use("t-two", one_time=False)
    assert m.reserve_invite_use("t-two") is True


def test_password_link_needs_session_authorisation_to_upload(client, fake_immich, login):
    r = login().post("/api/invites", json={"password": "pw", "maxUses": -1})
    token = r.json()["token"]
    client.cookies.clear()
    assert _upload(client, token).json()["error"] == "invite_password_required"


# ---- duplicates ---------------------------------------------------------------------------------

def test_different_files_with_same_name_size_mtime_are_both_uploaded(client, fake_immich):
    make_invite("t-dup")
    assert _upload(client, "t-dup", content=b"A" * 64, item="a1").json()["status"] == "created"
    second = _upload(client, "t-dup", content=b"B" * 64, item="a2")
    assert second.json()["status"] == "created" and fake_immich["upload"] == 2


def test_immich_duplicate_is_reported_but_guests_do_not_get_the_asset_id(client, login, fake_immich):
    make_invite("t-guest")
    fake_immich["bulk_result"] = _duplicate_verdict("secret-asset")
    r = _upload(client, "t-guest")
    assert r.json() == {"status": "duplicate", "id": None}
    login("user-a")
    client.headers.pop("X-Invite-Token", None)
    r = client.post("/api/upload", data={"item_id": "o1", "session_id": "s2"}, files={"file": ("a.jpg", b"A" * 64, "image/jpeg")})
    assert r.json()["id"] == "secret-asset"


def test_stale_cache_entry_does_not_block_an_upload_immich_accepts(client, login, fake_immich):
    login("user-a")
    content = b"C" * 64
    conn = db.connect()
    conn.execute("INSERT INTO uploads (owner_id, checksum, filename, size, device_asset_id) VALUES ('user-a', ?, 'c.jpg', 64, 'x')", (_sha1(content),))
    conn.commit()
    conn.close()
    fake_immich["bulk_result"] = {"*": {"action": "accept"}}
    r = client.post("/api/upload", data={"item_id": "c1", "session_id": "s1"}, files={"file": ("c.jpg", content, "image/jpeg")})
    assert r.json()["status"] == "created" and fake_immich["upload"] == 1
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM uploads WHERE checksum=?", (_sha1(content),)).fetchone()[0] == 1
    conn.close()


def test_local_cache_is_the_fallback_when_immich_does_not_answer(client, fake_immich):
    make_invite("t-fb")
    content = b"D" * 64
    conn = db.connect()
    conn.execute("INSERT INTO uploads (owner_id, checksum, filename, size, device_asset_id) VALUES ('', ?, 'd.jpg', 64, 'x')", (_sha1(content),))
    conn.commit()
    conn.close()
    assert _upload(client, "t-fb", content=content).json()["status"] == "duplicate" and fake_immich["upload"] == 0


# ---- chunked uploads ---------------------------------------------------------------------------

def _chunk(client, item, idx, total, data, session="ab12", token=None, header=True, name="big.jpg"):
    headers = {"X-Invite-Token": token} if token and header else {}
    form = {"item_id": item, "session_id": session, "chunk_index": str(idx), "total_chunks": str(total)}
    return client.post("/api/upload/chunk", data=form, files={"chunk": (f"{name}.part{idx}", data)}, headers=headers)


def test_chunked_roundtrip_streams_the_assembled_file(client, login, fake_immich):
    login("user-a")
    body = {"item_id": "c1", "session_id": "ab12", "name": "big.jpg", "size": 8, "content_type": "image/jpeg"}
    assert client.post("/api/upload/chunk/init", json=body).status_code == 200
    assert _chunk(client, "c1", 0, 2, b"1234").status_code == 200
    assert _chunk(client, "c1", 1, 2, b"5678").status_code == 200
    r = client.post("/api/upload/chunk/complete", json={**body, "total_chunks": 2})
    assert r.status_code == 200 and r.json()["status"] == "created"


def test_chunk_complete_reports_missing_parts(client, login, fake_immich):
    login("user-a")
    body = {"item_id": "c2", "session_id": "ab12", "name": "big.jpg"}
    client.post("/api/upload/chunk/init", json=body)
    _chunk(client, "c2", 0, 3, b"1234")
    r = client.post("/api/upload/chunk/complete", json={**body, "total_chunks": 3})
    assert r.status_code == 400 and r.json()["error"] == "missing_part"


@pytest.mark.parametrize("idx,total", [(5, 2), (-1, 2), (0, 0), (0, 10**7)])
def test_chunk_indexes_are_bounded(client, login, idx, total):
    login("user-a")
    assert _chunk(client, "c3", idx, total, b"x").status_code == 400


def test_chunk_parts_larger_than_the_chunk_size_are_refused(client, login, monkeypatch):
    login("user-a")
    monkeypatch.setattr(m.SETTINGS, "chunk_size_mb", 1)
    assert _chunk(client, "c4", 0, 1, b"x" * (3 * 1024 * 1024)).status_code == 413


def test_chunks_of_a_password_link_need_the_password_first(client, login):
    r = login().post("/api/invites", json={"password": "pw", "maxUses": -1})
    token = r.json()["token"]
    client.cookies.clear()
    assert client.post("/api/upload/chunk/init", json={"item_id": "c5", "session_id": "ab12", "invite_token": token}).json()["error"] == "invite_password_required"
    assert _chunk(client, "c5", 0, 1, b"x", token=token).status_code == 403


def test_chunks_of_unknown_links_are_refused_without_reading_the_body(client):
    assert _chunk(client, "c6", 0, 1, b"x", token="nope").status_code == 403


def test_second_complete_for_the_same_item_is_refused(client, login, fake_immich):
    login("user-a")
    m._ASSEMBLING.add(("ab12", "c7"))
    try:
        r = client.post("/api/upload/chunk/init", json={"item_id": "c7", "session_id": "ab12"})
        assert r.status_code == 200
        _chunk(client, "c7", 0, 1, b"x")
        assert client.post("/api/upload/chunk/complete", json={"item_id": "c7", "session_id": "ab12", "total_chunks": 1}).status_code == 409
    finally:
        m._ASSEMBLING.discard(("ab12", "c7"))


def test_cancel_needs_login_or_a_real_token(client):
    assert client.post("/api/upload/cancel", json={"session_id": "s", "item_id": "i", "invite_token": "nope"}).status_code == 403
    assert client.post("/api/upload/cancel", json={"session_id": "s", "item_id": "i"}).status_code == 401
    assert m._CANCELLED == {}


# ---- ownership and input handling --------------------------------------------------------------

def test_other_users_cannot_delete_a_links_upload_log(client, login):
    make_invite("t-own", owner="user-a")
    conn = db.connect()
    conn.execute("INSERT INTO upload_events (token, filename) VALUES ('t-own', 'a.jpg')")
    conn.commit()
    conn.close()
    login("user-b")
    assert client.post("/api/invites/delete", json={"tokens": ["t-own"]}).json()["deleted"] == 0
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM upload_events WHERE token='t-own'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM invites WHERE token='t-own'").fetchone()[0] == 1
    conn.close()
    login("user-a")
    assert client.post("/api/invites/delete", json={"tokens": ["t-own"]}).json()["deleted"] == 1
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM upload_events WHERE token='t-own'").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("tokens", ["abc", 5, None, [1, 2], {}, []])
def test_bulk_endpoints_reject_malformed_token_lists(client, login, tokens):
    login()
    assert client.post("/api/invites/delete", json={"tokens": tokens}).status_code == 400
    assert client.post("/api/invites/bulk", json={"tokens": tokens}).status_code == 400


def test_owner_scoping_of_list_update_uploads(client, login):
    make_invite("t-a", owner="user-a")
    login("user-b")
    assert client.get("/api/invites").json()["items"] == []
    assert client.patch("/api/invite/t-a", json={"name": "x"}).status_code == 404
    assert client.get("/api/invite/t-a/uploads").status_code == 403
    client.post("/api/invites/bulk", json={"tokens": ["t-a"], "action": "disable"})
    conn = db.connect()
    assert conn.execute("SELECT disabled FROM invites WHERE token='t-a'").fetchone()[0] in (0, None)
    conn.close()


def test_expiry_is_sent_as_utc(client, login):
    login()
    created = client.post("/api/invites", json={"expiresDays": 3}).json()
    assert created["expiresAt"].endswith("Z")
    assert client.get("/api/invites").json()["items"][0]["expiresAt"].endswith("Z")
    assert client.get(f"/api/invite/{created['token']}").json()["expiresAt"].endswith("Z")
    assert client.patch(f"/api/invite/{created['token']}", json={"expiresAt": created["expiresAt"]}).status_code == 200


@pytest.mark.parametrize("payload", [
    {"maxUses": 0}, {"maxUses": -2}, {"maxUses": "abc"}, {"maxUses": 1.5}, {"maxUses": True}, {"maxUses": 10**9},
    {"expiresDays": 0}, {"expiresDays": 99999}, {"expiresDays": "x"},
])
def test_invite_creation_validates_numbers(client, login, payload):
    login()
    assert client.post("/api/invites", json=payload).status_code == 400


def test_invite_creation_with_unicode_name_and_password(client, login):
    login()
    r = client.post("/api/invites", json={"name": "Ünïcødé 🎉 " + "x" * 200, "password": "pässwörd", "maxUses": 3})
    assert r.status_code == 200 and len(r.json()["name"]) == 120
    token = r.json()["token"]
    assert client.post(f"/api/invite/{token}/auth", json={"password": "wrong"}).status_code == 403
    assert client.post(f"/api/invite/{token}/auth", json={"password": "pässwörd"}).json()["authorized"] is True


def test_invite_password_guessing_is_rate_limited(client, login):
    token = login().post("/api/invites", json={"password": "pw", "maxUses": -1}).json()["token"]
    codes = [client.post(f"/api/invite/{token}/auth", json={"password": f"guess{i}"}).status_code for i in range(12)]
    assert codes[:10] == [403] * 10 and codes[10:] == [429, 429]
    assert client.post(f"/api/invite/{token}/auth", json={"password": "pw"}).status_code == 429


def test_invite_requires_an_api_key(client, login, monkeypatch):
    monkeypatch.setattr(m.SETTINGS, "immich_api_key", "")
    assert login().post("/api/invites", json={}).json()["error"] == "no_api_key"
