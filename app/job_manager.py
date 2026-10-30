"""In-memory job store for async URL uploads."""
import secrets
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Any

logger = logging.getLogger("immich_drop.job_manager")

JOB_TTL_SECONDS = 600  # how long a finished job stays pollable
JOB_MAX_RUNTIME_SECONDS = 2 * 60 * 60  # safety net for a job that never reports back
MAX_ACTIVE_JOBS_PER_USER = 5
_CLEANUP_INTERVAL = 60  # seconds between cleanup sweeps
_last_cleanup: float = 0.0
_FINISHED = ("completed", "failed")


@dataclass
class Job:
    id: str
    url: str
    album_name: Optional[str] = None
    user_id: str = ""
    status: str = "pending"  # pending, downloading, uploading, completed, failed
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    result: Optional[Any] = None
    error: Optional[str] = None


_jobs: dict[str, Job] = {}


def create_job(url: str, album_name: Optional[str] = None, user_id: str = "") -> Job:
    job_id = secrets.token_urlsafe(12)
    job = Job(id=job_id, url=url, album_name=album_name, user_id=user_id)
    _jobs[job_id] = job
    logger.info("Created job %s for URL %s", job_id, url)
    return job


def get_job(job_id: str, user_id: Optional[str] = None) -> Optional[Job]:
    """The job; with user_id given, only if it belongs to that user."""
    job = _jobs.get(job_id)
    if job is not None and user_id is not None and job.user_id != user_id:
        return None
    return job


def active_jobs(user_id: str) -> int:
    """Number of unfinished jobs of one user."""
    return sum(1 for j in _jobs.values() if j.user_id == user_id and j.status not in _FINISHED)


def update_job(
    job_id: str,
    status: Optional[str] = None,
    result: Optional[Any] = None,
    error: Optional[str] = None,
) -> None:
    job = _jobs.get(job_id)
    if not job:
        return
    if status:
        job.status = status
        if status in _FINISHED:
            job.finished_at = time.time()
    if result is not None:
        job.result = result
    if error is not None:
        job.error = error


def cleanup_expired() -> int:
    """Drop finished jobs a while after they finished (and jobs that hung for hours)."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return 0
    _last_cleanup = now
    expired = [
        jid for jid, j in _jobs.items()
        if (j.finished_at is not None and now - j.finished_at > JOB_TTL_SECONDS)
        or now - j.created_at > JOB_MAX_RUNTIME_SECONDS
    ]
    for jid in expired:
        del _jobs[jid]
    if expired:
        logger.debug("Cleaned up %d expired jobs", len(expired))
    return len(expired)
