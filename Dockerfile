FROM python:3.11-slim

# ffmpeg is required by yt-dlp for audio extraction and video merging.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ .

ENV DOWNLOAD_DIR=/app/downloads \
    PYTHONUNBUFFERED=1

RUN mkdir -p /app/downloads

EXPOSE 8000

# $PORT is provided by Render; default to 8000 for local runs.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
