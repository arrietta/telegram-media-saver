# Cloudflare WARP для YouTube (см. warp.py): wgcf регистрирует аккаунт, wireproxy
# поднимает туннель. Если сборка не удалась, образ собирается без них и бот
# работает как раньше.
FROM golang:1.24 AS warp
ENV GOTOOLCHAIN=auto GOBIN=/out
RUN mkdir -p /out && { \
      go install github.com/ViRb3/wgcf/v2@v2.3.1 && \
      go install github.com/windtf/wireproxy/cmd/wireproxy@v1.1.2 ; \
    } || { echo "WARP tools were not built"; rm -f /out/*; }

FROM python:3.12-slim

# ffmpeg склеивает видео и звук, deno нужен yt-dlp для YouTube.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
COPY --from=warp /out/ /usr/local/bin/

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

CMD ["python", "bot.py"]
