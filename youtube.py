"""YouTube: один простой путь.

YouTube не пускает серверы хостингов («Sign in to confirm you're not a bot»),
поэтому сами мы его не разбираем. Ссылки на готовые дорожки отдаёт сервис
«YouTube Media Downloader» на RapidAPI, а бот только скачивает видео и звук
и склеивает их ffmpeg без перекодирования.

Ссылка → лучшее качество до 720p, которое влезает в лимит Telegram → mp4.
"""

import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from common import BROWSER_UA, HAS_FFMPEG, MAX_BYTES, SSL_CONTEXT, DownloadError, Media

log = logging.getLogger("youtube")

API_HOST = "youtube-media-downloader.p.rapidapi.com"
API_KEY = (os.getenv("RAPIDAPI_KEY") or "").strip()

MAX_QUALITY = 720  # выше в чате разницы не видно, а файл вдвое тяжелее
MIN_QUALITY = 360  # ниже — уже каша; лучше честно сказать, что видео не влезает
CHUNK = 10 * 1024 * 1024  # YouTube режет скорость, если качать файл одним запросом

ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})")


def video_id(url: str) -> str | None:
    match = ID_RE.search(url)
    return match.group(1) if match else None


def browser_link(url: str) -> str | None:
    """Тот же ролик на ssyoutube.com — запасной выход, когда бот не справился."""
    vid = video_id(url)
    return f"https://ssyoutube.com/watch?v={vid}" if vid else None


@dataclass
class Choice:
    video: dict
    audio: dict | None  # None — в видеодорожке уже есть звук
    quality: int

    @property
    def size(self) -> int:
        return _size(self.video) + (_size(self.audio) if self.audio else 0)


def _size(item: dict) -> int:
    return int(item.get("size") or 0)


def _quality(item: dict) -> int:
    # У вертикальных Shorts «720p» — это ширина, поэтому меряем по короткой стороне.
    return min(int(item.get("width") or 0), int(item.get("height") or 0))


def _details(vid: str) -> dict:
    req = urllib.request.Request(
        f"https://{API_HOST}/v2/video/details?videoId={vid}&urlAccess=normal",
        headers={"X-RapidAPI-Key": API_KEY, "X-RapidAPI-Host": API_HOST},
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read(200).decode(errors="replace")
        log.warning("RapidAPI HTTP %s for %s: %s", e.code, vid, body)
        if e.code == 429:
            raise DownloadError("на этот месяц закончился лимит RapidAPI") from None
        if e.code in (401, 403):
            raise DownloadError(
                "RapidAPI не принял ключ — проверь RAPIDAPI_KEY и подписку на "
                "YouTube Media Downloader"
            ) from None
        if e.code == 404:
            raise DownloadError("такого видео на YouTube нет") from None
        raise DownloadError(f"RapidAPI ответил ошибкой {e.code}") from None
    if data.get("errorId") != "Success":
        log.warning("RapidAPI error for %s: %s", vid, data.get("errorId"))
        raise DownloadError("YouTube не отдаёт это видео (удалено, закрыто или с ограничением)")
    return data


def _choose(data: dict) -> Choice:
    # Только h264 + aac в mp4: это Telegram проигрывает прямо в чате на любом телефоне.
    videos = [
        v
        for v in (data.get("videos") or {}).get("items") or []
        if v.get("url") and "avc1" in (v.get("mimeType") or "") and _quality(v)
    ]
    audio = next(
        (
            a
            for a in (data.get("audios") or {}).get("items") or []
            if a.get("url") and a.get("extension") == "m4a" and not a.get("isDrc")
        ),
        None,
    )
    choices = [
        Choice(v, None if v.get("hasAudio") else audio, _quality(v))
        for v in videos
        # Без ffmpeg склеивать нечем — годятся только дорожки сразу со звуком.
        if v.get("hasAudio") or (audio and HAS_FFMPEG)
    ]
    choices = [c for c in choices if c.quality <= MAX_QUALITY and c.size]
    if not choices:
        raise DownloadError("у этого видео нет подходящего формата")

    # Лучшее качество; при равном — файл полегче.
    choices.sort(key=lambda c: (-c.quality, c.size))
    floor = min(MIN_QUALITY, choices[0].quality)  # у старых роликов 360p может и не быть
    for choice in choices:
        if choice.quality >= floor and choice.size <= MAX_BYTES:
            return choice

    smallest = min((c for c in choices if c.quality >= floor), key=lambda c: c.size)
    minutes = max(int(data.get("lengthSeconds") or 0) // 60, 1)
    raise DownloadError(
        f"ролик идёт {minutes} мин и даже в {smallest.quality}p весит "
        f"{smallest.size // 1024 // 1024} МБ, а Telegram пропускает ботам не больше 50 МБ"
    )


def _get(url: str, path: Path, size: int) -> None:
    """Качает файл кусками по CHUNK, с парой повторов на каждый кусок."""
    done = 0
    with path.open("wb") as f:
        while not size or done < size:
            headers = {"User-Agent": BROWSER_UA}
            if size:
                headers["Range"] = f"bytes={done}-{min(done + CHUNK, size) - 1}"
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                        part = resp.read()
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 403:
                        raise DownloadError(
                            "YouTube не отдал файл серверу по ссылке от RapidAPI (403)"
                        ) from None
                    if attempt == 2:
                        raise DownloadError(f"YouTube ответил ошибкой {e.code}") from None
                except OSError:
                    if attempt == 2:
                        raise DownloadError("связь с YouTube оборвалась") from None
                time.sleep(1)
            f.write(part)
            done += len(part)
            if not size or not part:
                break


def _merge(video: Path, audio: Path, result: Path) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-i", str(audio)]
        + ["-c", "copy", "-movflags", "+faststart", str(result)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    video.unlink()
    audio.unlink()
    if proc.returncode != 0:
        log.warning("ffmpeg failed: %s", proc.stderr[-300:])
        raise DownloadError("не получилось склеить видео со звуком")


def download(url: str, out: Path) -> Media:
    vid = video_id(url)
    if not vid:
        raise DownloadError("не понял ссылку — пришли ссылку на само видео")
    data = _details(vid)
    if data.get("isLiveNow"):
        raise DownloadError("это прямой эфир, его скачать нельзя")
    choice = _choose(data)
    log.info("youtube %s: %dp, %d MB", vid, choice.quality, choice.size // 1024 // 1024)

    result = out / f"{vid}.mp4"
    if choice.audio:
        video, audio = out / "video.part", out / "audio.part"
        _get(choice.video["url"], video, _size(choice.video))
        _get(choice.audio["url"], audio, _size(choice.audio))
        _merge(video, audio, result)
    else:
        _get(choice.video["url"], result, _size(choice.video))
    return Media(
        files=[result],
        caption=data.get("title") or None,
        width=int(choice.video.get("width") or 0) or None,
        height=int(choice.video.get("height") or 0) or None,
        duration=int(data.get("lengthSeconds") or 0) or None,
    )
