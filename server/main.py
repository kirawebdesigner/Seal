"""Seal-style backend: FastAPI wrapper around yt-dlp.

Endpoints:
  GET  /health          - service heartbeat (also used by Render health checks)
  POST /download        - start a download job {url, mode, quality, container}
  GET  /status/{id}     - job progress
  GET  /jobs            - list all jobs
  DELETE /jobs/{id}     - cancel a running job
  GET  /download/{id}   - stream the finished file to the caller
  POST /info            - fetch title/thumbnail/duration/formats for a URL
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, HttpUrl

from core import (
    AUDIO_PRESETS,
    DOWNLOAD_DIR,
    FORMATS,
    MAX_CONCURRENT_JOBS,
    MAX_FILE_SIZE,
    PUBLIC_BASE_URL,
    extract_video_id,
    extract_with_fallback,
    is_cookie_or_auth_required_error,
    sanitize_filename,
    yt_dlp_error_text,
)
import jobs as jobstore

app = FastAPI(
    title="Seal Download API",
    version="1.0.0",
    description="Backend server for pasting a YouTube link and downloading audio or video.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class DownloadRequest(BaseModel):
    url: HttpUrl
    mode: str = Field(default="video", pattern="^(audio|video)$")
    quality: str = Field(default="best")
    container: Optional[str] = None


class InfoRequest(BaseModel):
    url: HttpUrl


@app.get("/", include_in_schema=False)
def root() -> PlainTextResponse:
    return PlainTextResponse("Seal Download API is running. See /docs for the API.")


@app.get("/health")
def health() -> dict:
    active = jobstore.active_count()
    return {
        "ok": True,
        "service": "seal-download-api",
        "active_jobs": active,
        "capacity": MAX_CONCURRENT_JOBS,
    }


@app.post("/download", status_code=202)
def start_download(req: DownloadRequest) -> dict:
    url = str(req.url)
    mode = req.mode
    quality = (req.quality or "best").lower()
    container = (req.container or ("mp3" if mode == "audio" else "mp4")).lower()

    if mode == "audio" and container not in AUDIO_PRESETS:
        raise HTTPException(400, f"audio container must be one of {sorted(AUDIO_PRESETS)}")
    if mode == "video" and container not in FORMATS:
        raise HTTPException(400, f"video container must be one of {sorted(FORMATS)}")

    if jobstore.active_count() >= MAX_CONCURRENT_JOBS:
        raise HTTPException(429, "Too many concurrent downloads; try again shortly")

    job_id = jobstore.create_job(url, mode, quality, container)
    try:
        jobstore.start_job(job_id)
    except jobstore.JobError as exc:
        jobstore.delete_job(job_id)
        raise HTTPException(429, str(exc)) from exc

    return {"job_id": job_id, "status": jobstore.get_job(job_id)["status"], "mode": mode}


@app.get("/status/{job_id}")
def status(job_id: str) -> dict:
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(404, "Unknown job id")
    payload = {
        k: job[k]
        for k in (
            "id", "status", "progress", "speed", "eta", "title",
            "filename", "error", "mode", "quality", "container",
            "requires_cookies", "created_at", "finished_at",
        )
    }
    if job["status"] == "completed" and PUBLIC_BASE_URL:
        payload["file_url"] = f"{PUBLIC_BASE_URL}/download/{job_id}"
    return payload


@app.get("/jobs")
def list_jobs() -> dict:
    items = []
    for job in jobstore.all_jobs().values():
        items.append(
            {
                "id": job["id"],
                "status": job["status"],
                "progress": job["progress"],
                "title": job["title"],
                "mode": job["mode"],
                "filename": job["filename"],
            }
        )
    items.sort(key=lambda j: j["id"])
    return {"jobs": items, "active": jobstore.active_count()}


@app.delete("/jobs/{job_id}")
def cancel(job_id: str) -> dict:
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(404, "Unknown job id")
    if job["status"] in {"completed", "error", "canceled"}:
        jobstore.delete_job(job_id)
        return {"canceled": True, "note": "finished job removed"}
    jobstore.cancel_job(job_id)
    return {"canceled": True}


@app.get("/download/{job_id}")
def download(job_id: str):
    job = jobstore.get_job(job_id)
    if not job:
        raise HTTPException(404, "Unknown job id")
    if job["status"] != "completed":
        raise HTTPException(409, f"Job not finished (status={job['status']})")
    path = Path(DOWNLOAD_DIR) / (job["filename"] or "")
    if not path.is_file():
        raise HTTPException(410, "File missing on disk; re-run the download")
    size = path.stat().st_size
    if size > MAX_FILE_SIZE:
        raise HTTPException(413, "File exceeds MAX_FILE_SIZE")
    safe_name = sanitize_filename(job["title"] or path.name)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=safe_name,
        headers={
            "Content-Length": str(size),
            "Cache-Control": "no-store",
            "X-Job-Id": job_id,
        },
    )


@app.post("/info")
def info(req: InfoRequest) -> dict:
    """Lightweight metadata probe (no download)."""
    url = str(req.url)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 30,
    }
    try:
        data = extract_with_fallback(url, opts, download=False)
    except Exception as exc:
        detail = f"Could not fetch info: {exc}"
        if is_cookie_or_auth_required_error(exc):
            detail = (
                "YouTube asked for authentication/cookies for this video. "
                "The server cannot export browser cookies for you; if this is your own "
                "account, supply a Netscape-format cookie file via the YTDL_COOKIES env var."
            )
        raise HTTPException(400, detail) from exc
    if not data:
        raise HTTPException(400, "No media found at that URL")

    video_id = extract_video_id(url)
    formats = []
    for f in (data.get("formats") or []):
        if not f.get("format_id"):
            continue
        formats.append(
            {
                "format_id": f["format_id"],
                "ext": f.get("ext"),
                "height": f.get("height"),
                "fps": f.get("fps"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
            }
        )
    formats.sort(key=lambda f: (f.get("height") or 0), reverse=True)

    return {
        "id": data.get("id") or video_id,
        "title": data.get("title"),
        "uploader": data.get("uploader"),
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail"),
        "webpage_url": data.get("webpage_url"),
        "is_youtube": video_id is not None,
        "formats": formats[:40],
        "cookies_supported": bool(YTDL_COOKIES),
    }


@app.exception_handler(Exception)
async def unhandled(_, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": f"Internal error: {exc}"})
