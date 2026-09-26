# Seal Download API

FastAPI + yt-dlp backend: paste a YouTube link, get an audio or video file back.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | heartbeat + active job count |
| POST | `/download` | start a job: `{"url": "...", "mode": "audio"\|"video", "quality": "best"\|"1080p"\|"720p"\|"480p"\|"360p", "container": "mp3"\|"m4a"\|"opus"\|"wav"\|"mp4"\|"webm"\|"mkv"\|"best"}` |
| GET | `/status/{job_id}` | progress (0-100), speed, eta, filename |
| GET | `/jobs` | list all jobs |
| DELETE | `/jobs/{job_id}` | cancel / remove a job |
| GET | `/download/{job_id}` | stream the finished file |
| POST | `/info` | probe a URL for title, duration, thumbnail, formats (no download) |

Interactive docs: `https://<your-render-url>/docs`

## Run locally (no Docker)

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

ffmpeg is only needed when extracting audio or merging video+audio streams.

## Deploy on Render

> The Dockerfile lives at the **repo root** (it builds from `server/`). This means Render's default Docker settings work out of the box — no Dockerfile path or context configuration needed.

1. Commit & push everything to GitHub first — Render builds from your repo, not this workspace. If Render says `failed to read dockerfile: no such file or directory`, the files were not pushed.
2. Render → **New +** → **Blueprint**, pick the repo — `render.yaml` at the root is detected automatically. (Or **New +** → **Web Service** → pick the repo → runtime **Docker**; defaults work since the Dockerfile is at the root.)
3. Set `PUBLIC_BASE_URL` when prompted (your Render URL, e.g. `https://seal-download-api.onrender.com`) so `/status` returns absolute `file_url`s.
4. First deploy takes a few minutes (installs ffmpeg in the image).

Notes for the free plan:
- The service sleeps after ~15 min of inactivity; the first request then takes ~30-60s.
- Ephemeral disk: finished files disappear on restart/redeploy — download them promptly.
- Set `MAX_CONCURRENT_JOBS` and `MAX_FILE_SIZE` env vars to tune capacity.

## Design notes

- **YouTube compatibility:** yt-dlp needs a JavaScript runtime (deno, installed in the Dockerfile) and the `yt-dlp-ejs` challenge scripts (pulled in by `yt-dlp[default]` in `requirements.txt`) to solve YouTube's player challenges. If downloads start failing again with `failed to extract player response`, redeploy so the image rebuilds with the latest yt-dlp — stale yt-dlp versions are the usual culprit.
- Player clients are rotated per attempt (`default,tv,web_safari,mweb`) so one broken YouTube client can't take the service down; override with the `PLAYER_CLIENTS` env var.
- Jobs live in memory (dict + threads). One worker instance = predictable behavior. Move to Redis + RQ when you need multiple instances.
- `MAX_FILE_SIZE` guards `/download/{id}` against serving huge files.
- CORS defaults to `*`; set `CORS_ORIGINS=https://yourfrontend.com` to lock it down.
