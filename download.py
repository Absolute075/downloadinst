import os
import re
import logging
import tempfile
import shutil
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

import yt_dlp
import instaloader
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения из .env (если файл существует)
load_dotenv()

# Создаем папку для загрузок
DOWNLOAD_FOLDER = "downloads"
Path(DOWNLOAD_FOLDER).mkdir(exist_ok=True)

# Токен вашего бота читаем из переменной окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Please set it in .env or as an environment variable.")

# Инициализация Instaloader для Instagram
insta = instaloader.Instaloader(
    download_videos=True,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False
)


# ========== КОМАНДЫ БОТА ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_lang = context.user_data.get("language")

    # Первый запуск: показываем приветствие и выбор языка
    if not user_lang:
        welcome_text = """
Hi | Привет

Choose your language | Выберите язык

"""
        keyboard = [
            [
                InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
                InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
            ]
        ]

        await update.message.reply_text(
            welcome_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Повторный /start после выбора языка
    if user_lang == "ru":
        text = (
            "👋 Снова привет!\n"
            "Просто отправь мне ссылку на видео из Instagram или TikTok."
        )
    else:
        text = (
            "👋 Hi again!\n"
            "Just send me a video link from Instagram or TikTok."
        )

    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    user_lang = context.user_data.get("language", "ru")

    if user_lang == "ru":
        help_text = """
📖 **Как использовать:**
1. Скопируйте ссылку на видео
2. Отправьте ссылку боту
3. Получите видео

🔗 **Примеры ссылок:**
• Instagram: https://www.instagram.com/reel/Cxample123/
• TikTok: https://www.tiktok.com/@user/video/123456789

⚡ **Особенности:**
• Максимальный размер: 50 МБ (ограничение Telegram)
• Приватные видео не скачиваются
• Могут быть проблемы с некоторыми аккаунтами
"""
    else:
        help_text = """
📖 **How to use:**
1. Copy the video link
2. Send the link to the bot
3. Get the downloaded video

🔗 **Example links:**
• Instagram: https://www.instagram.com/reel/Cxample123/
• TikTok: https://www.tiktok.com/@user/video/123456789

⚡ **Notes:**
• Max file size: 50 MB (Telegram limit)
• Private videos cannot be downloaded
• Some accounts or links may not work
"""

    await update.message.reply_text(help_text, parse_mode='Markdown')


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора языка"""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    lang = "ru" if data == "lang_ru" else "en"
    context.user_data["language"] = lang

    if lang == "ru":
        text = (
            "🇷🇺 Язык установлен: Русский.\n\n"
            "Теперь просто отправьте мне ссылку на видео из Instagram или TikTok, "
            "и я постараюсь его скачать."
        )
    else:
        text = (
            "🇬🇧 Language set to English.\n\n"
            "Now just send me a video link from Instagram or TikTok, "
            "and I will try to download it."
        )

    await query.edit_message_text(text)


# ========== ОСНОВНАЯ ЛОГИКА СКАЧИВАНИЯ ==========

def clean_filename(filename: str) -> str:
    """Очистка имени файла от недопустимых символов"""
    # Удаляем недопустимые символы для файловой системы
    cleaned = re.sub(r'[<>:"/\\|?*]', '', filename)
    # Укорачиваем слишком длинные имена
    if len(cleaned) > 100:
        cleaned = cleaned[:100]
    return cleaned.strip()


def download_tiktok_ytdlp(url: str) -> str:
    """Скачивание TikTok видео через yt-dlp"""
    proxy = os.getenv("TIKTOK_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    ydl_opts = {
        'format': 'best',
        'outtmpl': f'{DOWNLOAD_FOLDER}/%(title)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'merge_output_format': 'mp4',
        'retries': 5,
        'fragment_retries': 5,
        'socket_timeout': 60,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        },
    }

    if proxy:
        ydl_opts['proxy'] = proxy

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

            # Проверяем, скачался ли файл
            if not os.path.exists(filename):
                # Пробуем найти файл с другим расширением
                base_name = os.path.splitext(filename)[0]
                for ext in ['.mp4', '.webm', '.mkv']:
                    if os.path.exists(base_name + ext):
                        return base_name + ext

            return filename if os.path.exists(filename) else None

    except Exception as e:
        logger.error(f"Error downloading TikTok: {e}")
        return None


def download_instagram_instaloader(url: str) -> str:
    """Скачивание Instagram видео через Instaloader"""
    temp_dir = None
    try:
        # Извлекаем shortcode из URL
        shortcode_match = re.search(r'/reel/([^/?]+)|/p/([^/?]+)', url)
        if not shortcode_match:
            return None

        shortcode = shortcode_match.group(1) or shortcode_match.group(2)

        # Создаем временную папку
        temp_dir = tempfile.mkdtemp()

        # Скачиваем пост
        post = instaloader.Post.from_shortcode(insta.context, shortcode)

        # Проверяем, есть ли видео
        # Скачиваем видео
        insta.download_post(post, target=temp_dir)

        # Ищем скачанный файл
        media_file = None
        for file in Path(temp_dir).rglob('*.mp4'):
            media_file = file
            break

        if not media_file:
            for pattern in ('*.jpg', '*.jpeg', '*.png', '*.webp'):
                for file in Path(temp_dir).rglob(pattern):
                    media_file = file
                    break
                if media_file:
                    break

        if not media_file:
            return None

        final_path = os.path.join(DOWNLOAD_FOLDER, clean_filename(media_file.name))
        shutil.move(str(media_file), final_path)

        return final_path if os.path.exists(final_path) else None

    except Exception as e:
        logger.error(f"Error downloading Instagram: {e}")
        return None
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass


def download_instagram_ytdlp(url: str) -> str:
    """Альтернативный способ для Instagram через yt-dlp"""
    ydl_opts = {
        'format': 'best',
        'outtmpl': f'{DOWNLOAD_FOLDER}/%(title)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            return filename if os.path.exists(filename) else None
    except Exception as e:
        logger.error(f"Error downloading Instagram with yt-dlp: {e}")
        return None


def download_video_direct(url: str) -> str:
    """Прямое скачивание по ссылке (для Stories и т.д.)"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

        response = requests.get(url, headers=headers, stream=True, timeout=30)
        response.raise_for_status()

        # Получаем имя файла из URL или генерируем
        parsed_url = urlparse(url)
        filename = parsed_url.path.split('/')[-1] or 'video.mp4'
        if not filename.endswith('.mp4'):
            filename += '.mp4'

        filepath = os.path.join(DOWNLOAD_FOLDER, clean_filename(filename))

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        return filepath if os.path.exists(filepath) else None

    except Exception as e:
        logger.error(f"Error direct download: {e}")
        return None


# ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка входящих сообщений с ссылками"""
    user = update.effective_user
    message_text = update.message.text

    logger.info(f"User {user.id} sent: {message_text}")

    # Проверяем, есть ли ссылка в сообщении (поддерживаем поддомены Instagram/TikTok)
    url_pattern = r'https?://(?:[\w.-]+\.)?(instagram\.com|tiktok\.com)/[^\s]+'

    match = re.search(url_pattern, message_text)

    if not match:
        await update.message.reply_text(
            "❌ Пожалуйста, отправьте корректную ссылку на видео Instagram или TikTok."
        )
        return

    url = match.group(0)

    # Отправляем сообщение о начале загрузки
    status_msg = await update.message.reply_text("⏳ Скачиваю видео...")
    filepath = None

    try:
        # Определяем платформу и выбираем метод скачивания
        if 'tiktok.com' in url:
            await status_msg.edit_text("⏳ Скачиваю TikTok видео...")
            filepath = download_tiktok_ytdlp(url)

            if not filepath:
                await status_msg.edit_text(
                    "❌ Не удалось скачать TikTok видео. Возможно, ссылка недоступна "
                    "или истек таймаут. Попробуйте другую ссылку."
                )
                return

        elif 'instagram.com' in url:
            await status_msg.edit_text("⏳ Скачиваю Instagram медиа...")

            # Пробуем разные методы для Instagram
            filepath = download_instagram_instaloader(url)

            if not filepath:
                filepath = download_instagram_ytdlp(url)

            if not filepath:
                await status_msg.edit_text(
                    "❌ Не удалось скачать Instagram медиа. Возможно:\n• Медиа приватное\n" \
                    "• Ссылка неверная\n• Проблемы с доступом"
                )
                return

        if filepath and os.path.exists(filepath):
            # Проверяем размер файла (Telegram ограничение: 50 МБ)
            file_size = os.path.getsize(filepath) / (1024 * 1024)  # в МБ

            if file_size > 50:
                await status_msg.edit_text(
                    f"❌ Файл слишком большой ({file_size:.1f} МБ). "
                    f"Telegram ограничивает отправку 50 МБ."
                )
                return

            await status_msg.edit_text(f"✅ Медиа скачано! ({file_size:.1f} МБ)\n📤 Отправляю...")

            _, ext = os.path.splitext(filepath)
            ext = ext.lower()

            # Отправляем медиа
            with open(filepath, 'rb') as media_file:
                if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                    await update.message.reply_photo(
                        photo=media_file,
                        caption="📷 Скачано через бота",
                    )
                else:
                    await update.message.reply_video(
                        video=media_file,
                        caption="🎬 Скачано через бота",
                        supports_streaming=True
                    )

            await status_msg.delete()

        else:
            await status_msg.edit_text("❌ Не удалось скачать медиа. Попробуйте другую ссылку.")

    except Exception as e:
        logger.error(f"Error in handle_message: {e}")
        await status_msg.edit_text(f"❌ Произошла ошибка: {str(e)}")

    finally:
        # Гарантированно удаляем временный файл, если он был скачан
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass


# ========== ЗАПУСК БОТА ==========

async def set_bot_commands(application: Application):
    commands = [
        BotCommand("start", "Start the bot / Запуск бота"),
        BotCommand("help", "Show help / Показать помощь"),
    ]
    await application.bot.set_my_commands(commands)


def main():
    """Запуск бота"""
    application = Application.builder().token(BOT_TOKEN).post_init(set_bot_commands).build()

    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(language_callback, pattern="^lang_"))

    # Регистрируем обработчик текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Запускаем бота
    print("🤖 Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()