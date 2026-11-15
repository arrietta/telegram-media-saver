"""ВРЕМЕННАЯ диагностика: при запуске пробует скачать короткий ролик YouTube
разными маршрутами и «клиентами» и пишет в лог, какие сочетания работают.
После выяснения файл удаляется.
"""

import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path

import yt_dlp

import warp

log = logging.getLogger("selftest")

VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
CLIENTS = [
    None, "android_vr", "visionos", "ios", "android", "tv", "tv_simply",
    "tv_downgraded", "web_embedded", "mweb", "web_safari", "web_music",
]


class _Quiet:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _run() -> None:
    for _ in range(20):  # ждём, пока поднимется WARP
        if warp.proxy():
            break
        time.sleep(3)
    routes: dict[str, str | None] = {"direct": None}
    if warp.proxy():
        routes = {
            "warp": warp.PROXY,
            "warp-h": warp.PROXY.replace("socks5://", "socks5h://"),
            "direct": None,
        }
    ok: list[str] = []
    for route, proxy in routes.items():
        for client in CLIENTS:
            tmp = Path(tempfile.mkdtemp(prefix="selftest_"))
            opts = {
                "outtmpl": str(tmp / "v.%(ext)s"),
                "format": "wv*/w",
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "logger": _Quiet(),
                "socket_timeout": 15,
                "retries": 1,
                "extractor_retries": 0,
            }
            if proxy:
                opts["proxy"] = proxy
            if client:
                opts["extractor_args"] = {"youtube": {"player_client": [client]}}
            name = f"{route}/{client or 'default'}"
            stage = "info"
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.sanitize_info(ydl.extract_info(VIDEO, download=False))
                    formats = len(info.get("formats") or [])
                    stage = f"download ({formats} formats)"
                    ydl.process_ie_result(info, download=True)
                size = sum(p.stat().st_size for p in tmp.iterdir())
                log.info("SELFTEST %s: OK, %d formats, %d bytes", name, formats, size)
                ok.append(name)
            except Exception as e:  # noqa: BLE001
                log.info(
                    "SELFTEST %s: FAIL at %s: %s", name, stage, str(e).replace("ERROR: ", "")[:110]
                )
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    log.info("SELFTEST DONE, working: %s", ", ".join(ok) or "none")


def start_in_background() -> None:
    threading.Thread(target=_run, daemon=True, name="selftest").start()
