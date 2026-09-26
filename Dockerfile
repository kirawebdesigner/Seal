FROM python:3.11-slim

ARG DENO_VERSION=2.9.7

# ffmpeg is required by yt-dlp for audio extraction and video merging.
# deno is the JS runtime yt-dlp needs to solve YouTube's player challenges (EJS);
# without it extraction fails with "failed to extract player response".
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" -o /tmp/deno.zip \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && deno --version

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
