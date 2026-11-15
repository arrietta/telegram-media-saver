"""Общее для всех загрузчиков."""

import os
import shutil
import ssl
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import certifi

# Bot API не принимает файлы больше 50 МБ — оставляем небольшой запас.
MAX_BYTES = 49 * 1024 * 1024
# Сертификаты из certifi: у Python на macOS своих корневых сертификатов нет.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
HAS_FFMPEG = shutil.which("ffmpeg") is not None
# Необязательный платный прокси с «домашними» адресами (http://user:pass@host:port):
# через него идут только запросы, которым сайт отказал напрямую.
PROXY_URL = (os.getenv("PROXY_URL") or "").strip() or None


def _prepare_cookies() -> str | None:
    """yt-dlp перезаписывает файл cookies, а секретные файлы на хостинге
    доступны только для чтения — поэтому работаем с копией."""
    # /etc/secrets/cookies.txt — сюда Render кладёт Secret File с именем cookies.txt.
    candidates = [os.getenv("COOKIES_FILE"), "/etc/secrets/cookies.txt", "cookies.txt"]
    src = next((c for c in candidates if c and Path(c).is_file()), None)
    if not src:
        return None
    dst = Path(tempfile.gettempdir()) / "tms_cookies.txt"
    shutil.copyfile(src, dst)
    return str(dst)


COOKIES = _prepare_cookies()



class DownloadError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""

    retry = False  # True — сбой маршрута, есть смысл попробовать другой


@dataclass
class Media:
    """Что отправить в чат. Подпись и размеры кадра нужны только одиночному видео."""

    files: list[Path]
    caption: str | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    # Для YouTube: что отправили и что ещё можно запросить кнопками под видео.
    video_id: str | None = None
    quality: int | None = None
    qualities: list[tuple[int, int | None]] = field(default_factory=list)  # (качество, байт)


class TooBig(Exception):
    """Файл перерос лимит Telegram — качать дальше нет смысла."""


CONTENT_TYPES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "video/mp4": ".mp4", "video/webm": ".webm", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
}


def open_url(url: str, data: bytes | None = None, timeout: int = 40):
    """Запрос к стороннему сервису; data — тело POST в формате JSON."""
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT)


def save_file(url: str, path: Path, limit: int = MAX_BYTES) -> Path:
    """Качает файл по частям и обрывает загрузку, как только он перерос limit.
    Расширение подбирает по типу содержимого."""
    with open_url(url, timeout=60) as resp:
        path = path.with_suffix(CONTENT_TYPES.get(resp.headers.get_content_type(), path.suffix))
        size = 0
        with path.open("wb") as f:
            while chunk := resp.read(256 * 1024):
                size += len(chunk)
                if size > limit:
                    raise TooBig
                f.write(chunk)
    if size == 0:
        raise RuntimeError("пришёл пустой файл")
    return path
