# ==============================================
# bot.py — CalBot с голосом + вложениями
# ==============================================

import logging
import os
import re
import tempfile
from datetime import datetime

import pytz
import openai
import requests as req

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from config import (
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT_ID,
    REMINDER_MINUTES,
    TIMEZONE,
    YOUR_EMAIL,
    OPENAI_API_KEY,
    TEMP_DIR,
    BOT_LOG,
)
from google_api import (
    create_calendar_event,
    get_events_for_day,
    send_email,
    upload_to_drive,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(BOT_LOG),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Состояния диалога добавления задачи
WAIT_TITLE, WAIT_DATETIME, WAIT_COMMENT, WAIT_ATTACHMENTS = range(4)

# Временное хранилище данных диалога
user_data_store = {}


# ==============================================
# УТИЛИТЫ
# ==============================================

def is_allowed(update: Update) -> bool:
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


def parse_datetime_from_text(text: str) -> datetime | None:
    """
    Попытка распарсить дату из текста.
    Форматы: ДД.ММ.ГГГГ ЧЧ:ММ  или  ГГГГ-ММ-ДД ЧЧ:ММ
    """
    patterns = [
        r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})",  # 25.05.2026 14:30
        r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})",    # 2026-05-25 14:30
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            g = m.groups()
            try:
                if len(g[0]) == 4:  # YYYY-MM-DD
                    return datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]))
                else:  # DD.MM.YYYY
                    return datetime(int(g[2]), int(g[1]), int(g[0]), int(g[3]), int(g[4]))
            except ValueError:
                continue
    return None


# ==============================================
# WHISPER — транскрипция голосовых
# ==============================================

async def transcribe_voice(file_path: str) -> str:
    """Транскрибировать аудиофайл через OpenAI Whisper."""
    client = openai.OpenAI(api_key=OPENAI_API_KEY)
    with open(file_path, "rb") as audio_file:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ru",
        )
    return result.text


async def download_telegram_file(file_id: str, context: ContextTypes.DEFAULT_TYPE, suffix: str = "") -> str:
    """Скачать файл из Telegram во временную папку. Вернуть путь к файлу."""
    os.makedirs(TEMP_DIR, exist_ok=True)
    tg_file = await context.bot.get_file(file_id)
    tmp = tempfile.NamedTemporaryFile(delete=False, dir=TEMP_DIR, suffix=suffix)
    await tg_file.download_to_drive(tmp.name)
    return tmp.name


# ==============================================
# КОМАНДЫ
# ==============================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = (
        "👋 *CalBot — твой личный планировщик*\n\n"
        "📋 *Команды:*\n"
        "/add — добавить задачу вручную\n"
        "/today — задачи на сегодня\n"
        "/tomorrow — задачи на завтра\n"
        "/week — задачи на неделю\n"
        "/help — помощь\n\n"
        "🎤 *Голосовое сообщение* — продиктуй задачу и дату\n"
        "📎 *Файл / фото / документ* — прикрепи к задаче\n"
        "🔔 Напоминания в Telegram и на почту"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = (
        "📖 *Способы добавить задачу:*\n\n"
        "*1. Вручную:* /add → название → дата → комментарий\n\n"
        "*2. Голосом:* Отправь голосовое сообщение.\n"
        "   Скажи что-то вроде:\n"
        "   _«Встреча с поставщиком 25 мая в 14:30, не забыть взять договор»_\n"
        "   Бот распознает и уточнит детали.\n\n"
        "*3. С вложением:* Отправь фото/документ/аудио с подписью или без.\n"
        "   Файл прикрепится к событию в Calendar и придёт на почту.\n\n"
        "🔔 Напоминания за *60 мин* и *15 мин* — Telegram + email"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ==============================================
# ГОЛОСОВЫЕ СООБЩЕНИЯ
# ==============================================

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принять голосовое, транскрибировать, создать задачу или уточнить детали."""
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("🎤 Распознаю голосовое...")

    try:
        # Скачать голосовое
        voice = update.message.voice
        file_path = await download_telegram_file(voice.file_id, context, suffix=".ogg")

        # Транскрибировать
        text = await transcribe_voice(file_path)
        os.unlink(file_path)

        await msg.edit_text(f"📝 Распознано:\n_{text}_\n\nОбрабатываю...", parse_mode="Markdown")

        # Попытка извлечь дату из текста
        dt = parse_datetime_from_text(text)

        # Сохранить в store для дальнейшего диалога
        user_data_store[chat_id] = {
            "title": text[:100],         # первые 100 символов как название
            "comment": text,             # полный текст как описание
            "attachments": [],
            "from_voice": True,
        }

        if dt:
            user_data_store[chat_id]["datetime"] = dt
            dt_str = dt.strftime("%d.%m.%Y %H:%M")

            keyboard = [
                [
                    InlineKeyboardButton("✅ Да, создать", callback_data="voice_confirm"),
                    InlineKeyboardButton("✏️ Изменить", callback_data="voice_edit"),
                ]
            ]
            await msg.edit_text(
                f"🎯 *Понял!*\n\n"
                f"📌 *{text[:80]}{'...' if len(text) > 80 else ''}*\n"
                f"📅 Дата: *{dt_str}*\n\n"
                f"Создать задачу?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            # Дата не найдена — спросить
            await msg.edit_text(
                f"🎤 *Записал:*\n_{text}_\n\n"
                f"📅 Не нашёл дату. Введи дату и время:\n`ДД.ММ.ГГГГ ЧЧ:ММ`",
                parse_mode="Markdown",
            )
            context.user_data["awaiting_voice_datetime"] = True

    except Exception as e:
        logger.error(f"Ошибка голосового: {e}")
        await msg.edit_text(f"❌ Ошибка распознавания: {e}")


async def handle_voice_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить дату от пользователя после голосового без распознанной даты."""
    if not is_allowed(update):
        return
    if not context.user_data.get("awaiting_voice_datetime"):
        return

    chat_id = update.effective_chat.id
    dt = parse_datetime_from_text(update.message.text.strip())
    if not dt:
        await update.message.reply_text("❌ Неверный формат. Попробуй: `25.05.2026 14:30`", parse_mode="Markdown")
        return

    user_data_store[chat_id]["datetime"] = dt
    context.user_data.pop("awaiting_voice_datetime", None)
    dt_str = dt.strftime("%d.%m.%Y %H:%M")
    keyboard = [
        [
            InlineKeyboardButton("✅ Да, создать", callback_data="voice_confirm"),
            InlineKeyboardButton("✏️ Изменить", callback_data="voice_edit"),
        ]
    ]
    title = user_data_store[chat_id].get("title", "")
    await update.message.reply_text(
        f"🎯 *Понял!*\n\n"
        f"📌 *{title[:80]}{'...' if len(title) > 80 else ''}*\n"
        f"📅 Дата: *{dt_str}*\n\n"
        f"Создать задачу?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def voice_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтвердить создание задачи из голосового."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await _save_task_from_store(query, chat_id)


async def voice_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перейти к ручному редактированию задачи из голосового."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = user_data_store.get(chat_id, {})
    await query.edit_message_text(
        f"✏️ Текущее название:\n_{data.get('title', '')}._\n\n"
        f"Введи новое название (или отправь то же самое):",
        parse_mode="Markdown",
    )
    context.user_data["state"] = "voice_wait_title"


# ==============================================
# ВЛОЖЕНИЯ (фото / документ / аудио)
# ==============================================

async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принять любой файл (фото, документ, аудио, видео).
    Если есть активный диалог задачи — прикрепить к нему.
    Если нет — спросить к какой задаче или создать новую.
    """
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    msg = update.message
    caption = msg.caption or ""

    # Определяем тип файла
    if msg.photo:
        file_id = msg.photo[-1].file_id
        filename = f"photo_{chat_id}_{int(datetime.now().timestamp())}.jpg"
        suffix = ".jpg"
        file_type = "📷 Фото"
    elif msg.document:
        file_id = msg.document.file_id
        filename = msg.document.file_name or f"doc_{chat_id}.bin"
        suffix = os.path.splitext(filename)[1] or ".bin"
        file_type = "📄 Документ"
    elif msg.audio:
        file_id = msg.audio.file_id
        filename = msg.audio.file_name or f"audio_{chat_id}.mp3"
        suffix = os.path.splitext(filename)[1] or ".mp3"
        file_type = "🎵 Аудио"
    elif msg.video:
        file_id = msg.video.file_id
        filename = f"video_{chat_id}_{int(datetime.now().timestamp())}.mp4"
        suffix = ".mp4"
        file_type = "🎬 Видео"
    else:
        await msg.reply_text("❓ Неизвестный тип файла.")
        return

    status = await msg.reply_text(f"{file_type} получен, загружаю на Drive...")

    try:
        # Скачать файл из Telegram
        local_path = await download_telegram_file(file_id, context, suffix=suffix)

        # Загрузить на Google Drive
        drive_file = upload_to_drive(local_path, filename)
        os.unlink(local_path)

        # Есть активная задача в store?
        store = user_data_store.get(chat_id, {})
        if store.get("datetime"):
            # Прикрепляем к текущей задаче
            store.setdefault("attachments", []).append(drive_file)
            user_data_store[chat_id] = store
            await status.edit_text(
                f"✅ {file_type} прикреплён к задаче *{store.get('title', '')}*\n"
                f"🔗 [Открыть на Drive]({drive_file['link']})",
                parse_mode="Markdown",
            )
        else:
            # Нет активной задачи — предложить создать
            user_data_store[chat_id] = {
                "attachments": [drive_file],
                "comment": caption,
            }
            keyboard = [
                [InlineKeyboardButton("➕ Создать задачу с этим файлом", callback_data="attach_add_task")],
                [InlineKeyboardButton("💾 Просто сохранить на Drive", callback_data="attach_drive_only")],
            ]
            await status.edit_text(
                f"✅ {file_type} загружен на Drive:\n"
                f"🔗 [Открыть]({drive_file['link']})\n\n"
                f"Что делаем дальше?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    except Exception as e:
        logger.error(f"Ошибка вложения: {e}")
        await status.edit_text(f"❌ Ошибка загрузки файла: {e}")


async def attach_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создать задачу с уже загруженным вложением."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await query.edit_message_text(
        "📝 *Шаг 1/2 — Название задачи*\n\nВведи название:",
        parse_mode="Markdown",
    )
    context.user_data["state"] = "attach_wait_title"


async def attach_drive_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Файл уже на Drive — ничего больше не делаем."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Файл сохранён на Google Drive в папке *CalBot*.", parse_mode="Markdown")


# ==============================================
# ДОБАВЛЕНИЕ ЗАДАЧИ ВРУЧНУЮ (/add)
# ==============================================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    chat_id = update.effective_chat.id
    # Сохраняем вложения если были до /add
    existing = user_data_store.get(chat_id, {})
    user_data_store[chat_id] = {"attachments": existing.get("attachments", [])}
    await update.message.reply_text("📝 *Шаг 1/3 — Название задачи*\n\nВведи название:", parse_mode="Markdown")
    return WAIT_TITLE


async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_data_store[chat_id]["title"] = update.message.text.strip()
    await update.message.reply_text(
        "📅 *Шаг 2/3 — Дата и время*\n\nФормат: `ДД.ММ.ГГГГ ЧЧ:ММ`\nПример: `25.05.2026 14:30`",
        parse_mode="Markdown",
    )
    return WAIT_DATETIME


async def add_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    dt = parse_datetime_from_text(update.message.text.strip())
    if not dt:
        await update.message.reply_text("❌ Неверный формат. Попробуй: `25.05.2026 14:30`", parse_mode="Markdown")
        return WAIT_DATETIME
    user_data_store[chat_id]["datetime"] = dt
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_comment")]]
    await update.message.reply_text(
        "💬 *Шаг 3/3 — Комментарий*\n\nДобавь описание или нажми «Пропустить»:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAIT_COMMENT


async def add_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_data_store[chat_id]["comment"] = update.message.text.strip()
    await _ask_for_attachments(update.message, chat_id)
    return WAIT_ATTACHMENTS


async def skip_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user_data_store[chat_id]["comment"] = ""
    await _ask_for_attachments(query.message, chat_id)
    return WAIT_ATTACHMENTS


async def _ask_for_attachments(message, chat_id: int):
    """Спросить про вложения."""
    keyboard = [
        [InlineKeyboardButton("✅ Сохранить без вложений", callback_data="save_no_attach")],
    ]
    existing = user_data_store.get(chat_id, {}).get("attachments", [])
    text = "📎 *Вложения*\n\n"
    if existing:
        text += f"Уже прикреплено файлов: {len(existing)}\n\n"
    text += "Отправь фото/документ/аудио прямо сейчас, или нажми «Сохранить»:"
    await message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def save_no_attach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await _save_task_from_store(query, chat_id)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


# ==============================================
# СОХРАНЕНИЕ ЗАДАЧИ
# ==============================================

async def _save_task_from_store(update_or_query, chat_id: int):
    """Создать событие в Calendar + отправить подтверждения."""
    data = user_data_store.get(chat_id, {})
    title = data.get("title", "Без названия")
    dt = data.get("datetime")
    comment = data.get("comment", "")
    attachments = data.get("attachments", [])

    if not dt:
        text = "❌ Не указана дата/время задачи."
        if hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(text)
        else:
            await update_or_query.edit_message_text(text)
        return

    tz = pytz.timezone(TIMEZONE)
    dt_aware = tz.localize(dt) if dt.tzinfo is None else dt

    try:
        event = create_calendar_event(
            title=title,
            description=comment,
            dt=dt_aware,
            reminder_minutes=REMINDER_MINUTES,
            attachments=attachments if attachments else None,
        )
        event_link = event.get("htmlLink", "")
        dt_str = dt.strftime("%d.%m.%Y %H:%M")

        attach_text = ""
        if attachments:
            attach_text = f"\n📎 Вложений: {len(attachments)}"
            for att in attachments:
                attach_text += f"\n  • [{att['name']}]({att['link']})"

        success_text = (
            f"✅ *Задача добавлена!*\n\n"
            f"📌 *{title}*\n"
            f"📅 {dt_str}\n"
            f"💬 {comment if comment else '—'}"
            f"{attach_text}\n\n"
            f"🔔 Напомню за 60 и 15 минут\n"
            f"🔗 [Открыть в Calendar]({event_link})"
        )

        if hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(success_text, parse_mode="Markdown")
        else:
            await update_or_query.edit_message_text(success_text, parse_mode="Markdown")

        # Email подтверждение
        attach_html = ""
        if attachments:
            attach_html = "<h3>📎 Вложения:</h3><ul>"
            for att in attachments:
                attach_html += f"<li><a href='{att['link']}'>{att['name']}</a></li>"
            attach_html += "</ul>"

        email_html = f"""
        <html><body style="font-family:Arial,sans-serif">
        <h2>✅ Новая задача добавлена</h2>
        <table style="border-collapse:collapse;width:100%;max-width:520px">
          <tr><td style="padding:8px;font-weight:bold;background:#f0f0f0">Задача:</td><td style="padding:8px">{title}</td></tr>
          <tr><td style="padding:8px;font-weight:bold;background:#f0f0f0">Дата и время:</td><td style="padding:8px">{dt_str}</td></tr>
          <tr><td style="padding:8px;font-weight:bold;background:#f0f0f0">Комментарий:</td><td style="padding:8px">{comment if comment else '—'}</td></tr>
        </table>
        {attach_html}
        <p>🔔 Напоминания за <b>60 мин</b> и <b>15 мин</b>.</p>
        <p><a href="{event_link}">Открыть в Google Calendar</a></p>
        </body></html>
        """

        # Локальные копии файлов для email уже удалены, Drive-ссылки в теле письма
        send_email(YOUR_EMAIL, f"📌 CalBot: {title} — {dt_str}", email_html)

        # Очистить store
        user_data_store.pop(chat_id, None)

    except Exception as e:
        logger.error(f"Ошибка создания задачи: {e}")
        err_text = f"❌ Ошибка: {e}"
        if hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(err_text)
        else:
            await update_or_query.edit_message_text(err_text)


# ==============================================
# ПРОСМОТР СОБЫТИЙ
# ==============================================

async def today_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    tz = pytz.timezone(TIMEZONE)
    await _send_events_list(update, datetime.now(tz), "сегодня")


async def tomorrow_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    from datetime import timedelta
    tz = pytz.timezone(TIMEZONE)
    await _send_events_list(update, datetime.now(tz) + timedelta(days=1), "завтра")


async def week_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    from datetime import timedelta
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    all_text = "📅 *События на неделю:*\n\n"
    has_events = False
    for i in range(7):
        day = now + timedelta(days=i)
        events = get_events_for_day(day)
        if events:
            has_events = True
            all_text += f"*{day.strftime('%d.%m (%A)')}*\n"
            for e in events:
                start = e.get("start", {})
                time_str = ""
                if "dateTime" in start:
                    time_str = datetime.fromisoformat(start["dateTime"]).strftime("%H:%M")
                desc = e.get("description", "")
                all_text += f"  🕐 {time_str} — {e.get('summary', '—')}"
                if desc:
                    short_desc = desc[:60] + ("..." if len(desc) > 60 else "")
                    all_text += f"\n  💬 _{short_desc}_"
                all_text += "\n"
            all_text += "\n"
    if not has_events:
        all_text = "📭 На этой неделе событий нет."
    await update.message.reply_text(all_text, parse_mode="Markdown")


async def _send_events_list(update: Update, target_date: datetime, label: str):
    events = get_events_for_day(target_date)
    if not events:
        await update.message.reply_text(f"📭 На {label} событий нет.")
        return
    text = f"📅 *События на {label}:*\n\n"
    for e in events:
        start = e.get("start", {})
        time_str = ""
        if "dateTime" in start:
            time_str = datetime.fromisoformat(start["dateTime"]).strftime("%H:%M")
        desc = e.get("description", "")
        text += f"🕐 *{time_str}* — {e.get('summary', '—')}"
        if desc:
            short_desc = desc[:80] + ("..." if len(desc) > 80 else "")
            text += f"\n💬 _{short_desc}_"
        text += "\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")


# ==============================================
# ЗАПУСК
# ==============================================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Conversation для ручного добавления задачи
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            WAIT_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            WAIT_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_datetime)],
            WAIT_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_comment),
                CallbackQueryHandler(skip_comment, pattern="^skip_comment$"),
            ],
            WAIT_ATTACHMENTS: [
                MessageHandler(
                    (filters.PHOTO | filters.Document.ALL | filters.AUDIO | filters.VIDEO) & ~filters.COMMAND,
                    handle_attachment,
                ),
                CallbackQueryHandler(save_no_attach, pattern="^save_no_attach$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today_events))
    app.add_handler(CommandHandler("tomorrow", tomorrow_events))
    app.add_handler(CommandHandler("week", week_events))
    app.add_handler(conv_handler)

    # Голосовые (вне диалога)
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Ввод даты после голосового без даты
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_voice_datetime))

    # Вложения (вне диалога)
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.ALL | filters.AUDIO | filters.VIDEO,
        handle_attachment,
    ))

    # Callback кнопки
    app.add_handler(CallbackQueryHandler(voice_confirm, pattern="^voice_confirm$"))
    app.add_handler(CallbackQueryHandler(voice_edit, pattern="^voice_edit$"))
    app.add_handler(CallbackQueryHandler(attach_add_task, pattern="^attach_add_task$"))
    app.add_handler(CallbackQueryHandler(attach_drive_only, pattern="^attach_drive_only$"))

    logger.info("CalBot запущен ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
