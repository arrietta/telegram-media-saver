"""Скачивание медиа по ссылке.

Instagram и TikTok — yt-dlp для видео и gallery-dl для фото и каруселей.
YouTube — отдельный простой путь в youtube.py.
"""

import asyncio
import contextlib
import copy
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yt_dlp

import cobalt
import warp
import youtube
from common import (
    BROWSER_UA,
    COOKIES,
    HAS_FFMPEG,
    MAX_BYTES,
    PROXY_URL,
    SSL_CONTEXT,
    DownloadError,
    Media,
)

MAX_DURATION = 60 * 60  # секунд; более длинное видео всё равно не влезет в 50 МБ
MAX_ITEMS = 10  # столько файлов помещается в один альбом Telegram
TIKTOK_MAX_ITEMS = 36  # в слайд-шоу TikTok до 35 фото + звуковая дорожка

URL_RE = re.compile(
    r"https?://(?:[\w-]+\.)*(?:instagram\.com|youtube\.com|youtu\.be|tiktok\.com)/[^\s<>]+",
    re.IGNORECASE,
)
PHOTO_FIRST_RE = re.compile(r"instagram\.com/p/|tiktok\.com/.+/photo/", re.IGNORECASE)
YT_RE = re.compile(r"https?://(?:[\w-]+\.)*(?:youtube\.com|youtu\.be)/", re.IGNORECASE)
YT_POST_RE = re.compile(r"youtube\.com/post/", re.IGNORECASE)
TIKTOK_RE = re.compile(r"https?://(?:[\w-]+\.)*tiktok\.com/", re.IGNORECASE)
TIKTOK_SHORT_RE = re.compile(
    r"https?://(?:(?:vm|vt)\.tiktok\.com|(?:www\.)?tiktok\.com/t)/", re.IGNORECASE
)
# По таким параметрам в раскрытой короткой ссылке видно, что это история (Story).
TIKTOK_STORY_RE = re.compile(r"[?&](?:story_type=1|aweme_type=40)(?:&|$)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wav"}


def find_urls(text: str) -> list[str]:
    seen: list[str] = []
    for url in URL_RE.findall(text or ""):
        url = url.rstrip(".,)")
        if url not in seen:
            seen.append(url)
    return seen


def browser_link(url: str) -> str | None:
    """Где скачать то же самое в браузере, если бот не справился (только YouTube)."""
    return youtube.browser_link(url) if YT_RE.match(url) else None


def _clean(error: object) -> str:
    text = ANSI_RE.sub("", str(error)).replace("ERROR: ", "").strip()
    if "[Instagram]" in text and "empty media response" in text:
        # Проверено: у таких постов Instagram и гостю в браузере пишет «контент
        # недоступен», и сторонние сервисы (cobalt) возвращают пустой ответ.
        return "автор ограничил просмотр этого поста (профиль 18+ или закрытый) — без входа в Instagram его не скачать."
    if "[Instagram]" in text and ("rate-limit" in text or "Failed to parse JSON" in text):
        return "Instagram временно не отвечает серверу — слишком много запросов без входа. Попробуй через несколько минут."
    return text[:300] or "неизвестная ошибка"


def _files(out: Path) -> list[Path]:
    skip = {".part", ".ytdl", ".json", ".txt"}
    return sorted(p for p in out.iterdir() if p.is_file() and p.suffix.lower() not in skip)


def _clear(out: Path) -> None:
    for p in out.iterdir():
        if p.is_file():
            p.unlink()


# ---------------------------------------------------------------- yt-dlp


def _ytdlp_opts(out: Path, height: int) -> dict:
    if HAS_FFMPEG:
        fmt = f"bv*[height<={height}]+ba/b[height<={height}]/b"
    else:
        # Без ffmpeg склеить видео и звук нечем — берём готовый файл похуже качеством.
        fmt = f"b[height<={height}]/b"
    opts = {
        "outtmpl": str(out / "%(autonumber)02d_%(id)s.%(ext)s"),
        "format": fmt,
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
    with yt_dlp.YoutubeDL(_ytdlp_opts(out, 1080)) as ydl:
        info = ydl.sanitize_info(ydl.extract_info(url, download=False) or {})
    # Разбор страницы — самая медленная часть на слабом сервере, поэтому для
    # одиночного видео делаем его один раз и дальше качаем по готовым данным.
    single = info.get("_type", "video") == "video" and bool(info.get("formats"))
    if info.get("is_live"):
        raise DownloadError("это прямой эфир, его скачать нельзя")
    if (info.get("duration") or 0) > MAX_DURATION:
        raise DownloadError("видео длиннее часа — в лимит Telegram (50 МБ) не влезет")

    with yt_dlp.YoutubeDL(_ytdlp_opts(out, 1080)) as ydl:
        if single:
            ydl.process_ie_result(copy.deepcopy(info), download=True)
        else:
            ydl.download([url])


# ------------------------------------------------------------ gallery-dl


def _gallery(url: str, out: Path) -> None:
    # Без повторов и с коротким таймаутом: если сайт требует вход, gallery-dl
    # должен сдаться быстро, а не держать пользователя минутами.
    limit = TIKTOK_MAX_ITEMS if TIKTOK_RE.match(url) else MAX_ITEMS
    cmd = [sys.executable, "-m", "gallery_dl", "-D", str(out), "--range", f"1-{limit}"]
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


# ---------------------------------------------------------------- TikTok


def _resolve_tiktok(url: str) -> str:
    """Короткие ссылки (vm.tiktok.com/…) не говорят, видео там или фото —
    раскрываем их до полной."""
    if not TIKTOK_SHORT_RE.match(url):
        return url
    try:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "facebookexternalhit/1.1"}
        )
        with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as resp:
            return resp.url
    except Exception:  # noqa: BLE001 — не вышло, пробуем как есть
        return url


def _fetch(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en", "Cookie": "SOCS=CAI"}
    )
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
        return resp.read(), resp.headers.get_content_type()


def _save(url: str, path: Path) -> Path:
    """Качает файл на диск по частям и подбирает расширение по типу содержимого."""
    types = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
        path = path.with_suffix(types.get(resp.headers.get_content_type(), path.suffix))
        with path.open("wb") as f:
            shutil.copyfileobj(resp, f)
    return path


def _tikwm(url: str, out: Path) -> None:
    """Запасной путь для TikTok: когда он блокирует IP сервера, медиа достаёт
    сторонний сервис tikwm.com (ключ не нужен)."""
    root = "https://www.tikwm.com"
    query = urllib.parse.urlencode({"url": url, "hd": 1})
    reply = json.loads(_fetch(f"{root}/api/?{query}")[0])
    if str(reply.get("code")) != "0" or not reply.get("data"):
        raise DownloadError(f"tikwm: {reply.get('msg') or 'нет данных'}")
    data = reply["data"]

    def full(link: str) -> str:
        return urllib.parse.urljoin(root, link)

    images = (data.get("images") or [])[: TIKTOK_MAX_ITEMS - 1]
    if images:
        for i, image in enumerate(images, 1):
            _save(full(image), out / f"{i:02d}.jpg")
        if data.get("music"):
            with contextlib.suppress(Exception):  # фото важнее музыки
                _save(full(data["music"]), out / f"{len(images) + 1:02d}.mp3")
        return
    # HD-версия без водяного знака, если влезает в лимит Telegram.
    hd_fits = data.get("hdplay") and int(data.get("hd_size") or 0) <= MAX_BYTES
    video = data.get("hdplay") if hd_fits else data.get("play")
    if not video:
        raise DownloadError("tikwm: в ответе нет видео")
    _save(full(video), out / f"01_{data.get('id', 'tiktok')}.mp4")


def _cobalt(url: str, out: Path) -> None:
    cobalt.fetch(url, out)


# ------------------------------------------------------------- Instagram


def _instagram_public(url: str, out: Path) -> None:
    """Запасной путь для Instagram без входа в аккаунт: фото и карусели.
    yt-dlp умеет качать только видео, но в данных поста, которые он получает
    без входа, есть и адреса фотографий — берём их оттуда."""
    opts = _ytdlp_opts(out, 1080)
    opts.pop("cookiefile", None)  # устаревшие cookies только мешают
    opts["ignore_no_formats_error"] = True
    # Instagram быстро ограничивает адреса хостингов — тогда пробуем с других адресов.
    routes = [None] + [p for p in (warp.proxy(), PROXY_URL) if p]
    for i, proxy in enumerate(routes):
        opts["proxy"] = proxy
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.sanitize_info(ydl.extract_info(url, download=False) or {})
            break
        except Exception:
            if i == len(routes) - 1:
                raise
    # Сами файлы лежат на отдельных серверах без ограничений — качаем их напрямую.
    opts["proxy"] = None
    entries = info.get("entries") or [] if info.get("_type") == "playlist" else [info]
    for i, entry in enumerate([e for e in entries if e][:MAX_ITEMS], 1):
        if entry.get("formats"):
            video = dict(opts, outtmpl=str(out / f"{i:02d}_%(id)s.%(ext)s"))
            with yt_dlp.YoutubeDL(video) as ydl:
                ydl.process_ie_result(copy.deepcopy(entry), download=True)
        elif entry.get("thumbnail"):
            # У фото «превью» — это и есть сама фотография в полном размере.
            _save(entry["thumbnail"], out / f"{i:02d}.jpg")


# --------------------------------------------------------------- YouTube


def _find_key(node: object, key: str):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _find_key(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _find_key(v, key)


def _youtube_post(url: str, out: Path, quality: int | None = None) -> Media:
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
        return youtube.download(f"https://www.youtube.com/watch?v={video_id}", out, quality)

    images = list(_find_key(attachment, "backstageImageRenderer"))[:MAX_ITEMS]
    if not images:
        raise DownloadError("в этом посте нет картинок и видео — только текст")
    types = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    for i, image in enumerate(images, 1):
        thumb = image["image"]["thumbnails"][-1]["url"]
        # «=s0» вместо параметров превью отдаёт оригинал без обрезки под квадрат.
        data, ctype = _fetch(thumb.split("=")[0] + "=s0")
        (out / f"{i:02d}{types.get(ctype, '.jpg')}").write_bytes(data)
    return Media(_files(out))


def _youtube(url: str, out: Path, quality: int | None = None) -> Media:
    try:
        if YT_POST_RE.search(url):
            return _youtube_post(url, out, quality)
        return youtube.download(url, out, quality)
    except DownloadError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DownloadError(_clean(e)) from None


# ------------------------------------------------------------------ вход


def _download_sync(url: str, out: Path, quality: int | None = None) -> Media:
    if YT_RE.match(url):
        return _youtube(url, out, quality)

    is_tiktok = bool(TIKTOK_RE.match(url))
    if is_tiktok:
        url = _resolve_tiktok(url)

    # Посты Instagram и фото-посты TikTok — это часто картинки, с ними лучше
    # справляется gallery-dl; для всего остального сначала пробуем yt-dlp.
    steps = [_gallery, _ytdlp] if PHOTO_FIRST_RE.search(url) else [_ytdlp, _gallery]
    if is_tiktok:
        steps.append(_tikwm)
    is_instagram = "instagram.com" in url.lower()
    if is_instagram and not COOKIES:
        # Без входа gallery-dl с Instagram не работает вообще, а анонимных запросов
        # Instagram даёт мало — поэтому один шаг, который берёт и видео, и фото.
        steps = [_instagram_public]
    elif is_instagram:
        steps.append(_instagram_public)
    # Последний шанс для всех сайтов — сторонний сервис cobalt (см. cobalt.py).
    steps.append(_cobalt)
    errors: list[str] = []
    for step in steps:
        _clear(out)
        try:
            step(url, out)
        except Exception as e:  # noqa: BLE001 — пробуем следующий способ
            errors.append(_clean(e))
            continue
        files = _files(out)
        if is_tiktok and step is _ytdlp and files and step is not steps[-1]:
            # Из фото-поста TikTok yt-dlp достаёт только музыку — за самими
            # фотографиями идём в gallery-dl.
            if all(f.suffix.lower() in AUDIO_EXT for f in files):
                errors.append("это фото-пост")
                continue
        if files:
            return Media(files)
    # Показываем ошибку yt-dlp — она обычно самая понятная.
    main = errors[steps.index(_ytdlp)] if _ytdlp in steps and len(errors) == len(steps) else None
    text = main or (errors[0] if errors else "по ссылке не нашлось медиа")
    if is_tiktok and TIKTOK_STORY_RE.search(url):
        text = (
            "это история (Story) TikTok — их видно только в приложении после входа "
            "в аккаунт, по ссылке они не скачиваются. Обычные видео и фото-посты работают."
        )
    raise DownloadError(text)


async def download(url: str, out: Path, quality: int | None = None) -> Media:
    """quality — качество YouTube-видео, выбранное кнопкой; для остальных сайтов не нужно."""
    return await asyncio.to_thread(_download_sync, url, out, quality)
