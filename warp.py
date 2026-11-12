"""Cloudflare WARP: бесплатный выход в интернет с адреса Cloudflare.

YouTube не пускает адреса хостингов, поэтому запросы к нему отправляем через
WARP. wgcf регистрирует бесплатный аккаунт WARP, wireproxy поднимает туннель
и открывает локальный SOCKS5-прокси — без прав администратора, прямо в контейнере.

Всё это эксперимент: если что-то не поднялось, proxy() возвращает None
и бот работает как раньше.
"""

import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import yt_dlp

log = logging.getLogger("warp")

PORT = 40000
PROXY = f"socks5h://127.0.0.1:{PORT}"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

_process: subprocess.Popen | None = None
_ready = False


def proxy() -> str | None:
    """Адрес прокси, если туннель поднят и жив."""
    return PROXY if _ready and _process and _process.poll() is None else None


def _run(*cmd: str, cwd: Path) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=40)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} {cmd[1]}: {(proc.stderr or proc.stdout).strip()[-200:]}")


def _trace() -> dict[str, str]:
    """Спрашивает у Cloudflare через прокси, с какого адреса мы пришли."""
    with yt_dlp.YoutubeDL({"proxy": PROXY, "quiet": True, "socket_timeout": 10}) as ydl:
        text = ydl.urlopen(TRACE_URL).read().decode()
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _start() -> None:
    global _process, _ready
    if not shutil.which("wgcf") or not shutil.which("wireproxy"):
        log.info("WARP is off: wgcf/wireproxy are not installed")
        return
    work = Path(tempfile.mkdtemp(prefix="warp_"))

    # Регистрация бесплатного аккаунта; Cloudflare иногда отвечает «слишком часто».
    for attempt in range(3):
        try:
            _run("wgcf", "register", "--accept-tos", cwd=work)
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise
            log.info("WARP: register failed (%s), retrying", e)
            time.sleep(5)
    _run("wgcf", "generate", cwd=work)

    conf = work / "wireproxy.conf"
    profile = (work / "wgcf-profile.conf").read_text()
    conf.write_text(f"{profile}\n[Socks5]\nBindAddress = 127.0.0.1:{PORT}\n")
    _process = subprocess.Popen(
        ["wireproxy", "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )

    # Туннель поднимается не мгновенно; хостинг может и вовсе не пропускать UDP.
    error: object = "no answer"
    for _ in range(6):
        time.sleep(3)
        if _process.poll() is not None:
            raise RuntimeError(f"wireproxy exited: {(_process.stderr.read() or '')[-200:]}")
        try:
            trace = _trace()
            _ready = True
            log.info("WARP is ready: ip=%s warp=%s", trace.get("ip"), trace.get("warp"))
            return
        except Exception as e:  # noqa: BLE001
            error = e
    _process.terminate()
    raise RuntimeError(f"tunnel does not pass traffic: {str(error)[:200]}")


def start_in_background() -> None:
    """Поднимает туннель, не задерживая запуск бота."""

    def run() -> None:
        try:
            _start()
        except Exception as e:  # noqa: BLE001 — бот должен работать и без WARP
            log.warning("WARP is off: %s", e)

    threading.Thread(target=run, daemon=True, name="warp").start()
