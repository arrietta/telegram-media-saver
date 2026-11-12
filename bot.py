"""Telegram Media Saver: присылаешь ссылку на Instagram / YouTube / TikTok —
бот отвечает в чат самим видео, фото или альбомом.

Локально работает через long polling, на хостинге (Render) — через webhook.
"""

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
import tempfile
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
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

from downloader import (  # noqa: E402 — после load_dotenv, чтобы увидеть COOKIES_FILE
    AUDIO_EXT,
    MAX_BYTES,
    PHOTO_EXT,
    VIDEO_EXT,
    DownloadError,
    YoutubeMenu,
    download,
    find_urls,
    is_youtube_video,
    ssyoutube_link,
    youtube_download,
    youtube_menu,
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


async def handle_url(message: Message, url: str) -> None:
    log.info("got link %s", url)
    status = await message.reply("⏳ Скачиваю…")
    tmp = Path(tempfile.mkdtemp(prefix="tms_"))
    try:
        if is_youtube_video(url):
            async with downloads:
                menu = await asyncio.wait_for(youtube_menu(url), DOWNLOAD_TIMEOUT)
            if menu:
                await message.reply(menu_text(menu), reply_markup=menu_keyboard(menu))
                return
        async with downloads:
            files = await asyncio.wait_for(download(url, tmp), DOWNLOAD_TIMEOUT)
        log.info("downloaded %d file(s) for %s", len(files), url)
        with contextlib.suppress(Exception):
            await status.edit_text("📤 Отправляю…")
        async with ChatActionSender.upload_video(bot=message.bot, chat_id=message.chat.id):
            await send_files(message, files)
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


def menu_text(menu: YoutubeMenu) -> str:
    minutes, seconds = divmod(menu.duration, 60)
    hours, minutes = divmod(minutes, 60)
    length = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    text = f"🎬 {menu.title[:200]}\n⏱ {length}\n\nВыбери качество:"
    if menu.hidden:
        text += "\n(качество выше не влезает в лимит Telegram — 50 МБ, его можно скачать в браузере)"
    return text


def menu_keyboard(menu: YoutubeMenu) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"yt:{menu.video_id}:{key}")
        for key, label in menu.options
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([browser_button(f"https://ssyoutube.com/watch?v={menu.video_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def browser_button(link: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🌐 Скачать в браузере (ssyoutube.com)", url=link)


def browser_keyboard(url: str) -> InlineKeyboardMarkup | None:
    """Кнопка-запасной путь для YouTube: открыть ролик на ssyoutube.com."""
    link = ssyoutube_link(url)
    return InlineKeyboardMarkup(inline_keyboard=[[browser_button(link)]]) if link else None


@dp.callback_query(F.data.startswith("yt:"))
async def on_quality(callback: CallbackQuery) -> None:
    _, video_id, choice = callback.data.split(":", 2)
    menu = callback.message
    await callback.answer()
    if not isinstance(menu, Message):  # сообщение слишком старое
        return
    label = "аудио" if choice == "audio" else f"{choice}p"
    # Убираем кнопки сразу, чтобы второе нажатие не запустило вторую загрузку.
    with contextlib.suppress(Exception):
        await menu.edit_text(f"⏳ Скачиваю {label}…")
    target = menu.reply_to_message or menu  # отвечаем на сообщение со ссылкой
    tmp = Path(tempfile.mkdtemp(prefix="tms_"))
    try:
        async with downloads:
            files, title = await asyncio.wait_for(
                youtube_download(video_id, choice, tmp), DOWNLOAD_TIMEOUT
            )
        with contextlib.suppress(Exception):
            await menu.edit_text("📤 Отправляю…")
        async with ChatActionSender.upload_video(bot=menu.bot, chat_id=menu.chat.id):
            await send_files(target, files, title=title)
    except TimeoutError:
        await target.reply("❌ Не успел скачать за 4 минуты — сайт отвечает слишком медленно.")
    except DownloadError as e:
        log.warning("youtube download failed for %s (%s): %s", video_id, choice, e)
        await target.reply(
            f"❌ Не получилось скачать: {e}",
            reply_markup=browser_keyboard(f"https://www.youtube.com/watch?v={video_id}"),
        )
    except Exception:
        log.exception("unexpected error for %s (%s)", video_id, choice)
        await target.reply("❌ Что-то пошло не так, попробуй ещё раз позже.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        with contextlib.suppress(Exception):
            await menu.delete()


async def send_files(message: Message, files: list[Path], title: str | None = None) -> None:
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
            await send_one(message, chunk[0])
        else:
            media = [
                InputMediaVideo(media=FSInputFile(f), supports_streaming=True)
                if f.suffix.lower() in VIDEO_EXT
                else InputMediaPhoto(media=FSInputFile(f))
                for f in chunk
            ]
            await message.reply_media_group(media, request_timeout=UPLOAD_TIMEOUT)
    for f in audio:
        await message.reply_audio(FSInputFile(f), title=title, request_timeout=UPLOAD_TIMEOUT)
    for f in documents:
        await message.reply_document(FSInputFile(f), request_timeout=UPLOAD_TIMEOUT)

    if too_big:
        await message.reply(
            f"⚠️ Файлов больше 50 МБ: {too_big}. Telegram не даёт ботам отправлять такие."
        )


async def send_one(message: Message, f: Path) -> None:
    if f.suffix.lower() in VIDEO_EXT:
        await message.reply_video(
            FSInputFile(f), supports_streaming=True, request_timeout=UPLOAD_TIMEOUT
        )
    else:
        await message.reply_photo(FSInputFile(f), request_timeout=UPLOAD_TIMEOUT)


async def run_polling(bot: Bot) -> None:
    await bot.delete_webhook()
    log.info("starting in polling mode")
    await dp.start_polling(bot)


def run_webhook(bot: Bot) -> None:
    async def set_webhook(bot: Bot) -> None:
        await bot.set_webhook(WEBHOOK_BASE + WEBHOOK_PATH, secret_token=WEBHOOK_SECRET)
        log.info("webhook set to %s%s", WEBHOOK_BASE, WEBHOOK_PATH)

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    dp.startup.register(set_webhook)
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dp, bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    # access_log=None: иначе проверки здоровья каждые 5 секунд забивают весь лог.
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")
    bot = Bot(BOT_TOKEN)
    if WEBHOOK_BASE:
        run_webhook(bot)
    else:
        asyncio.run(run_polling(bot))


if __name__ == "__main__":
    main()
