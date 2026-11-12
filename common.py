"""Общее для всех загрузчиков."""

import shutil
import ssl
from dataclasses import dataclass
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


class DownloadError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""


@dataclass
class Media:
    """Что отправить в чат. Подпись и размеры кадра нужны только одиночному видео."""

    files: list[Path]
    caption: str | None = None
    width: int | None = None
    height: int | None = None
    duration: int | None = None
