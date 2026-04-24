# ==============================================
# bot.py — CalBot с кнопочным меню
# ==============================================

import logging
import os
import re
import tempfile
from datetime import datetime, timedelta

import subprocess
import wave
import pytz
import requests as req
import speech_recognition as sr

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
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

# Состояния диалога
WAIT_TITLE, WAIT_DATETIME, WAIT_COMMENT, WAIT_ATTACHMENTS = range(4)

# Временное хранилище
user_data_store = {}

# Главное меню — постоянные кнопки внизу экрана
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Добавить задачу"],
        ["📋 Сегодня", "📅 Завтра"],
        ["🗓 Неделя", "❓ Помощь"],
    ],
    resize_keyboard=True,
)

BTN_ADD  = "➕ Добавить задачу"
BTN_TODAY = "📋 Сегодня"
BTN_TOMORROW = "📅 Завтра"
BTN_WEEK = "🗓 Неделя"
BTN_HELP = "❓ Помощь"


# ==============================================
# УТИЛИТЫ
# ==============================================

def is_allowed(update: Update) -> bool:
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


def parse_reminder_from_text(text: str) -> int | None:
    """Извлекает кастомное время напоминания в минутах: 'напомни за 30 минут'."""
    t = text.lower()
    m = re.search(
        r"(?:напомни|предупреди|напомнить|предупредить)\s+за\s+(\d+)\s*(минут|мин|часа?|часов)",
        t,
    )
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return n if "мин" in unit else n * 60
    if re.search(r"(?:напомни|предупреди)\s+за\s+час\b", t):
        return 60
    if re.search(r"(?:напомни|предупреди)\s+за\s+полчаса", t):
        return 30
    m = re.search(r"за\s+(\d+)\s*(минут|мин|часа?|часов)\s+до", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return n if "мин" in unit else n * 60
    return None


def extract_title_from_voice(text: str) -> str:
    """Извлекает название задачи из голосового, убирая дату и фразу о напоминании."""
    t = text
    # убрать "напомни за ..."
    t = re.sub(
        r"(?:напомни|предупреди|напомнить|предупредить)\s+за\s+\S+(?:\s+\S+)?",
        "", t, flags=re.IGNORECASE,
    )
    # убрать "через N единиц"
    t = re.sub(r"через\s+\d+\s*(?:часа?|часов|мин|минут|день|дней|дня)", "", t, flags=re.IGNORECASE)
    # убрать "сегодня/завтра в ЧЧ:ММ"
    t = re.sub(r"(?:сегодня|завтра)\s+(?:в\s+)?\d{1,2}[:.]\d{2}", "", t, flags=re.IGNORECASE)
    # убрать "в ЧЧ:ММ"
    t = re.sub(r"\bв\s+\d{1,2}[:.]\d{2}", "", t, flags=re.IGNORECASE)
    # убрать ДД.ММ.ГГГГ ЧЧ:ММ
    t = re.sub(r"\d{1,2}[./]\d{2}(?:[./]\d{4})?\s+\d{1,2}:\d{2}", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[,\s]+$", "", t.strip())
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t or text[:80]


def parse_datetime_from_text(text: str) -> datetime | None:
    """
    Парсит дату/время из текста.
    Поддерживает:
      - ДД.ММ.ГГГГ ЧЧ:ММ  /  ГГГГ-ММ-ДД ЧЧ:ММ
      - ДД.ММ ЧЧ:ММ  (текущий год)
      - сегодня в ЧЧ:ММ  /  сегодня ЧЧ:ММ
      - завтра в ЧЧ:ММ
      - через N часов/минут/дней
      - ЧЧ:ММ (сегодня, если время ещё не прошло)
    """
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz).replace(tzinfo=None)
    t = text.lower().strip()

    # через N единиц
    m = re.search(r"через\s+(\d+)\s*(час|мин|дн|день|минут|часов)", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if "мин" in unit:
            return now + timedelta(minutes=n)
        elif "час" in unit:
            return now + timedelta(hours=n)
        else:
            return now + timedelta(days=n)

    # сегодня / завтра в ЧЧ:ММ
    time_pat = r"(\d{1,2})[:\.](\d{2})"
    tm = re.search(time_pat, t)
    if tm:
        h, mi = int(tm.group(1)), int(tm.group(2))
        base = now
        if "завтра" in t:
            base = now + timedelta(days=1)
        if 0 <= h <= 23 and 0 <= mi <= 59:
            candidate = base.replace(hour=h, minute=mi, second=0, microsecond=0)
            # если "сегодня" или просто время и оно уже прошло — не используем без даты
            if "завтра" in t or "сегодня" in t:
                return candidate
            # просто ЧЧ:ММ без явной даты — только если нет дд.мм
            if not re.search(r"\d{1,2}[./]\d{1,2}", t):
                if candidate < now:
                    candidate += timedelta(days=1)
                return candidate

    # ДД.ММ.ГГГГ ЧЧ:ММ
    m = re.search(r"(\d{2})[./](\d{2})[./](\d{4})\s+(\d{1,2}):(\d{2})", text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                            int(m.group(4)), int(m.group(5)))
        except ValueError:
            pass

    # ГГГГ-ММ-ДД ЧЧ:ММ
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)))
        except ValueError:
            pass

    # ДД.ММ ЧЧ:ММ (текущий год)
    m = re.search(r"(\d{1,2})[./](\d{2})\s+(\d{1,2}):(\d{2})", text)
    if m:
        try:
            return datetime(now.year, int(m.group(2)), int(m.group(1)),
                            int(m.group(3)), int(m.group(4)))
        except ValueError:
            pass

    return None


# ==============================================
# WHISPER — транскрипция голосовых
# ==============================================

async def transcribe_voice(file_path: str) -> str:
    """Конвертирует OGG в WAV и распознаёт речь через Google Speech Recognition (бесплатно)."""
    wav_path = file_path.replace(".ogg", ".wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", file_path, "-ar", "16000", "-ac", "1", wav_path],
            check=True, capture_output=True,
        )
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
        return recognizer.recognize_google(audio, language="ru-RU")
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)


async def download_telegram_file(file_id: str, context: ContextTypes.DEFAULT_TYPE, suffix: str = "") -> str:
    os.makedirs(TEMP_DIR, exist_ok=True)
    tg_file = await context.bot.get_file(file_id)
    tmp = tempfile.NamedTemporaryFile(delete=False, dir=TEMP_DIR, suffix=suffix)
    await tg_file.download_to_drive(tmp.name)
    return tmp.name


# ==============================================
# ГЛАВНОЕ МЕНЮ
# ==============================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "👋 *CalBot — твой личный планировщик*\n\n"
        "Выбери действие на кнопках ниже 👇",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "📖 *Как добавить задачу:*\n\n"
        "• Нажми *➕ Добавить задачу* → введи название → дату\n"
        "• Или отправь *голосовое* — бот распознает и уточнит\n"
        "• Или *прикрепи фото/файл* — бот предложит создать задачу\n\n"
        "📅 *Форматы даты:*\n"
        "`25.04.2026 14:30`\n"
        "`сегодня в 14:30`\n"
        "`завтра в 9:00`\n"
        "`через 3 часа`\n\n"
        "🔔 Напоминания за *60 мин* и *15 мин*",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


# ==============================================
# ПРОСМОТР СОБЫТИЙ
# ==============================================

async def today_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    tz = pytz.timezone(TIMEZONE)
    await _send_events_list(update.message, datetime.now(tz), "сегодня")


async def tomorrow_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    tz = pytz.timezone(TIMEZONE)
    await _send_events_list(update.message, datetime.now(tz) + timedelta(days=1), "завтра")


async def week_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    text = "🗓 *События на неделю:*\n\n"
    has_events = False
    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    for i in range(7):
        day = now + timedelta(days=i)
        events = get_events_for_day(day)
        if events:
            has_events = True
            dow = day_names[day.weekday()]
            text += f"*{day.strftime('%d.%m')} ({dow})*\n"
            for e in events:
                start = e.get("start", {})
                time_str = ""
                if "dateTime" in start:
                    time_str = datetime.fromisoformat(start["dateTime"]).strftime("%H:%M")
                desc = e.get("description", "")
                text += f"  🕐 {time_str} — {e.get('summary', '—')}"
                if desc:
                    text += f"\n  💬 _{desc[:50]}{'…' if len(desc)>50 else ''}_"
                text += "\n"
            text += "\n"
    if not has_events:
        text = "📭 На этой неделе событий нет."
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_MENU)


async def _send_events_list(message, target_date: datetime, label: str):
    events = get_events_for_day(target_date)
    if not events:
        await message.reply_text(f"📭 На {label} событий нет.", reply_markup=MAIN_MENU)
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
            text += f"\n💬 _{desc[:80]}{'…' if len(desc)>80 else ''}_"
        text += "\n\n"
    await message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_MENU)


# ==============================================
# ДОБАВЛЕНИЕ ЗАДАЧИ — ConversationHandler
# ==============================================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    # Сохраняем вложения если были
    existing = user_data_store.get(chat_id, {})
    user_data_store[chat_id] = {"attachments": existing.get("attachments", [])}

    text = "📝 *Шаг 1/3 — Название задачи*\n\nВведи название:"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
    return WAIT_TITLE


async def attach_add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создать задачу с уже загруженным вложением — входная точка ConversationHandler."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user_data_store.setdefault(chat_id, {})
    await query.edit_message_text(
        "📝 *Шаг 1/3 — Название задачи*\n\nВведи название:",
        parse_mode="Markdown",
    )
    return WAIT_TITLE


async def add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    title = update.message.text.strip()
    user_data_store[chat_id]["title"] = title
    await update.message.reply_text(
        f"✅ Название: *{title}*\n\n"
        "📅 *Шаг 2/3 — Дата и время*\n\n"
        "Введи дату любым способом:\n"
        "`25.04.2026 14:30`\n"
        "`сегодня в 14:30`\n"
        "`завтра в 9:00`\n"
        "`через 3 часа`",
        parse_mode="Markdown",
    )
    return WAIT_DATETIME


async def add_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    dt = parse_datetime_from_text(update.message.text.strip())
    if not dt:
        await update.message.reply_text(
            "❌ Не удалось распознать дату. Попробуй:\n"
            "`25.04.2026 14:30`  или  `сегодня в 15:00`  или  `через 2 часа`",
            parse_mode="Markdown",
        )
        return WAIT_DATETIME
    user_data_store[chat_id]["datetime"] = dt
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_comment")]]
    await update.message.reply_text(
        f"✅ Дата: *{dt.strftime('%d.%m.%Y %H:%M')}*\n\n"
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
    keyboard = [[InlineKeyboardButton("✅ Сохранить без вложений", callback_data="save_no_attach")]]
    existing = user_data_store.get(chat_id, {}).get("attachments", [])
    text = "📎 *Вложения*\n\n"
    if existing:
        text += f"Уже прикреплено: {len(existing)} файл(а)\n\n"
    text += "Отправь фото/файл прямо сейчас, или нажми «Сохранить»:"
    await message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def save_no_attach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    await _save_task_from_store(query, chat_id)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ==============================================
# СОХРАНЕНИЕ ЗАДАЧИ
# ==============================================

async def _save_task_from_store(update_or_query, chat_id: int):
    data = user_data_store.get(chat_id, {})
    title = data.get("title", "Без названия")
    dt = data.get("datetime")
    comment = data.get("comment", "")
    attachments = data.get("attachments", [])

    if not dt:
        msg = "❌ Не указана дата/время задачи."
        if hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(msg)
        else:
            await update_or_query.edit_message_text(msg)
        return

    tz = pytz.timezone(TIMEZONE)
    dt_aware = tz.localize(dt) if dt.tzinfo is None else dt

    reminder_mins = data.get("custom_reminder", REMINDER_MINUTES)
    if isinstance(reminder_mins, int):
        reminder_mins = [reminder_mins]
    if len(reminder_mins) == 1:
        h, m_r = divmod(reminder_mins[0], 60)
        reminder_label = (f"за {h} ч" if h and not m_r else
                          f"за {m_r} мин" if not h else f"за {h} ч {m_r} мин")
    else:
        reminder_label = " и ".join(
            f"{r // 60} ч" if r >= 60 and r % 60 == 0 else f"{r} мин"
            for r in reminder_mins
        )

    try:
        event = create_calendar_event(
            title=title,
            description=comment,
            dt=dt_aware,
            reminder_minutes=reminder_mins,
            attachments=attachments if attachments else None,
        )
        event_link = event.get("htmlLink", "")
        dt_str = dt.strftime("%d.%m.%Y %H:%M")

        attach_text = ""
        if attachments:
            attach_text = f"\n📎 Вложений: {len(attachments)}"

        success = (
            f"✅ *Задача создана!*\n\n"
            f"📌 *{title}*\n"
            f"📅 {dt_str}\n"
            f"💬 {comment if comment else '—'}"
            f"{attach_text}\n\n"
            f"🔔 Напомню {reminder_label}\n"
            f"🔗 [Открыть в Calendar]({event_link})"
        )

        if hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(success, parse_mode="Markdown", reply_markup=MAIN_MENU)
        else:
            await update_or_query.edit_message_text(success, parse_mode="Markdown")
            # restore menu via separate message
            await update_or_query.message.reply_text("Главное меню 👇", reply_markup=MAIN_MENU)

        send_email(YOUR_EMAIL, f"📌 CalBot: {title} — {dt_str}", f"<b>{title}</b><br>{dt_str}<br>{comment}")
        user_data_store.pop(chat_id, None)

    except Exception as e:
        logger.error(f"Ошибка создания задачи: {e}")
        err = f"❌ Ошибка: {e}"
        if hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(err, reply_markup=MAIN_MENU)
        else:
            await update_or_query.edit_message_text(err)


# ==============================================
# ГОЛОСОВЫЕ СООБЩЕНИЯ
# ==============================================

def _voice_summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Создать", callback_data="voice_confirm"),
            InlineKeyboardButton("✏️ Изменить", callback_data="voice_edit"),
        ],
        [InlineKeyboardButton("📎 Добавить файл", callback_data="voice_add_file")],
    ])


def _voice_summary_text(chat_id: int) -> str:
    data = user_data_store.get(chat_id, {})
    title = data.get("title", "—")
    dt = data.get("datetime")
    dt_str = dt.strftime("%d.%m.%Y %H:%M") if dt else "не указано"
    custom = data.get("custom_reminder")
    if custom:
        h, m = divmod(custom[0], 60)
        reminder_label = (f"за {h} ч" if h and not m else
                          f"за {m} мин" if not h else f"за {h} ч {m} мин")
    else:
        reminder_label = f"за {REMINDER_MINUTES[0]} и {REMINDER_MINUTES[1]} мин"
    attachments = data.get("attachments", [])
    attach_line = f"\n📎 Вложений: {len(attachments)}" if attachments else ""
    return (
        f"🎯 *Понял!*\n\n"
        f"📌 *{title}*\n"
        f"📅 {dt_str}\n"
        f"🔔 Напомню {reminder_label}"
        f"{attach_line}\n\n"
        f"Создать задачу?"
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("🎤 Распознаю голосовое...")

    try:
        file_path = await download_telegram_file(update.message.voice.file_id, context, suffix=".ogg")
        text = await transcribe_voice(file_path)
        os.unlink(file_path)

        dt = parse_datetime_from_text(text)
        reminder_min = parse_reminder_from_text(text)
        title = extract_title_from_voice(text)

        user_data_store[chat_id] = {
            "title": title,
            "comment": text,
            "attachments": [],
            "from_voice": True,
        }

        if dt:
            user_data_store[chat_id]["datetime"] = dt
        if reminder_min:
            user_data_store[chat_id]["custom_reminder"] = [reminder_min]

        if dt:
            await msg.edit_text(
                _voice_summary_text(chat_id),
                parse_mode="Markdown",
                reply_markup=_voice_summary_keyboard(),
            )
        else:
            await msg.edit_text(
                f"🎤 *Записал:* _{text}_\n\n"
                "📅 Не нашёл дату. Введи дату и время:\n"
                "`сегодня в 14:30`  /  `через 2 часа`  /  `25.04 14:30`",
                parse_mode="Markdown",
            )
            context.user_data["awaiting_voice_datetime"] = True

    except Exception as e:
        logger.error(f"Ошибка голосового: {e}")
        await msg.edit_text(f"❌ Ошибка распознавания:\n{e}", reply_markup=MAIN_MENU)


async def voice_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _save_task_from_store(query, query.message.chat_id)


async def voice_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = user_data_store.get(chat_id, {})
    await query.edit_message_text(
        f"✏️ Текущее название:\n_{data.get('title', '')}._\n\nВведи новое название:",
        parse_mode="Markdown",
    )
    context.user_data["voice_wait_title"] = True


async def voice_add_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["voice_waiting_file"] = True
    await query.edit_message_text(
        "📎 Отправь фото или файл — прикреплю к задаче.",
    )


# ==============================================
# ВЛОЖЕНИЯ
# ==============================================

async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    msg = update.message

    if msg.photo:
        file_id = msg.photo[-1].file_id
        filename = f"photo_{chat_id}_{int(datetime.now().timestamp())}.jpg"
        suffix, file_type = ".jpg", "📷 Фото"
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
        suffix, file_type = ".mp4", "🎬 Видео"
    else:
        await msg.reply_text("❓ Неизвестный тип файла.")
        return

    status = await msg.reply_text(f"{file_type} получен, загружаю на Drive…")

    try:
        local_path = await download_telegram_file(file_id, context, suffix=suffix)
        drive_file = upload_to_drive(local_path, filename)
        os.unlink(local_path)

        store = user_data_store.get(chat_id, {})
        if store.get("datetime") and context.user_data.pop("voice_waiting_file", False):
            store.setdefault("attachments", []).append(drive_file)
            user_data_store[chat_id] = store
            await status.edit_text(
                f"✅ {file_type} прикреплён!\n"
                f"🔗 [Открыть на Drive]({drive_file['link']})",
                parse_mode="Markdown",
            )
            await msg.reply_text(
                _voice_summary_text(chat_id),
                parse_mode="Markdown",
                reply_markup=_voice_summary_keyboard(),
            )
        elif store.get("datetime"):
            store.setdefault("attachments", []).append(drive_file)
            user_data_store[chat_id] = store
            await status.edit_text(
                f"✅ {file_type} прикреплён к задаче *{store.get('title','')}*\n"
                f"🔗 [Открыть на Drive]({drive_file['link']})",
                parse_mode="Markdown",
            )
        else:
            user_data_store[chat_id] = {"attachments": [drive_file], "comment": msg.caption or ""}
            keyboard = [
                [InlineKeyboardButton("➕ Создать задачу с файлом", callback_data="attach_add_task")],
                [InlineKeyboardButton("💾 Только сохранить на Drive", callback_data="attach_drive_only")],
            ]
            await status.edit_text(
                f"✅ {file_type} загружен на Drive:\n"
                f"🔗 [Открыть]({drive_file['link']})\n\nЧто делаем дальше?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

    except Exception as e:
        logger.error(f"Ошибка вложения: {e}")
        await status.edit_text(f"❌ Ошибка загрузки: {e}", reply_markup=MAIN_MENU)


async def attach_drive_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Файл сохранён на Google Drive в папке *CalBot*.", parse_mode="Markdown")


# ==============================================
# ОБРАБОТЧИК ТЕКСТА ВНЕ ДИАЛОГА
# ==============================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки меню и ввод даты после голосового."""
    if not is_allowed(update):
        return

    text = update.message.text.strip()

    # Кнопки меню
    if text == BTN_TODAY:
        await today_events(update, context)
        return
    if text == BTN_TOMORROW:
        await tomorrow_events(update, context)
        return
    if text == BTN_WEEK:
        await week_events(update, context)
        return
    if text == BTN_HELP:
        await help_command(update, context)
        return

    # Ввод даты после голосового
    if context.user_data.get("awaiting_voice_datetime"):
        chat_id = update.effective_chat.id
        dt = parse_datetime_from_text(text)
        if not dt:
            await update.message.reply_text(
                "❌ Не удалось распознать дату. Попробуй:\n"
                "`сегодня в 14:30`  /  `через 2 часа`  /  `25.04 14:30`",
                parse_mode="Markdown",
            )
            return
        user_data_store[chat_id]["datetime"] = dt
        context.user_data.pop("awaiting_voice_datetime", None)
        keyboard = [[
            InlineKeyboardButton("✅ Создать", callback_data="voice_confirm"),
            InlineKeyboardButton("✏️ Изменить", callback_data="voice_edit"),
        ]]
        title = user_data_store[chat_id].get("title", "")
        await update.message.reply_text(
            f"🎯 *Понял!*\n\n"
            f"📌 *{title[:80]}*\n"
            f"📅 {dt.strftime('%d.%m.%Y %H:%M')}\n\nСоздать задачу?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Ввод нового названия после "Изменить" из голосового
    if context.user_data.get("voice_wait_title"):
        chat_id = update.effective_chat.id
        user_data_store.setdefault(chat_id, {})["title"] = text
        context.user_data.pop("voice_wait_title", None)
        await update.message.reply_text(
            f"✅ Название изменено на: *{text}*\n\n"
            "📅 Теперь введи дату и время:\n`сегодня в 14:30`  /  `через 2 часа`",
            parse_mode="Markdown",
        )
        context.user_data["awaiting_voice_datetime"] = True
        return


# ==============================================
# ЗАПУСК
# ==============================================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_ADD)}$"), add_start),
            CallbackQueryHandler(attach_add_task, pattern="^attach_add_task$"),
        ],
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
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("today", today_events))
    app.add_handler(CommandHandler("tomorrow", tomorrow_events))
    app.add_handler(CommandHandler("week", week_events))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.ALL | filters.AUDIO | filters.VIDEO,
        handle_attachment,
    ))
    app.add_handler(CallbackQueryHandler(voice_confirm, pattern="^voice_confirm$"))
    app.add_handler(CallbackQueryHandler(voice_edit, pattern="^voice_edit$"))
    app.add_handler(CallbackQueryHandler(voice_add_file, pattern="^voice_add_file$"))
    app.add_handler(CallbackQueryHandler(attach_drive_only, pattern="^attach_drive_only$"))

    logger.info("CalBot запущен ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
