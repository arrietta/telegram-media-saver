"""Скачивание медиа по ссылке: yt-dlp для видео, gallery-dl для фото и каруселей."""

import asyncio
import copy
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import certifi
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
YT_POST_RE = re.compile(r"youtube\.com/post/", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Сертификаты из certifi: у Python на macOS своих корневых сертификатов нет.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

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
    # /etc/secrets/cookies.txt — сюда Render кладёт Secret File с именем cookies.txt.
    candidates = [os.getenv("COOKIES_FILE"), "/etc/secrets/cookies.txt", "cookies.txt"]
    src = next((c for c in candidates if c and Path(c).is_file()), None)
    if not src:
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


def _fetch(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en", "Cookie": "SOCS=CAI"}
    )
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
        return resp.read(), resp.headers.get_content_type()


def _find_key(node: object, key: str):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _find_key(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _find_key(v, key)


def _youtube_post(url: str, out: Path) -> None:
    """Посты из вкладки «Сообщество»: yt-dlp и gallery-dl их не умеют,
    поэтому достаём картинки прямо из данных страницы."""
    page, _ = _fetch(url.replace("//m.youtube.com", "//www.youtube.com"))
    match = re.search(rb"var ytInitialData\s*=\s*(\{.*?\});\s*</script>", page, re.DOTALL)
    post = next(_find_key(json.loads(match.group(1)), "backstagePostRenderer"), None) if match else None
    if post is None:
        raise DownloadError("не удалось прочитать пост YouTube")
    attachment = post.get("backstageAttachment") or {}

    video_id = next(_find_key(attachment, "videoId"), None)
    if video_id:
        _ytdlp(f"https://www.youtube.com/watch?v={video_id}", out)
        return

    images = list(_find_key(attachment, "backstageImageRenderer"))[:MAX_ITEMS]
    if not images:
        raise DownloadError("в этом посте нет картинок и видео — только текст")
    types = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    for i, image in enumerate(images, 1):
        thumb = image["image"]["thumbnails"][-1]["url"]
        # «=s0» вместо параметров превью отдаёт оригинал без обрезки под квадрат.
        data, ctype = _fetch(thumb.split("=")[0] + "=s0")
        (out / f"{i:02d}{types.get(ctype, '.jpg')}").write_bytes(data)


def _download_sync(url: str, out: Path) -> list[Path]:
    if YT_POST_RE.search(url):
        try:
            _youtube_post(url, out)
        except DownloadError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DownloadError(_clean(e)) from None
        return _files(out)

    # Посты Instagram и фото-посты TikTok — это часто картинки, с ними лучше
    # справляется gallery-dl; для всего остального сначала пробуем yt-dlp.
    steps = [_gallery, _ytdlp] if PHOTO_FIRST_RE.search(url) else [_ytdlp, _gallery]
    is_instagram = "instagram.com" in url.lower()
    if is_instagram and not COOKIES:
        # Без входа gallery-dl с Instagram не работает вообще — не тратим на него время.
        steps = [_ytdlp]
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
    text = main or (errors[0] if errors else "по ссылке не нашлось медиа")
    if is_instagram and not COOKIES:
        text = f"Instagram не отдаёт это серверу без входа в аккаунт (нужен cookies.txt). {text}"
    raise DownloadError(text)


async def download(url: str, out: Path) -> list[Path]:
    return await asyncio.to_thread(_download_sync, url, out)
