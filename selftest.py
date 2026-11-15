"""ВРЕМЕННО: при запуске качает один длинный ролик через Invidious и пишет
в лог, удалась ли загрузка и склейка. После проверки файл удаляется."""

import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path

import warp
import youtube

log = logging.getLogger("selftest")


def _run() -> None:
    for _ in range(15):
        if warp.proxy():
            break
        time.sleep(3)
    for video_id, quality in (("Haa0V072Prg", None), ("Haa0V072Prg", 320), ("jNQXAC9IVRw", None)):
        tmp = Path(tempfile.mkdtemp(prefix="selftest_"))
        started = time.time()
        try:
            media = youtube._via_invidious(video_id, tmp, quality)
            f = media.files[0]
            log.info(
                "SELFTEST %s q=%s: OK %d bytes, %sx%s, quality=%s, menu=%s, probe=%s, %.0fs",
                video_id, quality, f.stat().st_size, media.width, media.height, media.quality,
                media.qualities, youtube._probe(f), time.time() - started,
            )
        except Exception as e:  # noqa: BLE001
            log.info("SELFTEST %s q=%s: FAIL %s: %s, %.0fs", video_id, quality, type(e).__name__, e, time.time() - started)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    log.info("SELFTEST DONE")


def start_in_background() -> None:
    threading.Thread(target=_run, daemon=True, name="selftest").start()
