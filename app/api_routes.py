"""
API Routes for immich-drop extensions
- URL download and upload to Immich
- Optional batch/file/base64 upload for iOS Shortcuts (SHORTCUT_ENABLED)
"""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime, timezone
import asyncio
import base64
import hashlib
import httpx
import io
import logging
import mimetypes
import os
import re
import tempfile
import uuid

logger = logging.getLogger("immich_drop.api_routes")

from . import immich_client
from .url_downloader import (
    download_from_url_multi,
    download_multiple_urls,
    cleanup_workdir,
    identify_platform,
    is_direct_image_url,
    prepare_url,
    SUPPORTED_PATTERNS,
)
from . import db
from .cookie_manager import get_cookie_file_for_platform
from .job_manager import MAX_ACTIVE_JOBS_PER_USER, active_jobs, create_job, get_job, update_job
from .utils import detect_file_type


# ============================================================================
# Request/Response Models
# ============================================================================

class UrlUploadRequest(BaseModel):
    url: str
    album_name: Optional[str] = None


class UrlBatchUploadRequest(BaseModel):
    urls: List[str]
    album_name: Optional[str] = None


class Base64UploadRequest(BaseModel):
    """Request model for base64-encoded file upload (iOS Shortcuts compatible)"""
    data: str  # base64-encoded file content
    filename: Optional[str] = None
    album_name: Optional[str] = None


class UploadResult(BaseModel):
    filename: str
    status: str  # "success", "error", "duplicate"
    asset_id: Optional[str] = None
    duplicate: bool = False
    error: Optional[str] = None
    platform: Optional[str] = None


class UrlUploadResponse(BaseModel):
    success: bool
    result: Optional[UploadResult] = None
    error: Optional[str] = None
    total_uploaded: int = 0
    additional_results: List[UploadResult] = []


class BatchUploadResponse(BaseModel):
    total: int
    successful: int
    duplicates: int
    failed: int
    results: List[UploadResult]


class SupportedPlatformsResponse(BaseModel):
    platforms: List[str]
    examples: dict


class JobResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    done: str = "0"  # "0" = in progress, "1" = completed, "2" = failed (string for iOS Shortcuts compatibility)
    created_at: float
    result: Optional[dict] = None
    error: Optional[str] = None


# ============================================================================
# Helper Functions
# ============================================================================

def _created_at(metadata: Optional[dict]) -> Optional[datetime]:
    """Creation time from a download's metadata timestamp; None if missing or out of range."""
    timestamp = (metadata or {}).get("timestamp")
    if not timestamp:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# Background tasks are only weakly referenced by the event loop: keep them until they finish
_background_tasks: set = set()


async def upload_to_immich(
    file,  # seekable binary file; read in blocks, never loaded whole
    filename: str,
    content_type: str,
    config,  # Config object from main app
    httpx_client: httpx.AsyncClient,  # Shared httpx client
    file_created_at: Optional[datetime] = None,
    api_key: Optional[str] = None,  # defaults to the server key
) -> UploadResult:
    """Upload a file to Immich server"""
    sha1 = await asyncio.to_thread(immich_client.sha1_file, file)
    size = file.seek(0, os.SEEK_END)
    file.seek(0)
    headers = {"x-api-key": api_key or config.immich_api_key}
    outcome = await immich_client.upload_asset(
        httpx_client,
        config.normalized_base_url,
        headers,
        file=file,
        size=size,
        filename=filename,
        content_type=content_type,
        checksum=sha1,
        created_at=file_created_at,
        modified_at=file_created_at,
        timeout=300.0,
    )
    if outcome.ok:
        return UploadResult(
            filename=filename,
            status="success",
            asset_id=outcome.asset_id,
            duplicate=outcome.status == "duplicate",
        )
    return UploadResult(
        filename=filename,
        status="error",
        error=f"Immich returned {outcome.status_code}: {outcome.error}" if outcome.status_code else (outcome.error or "upload failed"),
    )


async def add_asset_to_album(
    asset_id: str,
    album_name: str,
    config,
    httpx_client: httpx.AsyncClient,  # Shared httpx client
    api_key: Optional[str] = None,  # defaults to the server key
    owner_id: str = "",
) -> bool:
    """Add an asset to an album (creates album if needed)"""
    headers = {"x-api-key": api_key or config.immich_api_key}
    album_id = await immich_client.find_or_create_album(
        httpx_client, config.normalized_base_url, headers, album_name, owner_id=owner_id
    )
    if not album_id:
        return False
    return await immich_client.add_to_album(
        httpx_client, config.normalized_base_url, headers, album_id, asset_id
    )


# ============================================================================
# API Endpoints
# ============================================================================

def create_api_routes(config, key_for_user=lambda user_id: None):
    """Factory function to create routes with config injection"""

    def require_access(request: Request) -> None:
        """Logged-in session, or (only when SHORTCUT_ENABLED is set) a user's shortcut token.

        These routes run with Immich credentials, so they are never anonymous. The user is kept in request.state.
        """
        if request.session.get("accessToken"):
            request.state.user_id = str(request.session.get("userId") or "")
            return
        if config.shortcut_enabled:
            auth = request.headers.get("authorization", "")
            supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if supplied:
                conn = db.connect()
                row = conn.execute(
                    "SELECT user_id FROM shortcut_tokens WHERE token_hash = ?",
                    (hashlib.sha256(supplied.encode("utf-8")).hexdigest(),),
                ).fetchone()
                conn.close()
                if row:
                    request.state.user_id = row[0]
                    return
        raise HTTPException(status_code=401, detail="Login required")

    def key_and_user(request: Request) -> tuple[str, str]:
        """The caller's API key and user id (cookies are per user); 409 if there is no key."""
        user_id = request.state.user_id
        api_key = key_for_user(user_id)
        if not api_key:
            raise HTTPException(status_code=409, detail="no_api_key")
        return api_key, user_id

    router = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(require_access)])
    # Batch/file/base64 endpoints for the iOS Shortcut and scripts; only mounted when enabled.
    shortcut_router = APIRouter(tags=["shortcut"])

    @router.get("/supported-platforms", response_model=SupportedPlatformsResponse)
    async def get_supported_platforms():
        """Advisory list of platforms with dedicated routing/cookie handling.
        Any URL is accepted by /upload/url; this list is examples only."""
        return SupportedPlatformsResponse(
            platforms=list(SUPPORTED_PATTERNS.keys()),
            examples={
                "tiktok": "https://www.tiktok.com/@user/video/123456",
                "instagram": "https://www.instagram.com/reel/ABC123/",
                "reddit": "https://www.reddit.com/r/subreddit/comments/abc123/title",
                "youtube": "https://www.youtube.com/shorts/ABC123",
                "twitter": "https://twitter.com/user/status/123456789",
                "facebook": "https://www.facebook.com/reel/123456789",
            },
        )

    @router.post("/upload/url", response_model=JobResponse)
    async def upload_from_url(
        url_request: UrlUploadRequest,
        request: Request,
    ):
        """
        Start async download from a supported URL.
        Returns a job ID immediately. Poll /api/upload/url/status/{job_id} for results.
        """
        if not config.social_media_uploads:
            raise HTTPException(status_code=403, detail="Downloads from platforms are disabled")
        api_key, user_id = key_and_user(request)
        if not url_request.url.strip():
            raise HTTPException(status_code=400, detail="URL is required")
        # Scheme, option-looking input and private targets are refused here, before a job exists
        url, url_error = await asyncio.to_thread(prepare_url, url_request.url)
        if url_error:
            raise HTTPException(status_code=400, detail=url_error)
        if active_jobs(user_id) >= MAX_ACTIVE_JOBS_PER_USER:
            raise HTTPException(status_code=429, detail="Too many downloads at once")

        platform = identify_platform(url)
        direct_image = is_direct_image_url(url)
        logger.info(
            "URL upload request: platform=%s direct_image=%s url=%s",
            platform or "generic", direct_image, url,
        )

        job = create_job(url, url_request.album_name, user_id)
        httpx_client = request.app.state.httpx_client

        task = asyncio.create_task(_process_url_job(job.id, url, url_request.album_name, platform, httpx_client, api_key, user_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        return JobResponse(job_id=job.id, status=job.status)

    def _check_size(size: int) -> None:
        limit = config.max_upload_size_mb * 1024 * 1024
        if limit and size > limit:
            raise HTTPException(status_code=413, detail="file_too_large")

    async def _store(
        file,
        filename: str,
        content_type: str,
        album_name: Optional[str],
        httpx_client,
        api_key: Optional[str],
        user_id: str,
        file_created_at: Optional[datetime] = None,
        platform: Optional[str] = None,
    ) -> UploadResult:
        """Upload one file to Immich and add it to the requested (or default) album."""
        upload_result = await upload_to_immich(
            file=file,
            filename=filename,
            content_type=content_type,
            config=config,
            httpx_client=httpx_client,
            file_created_at=file_created_at,
            api_key=api_key,
        )
        if platform is not None:
            upload_result.platform = platform
        target_album = album_name if album_name is not None else getattr(config, 'album_name', None)
        if target_album and upload_result.asset_id and upload_result.status == "success":
            await add_asset_to_album(upload_result.asset_id, target_album, config, httpx_client, api_key, owner_id=user_id)
        return upload_result

    async def _store_downloads(
        downloads,
        album_name: Optional[str],
        source_label: Optional[str],
        httpx_client,
        api_key: Optional[str],
        user_id: str,
    ) -> List[UploadResult]:
        """Upload every successful download; a failure of one file does not stop the others."""
        results = []
        for dr in downloads:
            label = source_label or (identify_platform((dr.metadata or {}).get("post_url", "")) or (dr.metadata or {}).get("source", "direct_image"))
            try:
                with open(dr.filepath, "rb") as f:
                    logger.info("Uploading to Immich: filename=%s content_type=%s size=%d bytes", dr.filename, dr.content_type, os.fstat(f.fileno()).st_size)
                    results.append(await _store(f, dr.filename, dr.content_type or "application/octet-stream", album_name, httpx_client, api_key, user_id, _created_at(dr.metadata), label))
            except Exception as e:
                logger.exception("Upload of %s failed: %s", dr.filename, e)
                results.append(UploadResult(filename=dr.filename or "unknown", status="error", error=str(e), platform=label))
        return results

    async def _process_url_job(
        job_id: str,
        url: str,
        album_name: Optional[str],
        platform: Optional[str],
        httpx_client,
        api_key: Optional[str] = None,
        user_id: str = "",
    ):
        """Background task: download media from URL and upload to Immich."""
        # One private folder per job; it is removed in the end, whatever happened (sidecars, partial files, ...)
        work_dir = tempfile.mkdtemp(prefix="immich_drop_")
        try:
            update_job(job_id, status="downloading")

            cookies_file = get_cookie_file_for_platform(platform, config.state_db, user_id) if platform else None
            download_results = await download_from_url_multi(url, work_dir, cookies_file=cookies_file, settings=config)

            successful_downloads = [r for r in download_results if r.success]
            if not successful_downloads:
                first_error = download_results[0].error if download_results else "No media found"
                logger.error("Download failed for %s: %s", url, first_error)
                update_job(job_id, status="failed", error=first_error)
                return

            update_job(job_id, status="uploading")
            uploads = await _store_downloads(successful_downloads, album_name, platform or "direct_image", httpx_client, api_key, user_id)
            uploaded = [r for r in uploads if r.status == "success"]
            if not uploaded:
                update_job(job_id, status="failed", error=uploads[0].error or "Upload failed")
                return
            if len(uploaded) > 1:
                logger.info("Gallery upload complete: %d/%d items uploaded for %s", len(uploaded), len(successful_downloads), url)
            primary = uploaded[0]
            response = UrlUploadResponse(
                success=True,
                result=primary,
                error=None,
                total_uploaded=len(uploaded),
                additional_results=[r for r in uploads if r is not primary],
            )
            update_job(job_id, status="completed", result=response.model_dump())

        except Exception as e:
            logger.exception("Job %s failed: %s", job_id, e)
            update_job(job_id, status="failed", error=str(e))
        finally:
            cleanup_workdir(work_dir)

    @router.get("/upload/url/status/{job_id}", response_model=JobStatusResponse)
    async def get_upload_status(job_id: str, request: Request):
        """Poll for the status of an async URL upload job (only its creator can see it)."""
        job = get_job(job_id, request.state.user_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found or expired")
        resp = {
            "job_id": job.id,
            "status": job.status,
            "created_at": job.created_at,
        }
        if job.status == "completed" and job.result is not None:
            resp["result"] = job.result
        if job.status == "failed":
            resp["error"] = job.error or "Unknown error"
        return JSONResponse(resp)

    @shortcut_router.post("/upload/urls", response_model=BatchUploadResponse)
    async def upload_from_urls(
        batch_request: UrlBatchUploadRequest,
        request: Request,
    ):
        """
        Download and upload multiple URLs to Immich

        Max 10 URLs per request
        """
        if not config.social_media_uploads:
            raise HTTPException(status_code=403, detail="Downloads from platforms are disabled")
        api_key, user_id = key_and_user(request)
        urls = [u.strip() for u in batch_request.urls if u.strip()]
        httpx_client = request.app.state.httpx_client

        if not urls:
            raise HTTPException(status_code=400, detail="No URLs provided")

        if len(urls) > 10:
            raise HTTPException(status_code=400, detail="Maximum 10 URLs per request")

        if active_jobs(user_id) >= MAX_ACTIVE_JOBS_PER_USER:
            raise HTTPException(status_code=429, detail="Too many downloads at once")

        # Collect platforms for cookie selection; unknown URLs are allowed through
        # and attempted by yt-dlp / gallery-dl (after the same vetting as single URLs).
        platforms = [identify_platform(u) for u in urls]

        # Look up cookies - if all URLs are from the same platform, use cookies
        cookies_file = None
        unique_platforms = set(p for p in platforms if p is not None)
        if len(unique_platforms) == 1:
            cookies_file = get_cookie_file_for_platform(list(unique_platforms)[0], config.state_db, user_id)

        results = []
        batch_dir = tempfile.mkdtemp(prefix="immich_drop_batch_")
        try:
            download_results = await download_multiple_urls(urls, output_dir=batch_dir, cookies_file=cookies_file, settings=config)

            for download_result in download_results:
                if not download_result.success:
                    post_url = (download_result.metadata or {}).get("post_url", "")
                    results.append(UploadResult(
                        filename=download_result.filename or post_url or "unknown",
                        status="error",
                        error=download_result.error,
                        platform=identify_platform(post_url) if post_url else (download_result.metadata or {}).get("source"),
                    ))
                    continue
                results.extend(await _store_downloads([download_result], batch_request.album_name, None, httpx_client, api_key, user_id))
        finally:
            cleanup_workdir(batch_dir)

        successful = sum(1 for r in results if r.status == "success" and not r.duplicate)
        duplicates = sum(1 for r in results if r.duplicate)
        failed = sum(1 for r in results if r.status == "error")

        return BatchUploadResponse(
            total=len(results),
            successful=successful,
            duplicates=duplicates,
            failed=failed,
            results=results,
        )

    def _upload_name(name: Optional[str]) -> str:
        """File name from the client without control characters; a random one if there is none."""
        cleaned = re.sub(r"[\x00-\x1f\x7f]", "", name or "").strip()
        return cleaned or f"upload_{uuid.uuid4().hex}"

    @shortcut_router.post("/upload/batch", response_model=BatchUploadResponse)
    async def upload_batch_files(
        request: Request,
        files: List[UploadFile] = File(...),
        album_name: Optional[str] = Form(None),
    ):
        """
        Batch upload files - designed for iOS Shortcuts

        POST multipart/form-data with one or more files
        """
        api_key, user_id = key_and_user(request)
        httpx_client = request.app.state.httpx_client

        if not files:
            raise HTTPException(status_code=400, detail="No files provided")

        if len(files) > 50:
            raise HTTPException(status_code=400, detail="Maximum 50 files per request")

        results = []

        for file in files:
            _check_size(file.size or 0)
            results.append(await _store(
                file.file, _upload_name(file.filename), file.content_type or "application/octet-stream",
                album_name, httpx_client, api_key, user_id,
            ))

        successful = sum(1 for r in results if r.status == "success" and not r.duplicate)
        duplicates = sum(1 for r in results if r.duplicate)
        failed = sum(1 for r in results if r.status == "error")

        return BatchUploadResponse(
            total=len(results),
            successful=successful,
            duplicates=duplicates,
            failed=failed,
            results=results,
        )

    @shortcut_router.post("/upload/file", response_model=UploadResult)
    async def upload_single_file(
        request: Request,
        file: UploadFile = File(...),
        album_name: Optional[str] = Form(None),
    ):
        """
        Upload a single file - simpler endpoint for iOS Shortcuts
        """
        api_key, user_id = key_and_user(request)
        httpx_client = request.app.state.httpx_client
        _check_size(file.size or 0)
        return await _store(
            file.file, _upload_name(file.filename), file.content_type or "application/octet-stream",
            album_name, httpx_client, api_key, user_id,
        )

    @shortcut_router.post("/upload/base64", response_model=UploadResult)
    async def upload_base64_file(
        request: Request,
        upload_request: Base64UploadRequest,
    ):
        """
        Upload a base64-encoded file - most reliable for iOS Shortcuts

        JSON body:
        {
            "data": "base64-encoded-file-content",
            "filename": "photo.jpg" (optional),
            "album_name": "Album Name" (optional)
        }
        """
        api_key, user_id = key_and_user(request)
        httpx_client = request.app.state.httpx_client

        # Decode base64 data
        try:
            # Handle data URL format (e.g., "data:image/jpeg;base64,/9j/4AAQ...")
            data_str = upload_request.data
            if data_str.startswith("data:"):
                # Extract the base64 part after the comma
                header, data_str = data_str.split(",", 1)

            file_content = base64.b64decode(data_str)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64 data: {str(e)}")
        _check_size(len(file_content))

        # Detect file type from magic bytes
        detected_ext, detected_mime = detect_file_type(file_content)

        # Determine filename - use provided name or generate one
        base_filename = _upload_name(upload_request.filename)

        # If filename lacks extension or has wrong extension, use detected type
        _, existing_ext = os.path.splitext(base_filename)
        if detected_ext and (not existing_ext or existing_ext.lower() not in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.avif', '.mp4', '.mov', '.bmp', '.tiff']):
            filename = base_filename + detected_ext
        else:
            filename = base_filename if existing_ext else base_filename + (detected_ext or '.jpg')

        # Use detected MIME type or fall back to guessing from filename
        if detected_mime:
            content_type = detected_mime
        else:
            content_type, _ = mimetypes.guess_type(filename)
            content_type = content_type or "application/octet-stream"

        return await _store(io.BytesIO(file_content), filename, content_type, upload_request.album_name, httpx_client, api_key, user_id)

    if config.shortcut_enabled:
        router.include_router(shortcut_router)

    return router
