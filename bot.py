"""Telegram Media Saver: присылаешь ссылку на Instagram / YouTube / TikTok —
бот отвечает в чат самим видео, фото или альбомом.

Локально работает через long polling, на хостинге (Render) — через webhook.
"""

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import ClientSession, ClientTimeout, web
from dotenv import load_dotenv

load_dotenv()

import instagram  # noqa: E402
import warp  # noqa: E402
from common import MAX_BYTES, DownloadError, Media  # noqa: E402 — после load_dotenv,
from downloader import (  # noqa: E402 — чтобы модули увидели переменные из .env
    AUDIO_EXT,
    PHOTO_EXT,
    VIDEO_EXT,
    browser_link,
    download,
    find_urls,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# Render сам подставляет RENDER_EXTERNAL_URL; на другом хостинге задай WEBHOOK_URL.
WEBHOOK_BASE = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
PORT = int(os.getenv("PORT", "8080"))

MAX_PHOTO_BYTES = 10 * 1024 * 1024  # лимит Telegram на фото
MAX_URLS_PER_MESSAGE = 3
UPLOAD_TIMEOUT = 300
DOWNLOAD_TIMEOUT = 240
KEEP_AWAKE_INTERVAL = 600
QUALITY_RE = re.compile(r"yt:([\w-]{11}):(\d{3,4})")

dp = Dispatcher()
# На бесплатном тарифе мало памяти и CPU — не качаем больше двух ссылок сразу.
downloads = asyncio.Semaphore(2)


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "Привет! Пришли ссылку на Instagram, YouTube или TikTok — "
        "я скачаю и отправлю сюда видео или фото.\n\n"
        "Меня можно добавить и в группу: буду отвечать на ссылки там."
    )


@dp.message(F.text | F.caption)
async def on_message(message: Message) -> None:
    urls = find_urls(message.text or message.caption or "")
    if not urls:
        if message.chat.type == "private":
            await message.answer("Пришли ссылку на Instagram, YouTube или TikTok 🙂")
        return
    for url in urls[:MAX_URLS_PER_MESSAGE]:
        await handle_url(message, url)


@dp.callback_query(F.data.regexp(QUALITY_RE))
async def on_quality(callback: CallbackQuery) -> None:
    """Кнопка с другим качеством под YouTube-видео."""
    video_id, quality = QUALITY_RE.fullmatch(callback.data).groups()
    await callback.answer(f"Скачиваю в {quality}p…")
    if isinstance(callback.message, Message):
        url = f"https://www.youtube.com/watch?v={video_id}"
        await handle_url(callback.message, url, int(quality))


async def handle_url(message: Message, url: str, quality: int | None = None) -> None:
    log.info("got link %s%s", url, f" at {quality}p" if quality else "")
    status = await message.reply(f"⏳ Скачиваю в {quality}p…" if quality else "⏳ Скачиваю…")
    tmp = Path(tempfile.mkdtemp(prefix="tms_"))
    try:
        async with downloads:
            media = await asyncio.wait_for(download(url, tmp, quality), DOWNLOAD_TIMEOUT)
        log.info("downloaded %d file(s) for %s", len(media.files), url)
        with contextlib.suppress(Exception):
            await status.edit_text("📤 Отправляю…")
        async with ChatActionSender.upload_video(bot=message.bot, chat_id=message.chat.id):
            await send_files(message, media)
    except TimeoutError:
        log.warning("download timed out for %s", url)
        await message.reply("❌ Не успел скачать за 4 минуты — сайт отвечает слишком медленно.")
    except DownloadError as e:
        log.warning("download failed for %s: %s", url, e)
        await message.reply(f"❌ Не получилось скачать: {e}", reply_markup=browser_keyboard(url))
    except Exception:
        log.exception("unexpected error for %s", url)
        await message.reply("❌ Что-то пошло не так, попробуй ещё раз позже.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        with contextlib.suppress(Exception):
            await status.delete()


def browser_button(link: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🌐 Скачать в браузере (ssyoutube.com)", url=link)


def browser_keyboard(url: str) -> InlineKeyboardMarkup | None:
    """Кнопка-запасной путь для YouTube: открыть ролик на ssyoutube.com."""
    link = browser_link(url)
    return InlineKeyboardMarkup(inline_keyboard=[[browser_button(link)]]) if link else None


def quality_keyboard(media: Media) -> InlineKeyboardMarkup | None:
    """Кнопки «другое качество» под YouTube-видео: только те варианты, что влезают в Telegram."""
    if not media.video_id:
        return None
    buttons = [
        InlineKeyboardButton(
            text=f"{quality}p · {max(size // 1024 // 1024, 1)} МБ" if size else f"{quality}p",
            callback_data=f"yt:{media.video_id}:{quality}",
        )
        for quality, size in media.qualities
        if quality != media.quality
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def send_files(message: Message, media: Media) -> None:
    files = media.files
    album: list[Path] = []  # фото и видео — уходят одним альбомом
    audio: list[Path] = []
    documents: list[Path] = []
    too_big = 0

    for f in files:
        ext, size = f.suffix.lower(), f.stat().st_size
        if size > MAX_BYTES:
            too_big += 1
        elif ext in VIDEO_EXT or (ext in PHOTO_EXT and size <= MAX_PHOTO_BYTES):
            album.append(f)
        elif ext in AUDIO_EXT:
            audio.append(f)
        else:
            documents.append(f)

    for i in range(0, len(album), 10):
        chunk = album[i : i + 10]
        if len(chunk) == 1:
            await send_one(message, chunk[0], media)
        else:
            media = [
                InputMediaVideo(media=FSInputFile(f), supports_streaming=True)
                if f.suffix.lower() in VIDEO_EXT
                else InputMediaPhoto(media=FSInputFile(f))
                for f in chunk
            ]
            await message.reply_media_group(media, request_timeout=UPLOAD_TIMEOUT)
    for f in audio:
        await message.reply_audio(FSInputFile(f), request_timeout=UPLOAD_TIMEOUT)
    for f in documents:
        await message.reply_document(FSInputFile(f), request_timeout=UPLOAD_TIMEOUT)

    if too_big:
        await message.reply(
            f"⚠️ Файлов больше 50 МБ: {too_big}. Telegram не даёт ботам отправлять такие."
        )


async def send_one(message: Message, f: Path, media: Media) -> None:
    if f.suffix.lower() in VIDEO_EXT:
        await message.reply_video(
            FSInputFile(f),
            caption=media.caption[:1024] if media.caption else None,
            # С размерами кадра Telegram сразу рисует превью нужной формы.
            width=media.width,
            height=media.height,
            duration=media.duration,
            supports_streaming=True,
            reply_markup=quality_keyboard(media),
            request_timeout=UPLOAD_TIMEOUT,
        )
    else:
        await message.reply_photo(FSInputFile(f), request_timeout=UPLOAD_TIMEOUT)


async def run_polling(bot: Bot) -> None:
    await bot.delete_webhook()
    log.info("starting in polling mode")
    await dp.start_polling(bot)


def run_webhook(bot: Bot) -> None:
    async def set_webhook(bot: Bot) -> None:
        await bot.set_webhook(
            WEBHOOK_BASE + WEBHOOK_PATH,
            secret_token=WEBHOOK_SECRET,
            # Без этого Telegram не присылает нажатия кнопок (callback_query).
            allowed_updates=dp.resolve_used_update_types(),
        )
        log.info("webhook set to %s%s", WEBHOOK_BASE, WEBHOOK_PATH)

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def keep_awake(_: web.Application):
        # Бесплатный Render усыпляет сервис через 15 минут без входящих запросов.
        # Раз в 10 минут обращаемся к себе по внешнему адресу, чтобы этого не было.
        async def ping() -> None:
            async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                while True:
                    await asyncio.sleep(KEEP_AWAKE_INTERVAL)
                    with contextlib.suppress(Exception):
                        async with session.get(WEBHOOK_BASE + "/"):
                            pass

        task = asyncio.create_task(ping())
        yield
        task.cancel()

    dp.startup.register(set_webhook)
    app = web.Application()
    app.cleanup_ctx.append(keep_awake)
    app.router.add_get("/", health)
    SimpleRequestHandler(dp, bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    # access_log=None: иначе проверки здоровья каждые 5 секунд забивают весь лог.
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
    warp.start_in_background()
    # Только статус (ok / expired / none…) — значения cookies в лог не пишем.
    threading.Thread(
        target=lambda: log.info("instagram login: %s", instagram.check()), daemon=True
    ).start()
    bot = Bot(BOT_TOKEN)
    if WEBHOOK_BASE:
        run_webhook(bot)
    else:
        asyncio.run(run_polling(bot))


if __name__ == "__main__":
    main()
