"""Which endpoints answer without a login. Anything not on the public list must refuse anonymous callers."""
import pytest

# Public on purpose: login flow, invite guests (token is the credential) and read-only status.
PUBLIC = {
    ("GET", "/"), ("GET", "/login"), ("POST", "/api/logout"),
    ("GET", "/api/config"), ("POST", "/api/ping"), ("GET", "/api/auth/options"), ("POST", "/api/login"),
    ("GET", "/oauth/start"), ("GET", "/oauth/callback"),
    ("GET", "/api/invite/{token}"), ("POST", "/api/invite/{token}/auth"), ("GET", "/invite/{token}"),
    ("POST", "/api/upload"), ("POST", "/api/upload/cancel"),
    ("POST", "/api/upload/chunk/init"), ("POST", "/api/upload/chunk"), ("POST", "/api/upload/chunk/complete"),
    ("GET", "/favicon.ico"),
}


def _routes(app):
    for r in app.routes:
        path = getattr(r, "path", "")
        for method in sorted(getattr(r, "methods", None) or []):
            if method in ("HEAD", "OPTIONS"):
                continue
            if path.startswith(("/static", "/_app", "/docs", "/redoc", "/openapi")) or path == "/ws":
                continue
            yield method, path


def test_every_non_public_endpoint_rejects_anonymous_callers(client, app):
    leaked = []
    for method, path in _routes(app):
        if (method, path) in PUBLIC:
            continue
        url = path.replace("{token}", "x").replace("{platform}", "instagram").replace("{job_id}", "abc")
        r = client.request(method, url, json={}, follow_redirects=False)
        if r.status_code not in (401, 403):
            leaked.append((method, path, r.status_code))
    assert leaked == []


def test_public_list_is_not_stale(app):
    existing = {(m, p) for m, p in _routes(app)}
    assert PUBLIC - existing == set(), "PUBLIC names endpoints that no longer exist"


def test_upload_needs_token_or_login(client):
    r = client.post("/api/upload", data={"item_id": "a1", "session_id": "b2"}, files={"file": ("a.jpg", b"x" * 20)})
    assert r.status_code == 401


def test_owner_pages_redirect_anonymous_users_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"].endswith("/login")


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "a b", "", "x" * 80, "g-ZZ"])
def test_chunk_dir_rejects_path_tricks(bad):
    from fastapi import HTTPException

    from app import app as m

    with pytest.raises(HTTPException):
        m._chunk_dir(bad, "abcd")
    with pytest.raises(HTTPException):
        m._chunk_dir("abcd", bad)
