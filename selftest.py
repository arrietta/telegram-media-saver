"""ВРЕМЕННО: при запуске проверяет, как сервер Invidious отдаёт куски файла
с этого хостинга — напрямую и через WARP, кусками разного размера."""

import logging
import threading
import time

import yt_dlp
from yt_dlp.networking import Request

import invidious
import warp
from common import BROWSER_UA

log = logging.getLogger("selftest")


def _run() -> None:
    for _ in range(15):
        if warp.proxy():
            break
        time.sleep(3)
    try:
        data = invidious.info("Haa0V072Prg")
    except Exception as e:  # noqa: BLE001
        log.info("SELFTEST info FAIL %s", e)
        return
    pick = [p for p in data["picks"] if p["quality"] == 320][0]
    routes = {"direct": None}
    if warp.proxy():
        routes["warp"] = warp.PROXY
    for name, proxy in routes.items():
        opts = {"quiet": True, "socket_timeout": 30}
        if proxy:
            opts["proxy"] = proxy
        with yt_dlp.YoutubeDL(opts) as ydl:
            for kind in ("audio", "video"):
                for size in (256 * 1024, 1024 * 1024, 4 * 1024 * 1024, 8 * 1024 * 1024):
                    for attempt in (1, 2):
                        started = time.time()
                        try:
                            req = Request(pick[kind], headers={"User-Agent": BROWSER_UA, "Range": f"bytes=0-{size - 1}"})
                            got = len(ydl.urlopen(req).read())
                            result = f"OK {got} bytes" if got == size else f"SHORT {got} bytes"
                        except Exception as e:  # noqa: BLE001
                            result = f"FAIL {str(e)[:70]}"
                        log.info("SELFTEST %s %s %dK #%d: %s, %.1fs", name, kind, size // 1024, attempt, result, time.time() - started)
    log.info("SELFTEST DONE")


def start_in_background() -> None:
    threading.Thread(target=_run, daemon=True, name="selftest").start()
