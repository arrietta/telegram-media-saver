"""Скачивание медиа по ссылке: yt-dlp для видео, gallery-dl для фото и каруселей."""

import asyncio
import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yt_dlp

# Bot API не принимает файлы больше 50 МБ — оставляем небольшой запас.
MAX_BYTES = 49 * 1024 * 1024
MAX_DURATION = 60 * 60  # секунд; более длинное видео всё равно не влезет в 50 МБ
MAX_ITEMS = 10  # столько файлов помещается в один альбом Telegram

URL_RE = re.compile(
    r"https?://(?:[\w-]+\.)*(?:instagram\.com|youtube\.com|youtu\.be|tiktok\.com)/[^\s<>]+",
    re.IGNORECASE,
)
PHOTO_FIRST_RE = re.compile(r"instagram\.com/p/|tiktok\.com/.+/photo/", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wav"}


class DownloadError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""


def find_urls(text: str) -> list[str]:
    seen: list[str] = []
    for url in URL_RE.findall(text or ""):
        url = url.rstrip(".,)")
        if url not in seen:
            seen.append(url)
    return seen


def _prepare_cookies() -> str | None:
    """yt-dlp перезаписывает файл cookies, а секретные файлы на хостинге
    доступны только для чтения — поэтому работаем с копией."""
    src = os.getenv("COOKIES_FILE")
    if not src or not Path(src).is_file():
        return None
    dst = Path(tempfile.gettempdir()) / "tms_cookies.txt"
    shutil.copyfile(src, dst)
    return str(dst)


COOKIES = _prepare_cookies()
HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _clean(error: object) -> str:
    text = ANSI_RE.sub("", str(error)).replace("ERROR: ", "").strip()
    return text[:300] or "неизвестная ошибка"


def _files(out: Path) -> list[Path]:
    skip = {".part", ".ytdl", ".json", ".txt"}
    return sorted(p for p in out.iterdir() if p.is_file() and p.suffix.lower() not in skip)


def _clear(out: Path) -> None:
    for p in out.iterdir():
        if p.is_file():
            p.unlink()


def _ytdlp_opts(out: Path, height: int) -> dict:
    opts = {
        "outtmpl": str(out / "%(autonumber)02d_%(id)s.%(ext)s"),
        # Без ffmpeg склеить видео и звук нечем — берём готовый файл похуже качеством.
        "format": f"bv*[height<={height}]+ba/b[height<={height}]/b"
        if HAS_FFMPEG
        else f"b[height<={height}]/b",
        # h264 + aac в mp4 — то, что Telegram проигрывает прямо в чате.
        "format_sort": ["vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlist_items": f"1-{MAX_ITEMS}",
        "restrictfilenames": True,
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


def _ytdlp(url: str, out: Path) -> None:
    with yt_dlp.YoutubeDL(_ytdlp_opts(out, 720)) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(url, download=False) or {})
    # Разбор страницы — самая медленная часть на слабом сервере, поэтому для
    # одиночного видео делаем его один раз и дальше качаем по готовым данным.
    single = info.get("_type", "video") == "video" and bool(info.get("formats"))
    if info.get("is_live"):
        raise DownloadError("это прямой эфир, его скачать нельзя")
    if (info.get("duration") or 0) > MAX_DURATION:
        raise DownloadError("видео длиннее часа — в лимит Telegram (50 МБ) не влезет")

    # На YouTube есть выбор качества: понижаем его, пока файл не влезет в лимит.
    is_youtube = "youtu" in url.lower()
    for height in (720, 480, 360) if is_youtube else (1080,):
        _clear(out)
        with yt_dlp.YoutubeDL(_ytdlp_opts(out, height)) as ydl:
            if single:
                ydl.process_ie_result(copy.deepcopy(info), download=True)
            else:
                ydl.download([url])
        files = _files(out)
        if files and max(f.stat().st_size for f in files) <= MAX_BYTES:
            return


def _gallery(url: str, out: Path) -> None:
    # Без повторов и с коротким таймаутом: если сайт требует вход, gallery-dl
    # должен сдаться быстро, а не держать пользователя минутами.
    cmd = [sys.executable, "-m", "gallery_dl", "-D", str(out), "--range", f"1-{MAX_ITEMS}"]
    cmd += ["--retries", "0", "--http-timeout", "15"]
    if COOKIES:
        cmd += ["--cookies", COOKIES]
    try:
        proc = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise DownloadError("скачивание заняло слишком много времени") from None
    if proc.returncode != 0 and not _files(out):
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        raise DownloadError(_clean(lines[-1] if lines else "gallery-dl ничего не нашёл"))


def _download_sync(url: str, out: Path) -> list[Path]:
    # Посты Instagram и фото-посты TikTok — это часто картинки, с ними лучше
    # справляется gallery-dl; для всего остального сначала пробуем yt-dlp.
    steps = [_gallery, _ytdlp] if PHOTO_FIRST_RE.search(url) else [_ytdlp, _gallery]
    errors: list[str] = []
    for step in steps:
        _clear(out)
        try:
            step(url, out)
        except Exception as e:  # noqa: BLE001 — пробуем следующий способ
            errors.append(_clean(e))
            continue
        files = _files(out)
        if files:
            return files
    # Показываем ошибку yt-dlp — она обычно самая понятная.
    main = errors[steps.index(_ytdlp)] if len(errors) == len(steps) else None
    raise DownloadError(main or (errors[0] if errors else "по ссылке не нашлось медиа"))


async def download(url: str, out: Path) -> list[Path]:
    return await asyncio.to_thread(_download_sync, url, out)
