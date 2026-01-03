import os
import re
import logging
import time
import random
import asyncio
import html
import http.cookiejar

from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

import yt_dlp
import requests

from yt_dlp.utils import DownloadError

from urllib.parse import urlparse
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

# Загружаем переменные окружения из .env (если файл существует)
load_dotenv()

# Создаем папку для загрузок
DOWNLOAD_FOLDER = os.getenv("DOWNLOAD_FOLDER", "downloads")
Path(DOWNLOAD_FOLDER).mkdir(exist_ok=True)

# Токен вашего бота читаем из переменной окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Please set it in .env or as an environment variable.")

INSTAGRAM_COOLDOWN_SECONDS = int(os.getenv("INSTAGRAM_COOLDOWN_SECONDS", "30"))
INSTAGRAM_MAX_CONCURRENT = int(os.getenv("INSTAGRAM_MAX_CONCURRENT", "1"))
_instagram_semaphore = asyncio.Semaphore(max(1, INSTAGRAM_MAX_CONCURRENT))

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


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    url = url.rstrip('.,);]>\'"')
    url = url.split('?', 1)[0]
    url = url.split('#', 1)[0]
    return url


def _unescape_jsonish_url(s: str) -> str:
    s = (s or "").strip()
    s = html.unescape(s)
    s = s.replace('\\/', '/')
    s = s.replace('\\u0026', '&')
    s = s.replace('\\u003d', '=')
    return s


def _extract_display_urls_from_html(page_url: str, cookiejar: http.cookiejar.CookieJar | None = None) -> list[str]:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.instagram.com/',
    }
    response = requests.get(page_url, headers=headers, cookies=cookiejar, timeout=30)
    response.raise_for_status()
    text = response.text or ""

    urls = []

    # Prefer best display_resources src (usually full-size, not cropped thumbnail)
    best_src = None
    best_score = -1
    for m in re.finditer(r'"config_width"\s*:\s*(\d+)\s*,\s*"config_height"\s*:\s*(\d+)\s*,\s*"src"\s*:\s*"([^"]+)"', text):
        try:
            w = int(m.group(1))
            h = int(m.group(2))
        except Exception:
            continue
        src = _unescape_jsonish_url(m.group(3))
        if not src.startswith("http"):
            continue
        if not any(ext in src.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            continue
        score = w * h
        if score > best_score:
            best_score = score
            best_src = src

    if best_src:
        urls.append(best_src)

    for m in re.finditer(r'"display_url"\s*:\s*"([^"]+)"', text):
        u = _unescape_jsonish_url(m.group(1))
        if u.startswith("http"):
            urls.append(u)

    # Fallback: sometimes image URLs appear as plain "url":"https:\/\/...fbcdn..."
    for m in re.finditer(r'"url"\s*:\s*"(https?:\\/\\/[^\"]+)"', text):
        u = _unescape_jsonish_url(m.group(1))
        if u.startswith("http") and any(ext in u.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            urls.append(u)

    # de-dup preserving order
    seen = set()
    out = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _guess_ext_from_url(url: str) -> str:
    try:
        path = urlparse(url).path
        ext = os.path.splitext(path)[1].lower()
        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
            return ext
    except Exception:
        pass
    return ".jpg"


def _load_cookiejar(cookies_path: Path) -> http.cookiejar.MozillaCookieJar | None:
    try:
        jar = http.cookiejar.MozillaCookieJar()
        jar.load(str(cookies_path), ignore_discard=True, ignore_expires=True)
        return jar
    except Exception:
        return None


def _download_binary_to_file(url: str, filepath: str, cookiejar: http.cookiejar.CookieJar | None = None) -> str:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.instagram.com/',
    }
    response = requests.get(url, headers=headers, cookies=cookiejar, stream=True, timeout=60)
    response.raise_for_status()

    Path(os.path.dirname(filepath)).mkdir(parents=True, exist_ok=True)
    with open(filepath, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)

    return filepath if os.path.exists(filepath) else None


def _extract_og_media_urls(page_url: str, cookiejar: http.cookiejar.CookieJar | None = None) -> list[str]:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://www.instagram.com/',
    }
    response = requests.get(page_url, headers=headers, cookies=cookiejar, timeout=30)
    response.raise_for_status()
    text = response.text or ""

    urls = []
    for prop in ["og:video", "og:image"]:
        m = re.search(rf'property="{re.escape(prop)}"\s+content="([^"]+)"', text)
        if m:
            u = html.unescape(m.group(1)).strip()
            if u:
                urls.append(u)
    return urls


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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
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


def download_instagram_ytdlp(url: str) -> str:
    """Альтернативный способ для Instagram через yt-dlp (видео, фото, карусели)"""
    proxy = os.getenv("INSTAGRAM_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    cookies_path = Path(os.getenv("INSTAGRAM_COOKIES_FILE") or "cookies.txt")

    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    ]
    ua = random.choice(user_agents)

    ratelimit = os.getenv("INSTAGRAM_RATELIMIT")
    ratelimit = int(ratelimit) if ratelimit and ratelimit.isdigit() else 0

    sleep_interval = float(os.getenv("INSTAGRAM_SLEEP_INTERVAL", "1.0"))
    max_sleep_interval = float(os.getenv("INSTAGRAM_MAX_SLEEP_INTERVAL", "3.0"))
    ydl_opts = {
        'format': 'best',
        'outtmpl': f'{DOWNLOAD_FOLDER}/%(id)s/%(autonumber)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'ignore_no_formats_error': True,
        'http_headers': {
            'User-Agent': ua,
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.instagram.com/',
        },
        # Делаем скачивание более устойчивым
        'socket_timeout': 120,
        'retries': 5,
        'fragment_retries': 5,
        'extractor_retries': 5,
        'concurrent_fragment_downloads': 1,
        'sleep_interval': max(0.0, sleep_interval),
        'max_sleep_interval': max(0.0, max_sleep_interval),
    }

    if ratelimit > 0:
        ydl_opts['ratelimit'] = ratelimit

    if proxy:
        ydl_opts['proxy'] = proxy

    if cookies_path.is_file():
        ydl_opts['cookiefile'] = str(cookies_path)

    cookiejar = _load_cookiejar(cookies_path) if cookies_path.is_file() else None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            started_at = time.time()
            info = None
            try:
                info = ydl.extract_info(url, download=False)
            except Exception:
                info = None

            has_video_formats = False

            def _probe_formats(info_dict):
                nonlocal has_video_formats
                if has_video_formats:
                    return
                if isinstance(info_dict, dict) and info_dict.get("entries"):
                    for entry in info_dict["entries"]:
                        _probe_formats(entry)
                    return
                if not isinstance(info_dict, dict):
                    return
                fmts = info_dict.get("formats") or []
                if not isinstance(fmts, list) or not fmts:
                    return

                for f in fmts:
                    if not isinstance(f, dict):
                        continue
                    vcodec = f.get("vcodec")
                    ext = (f.get("ext") or "").lower()
                    if vcodec and vcodec != "none":
                        has_video_formats = True
                        return
                    if ext in ["mp4", "webm", "mkv"] and vcodec and vcodec != "none":
                        has_video_formats = True
                        return

            _probe_formats(info)

            if has_video_formats:
                try:
                    info = ydl.extract_info(url, download=True)
                except DownloadError as e:
                    if "no video formats found" not in str(e).lower():
                        raise

            downloaded_files = []

            def _collect_files(info_dict):
                if isinstance(info_dict, dict) and info_dict.get("entries"):
                    for entry in info_dict["entries"]:
                        _collect_files(entry)
                if not isinstance(info_dict, dict):
                    return

                base = Path(DOWNLOAD_FOLDER) / info_dict.get("id", "")
                if not base.exists():
                    return

                for f in base.iterdir():
                    if f.suffix.lower() in [".mp4", ".jpg", ".jpeg", ".png", ".webp"]:
                        downloaded_files.append(str(f))

            _collect_files(info)

            if not downloaded_files:
                image_urls = []

                def _collect_image_urls(info_dict):
                    if isinstance(info_dict, dict) and info_dict.get("entries"):
                        for entry in info_dict["entries"]:
                            _collect_image_urls(entry)
                    if not isinstance(info_dict, dict):
                        return

                    direct_url = info_dict.get("url")
                    if isinstance(direct_url, str) and direct_url.startswith("http"):
                        ext = _guess_ext_from_url(direct_url)
                        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
                            image_urls.append(direct_url)

                    fmts = info_dict.get("formats") or []
                    if isinstance(fmts, list):
                        best = None
                        best_score = -1
                        for f in fmts:
                            if not isinstance(f, dict):
                                continue
                            u = f.get("url")
                            if not isinstance(u, str) or not u.startswith("http"):
                                continue
                            vcodec = f.get("vcodec")
                            if vcodec not in [None, "none"]:
                                continue
                            ext = (f.get("ext") or "").lower()
                            if ext and ("." + ext) not in [".jpg", ".jpeg", ".png", ".webp"]:
                                continue
                            score = (f.get("width") or 0) * (f.get("height") or 0)
                            if score > best_score:
                                best = u
                                best_score = score
                        if best:
                            image_urls.append(best)

                    thumbs = info_dict.get("thumbnails") or []
                    if isinstance(thumbs, list) and thumbs:
                        best = None
                        best_score = -1
                        for t in thumbs:
                            if not isinstance(t, dict):
                                continue
                            u = t.get("url")
                            if not u:
                                continue
                            score = (t.get("width") or 0) * (t.get("height") or 0)
                            if score > best_score:
                                best = u
                                best_score = score
                        if best:
                            image_urls.append(best)
                            return

                    thumb = info_dict.get("thumbnail")
                    if isinstance(thumb, str) and thumb:
                        image_urls.append(thumb)

                _collect_image_urls(info)

                # Always try to prepend full-size URLs from page HTML (display_resources/display_url)
                try:
                    html_urls = _extract_display_urls_from_html(url, cookiejar)
                except Exception:
                    html_urls = []

                if html_urls:
                    seen = set()
                    merged = []
                    for u in (html_urls + image_urls):
                        if u in seen:
                            continue
                        seen.add(u)
                        merged.append(u)
                    image_urls = merged

                if image_urls:
                    base_id = None
                    if isinstance(info, dict):
                        base_id = info.get("id")
                    base_dir = Path(DOWNLOAD_FOLDER) / (base_id or "ig")
                    for idx, u in enumerate(image_urls[:10], start=1):
                        ext = _guess_ext_from_url(u)
                        out = str(base_dir / f"fallback_{idx}{ext}")
                        try:
                            fp = _download_binary_to_file(u, out, cookiejar)
                            if fp:
                                downloaded_files.append(fp)
                        except Exception:
                            continue

            if not downloaded_files:
                try:
                    og_urls = _extract_og_media_urls(url, cookiejar)
                except Exception:
                    og_urls = []

                if og_urls:
                    base_dir = Path(DOWNLOAD_FOLDER) / "ig"
                    for idx, u in enumerate(og_urls[:5], start=1):
                        ext = _guess_ext_from_url(u)
                        out = str(base_dir / f"og_{idx}{ext}")
                        try:
                            fp = _download_binary_to_file(u, out, cookiejar)
                            if fp:
                                downloaded_files.append(fp)
                        except Exception:
                            continue

            if not downloaded_files:
                return None

            # Если один файл – ведём себя как раньше, возвращая строку
            if len(downloaded_files) == 1:
                return downloaded_files[0]

            # Если несколько – возвращаем список путей (карусель)
            return downloaded_files

    except Exception as e:
        logger.exception("Error downloading Instagram with yt-dlp")
        err_str = str(e).lower()
        if 'cookies' in err_str or 'login' in err_str or 'rate-limit' in err_str:
            logger.error("Instagram может требовать авторизацию или куки устарели. Обновите cookies.txt.")
        return None


def download_video_direct(url: str) -> str:
    """Прямое скачивание видео"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
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

    url = _normalize_url(match.group(0))

    # Отправляем сообщение о начале загрузки
    status_msg = await update.message.reply_text("⏳ Скачиваю видео...")
    filepath = None
    filepaths = []

    try:
        # Определяем платформу и выбираем метод скачивания
        if 'tiktok.com' in url:
            await status_msg.edit_text("⏳ Скачиваю TikTok видео...")
            filepath = await asyncio.to_thread(download_tiktok_ytdlp, url)

            if not filepath:
                await status_msg.edit_text(
                    "❌ Не удалось скачать TikTok видео. Возможно, ссылка недоступна "
                    "или истек таймаут. Попробуйте другую ссылку."
                )
                return

            filepaths = [filepath]

        elif 'instagram.com' in url:
            await status_msg.edit_text("⏳ Скачиваю Instagram медиа...")

            now = time.time()
            last_ig = float(context.user_data.get("ig_last_ts", 0) or 0)
            remaining = INSTAGRAM_COOLDOWN_SECONDS - (now - last_ig)
            if remaining > 0:
                await status_msg.edit_text(
                    f"⏳ Подождите {int(remaining)} сек перед следующим скачиванием из Instagram."
                )
                return

            context.user_data["ig_last_ts"] = now

            async with _instagram_semaphore:
                filepath = await asyncio.to_thread(download_instagram_ytdlp, url)

            if not filepath:
                await status_msg.edit_text(
                    "❌ Не удалось скачать Instagram медиа. Возможно:\n• Медиа приватное\n" \
                    "• Ссылка неверная\n• Проблемы с доступом"
                )
                return

            if isinstance(filepath, list):
                filepaths = [p for p in filepath if p]
            else:
                filepaths = [filepath]

        valid_paths = [p for p in filepaths if p and os.path.exists(p)]

        if valid_paths:
            # Проверяем размер файла (Telegram ограничение: 50 МБ) для каждого
            for p in valid_paths:
                file_size = os.path.getsize(p) / (1024 * 1024)  # в МБ

                if file_size > 50:
                    await status_msg.edit_text(
                        f"❌ Файл слишком большой ({file_size:.1f} МБ). "
                        f"Telegram ограничивает отправку 50 МБ."
                    )
                    return

            await status_msg.edit_text(
                f"✅ Медиа скачано! ({len(valid_paths)} файл(ов))\n📤 Отправляю..."
            )

            # Отправляем все медиа (фото/видео)
            for path in valid_paths:
                _, ext = os.path.splitext(path)
                ext = ext.lower()

                with open(path, 'rb') as media_file:
                    if ext in [".jpg", ".jpeg", ".png"]:
                        await update.message.reply_photo(
                            photo=media_file,
                            caption="📷 Скачано через бота",
                        )
                    elif ext in [".webp"]:
                        await update.message.reply_document(
                            document=media_file,
                            filename=os.path.basename(path),
                            caption="📎 Скачано через бота",
                        )
                    else:
                        await update.message.reply_video(
                            video=media_file,
                            caption="🎬 Скачано через бота",
                            supports_streaming=True
                        )

            # После отправки очищаем скачанные файлы
            for path in valid_paths:
                try:
                    os.remove(path)
                except Exception:
                    pass

            await status_msg.delete()

        else:
            await status_msg.edit_text("❌ Не удалось скачать медиа. Попробуйте другую ссылку.")

    except Exception as e:
        logger.exception("Error in handle_message")
        await status_msg.edit_text(f"❌ Произошла ошибка: {str(e)}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = getattr(context, "error", None)
    if err is None:
        logger.exception("Unhandled exception in Telegram handler")
        return

    logger.error(
        "Unhandled exception in Telegram handler: %s",
        err,
        exc_info=(type(err), err, err.__traceback__),
    )


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

    application.add_error_handler(error_handler)

    # Запускаем бота
    print("🤖 Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()