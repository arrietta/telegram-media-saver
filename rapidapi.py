"""Instagram через сторонний API «Download All in One!» на RapidAPI
(https://rapidapi.com/coder2077/api/instagram-downloader-download-instagram-videos-stories1).

У сервиса свои аккаунты Instagram, поэтому он видит и посты, которые гостю не
показывают (профили 18+), — без входа в твой аккаунт и без cookies. Нужен только
ключ RapidAPI в переменной RAPIDAPI_KEY и подписка на бесплатный план Basic:
1000 кредитов в месяц, ссылка стоит 2 кредита (неудачная — 0), то есть ~500 ссылок.
Бот зовёт его, только когда без него не вышло, так что лимит тратится на 18+ посты.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from common import SSL_CONTEXT, DownloadError, save_file

log = logging.getLogger("rapidapi")

HOST = "instagram-downloader-download-instagram-videos-stories1.p.rapidapi.com"
KEY = (os.getenv("RAPIDAPI_KEY") or "").strip() or None
MAX_ITEMS = 10


def enabled() -> bool:
    return KEY is not None


def _call(url: str) -> dict:
    query = urllib.parse.urlencode({"url": url})
    req_url = f"https://{HOST}/fetch?{query}"
    try:
        req = urllib.request.Request(
            req_url, headers={"x-rapidapi-host": HOST, "x-rapidapi-key": KEY, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        log.info("rapidapi HTTP %s: %s", e.code, body)
        if e.code == 403 and "not subscribed" in body.lower():
            raise DownloadError(
                "ключ RapidAPI не подписан на API «Download All in One!» — открой его страницу "
                "и нажми Start Free Plan"
            ) from None
        if e.code in (401, 403):
            raise DownloadError("RapidAPI не принял ключ — проверь переменную RAPIDAPI_KEY в Render") from None
        if e.code == 429:
            raise DownloadError(
                "закончился бесплатный лимит стороннего сервиса на этот месяц (~500 ссылок) — "
                "он обновится в начале следующего месяца"
            ) from None
        raise DownloadError(f"сторонний сервис ответил ошибкой {e.code}") from None


def fetch(url: str, out: Path) -> None:
    if not KEY:
        raise DownloadError("сторонний сервис не настроен (нет RAPIDAPI_KEY)")
    data = _call(url)
    if not data.get("ok"):
        message = str(data.get("error") or data.get("message") or "нет данных")[:150]
        log.info("rapidapi refused %s: %s", url, message)
        raise DownloadError(f"сторонний сервис не смог достать этот пост ({message})")
    parts = (data.get("items") or []) if data.get("type") == "album" else [data]
    for i, part in enumerate(parts[:MAX_ITEMS], 1):
        link = part.get("download_url")
        if not link:
            continue
        ext = ".mp4" if part.get("type") in ("video", "gif") else ".jpg"
        save_file(link, out / f"{i:02d}{ext}")
    if not any(out.iterdir()):
        raise DownloadError("сторонний сервис вернул пост без файлов")
    log.info("rapidapi: got %d file(s) for %s", len(list(out.iterdir())), url)
