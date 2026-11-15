"""YouTube через Invidious — открытый «альтернативный интерфейс» к YouTube
(https://github.com/iv-org/invidious), копии которого держат добровольцы.

В отличие от cobalt, Invidious отдаёт точный список качеств с размерами и не
спотыкается на длинных роликах: видео и звук качаем через сам сервер отдельными
файлами и склеиваем ffmpeg без перекодирования. Ни ключей, ни входа в аккаунт.

Список серверов с открытым API берём с api.invidious.io; свой можно задать
переменной INVIDIOUS_API (несколько — через запятую).
"""

import json
import logging
import os
import subprocess
import time
import urllib.parse
from pathlib import Path

import yt_dlp
from yt_dlp.networking import Request

import warp

from common import (
    BROWSER_UA,
    HAS_FFMPEG,
    MAX_BYTES,
    DownloadError,
    TooBig,
    open_url,
    save_file,
)

log = logging.getLogger("invidious")

DIRECTORY = "https://api.invidious.io/instances.json?sort_by=type,health"
# На случай, если каталог недоступен: сервер с открытым API на момент написания.
FALLBACK = ["yt.chocolatemoo53.com"]
MAX_INSTANCES = 3
CHUNK_WARP = 4 * 1024 * 1024
CHUNK_DIRECT = 1024 * 1024
MAX_FAILURES = 6  # на весь файл

_cache: tuple[float, list[str]] = (0.0, [])


def _retry(text: str) -> DownloadError:
    """Сбой сервера, а не ролика — есть смысл попробовать другой способ."""
    error = DownloadError(text)
    error.retry = True
    return error


def _instances() -> list[str]:
    global _cache
    own = [
        h.strip().removeprefix("https://").strip("/")
        for h in os.getenv("INVIDIOUS_API", "").split(",")
        if h.strip()
    ]
    if time.time() - _cache[0] > 3600:
        found = []
        try:
            with open_url(DIRECTORY, timeout=15) as resp:
                for name, meta in json.loads(resp.read()):
                    if meta.get("type") == "https" and meta.get("api"):
                        found.append(name)
        except Exception as e:  # noqa: BLE001 — каталог недоступен, берём запасной список
            log.info("api.invidious.io is unavailable: %s", e)
        _cache = (time.time(), found)
    return list(dict.fromkeys(own + _cache[1] + FALLBACK))[:MAX_INSTANCES]


def info(video_id: str) -> dict:
    """Название, длительность и варианты качества (h264 + звук aac), от лучшего к худшему."""
    last = "нет доступных серверов"
    fields = "title,lengthSeconds,liveNow,adaptiveFormats,error"
    for host in _instances():
        try:
            # local=true — ссылки на потоки ведут через сам сервер, а не на YouTube,
            # который привязывает их к адресу того, кто спросил.
            with open_url(f"https://{host}/api/v1/videos/{video_id}?local=true&fields={fields}", timeout=25) as resp:
                data = json.loads(resp.read())
        except Exception as e:  # noqa: BLE001
            last = str(e)[:120]
            log.info("invidious %s failed: %s", host, last)
            continue
        if data.get("error") or not data.get("adaptiveFormats"):
            last = str(data.get("error") or "нет форматов")[:120]
            log.info("invidious %s failed: %s", host, last)
            continue
        if data.get("liveNow"):
            raise DownloadError("это прямой эфир, его скачать нельзя")

        formats = data["adaptiveFormats"]
        audio = [f for f in formats if f.get("type", "").startswith("audio/mp4") and f.get("url")]
        if not audio:
            last = "нет звуковой дорожки"
            continue
        # itag 140 — обычный звук aac 128 кбит/с; он же идёт первым у роликов с дубляжом.
        sound = next((f for f in audio if f.get("itag") == "140"), audio[-1])
        picks: list[dict] = []
        for f in formats:
            if "avc1" not in f.get("type", "") or not f.get("url") or "x" not in f.get("size", ""):
                continue
            width, height = (int(n) for n in f["size"].split("x"))
            quality = min(width, height)
            if quality in [p["quality"] for p in picks]:
                continue
            picks.append({
                "quality": quality,
                "size": int(f.get("clen") or 0) + int(sound.get("clen") or 0) or None,
                "width": width,
                "height": height,
                "video_size": int(f.get("clen") or 0),
                "audio_size": int(sound.get("clen") or 0),
                "video": urllib.parse.urljoin(f"https://{host}", f["url"]),
                "audio": urllib.parse.urljoin(f"https://{host}", sound["url"]),
            })
        if not picks:
            last = "нет видео в формате h264"
            continue
        picks.sort(key=lambda p: -p["quality"])
        return {
            "host": host,
            "title": data.get("title") or None,
            "duration": int(data.get("lengthSeconds") or 0),
            "picks": picks,
        }
    raise _retry(f"Invidious не справился ({last})")


def _save_in_parts(url: str, path: Path, total: int, limit: int) -> Path:
    """Качает файл кусками с повторами.

    Проверено на Render: напрямую сервер Invidious отдаёт хостингу только мелкие
    куски (до 1 МБ), а на крупных рвёт связь или отвечает 502. Через WARP проходят
    любые. Поэтому идём через WARP кусками по 4 МБ, а если его нет или он
    подвёл — напрямую по 1 МБ."""
    if total > limit:
        raise TooBig
    routes = [(warp.proxy(), CHUNK_WARP)] if warp.proxy() else []
    routes.append((None, CHUNK_DIRECT))
    clients = [
        (yt_dlp.YoutubeDL({"quiet": True, "socket_timeout": 30, **({"proxy": proxy} if proxy else {})}), size)
        for proxy, size in routes
    ]
    try:
        with path.open("wb") as f:
            start = 0
            failures = 0
            while start < total:
                # После сбоя пробуем другой путь; мелкий кусок не страшен ни одному.
                client, size = clients[failures % len(clients)]
                end = min(start + size, total) - 1
                try:
                    req = Request(url, headers={"User-Agent": BROWSER_UA, "Range": f"bytes={start}-{end}"})
                    data = client.urlopen(req).read()
                    if len(data) != end - start + 1:
                        raise RuntimeError(f"кусок {len(data)} байт вместо {end - start + 1}")
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    if failures > MAX_FAILURES:
                        raise RuntimeError(f"{str(e)[:80]} на {start * 100 // total}%") from None
                    log.info("invidious: chunk at %d%% failed (%s), retrying", start * 100 // total, str(e)[:60])
                    time.sleep(1)
                    continue
                f.write(data)
                start = end + 1
    finally:
        for client, _ in clients:
            client.close()
    return path


def _save(url: str, path: Path, total: int, limit: int) -> Path:
    # Размер известен почти всегда; без него остаётся качать одним запросом.
    return _save_in_parts(url, path, total, limit) if total else save_file(url, path, limit)


def download(pick: dict, out: Path) -> Path:
    """Качает выбранный вариант и склеивает видео со звуком в один mp4."""
    if not HAS_FFMPEG:
        raise _retry("для склейки видео и звука нужен ffmpeg")
    for old in out.iterdir():
        old.unlink()
    try:
        video = _save(pick["video"], out / "video.mp4", pick["video_size"], MAX_BYTES)
        audio = _save(
            pick["audio"], out / "audio.m4a", pick["audio_size"], MAX_BYTES - video.stat().st_size
        )
    except TooBig:
        raise
    except Exception as e:  # noqa: BLE001
        raise _retry(f"Invidious оборвал загрузку ({str(e)[:100]})") from None
    result = out / "merged.mp4"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(audio),
         "-c", "copy", "-movflags", "+faststart", str(result)],
        capture_output=True, text=True, timeout=120,
    )
    video.unlink()
    audio.unlink()
    if proc.returncode != 0 or not result.exists():
        raise _retry(f"ffmpeg не склеил видео ({proc.stderr.strip()[-120:]})")
    return result
