import os
import re
import logging
import tempfile
import asyncio
from pathlib import Path
from typing import Optional

import yt_dlp
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_FILE_SIZE_MB = 50
QUALITY_TIERS = [2160, 1440, 1080, 720, 480, 360]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("SaveNovaBot")

URL_PATTERN = re.compile(
    r"(https?://(?:www\.|vm\.|vt\.|m\.)?"
    r"(?:tiktok\.com|instagram\.com|youtube\.com|youtu\.be)"
    r"/[^\s]+)",
    re.IGNORECASE,
)

WELCOME_TEXT = (
    "Привет! Я SaveNovaBot.\n\n"
    "Пришли мне ссылку на видео из TikTok, Instagram Reels или YouTube Shorts, "
    "выбери качество — и я скачаю его без водяного знака."
)

HELP_TEXT = (
    "Просто пришли ссылку на видео из TikTok, Instagram Reels или "
    "YouTube Shorts, выбери качество на кнопках — получишь файл без "
    "водяного знака.\n\n"
    "Команды:\n"
    "/start — приветствие\n"
    "/help — эта справка"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


def _extract_url(text: str) -> Optional[str]:
    match = URL_PATTERN.search(text)
    return match.group(1) if match else None


def _probe(url: str) -> tuple[list[int], Optional[str]]:
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get("formats") or []
        heights = set()
        for f in formats:
            h = f.get("height")
            vcodec = f.get("vcodec")
            if h and vcodec and vcodec != "none":
                heights.add(h)
        title = info.get("title")
        return sorted(heights, reverse=True), title


def _download_video(url: str, out_dir: str, height: Optional[int]) -> tuple[Path, Optional[str]]:
    outtmpl = os.path.join(out_dir, "%(id)s.%(ext)s")

    if height:
        fmt = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
    else:
        fmt = "mp4/best"

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": fmt,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        path = Path(filename)
        if not path.exists():
            alt = path.with_suffix(".mp4")
            if alt.exists():
                path = alt
        title = info.get("title")
        return path, title


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    url = _extract_url(text)

    if not url:
        await update.message.reply_text(
            "Не вижу ссылку на TikTok, Instagram или YouTube Shorts"
        )
        return

    status_msg = await update.message.reply_text("Смотрю доступные качества...")

    try:
        heights, title = await asyncio.to_thread(_probe, url)
    except Exception:
        logger.exception("Probe failed for %s", url)
        heights, title = [], None

    available = [h for h in QUALITY_TIERS if any(abs(x - h) <= 40 for x in heights)]
    available = available[:4]

    context.user_data["pending_url"] = url
    context.user_data["pending_title"] = title

    if not available:
        await status_msg.edit_text("Скачиваю в лучшем доступном качестве...")
        await _do_download(update, context, status_msg, url, None, title)
        return

    buttons = [
        InlineKeyboardButton(
            f"{h}p" + (" (4K)" if h >= 2160 else ""), callback_data=f"q:{h}"
        )
        for h in available
    ]
    buttons.append(InlineKeyboardButton("Оригинал", callback_data="q:0"))
    keyboard = InlineKeyboardMarkup([buttons[i : i + 2] for i in range(0, len(buttons), 2)])

    caption = title or "Выбери качество:"
    await status_msg.edit_text(f"{caption}\n\nВыбери качество:", reply_markup=keyboard)


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    url = context.user_data.get("pending_url")
    title = context.user_data.get("pending_title")
    if not url:
        await query.edit_message_text("Ссылка устарела, пришли её заново.")
        return

    height_str = query.data.split(":", 1)[1]
    height = int(height_str) if height_str != "0" else None

    label = f"{height}p" if height else "оригинальном качестве"
    await query.edit_message_text(f"Скачиваю в {label}...")

    await _do_download(update, context, query.message, url, height, title)


async def _do_download(
    update, context, status_msg, url: str, height: Optional[int], title: Optional[str]
) -> None:
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            file_path, real_title = await asyncio.to_thread(
                _download_video, url, tmp_dir, height
            )
        except yt_dlp.utils.DownloadError as e:
            logger.warning("Download failed for %s: %s", url, e)
            await status_msg.edit_text(
                "Не получилось скачать это видео в выбранном качестве. "
                "Возможно, оно приватное, удалено или качество недоступно."
            )
            return
        except Exception:
            logger.exception("Unexpected error downloading %s", url)
            await status_msg.edit_text("Произошла ошибка при скачивании.")
            return

        if not file_path.exists():
            await status_msg.edit_text("Файл не найден после скачивания.")
            return

        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                f"Видео весит {size_mb:.1f} МБ — это больше лимита в "
                f"{MAX_FILE_SIZE_MB} МБ для прямой отправки ботом. "
                f"Попробуй выбрать качество пониже."
            )
            return

        bot_username = context.bot.username or "SaveNovaBot"
        quality_label = f"{height}p" if height else "оригинал"
        final_title = title or real_title or ""

        caption_parts = []
        if final_title:
            caption_parts.append(final_title)
        caption_parts.append(f"🔗 {url}")
        caption_parts.append(f"📺 {quality_label}")
        caption_parts.append(f"Скачано с @{bot_username}")
        caption = "\n\n".join(caption_parts)

        try:
            with open(file_path, "rb") as video_file:
                await context.bot.send_video(
                    chat_id=status_msg.chat_id,
                    video=video_file,
                    caption=caption,
                    supports_streaming=True,
                )
            await status_msg.delete()
        except Exception:
            logger.exception("Failed to send video for %s", url)
            await status_msg.edit_text("Не получилось отправить видео.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^q:"))

    logger.info("SaveNovaBot запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
    
