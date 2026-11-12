"""YouTube: один простой путь.

Ссылка → лучшее качество до 720p, которое влезает в лимит Telegram → mp4
с названием ролика в подписи. Разбирает и качает yt-dlp, видео со звуком
склеивает ffmpeg без перекодирования.

На хостинге YouTube требует вход («Sign in to confirm you're not a bot») —
там нужен файл cookies.txt, см. README.
"""

import copy
import logging
import re
from pathlib import Path

import yt_dlp

from common import COOKIES, HAS_FFMPEG, MAX_BYTES, DownloadError, Media

log = logging.getLogger("youtube")

QUALITIES = (720, 480, 360, 240, 144)  # выше 720p в чате разницы не видно, а файл тяжелее
MIN_QUALITY = 360  # ниже — уже каша; лучше честно сказать, что видео не влезает

ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def browser_link(url: str) -> str | None:
    """Тот же ролик на ssyoutube.com — запасной выход, когда бот не справился."""
    match = ID_RE.search(url)
    return f"https://ssyoutube.com/watch?v={match.group(1)}" if match else None


def _opts(out: Path, quality: int, vertical: bool) -> dict:
    # У вертикальных Shorts «720p» — это ширина, поэтому меряем по короткой стороне.
    side = f"[{'width' if vertical else 'height'}<={quality}]"
    opts = {
        "outtmpl": str(out / "%(id)s.%(ext)s"),
        # Без ffmpeg склеить видео и звук нечем — тогда только готовый файл со звуком.
        "format": f"bv{side}+ba/b{side}" if HAS_FFMPEG else f"b{side}",
        # h264 + aac в mp4 — то, что Telegram проигрывает прямо в чате на любом телефоне.
        "format_sort": ["vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 1,
    }
    if COOKIES:
        opts["cookiefile"] = COOKIES
    return opts


def _explain(error: object) -> str:
    text = ANSI_RE.sub("", str(error)).replace("ERROR: ", "").strip()
    if "confirm you" in text and "not a bot" in text:
        return "YouTube не пускает сервер без входа в аккаунт — " + (
            "файл cookies.txt на хостинге устарел, его нужно обновить (см. README)"
            if COOKIES
            else "на хостинг нужно добавить файл cookies.txt (см. README)"
        )
    return text[:300] or "неизвестная ошибка"


def _pick(info: dict, quality: int, vertical: bool) -> dict | None:
    """Что yt-dlp выберет при таком потолке качества: настоящее качество и вес
    (None — неизвестен). Сеть не трогает, считает по уже полученным данным."""
    try:
        with yt_dlp.YoutubeDL(_opts(Path("."), quality, vertical)) as ydl:
            res = ydl.process_ie_result(copy.deepcopy(info), download=False)
    except Exception:  # noqa: BLE001 — такого качества у ролика нет
        return None
    size = 0
    for f in res.get("requested_formats") or [res]:
        part = f.get("filesize") or f.get("filesize_approx")
        if not part and f.get("tbr") and info.get("duration"):
            part = f["tbr"] * 1000 / 8 * info["duration"]
        size = None if size is None or not part else size + int(part)
    width, height = res.get("width") or 0, res.get("height") or 0
    return {"quality": min(width, height) or height, "size": size, "width": width, "height": height}


def _choices(info: dict) -> list[dict]:
    """Варианты от лучшего к худшему, которые не стыдно отправить и которые
    по оценке влезают в лимит Telegram."""
    vertical = (info.get("width") or 0) < (info.get("height") or 0)
    picks: list[dict] = []
    for quality in QUALITIES:
        pick = _pick(info, quality, vertical)
        if pick and pick["quality"] not in [p["quality"] for p in picks]:
            picks.append({**pick, "limit": quality, "vertical": vertical})
    if not picks:
        raise DownloadError("у этого видео нет подходящего формата")

    floor = min(MIN_QUALITY, picks[0]["quality"])  # у старых роликов 360p может и не быть
    decent = [p for p in picks if p["quality"] >= floor]
    fitting = [p for p in decent if p["size"] is None or p["size"] <= MAX_BYTES]
    if not fitting:
        smallest = decent[-1]
        minutes = max(int(info.get("duration") or 0) // 60, 1)
        raise DownloadError(
            f"ролик идёт {minutes} мин и даже в {smallest['quality']}p весит "
            f"{smallest['size'] // 1024 // 1024} МБ, а Telegram пропускает ботам не больше 50 МБ"
        )
    return fitting


def download(url: str, out: Path) -> Media:
    try:
        with yt_dlp.YoutubeDL(_opts(out, QUALITIES[0], False)) as ydl:
            info = ydl.sanitize_info(ydl.extract_info(url, download=False) or {})
        if info.get("_type", "video") != "video" or not info.get("formats"):
            raise DownloadError("пришли ссылку на само видео, а не на плейлист или канал")
        if info.get("is_live"):
            raise DownloadError("это прямой эфир, его скачать нельзя")

        # Размер до скачивания — оценка; если не угадали, берём качество пониже.
        for choice in _choices(info):
            for old in out.iterdir():
                old.unlink()
            log.info("youtube %s: trying %dp", info.get("id"), choice["quality"])
            with yt_dlp.YoutubeDL(_opts(out, choice["limit"], choice["vertical"])) as ydl:
                ydl.process_ie_result(copy.deepcopy(info), download=True)
            files = [p for p in out.iterdir() if p.suffix.lower() in (".mp4", ".webm", ".mkv")]
            if files and files[0].stat().st_size <= MAX_BYTES:
                return Media(
                    files=files[:1],
                    caption=info.get("title") or None,
                    width=choice["width"] or None,
                    height=choice["height"] or None,
                    duration=int(info.get("duration") or 0) or None,
                )
        raise DownloadError("видео не влезает в лимит Telegram — 50 МБ")
    except DownloadError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DownloadError(_explain(e)) from None
