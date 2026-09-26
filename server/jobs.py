"""In-memory job registry: one thread per download, dict-based progress.

Single-worker friendly (Render free tier / this preview). Swap this module for
Redis + RQ/Celery when you scale beyond one instance.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import yt_dlp

from core import (
    DOWNLOAD_DIR,
    MAX_CONCURRENT_JOBS,
    base_opts,
    extract_with_fallback,
    format_for,
    is_cookie_or_auth_required_error,
    postprocessors_for,
)

_lock = threading.Lock()
_jobs: Dict[str, dict] = {}


class JobError(Exception):
    pass


def create_job(url: str, mode: str, quality: str, container: Optional[str]) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "url": url,
        "mode": mode,  # "audio" | "video"
        "quality": quality,
        "container": container or ("mp3" if mode == "audio" else "mp4"),
        "status": "queued",  # queued -> downloading -> processing -> completed | error | canceled
        "progress": 0.0,
        "speed": None,
        "eta": None,
        "title": None,
        "filename": None,
        "error": None,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "cancel": threading.Event(),
        "thread": None,
    }
    with _lock:
        _jobs[job_id] = job
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    with _lock:
        return _jobs.get(job_id)


def all_jobs() -> Dict[str, dict]:
    with _lock:
        return dict(_jobs)


def cancel_job(job_id: str) -> bool:
    job = get_job(job_id)
    if not job:
        return False
    job["cancel"].set()
    return True


def delete_job(job_id: str) -> bool:
    with _lock:
        job = _jobs.pop(job_id, None)
    return job is not None


def active_count() -> int:
    return sum(
        1
        for j in all_jobs().values()
        if j["status"] in {"queued", "downloading", "processing"}
    )


def _running_jobs() -> int:
    return active_count()


def run_job(job_id: str) -> None:
    """Execute a download synchronously; call from a worker thread."""
    job = get_job(job_id)
    if not job:
        return
    job["status"] = "downloading"
    job["started_at"] = time.time()

    def hook(d: dict) -> None:
        if job["cancel"].is_set():
            raise yt_dlp.utils.DownloadCancelled("canceled by user")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            got = d.get("downloaded_bytes")
            if total and got is not None:
                job["progress"] = round(min(got / total, 1.0) * 100.0, 1)
            job["speed"] = d.get("speed")
            job["eta"] = d.get("eta")
            if not job["title"]:
                job["title"] = d.get("info_dict", {}).get("title")
        elif d.get("status") == "finished":
            job["progress"] = 100.0

    opts = base_opts([])
    opts["progress_hooks"] = [hook]
    opts["format"] = format_for(job["quality"], job["container"])
    if job["mode"] == "audio":
        opts["postprocessors"] = postprocessors_for("audio", job["container"])
    else:
        opts["merge_output_format"] = job["container"] if job["container"] != "best" else "mp4"

    try:
        info = extract_with_fallback(job["url"], opts, job=job)
        if info is None:
            raise JobError("No media found at that URL")
        job["title"] = info.get("title") or job["title"]
        requested = info.get("requested_downloads") or []
        if requested:
            filepath = requested[0].get("filepath") or requested[0].get("_filename")
        else:
            # Fallback: yt-dlp sometimes omits requested_downloads; find the
            # newest file in DOWNLOAD_DIR containing this video's id (the
            # outtmpl always embeds " [<id>]" before the extension).
            vid = info.get("id")
            marker = f"[{vid}]"
            candidates = (
                [p for p in Path(DOWNLOAD_DIR).iterdir() if p.is_file() and marker in p.name]
                if vid else []
            )
            filepath = str(max(candidates, key=lambda p: p.stat().st_mtime)) if candidates else None
        if filepath:
            job["filename"] = os.path.basename(filepath)
        job["status"] = "completed"
        job["progress"] = 100.0
    except yt_dlp.utils.DownloadCancelled:
        job["status"] = "canceled"
    except Exception as exc:  # yt-dlp raises many exception types
        job["status"] = "error"
        job["error"] = yt_dlp_error_text(exc)
        job["requires_cookies"] = bool(is_cookie_or_auth_required_error(exc))
    finally:
        job["finished_at"] = time.time()
        job["speed"] = None
        job["eta"] = None


def start_job(job_id: str) -> bool:
    job = get_job(job_id)
    if not job:
        return False
    if active_count() >= MAX_CONCURRENT_JOBS:
        raise JobError(f"Server busy: {MAX_CONCURRENT_JOBS} concurrent downloads max")
    if job["status"] not in {"queued", "error", "canceled"}:
        return False
    t = threading.Thread(target=run_job, args=(job_id,), daemon=True)
    job["thread"] = t
    t.start()
    return True
