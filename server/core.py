"""Shared yt-dlp helpers for the Seal-style download API."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(os.path.dirname(__file__), "downloads"))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(2 * 1024 * 1024 * 1024)))  # 2 GiB
YTDL_COOKIES = os.getenv("YTDL_COOKIES")  # path to Netscape cookies file, optional

AUDIO_PRESETS = {"mp3", "m4a", "opus", "wav", "best"}
FORMATS = {"mp4", "webm", "mkv", "best"}

# Player clients to try, in order, per attempt. YouTube periodically breaks
# individual clients (po_token requirements, player changes, throttling), so we
# rotate through a few rather than betting everything on one.
PLAYER_CLIENTS = [c for c in os.getenv("PLAYER_CLIENTS", "default,tv,web_safari,mweb").split(",") if c]
YTDL_PROXY = os.getenv("YTDL_PROXY")  # http(s) proxy optional
PLAYER_CLIENT_STR = os.getenv("PLAYER_CLIENT", "ios")


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
    opts: Dict[str, Any] = {
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
    if YTDL_COOKIES and os.path.isfile(YTDL_COOKIES):
        opts["cookiefile"] = YTDL_COOKIES
    if YTDL_PROXY:
        opts["proxy"] = YTDL_PROXY
    return opts


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


def extract_with_fallback(
    url: str, opts: Dict[str, Any], job: Optional[dict] = None, download: bool = True
) -> Any:
    """Run a yt-dlp extraction, retrying with a different YouTube player client
    if one is broken/throttled. Individual clients fail regularly, so we rotate.

    YouTube occasionally asks for a real session and surfaces it as a bot/
    interstitial error. We do not handle that here automatically because the
    server should not store or export browser cookies for users.
    """
    import yt_dlp

    last_error: Optional[Exception] = None
    for client in PLAYER_CLIENTS:
        attempt_opts = dict(opts)
        if client and client != "default":
            attempt_opts["extractor_args"] = {
                "youtube": {
                    "player_client": client,
                    "skip": ["webpage"],
                }
            }
        if client == "default":
            attempt_opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["ios", "android"],
                    "skip": ["webpage"],
                }
            }
        if YTDL_PROXY:
            attempt_opts["proxy"] = YTDL_PROXY
        attempt_opts["sleep_interval"] = 5
        attempt_opts["max_sleep_interval"] = 10
        try:
            with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                return ydl.extract_info(url, download=download)
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
            last_error = exc
            if job is not None:
                job["error"] = f"player_client={client}: {exc}"
            continue
    raise last_error if last_error else JobRuntimeError("extraction failed")


class JobRuntimeError(RuntimeError):
    """Internal sentinel for the (unreachable) no-error fallback path."""


def is_cookie_or_auth_required_error(exc: Exception) -> bool:
    """Detect when yt-dlp is asking for cookies/a session instead of a
    transient extraction failure.

    YouTube surfaces this as 'Sign in to confirm you're not a bot' or as a
    throttling / unavailable-player response when the request is classified
    as suspicious.

    Note: yt-dlp raises many exception shapes. This helper errs toward false
    positives for unknown exceptions only when the message strongly matches a
    cookie/auth class.
    """
    text = str(exc)
    low = text.lower()
    for marker in (
        "sign in to confirm you",
        "cookies-from-browser",
        "use --cookies",
        "not a bot",
        "cookies file",
        "cookie file",
        "pass cookies",
        "how-do-i-pass-cookies",
        "i-pass-cookies-to-yt-dlp",
        "exporting-youtube-cookies",
        "request throttled",
        "request was throttled",
        "cw 청구",
    ):
        if marker in low:
            return True
    return False


def yt_dlp_error_text(exc: Exception) -> str:
    """Best-effort user-facing summary of a yt-dlp exception."""
    text = str(exc)
    if not text:
        return exc.__class__.__name__
    # Collapse multi-line traceback noise down to the first readable line.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return exc.__class__.__name__
    return lines[0]
