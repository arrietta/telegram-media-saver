"""Скачивание медиа по ссылке: yt-dlp для видео, gallery-dl для фото и каруселей."""

import asyncio
import contextlib
import copy
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import certifi
import yt_dlp

# Bot API не принимает файлы больше 50 МБ — оставляем небольшой запас.
MAX_BYTES = 49 * 1024 * 1024
MAX_DURATION = 60 * 60  # секунд; более длинное видео всё равно не влезет в 50 МБ
MAX_ITEMS = 10  # столько файлов помещается в один альбом Telegram
TIKTOK_MAX_ITEMS = 36  # в слайд-шоу TikTok до 35 фото + звуковая дорожка
# Качества, которые бот предлагает на выбор для YouTube (как на ssyoutube.com).
YT_HEIGHTS = (1080, 720, 480, 360, 240, 144)
YT_CACHE_TTL = 30 * 60  # секунд; прямые ссылки YouTube живут несколько часов

URL_RE = re.compile(
    r"https?://(?:[\w-]+\.)*(?:instagram\.com|youtube\.com|youtu\.be|tiktok\.com)/[^\s<>]+",
    re.IGNORECASE,
)
PHOTO_FIRST_RE = re.compile(r"instagram\.com/p/|tiktok\.com/.+/photo/", re.IGNORECASE)
YT_POST_RE = re.compile(r"youtube\.com/post/", re.IGNORECASE)
YT_RE = re.compile(r"https?://(?:[\w-]+\.)*(?:youtube\.com|youtu\.be)/", re.IGNORECASE)
TIKTOK_RE = re.compile(r"https?://(?:[\w-]+\.)*tiktok\.com/", re.IGNORECASE)
TIKTOK_SHORT_RE = re.compile(
    r"https?://(?:(?:vm|vt)\.tiktok\.com|(?:www\.)?tiktok\.com/t)/", re.IGNORECASE
)
# По таким параметрам в раскрытой короткой ссылке видно, что это история (Story).
TIKTOK_STORY_RE = re.compile(r"[?&](?:story_type=1|aweme_type=40)(?:&|$)")
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
    if "confirm you" in text and "not a bot" in text:
        return (
            "YouTube не отдаёт видео серверу без входа в аккаунт. Владельцу бота нужно "
            "добавить на хостинг файл cookies.txt (см. README)"
            + (" — текущий файл устарел, его надо обновить." if COOKIES else ".")
        )
    return text[:300] or "неизвестная ошибка"


def _files(out: Path) -> list[Path]:
    skip = {".part", ".ytdl", ".json", ".txt"}
    return sorted(p for p in out.iterdir() if p.is_file() and p.suffix.lower() not in skip)


def _clear(out: Path) -> None:
    for p in out.iterdir():
        if p.is_file():
            p.unlink()


def _ytdlp_opts(out: Path, height: int, audio: bool = False) -> dict:
    if audio:
        # m4a Telegram проигрывает как музыку без перекодирования.
        fmt = "ba[ext=m4a]/ba/b"
    elif HAS_FFMPEG:
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
    if audio and HAS_FFMPEG:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
    if COOKIES:
        opts["cookiefile"] = COOKIES
    return opts


def _extract(url: str) -> dict:
    try:
        with yt_dlp.YoutubeDL(_ytdlp_opts(Path(tempfile.gettempdir()), 720)) as ydl:
            return ydl.sanitize_info(ydl.extract_info(url, download=False) or {})
    except Exception:
        # YouTube часто не пускает серверные IP — тогда спрашиваем сторонний API.
        video_id = YT_ID_RE.search(url) if RAPIDAPI_KEY and is_youtube_video(url) else None
        if not video_id:
            raise
        try:
            return _rapidapi_info(video_id.group(1))
        except Exception:  # noqa: BLE001 — запасной путь не помог, покажем исходную ошибку
            pass
        raise


RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST = "youtube-media-downloader.p.rapidapi.com"


def _rapidapi_info(video_id: str) -> dict:
    """Данные о ролике через «YouTube Media Downloader» на RapidAPI — в том же
    виде, что отдаёт yt-dlp, чтобы дальше работал обычный код (меню, склейка)."""
    req = urllib.request.Request(
        f"https://{RAPIDAPI_HOST}/v2/video/details?videoId={video_id}&urlAccess=normal",
        headers={"X-RapidAPI-Key": RAPIDAPI_KEY or "", "X-RapidAPI-Host": RAPIDAPI_HOST},
    )
    with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
        data = json.loads(resp.read())
    if data.get("errorId") != "Success":
        raise DownloadError(f"RapidAPI: {data.get('errorId') or data.get('message')}")
    duration = int(data.get("lengthSeconds") or 0)

    formats = []
    streams = [(v, True) for v in (data.get("videos") or {}).get("items") or []]
    streams += [(a, False) for a in (data.get("audios") or {}).get("items") or []]
    for i, (item, is_video) in enumerate(streams):
        if not item.get("url") or item.get("isDrc"):
            continue
        codecs = re.search(r'codecs="([^"]*)"', item.get("mimeType") or "")
        codecs = [c.strip() for c in codecs.group(1).split(",")] if codecs else ["unknown"]
        size = int(item.get("size") or 0)
        fmt = {
            "format_id": str(i),
            "url": item["url"],
            "ext": {"weba": "webm"}.get(item.get("extension"), item.get("extension") or "mp4"),
            "protocol": "https",
            "filesize": size or None,
            "tbr": size * 8 / 1000 / duration if size and duration else None,
            # Без скачивания кусками YouTube режет скорость (так же делает yt-dlp).
            "downloader_options": {"http_chunk_size": 10 * 1024 * 1024},
        }
        if is_video:
            fmt["vcodec"] = codecs[0]
            fmt["acodec"] = (codecs[1] if len(codecs) > 1 else "aac") if item.get("hasAudio") else "none"
            fmt["height"], fmt["width"] = item.get("height"), item.get("width")
        else:
            fmt["vcodec"], fmt["acodec"] = "none", codecs[0]
        formats.append(fmt)
    if not formats:
        raise DownloadError("RapidAPI: нет ссылок на видео")
    return {
        "id": video_id,
        "title": data.get("title") or "",
        "duration": duration,
        "is_live": bool(data.get("isLiveNow")),
        "formats": formats,
        "extractor": "youtube",
        "extractor_key": "Youtube",
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
    }


def _is_single(info: dict) -> bool:
    return info.get("_type", "video") == "video" and bool(info.get("formats"))


def _ytdlp(
    url: str,
    out: Path,
    *,
    info: dict | None = None,
    heights: tuple[int, ...] | None = None,
    audio: bool = False,
) -> None:
    """heights/audio задаются, когда качество выбрал пользователь (меню YouTube)."""
    chosen = heights is not None or audio
    if info is None:
        info = _extract(url)
    # Разбор страницы — самая медленная часть на слабом сервере, поэтому для
    # одиночного видео делаем его один раз и дальше качаем по готовым данным.
    single = _is_single(info)
    if info.get("is_live"):
        raise DownloadError("это прямой эфир, его скачать нельзя")
    if not chosen and (info.get("duration") or 0) > MAX_DURATION:
        raise DownloadError("видео длиннее часа — в лимит Telegram (50 МБ) не влезет")

    # На YouTube есть выбор качества: понижаем его, пока файл не влезет в лимит.
    if audio:
        heights = (0,)
    elif heights is None:
        heights = (720, 480, 360) if YT_RE.match(url) else (1080,)
    for height in heights:
        _clear(out)
        with yt_dlp.YoutubeDL(_ytdlp_opts(out, height, audio)) as ydl:
            if single:
                ydl.process_ie_result(copy.deepcopy(info), download=True)
            else:
                ydl.download([url])
        files = _files(out)
        if files and max(f.stat().st_size for f in files) <= MAX_BYTES:
            return


@dataclass
class YoutubeMenu:
    """Что показать пользователю перед скачиванием ролика с YouTube."""

    video_id: str
    title: str
    duration: int
    # (ключ, подпись): ключ — высота кадра или "audio".
    options: list[tuple[str, str]] = field(default_factory=list)
    hidden: int = 0  # сколько качеств не влезло в лимит Telegram


_yt_cache: dict[str, tuple[float, dict]] = {}


def _yt_cached(video_id: str) -> dict | None:
    now = time.monotonic()
    for key in [k for k, (ts, _) in _yt_cache.items() if now - ts > YT_CACHE_TTL]:
        del _yt_cache[key]
    entry = _yt_cache.get(video_id)
    return entry[1] if entry else None


def _selected(info: dict, height: int, audio: bool = False) -> tuple[int, int | None]:
    """Какое качество реально достанется при таком выборе и сколько оно весит
    (None — размер неизвестен). Сеть не трогает: считает по готовым данным."""
    with yt_dlp.YoutubeDL(_ytdlp_opts(Path(tempfile.gettempdir()), height, audio)) as ydl:
        res = ydl.process_ie_result(copy.deepcopy(info), download=False)
    total = 0
    for f in res.get("requested_formats") or [res]:
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and f.get("tbr") and info.get("duration"):
            size = f["tbr"] * 1000 / 8 * info["duration"]
        if not size:
            return res.get("height") or 0, None
        total += int(size)
    return res.get("height") or 0, total


def _mb(size: int | None) -> str:
    if size is None:
        return ""
    return f" · {max(size / 1024 / 1024, 0.1):.1f} МБ".replace(".0 ", " ")


def _youtube_menu_sync(url: str) -> YoutubeMenu | None:
    info = _extract(url)
    if not _is_single(info) or not info.get("id"):
        return None  # плейлист, канал и т. п. — пусть идёт обычным путём
    if info.get("is_live"):
        raise DownloadError("это прямой эфир, его скачать нельзя")
    _yt_cache[info["id"]] = (time.monotonic(), info)

    menu = YoutubeMenu(info["id"], info.get("title") or "", int(info.get("duration") or 0))
    seen: set[int] = set()
    for height in YT_HEIGHTS:
        try:
            real, size = _selected(info, height)
        except Exception:  # noqa: BLE001 — такого качества у ролика нет
            continue
        if not real or real in seen:
            continue
        seen.add(real)
        if size is not None and size > MAX_BYTES:
            menu.hidden += 1
            continue
        menu.options.append((str(real), f"📹 {real}p{_mb(size)}"))
    try:
        _, size = _selected(info, 0, audio=True)
        if size is None or size <= MAX_BYTES:
            menu.options.append(("audio", f"🎵 Аудио{_mb(size)}"))
    except Exception:  # noqa: BLE001
        pass
    if not menu.options:
        raise DownloadError("даже в самом низком качестве видео больше 50 МБ — лимит Telegram")
    return menu


def _youtube_download_sync(video_id: str, choice: str, out: Path) -> tuple[list[Path], str]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        info = _yt_cached(video_id) or _extract(url)
        if choice == "audio":
            _ytdlp(url, out, info=info, audio=True)
        else:
            # Размер в меню — оценка; если не угадали, спускаемся на ступень ниже.
            heights = tuple(h for h in YT_HEIGHTS if h <= int(choice)) or (int(choice),)
            _ytdlp(url, out, info=info, heights=heights)
    except DownloadError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DownloadError(_clean(e)) from None
    files = _files(out)
    if not files:
        raise DownloadError("по ссылке не нашлось медиа")
    return files, info.get("title") or ""


YT_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})")


def ssyoutube_link(url: str) -> str | None:
    """Ссылка на тот же ролик на ssyoutube.com — там его можно скачать в браузере,
    когда бот не может (YouTube требует вход или файл больше 50 МБ)."""
    match = YT_ID_RE.search(url) if is_youtube_video(url) else None
    return f"https://ssyoutube.com/watch?v={match.group(1)}" if match else None


def is_youtube_video(url: str) -> bool:
    """Ссылки YouTube, для которых стоит показать меню качества."""
    return bool(YT_RE.match(url)) and not YT_POST_RE.search(url)


async def youtube_menu(url: str) -> YoutubeMenu | None:
    try:
        return await asyncio.to_thread(_youtube_menu_sync, url)
    except DownloadError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DownloadError(_clean(e)) from None


async def youtube_download(video_id: str, choice: str, out: Path) -> tuple[list[Path], str]:
    return await asyncio.to_thread(_youtube_download_sync, video_id, choice, out)


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
        if is_tiktok and step is _ytdlp and files and step is not steps[-1]:
            # Из фото-поста TikTok yt-dlp достаёт только музыку — за самими
            # фотографиями идём в gallery-dl.
            if all(f.suffix.lower() in AUDIO_EXT for f in files):
                errors.append("это фото-пост")
                continue
        if files:
            return files
    # Показываем ошибку yt-dlp — она обычно самая понятная.
    main = errors[steps.index(_ytdlp)] if len(errors) == len(steps) else None
    text = main or (errors[0] if errors else "по ссылке не нашлось медиа")
    if is_tiktok and TIKTOK_STORY_RE.search(url):
        text = (
            "это история (Story) TikTok — их видно только в приложении после входа "
            "в аккаунт, по ссылке они не скачиваются. Обычные видео и фото-посты работают."
        )
    if is_instagram and not COOKIES:
        text = f"Instagram не отдаёт это серверу без входа в аккаунт (нужен cookies.txt). {text}"
    raise DownloadError(text)


async def download(url: str, out: Path) -> list[Path]:
    return await asyncio.to_thread(_download_sync, url, out)
