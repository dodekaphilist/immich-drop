"""URL download pipeline (yt-dlp / gallery-dl are replaced by a stub; nothing touches the network)."""
import asyncio
import json
import os
import time

import pytest

from app import app as m
from app import api_routes, job_manager, url_downloader as ud


class _Proc:
    def __init__(self, out=b"", err=b"", rc=0):
        self._out, self._err, self.returncode = out, err, rc
        self.pid = 1

    async def communicate(self):
        return self._out, self._err


@pytest.fixture()
def stub_tools(monkeypatch):
    """Records every command; yt-dlp 'downloads' one file, gallery-dl fails."""
    calls = []
    meta = {"id": "vid1", "ext": "mp4", "uploader": "someone", "title": "A title"}

    async def fake_exec(*cmd, **kw):
        calls.append(list(cmd))
        if cmd[0] == "yt-dlp":
            out_dir = os.path.dirname(cmd[cmd.index("-o") + 1])
            with open(os.path.join(out_dir, "vid1.mp4"), "wb") as f:
                f.write(b"\x00" * 20000)
            return _Proc(out=json.dumps(meta).encode() + b"\n")
        return _Proc(rc=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls, meta


def run(coro):
    return asyncio.run(coro)


# ---- URL vetting -------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "--batch-file=/etc/passwd", "-o /tmp/x", "https://example.com/a b", "https://example.com/\n--exec", "",
    "ftp://example.com/x", "file:///etc/passwd", "http://127.0.0.1/x", "http://169.254.169.254/latest",
    "http://[::1]/x", "http://10.0.0.5/x", "https://example.com/" + "a" * 3000,
])
def test_prepare_url_refuses_unsafe_input(bad):
    url, err = ud.prepare_url(bad)
    assert url is None and err


def test_prepare_url_adds_scheme_and_keeps_good_urls():
    assert ud.prepare_url("example.com/video/1") == ("https://example.com/video/1", None)
    assert ud.prepare_url(" https://example.com/a?b=c ") == ("https://example.com/a?b=c", None)


def test_url_that_looks_like_an_option_is_never_passed_to_the_tools(stub_tools, tmp_path):
    calls, _ = stub_tools
    res = run(ud.download_from_url_multi("--batch-file=/etc/passwd", str(tmp_path)))
    assert not res[0].success and calls == []


def test_url_is_always_after_the_option_separator(stub_tools, tmp_path):
    calls, _ = stub_tools
    res = run(ud.download_from_url_multi("https://example.com/watch/1", str(tmp_path)))
    assert res[0].success
    assert calls[0][-2:] == ["--", "https://example.com/watch/1"]


def test_internal_addresses_are_not_handed_to_the_tools(stub_tools, tmp_path):
    calls, _ = stub_tools
    for url in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:2283/api/server/ping", "http://[::1]/"):
        assert not run(ud.download_from_url_multi(url, str(tmp_path)))[0].success
    assert calls == []


def test_reddit_media_wrapper_cannot_smuggle_an_internal_url(stub_tools, tmp_path):
    calls, _ = stub_tools
    res = run(ud.download_from_url_multi("https://www.reddit.com/media?url=http%3A%2F%2F10.0.0.1%2Fx", str(tmp_path)))
    assert not res[0].success and calls == []


def test_direct_image_blocks_private_targets():
    res = run(ud.download_direct_image("http://127.0.0.1/a.jpg"))
    assert not res.success and "blocked" in res.error.lower()


def test_direct_image_blocks_redirect_to_private_address(monkeypatch):
    import httpx

    def handler(request):
        if request.url.path == "/a.jpg":
            return httpx.Response(302, headers={"location": "/next"})
        return httpx.Response(302, headers={"location": "http://10.1.2.3/secret.jpg"})

    real = httpx.AsyncClient
    monkeypatch.setattr(ud.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    res = run(ud.download_direct_image("http://cdn.example.com/a.jpg"))
    assert not res.success and "blocked" in res.error.lower()


def test_direct_image_enforces_size_while_streaming(monkeypatch):
    import httpx

    monkeypatch.setattr(ud, "MAX_DIRECT_IMAGE_SIZE", 1000)
    handler = lambda request: httpx.Response(200, content=b"\xff\xd8\xff" + b"0" * 5000)  # noqa: E731
    real = httpx.AsyncClient
    monkeypatch.setattr(ud.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    res = run(ud.download_direct_image("http://cdn.example.com/a.jpg"))
    assert not res.success and "too large" in res.error.lower()


def test_direct_image_happy_path(monkeypatch, tmp_path):
    import httpx

    handler = lambda request: httpx.Response(200, content=b"\xff\xd8\xff" + b"0" * 200)  # noqa: E731
    real = httpx.AsyncClient
    monkeypatch.setattr(ud.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    res = run(ud.download_direct_image("http://cdn.example.com/a.jpg", str(tmp_path)))
    assert res.success and res.filename.endswith(".jpg") and os.path.getsize(res.filepath) == 203


# ---- metadata and file names -----------------------------------------------------------------

def test_null_uploader_and_title_do_not_fail_the_download(stub_tools, tmp_path):
    _, meta = stub_tools
    meta.update(uploader=None, title=None)
    res = run(ud.download_from_url_multi("https://example.com/watch/1", str(tmp_path)))
    assert res[0].success, res[0].error


def test_filename_has_no_none_prefix(stub_tools, tmp_path):
    res = run(ud.download_from_url_multi("https://example.com/watch/1", str(tmp_path)))
    assert res[0].success and res[0].filename.startswith("media_")


def test_remote_id_cannot_escape_the_work_folder(stub_tools, tmp_path):
    _, meta = stub_tools
    meta["id"] = "../../evil"
    res = run(ud.download_from_url_multi("https://example.com/watch/1", str(tmp_path)))
    assert res[0].success and os.path.dirname(res[0].filepath) == str(tmp_path)


def test_leftovers_of_a_failed_gallery_dl_run_are_not_picked_up(monkeypatch, tmp_path):
    meta = {"id": "v", "ext": "mp4"}

    async def fake_exec(*cmd, **kw):
        out_dir = str(tmp_path)
        if cmd[0] == "gallery-dl":
            os.makedirs(os.path.join(out_dir, "site", "user"))
            with open(os.path.join(out_dir, "site", "user", "a.jpg.json"), "w") as f:
                f.write("{}")
            return _Proc(rc=1)
        with open(os.path.join(out_dir, "v.mp4"), "wb") as f:
            f.write(b"\x00" * 20000)
        return _Proc(out=json.dumps(meta).encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    res = run(ud.download_from_url_multi("https://www.reddit.com/r/x/comments/abc/t", str(tmp_path)))
    assert res[0].success and res[0].filepath.endswith(".mp4")


@pytest.mark.parametrize("value,expected", [
    ("2021-03-04 05:06:07", 1614834367.0), (1614834367, 1614834367.0), ("2021-03-04T05:06:07Z", 1614834367.0),
    ("garbage", None), (None, None), ("", None), (True, None), (-5, None),
])
def test_gallery_dl_dates_become_timestamps(value, expected):
    assert ud._gallery_timestamp(value) == expected


# ---- jobs --------------------------------------------------------------------------------------

def test_running_job_survives_cleanup():
    job = job_manager.create_job("https://example.com/x", user_id="u")
    job_manager.update_job(job.id, status="downloading")
    job.created_at = time.time() - job_manager.JOB_TTL_SECONDS - 5
    job_manager._last_cleanup = 0.0
    job_manager.cleanup_expired()
    assert job_manager.get_job(job.id) is not None


def test_finished_job_expires_after_the_ttl():
    job = job_manager.create_job("https://example.com/y", user_id="u")
    job_manager.update_job(job.id, status="completed")
    job.finished_at = time.time() - job_manager.JOB_TTL_SECONDS - 5
    job_manager._last_cleanup = 0.0
    assert job_manager.cleanup_expired() >= 1
    assert job_manager.get_job(job.id) is None


def test_job_ids_are_long_and_private_to_their_user():
    job = job_manager.create_job("https://example.com/z", user_id="alice")
    assert len(job.id) >= 16
    assert job_manager.get_job(job.id, "alice") is job
    assert job_manager.get_job(job.id, "bob") is None


def test_job_status_is_not_visible_to_other_users(client, login):
    job = job_manager.create_job("https://example.com/z", user_id="alice")
    login("alice")
    assert client.get(f"/api/upload/url/status/{job.id}").status_code == 200
    login("bob")
    assert client.get(f"/api/upload/url/status/{job.id}").status_code == 404


def test_url_job_rejects_bad_urls_and_caps_parallel_jobs(client, login, monkeypatch):
    login("alice")
    assert client.post("/api/upload/url", json={"url": "--batch-file=/etc/passwd"}).status_code == 400
    assert client.post("/api/upload/url", json={"url": "http://127.0.0.1/x"}).status_code == 400

    async def hang(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(api_routes, "download_from_url_multi", hang)
    codes = [client.post("/api/upload/url", json={"url": f"https://example.com/{i}"}).status_code for i in range(job_manager.MAX_ACTIVE_JOBS_PER_USER + 1)]
    assert codes[:-1] == [200] * job_manager.MAX_ACTIVE_JOBS_PER_USER and codes[-1] == 429


def _wait_for(client, job_id, states=("completed", "failed")):
    for _ in range(100):
        body = client.get(f"/api/upload/url/status/{job_id}").json()
        if body["status"] in states:
            return body
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_work_folder_is_removed_after_a_failed_job(client, login, monkeypatch):
    seen = []

    async def failing(url, output_dir=None, cookies_file=None, settings=None):
        seen.append(output_dir)
        os.makedirs(os.path.join(output_dir, "nested"))
        open(os.path.join(output_dir, "nested", "x.json"), "w").close()
        return [ud.DownloadResult(success=False, error="nope")]

    monkeypatch.setattr(api_routes, "download_from_url_multi", failing)
    login("alice")
    job_id = client.post("/api/upload/url", json={"url": "https://example.com/v"}).json()["job_id"]
    assert _wait_for(client, job_id)["status"] == "failed"
    assert seen and not os.path.exists(seen[0])


def test_job_fails_when_every_upload_fails(client, login, monkeypatch):
    async def ok(url, output_dir=None, cookies_file=None, settings=None):
        path = os.path.join(output_dir, "a.jpg")
        open(path, "wb").write(b"\xff\xd8\xff" + b"0" * 50)
        return [ud.DownloadResult(success=True, filepath=path, filename="a.jpg", content_type="image/jpeg", metadata={})]

    async def boom(*a, **k):
        return m.immich_client.UploadOutcome(ok=False, status_code=500, error="immich down")

    monkeypatch.setattr(api_routes, "download_from_url_multi", ok)
    monkeypatch.setattr(m.immich_client, "upload_asset", boom)
    login("alice")
    job_id = client.post("/api/upload/url", json={"url": "https://example.com/v"}).json()["job_id"]
    body = _wait_for(client, job_id)
    assert body["status"] == "failed" and "immich down" in body["error"]


@pytest.mark.parametrize("url,platform", [
    ("https://www.instagram.com/reel/ABC123/", "instagram"),
    ("https://x.com/user/status/123", "twitter"),
    ("https://youtu.be/abc", "youtube"),
    ("https://example.com/whatever", None),
    ("https://instagram.com.evil.test/p/abc", None),
    ("https://evil.test/?u=https://www.instagram.com/p/abc", None),
])
def test_platform_detection_does_not_match_lookalike_hosts(url, platform):
    assert ud.identify_platform(url) == platform


def test_platform_cookie_names_cover_every_gallery_platform():
    from app.cookie_manager import PLATFORM_DOMAINS

    # artstation is supported for downloads but has no cookie domain
    assert set(ud.SUPPORTED_PATTERNS) - set(PLATFORM_DOMAINS) == {"artstation"}
