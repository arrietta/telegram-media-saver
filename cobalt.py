"""Запасной путь через cobalt — открытый сервис-скачивалку (https://github.com/imputnet/cobalt).

Его копии («экземпляры») держат добровольцы на своих серверах; мы отправляем
туда ссылку, а в ответ получаем готовый файл. Ни ключей, ни входа в аккаунт,
ни cookies не нужно. Используем только экземпляры без защиты от ботов — то есть
те, владельцы которых сами открыли их для программ.

Экземпляры появляются и пропадают, поэтому список берём с cobalt.directory
(там их проверяют каждые 10 минут) и перебираем по очереди. Свой или любимый
экземпляр можно задать переменной COBALT_API (несколько — через запятую).
"""

import json
import logging
import os
import re
import time
from pathlib import Path

from common import DownloadError, TooBig, open_url, save_file

log = logging.getLogger("cobalt")

DIRECTORY = "https://cobalt.directory/"
# На случай, если каталог недоступен: экземпляры, открытые на момент написания.
FALLBACK = ["rue-cobalt.xenon.zone", "cobaltapi.cjs.nz"]
MAX_INSTANCES = 4
MAX_ITEMS = 10
_cache: tuple[float, list[str]] = (0.0, [])


def _discover() -> list[str]:
    """Экземпляры из каталога, у которых выключена защита от ботов (колонка Turnstile)."""
    with open_url(DIRECTORY, timeout=15) as resp:
        page = resp.read().decode("utf-8", "replace")
    found = []
    for row in re.findall(r"<tr[\s\S]*?</tr>", page):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[\s\S]*?</td>", row)]
        if len(cells) >= 5 and cells[4] == "❌" and re.fullmatch(r"[\w.-]+\.[a-z]{2,}", cells[1]):
            found.append(cells[1])  # каталог уже отсортирован от лучших к худшим
    return found


def _instances() -> list[str]:
    global _cache
    own = [
        h.strip().removeprefix("https://").strip("/")
        for h in os.getenv("COBALT_API", "").split(",")
        if h.strip()
    ]
    if time.time() - _cache[0] > 3600:
        try:
            found = _discover()
        except Exception as e:  # noqa: BLE001 — каталог недоступен, берём запасной список
            log.info("cobalt.directory is unavailable: %s", e)
            found = []
        _cache = (time.time(), found)
    return list(dict.fromkeys(own + _cache[1] + FALLBACK))[:MAX_INSTANCES]


def _title(filename: str) -> str | None:
    """«Название - Канал (720p, h264).mp4» → «Название - Канал»."""
    title = re.sub(r"\s*\([^()]*\)\s*$", "", Path(filename).stem).strip()
    return title or None


def fetch(url: str, out: Path, qualities: tuple[str, ...] = ("720", "480", "360")) -> tuple[list[Path], str | None]:
    """Скачивает медиа по ссылке в папку out. Возвращает файлы и название (если есть)."""
    last = "нет доступных серверов"
    for host in _instances():
        for quality in qualities:
            for old in out.iterdir():
                old.unlink()
            try:
                body = json.dumps(
                    {"url": url, "videoQuality": quality, "youtubeVideoCodec": "h264", "filenameStyle": "basic"}
                ).encode()
                with open_url(f"https://{host}/", data=body) as resp:
                    reply = json.loads(resp.read())
                status = reply.get("status")
                if status in ("tunnel", "redirect"):
                    name = reply.get("filename") or "video.mp4"
                    path = save_file(reply["url"], out / f"01{Path(name).suffix or '.mp4'}")
                    log.info("cobalt %s: got %s at %sp", host, path.name, quality)
                    return [path], _title(name)
                if status == "picker":
                    files = []
                    for i, item in enumerate((reply.get("picker") or [])[:MAX_ITEMS], 1):
                        ext = ".jpg" if item.get("type") == "photo" else ".mp4"
                        files.append(save_file(item["url"], out / f"{i:02d}{ext}"))
                    if files:
                        log.info("cobalt %s: got %d files", host, len(files))
                        return files, None
                last = (reply.get("error") or {}).get("code") or f"непонятный ответ ({status})"
            except TooBig:
                last = "видео не влезает в лимит Telegram — 50 МБ"
                continue  # пробуем качество пониже на том же сервере
            except Exception as e:  # noqa: BLE001 — сервер лёг или отказал, идём к следующему
                body = getattr(e, "read", lambda: b"")() or b""
                try:
                    last = json.loads(body)["error"]["code"]
                except Exception:  # noqa: BLE001
                    last = str(e)[:120]
            log.info("cobalt %s failed: %s", host, last)
            break
    for old in out.iterdir():
        old.unlink()
    raise DownloadError(f"запасной сервис не справился ({last})")
