"""Instagram с входом в аккаунт — для постов, которые гостю не показывают
(профили 18+, ограничения автора).

Берёт cookies Instagram из cookies.txt на хостинге (Secret File в Render) и
спрашивает пост у того же API, что и сайт instagram.com у вошедшего человека.
Обычные посты бот сначала пробует без входа — аккаунт используем, только
когда без него не вышло.

Значения cookies никуда не пишутся; в лог попадает только, есть ли вход и жив ли он.
"""

import http.cookiejar
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from common import BROWSER_UA, COOKIES, SSL_CONTEXT, DownloadError, save_file

log = logging.getLogger("instagram")

API = "https://www.instagram.com/api/v1"
APP_ID = "936619743392459"  # то же, что у сайта instagram.com
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
MAX_ITEMS = 10

EXPIRED = (
    "вход в Instagram в cookies.txt на хостинге устарел — экспортируй cookies заново "
    "и обнови Secret File в Render"
)


def _jar() -> http.cookiejar.MozillaCookieJar | None:
    if not COOKIES:
        return None
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(COOKIES, ignore_discard=True, ignore_expires=True)
    except Exception as e:  # noqa: BLE001 — файл битый или не в формате Netscape
        log.warning("cookies.txt is not readable: %s", type(e).__name__)
        return None
    return jar


def _cookie(jar: http.cookiejar.CookieJar, name: str) -> str | None:
    for c in jar:
        if c.name == name and c.domain.lstrip(".").endswith("instagram.com"):
            return c.value
    return None


def has_session() -> bool:
    """Есть ли в cookies.txt вход в Instagram (cookie sessionid)."""
    jar = _jar()
    return bool(jar and _cookie(jar, "sessionid"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Редирект на страницу входа = сессия не принята; ловим его, а не идём следом."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _get(jar: http.cookiejar.CookieJar, url: str) -> dict:
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=SSL_CONTEXT),
        _NoRedirect(),
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "X-IG-App-ID": APP_ID,
            "X-ASBD-ID": "359341",
            "X-IG-WWW-Claim": "0",
            "X-CSRFToken": _cookie(jar, "csrftoken") or "",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/",
            "Accept": "*/*",
        },
    )
    try:
        with opener.open(req, timeout=25) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            if "/accounts/login" in (e.headers.get("Location") or "") or e.code == 302:
                raise DownloadError(EXPIRED) from None
        body = e.read().decode("utf-8", "replace")
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        message = str(data.get("message") or "")
        if e.code in (401, 403) or "login_required" in message or data.get("require_login"):
            raise DownloadError(EXPIRED) from None
        if "checkpoint" in message or data.get("checkpoint_url"):
            raise DownloadError(
                "Instagram попросил подтвердить вход в аккаунт из cookies.txt — открой Instagram "
                "в браузере под этим аккаунтом, пройди проверку и экспортируй cookies заново"
            ) from None
        raise DownloadError(f"Instagram ответил {e.code}: {message or body[:100]}") from None


def _shortcode(url: str) -> str:
    for part in ("/p/", "/reel/", "/reels/", "/tv/"):
        if part in url:
            return url.split(part, 1)[1].split("/")[0].split("?")[0]
    raise DownloadError("не понял ссылку на пост Instagram")


def _media_id(shortcode: str) -> int:
    pk = 0
    for ch in shortcode[:11]:  # у закрытых постов к коду приклеено ещё 28 символов
        pk = pk * 64 + ALPHABET.index(ch)
    return pk


def check() -> str:
    """Короткий статус входа для лога при запуске: none / no-session / ok / expired / error."""
    jar = _jar()
    if jar is None:
        return "none"
    if not _cookie(jar, "sessionid"):
        return "no-session"
    try:
        _get(jar, f"{API}/accounts/current_user/?edit=true")
        return "ok"
    except DownloadError as e:
        return "expired" if str(e) == EXPIRED else f"error ({str(e)[:80]})"
    except Exception as e:  # noqa: BLE001
        return f"error ({type(e).__name__})"


def fetch(url: str, out: Path) -> None:
    """Скачивает пост, рилс или карусель с входом в аккаунт."""
    jar = _jar()
    if jar is None or not _cookie(jar, "sessionid"):
        raise DownloadError(
            "этот пост Instagram показывает только вошедшим в аккаунт, а в cookies.txt "
            "на хостинге нет входа в Instagram"
        )
    try:
        data = _get(jar, f"{API}/media/{_media_id(_shortcode(url))}/info/")
    except DownloadError as e:
        if "Media not found" in str(e) or "400" in str(e):
            raise DownloadError(
                "даже с входом в аккаунт Instagram не показывает этот пост — возможно, у аккаунта "
                "в cookies.txt не подтверждён возраст 18+, автор его заблокировал или пост удалён"
            ) from None
        raise
    items = data.get("items") or []
    if not items:
        raise DownloadError("Instagram вернул пустой ответ даже с входом в аккаунт")
    item = items[0]
    parts = item.get("carousel_media") or [item]
    for i, part in enumerate(parts[:MAX_ITEMS], 1):
        videos = part.get("video_versions") or []
        images = (part.get("image_versions2") or {}).get("candidates") or []
        if videos:
            save_file(videos[0]["url"], out / f"{i:02d}.mp4")
        elif images:
            # Первый вариант — самый большой.
            save_file(images[0]["url"], out / f"{i:02d}.jpg")
    if not any(out.iterdir()):
        raise DownloadError("в этом посте Instagram не нашлось ни фото, ни видео")
