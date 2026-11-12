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
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import certifi
import yt_dlp

# Bot API не принимает файлы больше 50 МБ — оставляем небольшой запас.
MAX_BYTES = 49 * 1024 * 1024
MAX_DURATION = 60 * 60  # секунд; более длинное видео всё равно не влезет в 50 МБ
MAX_ITEMS = 10  # столько файлов помещается в один альбом Telegram
TIKTOK_MAX_ITEMS = 36  # в слайд-шоу TikTok до 35 фото + звуковая дорожка
# Качества YouTube от лучшего к худшему; выше 720p в чате смысла мало, а файл тяжелее.
YT_HEIGHTS = (720, 480, 360, 240, 144)

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


log = logging.getLogger("downloader")


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
            "YouTube не пустил сервер, а переменная RAPIDAPI_KEY на хостинге не задана — "
            "бот её не видит. Проверь имя переменной в Render и перезапусти сервис."
        )
    return text[:300] or "неизвестная ошибка"


def _files(out: Path) -> list[Path]:
    skip = {".part", ".ytdl", ".json", ".txt"}
    return sorted(p for p in out.iterdir() if p.is_file() and p.suffix.lower() not in skip)


def _clear(out: Path) -> None:
    for p in out.iterdir():
        if p.is_file():
            p.unlink()


def _ytdlp_opts(out: Path, height: int) -> dict:
    if HAS_FFMPEG:
        # Сначала дорожка «только видео» + звук: иначе из-за сортировки по aac
        # yt-dlp на YouTube всегда берёт готовый файл 360p вместо нужного качества.
        fmt = f"bv[height<={height}]+ba/b[height<={height}]/bv*[height<={height}]+ba/b"
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


def _extract(url: str) -> dict:
    try:
        with yt_dlp.YoutubeDL(_ytdlp_opts(Path(tempfile.gettempdir()), 720)) as ydl:
            return ydl.sanitize_info(ydl.extract_info(url, download=False) or {})
    except Exception:
        # YouTube часто не пускает серверные IP — тогда спрашиваем сторонний API.
        video_id = YT_ID_RE.search(url) if is_youtube_video(url) else None
        if not video_id:
            raise
        if not RAPIDAPI_KEY:
            log.warning("YouTube refused %s and RAPIDAPI_KEY is not set", url)
            raise
        try:
            return _rapidapi_info(video_id.group(1))
        except Exception as e:  # noqa: BLE001
            log.warning("RapidAPI fallback failed for %s: %s", url, e)
            raise DownloadError(
                f"YouTube не пустил сервер, а запасной путь через RapidAPI не сработал: {_clean(e)}"
            ) from None


RAPIDAPI_KEY = (os.getenv("RAPIDAPI_KEY") or "").strip()
RAPIDAPI_HOST = "youtube-media-downloader.p.rapidapi.com"


def _rapidapi_info(video_id: str) -> dict:
    """Данные о ролике через «YouTube Media Downloader» на RapidAPI — в том же
    виде, что отдаёт yt-dlp, чтобы дальше работал обычный код (меню, склейка)."""
    req = urllib.request.Request(
        f"https://{RAPIDAPI_HOST}/v2/video/details?videoId={video_id}&urlAccess=normal",
        headers={"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": RAPIDAPI_HOST},
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # В теле ответа RapidAPI пишет причину: неверный ключ, нет подписки, кончилась квота.
        raise DownloadError(f"HTTP {e.code}: {e.read(200).decode(errors='replace')}") from None
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
            # API отдаёт числа строками, а yt-dlp сравнивает высоту как число.
            # У вертикальных Shorts «720p» — это ширина, а высота 1280. Качество
            # считаем по короткой стороне, иначе Shorts всегда выходили бы в 360p.
            sides = [int(item.get(k) or 0) for k in ("width", "height")]
            fmt["height"], fmt["width"] = (min(sides) or None), (max(sides) or None)
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


def _selected(info: dict, height: int) -> tuple[int, int | None]:
    """Какое качество реально достанется при таком ограничении и сколько оно
    весит (None — размер неизвестен). Сеть не трогает: считает по готовым данным."""
    with yt_dlp.YoutubeDL(_ytdlp_opts(Path(tempfile.gettempdir()), height)) as ydl:
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


def _youtube_heights(info: dict) -> list[int]:
    """Качества YouTube от лучшего к худшему, которые по оценке влезают в лимит
    Telegram, — чтобы не качать зря то, что всё равно не отправить."""
    heights: list[int] = []
    for height in YT_HEIGHTS:
        try:
            real, size = _selected(info, height)
        except Exception:  # noqa: BLE001 — такого качества у ролика нет
            continue
        if real and real not in heights and (size is None or size <= MAX_BYTES):
            heights.append(real)
    if not heights:
        raise DownloadError(
            "видео слишком длинное: даже в самом низком качестве оно больше 50 МБ — "
            "это лимит Telegram для ботов"
        )
    return heights


def _ytdlp(url: str, out: Path) -> None:
    info = _extract(url)
    # Разбор страницы — самая медленная часть на слабом сервере, поэтому для
    # одиночного видео делаем его один раз и дальше качаем по готовым данным.
    single = _is_single(info)
    if info.get("is_live"):
        raise DownloadError("это прямой эфир, его скачать нельзя")

    # На YouTube есть выбор качества: берём лучшее, что влезает в лимит Telegram,
    # и спускаемся ниже, если оценка размера не оправдалась.
    if single and YT_RE.match(url):
        heights = _youtube_heights(info)
    else:
        if (info.get("duration") or 0) > MAX_DURATION:
            raise DownloadError("видео длиннее часа — в лимит Telegram (50 МБ) не влезет")
        heights = [720, 480, 360] if YT_RE.match(url) else [1080]
    for height in heights:
        _clear(out)
        with yt_dlp.YoutubeDL(_ytdlp_opts(out, height)) as ydl:
            if single:
                ydl.process_ie_result(copy.deepcopy(info), download=True)
            else:
                ydl.download([url])
        files = _files(out)
        if files and max(f.stat().st_size for f in files) <= MAX_BYTES:
            return


YT_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})")


def is_youtube_video(url: str) -> bool:
    return bool(YT_RE.match(url)) and not YT_POST_RE.search(url)


def ssyoutube_link(url: str) -> str | None:
    """Ссылка на тот же ролик на ssyoutube.com — там его можно скачать в браузере,
    когда бот не может (YouTube требует вход или файл больше 50 МБ)."""
    match = YT_ID_RE.search(url) if is_youtube_video(url) else None
    return f"https://ssyoutube.com/watch?v={match.group(1)}" if match else None


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
