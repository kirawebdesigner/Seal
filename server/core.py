"""Shared yt-dlp helpers for the Seal-style download API."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(os.path.dirname(__file__), "downloads"))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(2 * 1024 * 1024 * 1024)))  # 2 GiB

AUDIO_PRESETS = {"mp3", "m4a", "opus", "wav", "best"}
FORMATS = {"mp4", "webm", "mkv", "best"}


def sanitize_filename(name: str) -> str:
    """Make a string safe to use as a downloaded filename."""
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip(" .")
    return name[:180] or "download"


def extract_video_id(url: str) -> Optional[str]:
    """Best-effort YouTube video id from a URL; None for non-YouTube links."""
    m = re.search(
        r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})", url
    )
    if m:
        return m.group(1)
    # Bare id passed directly
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url.strip()):
        return url.strip()
    return None


def base_opts(jobs: "list[dict]") -> Dict[str, Any]:
    """Common yt-dlp options shared by every job."""
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "ignoreerrors": False,
        "progress_hooks": [lambda d: _progress_hook(d, jobs)],
        "outtmpl": {"default": os.path.join(DOWNLOAD_DIR, "%(title).150B [%(id)s].%(ext)s")},
        "restrictfilenames": False,
        "windowsfilenames": True,
    }


def _progress_hook(d: dict, jobs: "list[dict]") -> None:  # pragma: no cover - set per job
    """Placeholder; each job installs its own closure. Kept for signature parity."""
    return None


def format_for(quality: str, container: Optional[str]) -> str:
    """Translate quality preset + container into a yt-dlp format selector."""
    q = (quality or "best").lower()
    if q == "best":
        return "bestvideo*+bestaudio/best"
    if q == "1080p":
        height = 1080
    elif q == "720p":
        height = 720
    elif q == "480p":
        height = 480
    elif q == "360p":
        height = 360
    else:
        return "bestvideo*+bestaudio/best"
    if container and container != "best":
        return (
            f"bestvideo[height<={height}][ext={container}]+bestaudio/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        )
    return (
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/best"
    )


def audio_codec_for(preset: str) -> str:
    preset = preset.lower()
    if preset in AUDIO_PRESETS:
        if preset == "best":
            return "best"
        return preset
    return "mp3"


def postprocessors_for(mode: str, preset: str) -> List[Dict[str, Any]]:
    """Post-processors for audio extraction (Seal's 'audio only' mode)."""
    if mode != "audio":
        return []
    codec = audio_codec_for(preset)
    if codec == "best":
        return [{"key": "FFmpegExtractAudio"}]  # keeps source codec (m4a/webm/opus)
    return [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
            "preferredquality": "0" if codec in {"mp3", "opus"} else "192",
        },
        {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        {"key": "FFmpegMetadata"},
    ]
