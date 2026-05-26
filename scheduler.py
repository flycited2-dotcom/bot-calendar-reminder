# ==============================================
# scheduler.py — планировщик напоминаний
# Запускается отдельно от бота как systemd сервис
# ==============================================

import logging
import time
import json
import os
from datetime import datetime, timedelta

import pytz
import requests

from config import (
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT_ID,
    SCHEDULER_INTERVAL,
    TIMEZONE,
    YOUR_EMAIL,
    REMINDER_MINUTES,
    SCHEDULER_LOG,
)
from google_api import get_upcoming_events, send_email

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(SCHEDULER_LOG),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Файл для хранения уже отправленных напоминаний (чтобы не дублировать)
SENT_REMINDERS_FILE = "/root/calbot/sent_reminders.json"


def load_sent_reminders() -> dict:
    """Загрузить список уже отправленных напоминаний."""
    if os.path.exists(SENT_REMINDERS_FILE):
        with open(SENT_REMINDERS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_sent_reminders(data: dict):
    """Сохранить список отправленных напоминаний."""
    with open(SENT_REMINDERS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def clean_old_reminders(sent: dict) -> dict:
    """Удалить записи старше 24 часов."""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    cutoff = now - timedelta(hours=24)
    return {
        k: v for k, v in sent.items()
        if datetime.fromisoformat(v.get("sent_at", now.isoformat())) > cutoff
    }


def send_telegram_message(text: str) -> bool:
    """Отправить сообщение в Telegram через Bot API."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        }
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Ошибка отправки Telegram: {e}")
        return False


def check_and_send_reminders():
    """Основная функция: проверить предстоящие события и отправить напоминания."""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    sent = load_sent_reminders()
    sent = clean_old_reminders(sent)

    # Проверяем события на ближайшие max(REMINDER_MINUTES) + 5 минут
    max_ahead = max(REMINDER_MINUTES) + 5
    events = get_upcoming_events(minutes_ahead=max_ahead)

    for event in events:
        event_id = event.get("id", "")
        title = event.get("summary", "Без названия")
        description = event.get("description", "")

        start = event.get("start", {})
        if "dateTime" not in start:
            continue  # пропускаем all-day события

        event_dt = datetime.fromisoformat(start["dateTime"])
        if event_dt.tzinfo is None:
            event_dt = tz.localize(event_dt)

        minutes_until = int((event_dt - now).total_seconds() / 60)

        for reminder_min in REMINDER_MINUTES:
            # Ключ: событие + тип напоминания
            reminder_key = f"{event_id}_{reminder_min}"

            # Уже отправляли это напоминание?
            if reminder_key in sent:
                continue

            # Попадаем в окно ±2 минуты от нужного момента?
            if abs(minutes_until - reminder_min) <= 2:
                dt_str = event_dt.strftime("%d.%m.%Y %H:%M")

                if reminder_min >= 60:
                    time_label = f"{reminder_min // 60} час{'а' if reminder_min // 60 in [2,3,4] else ''}"
                else:
                    time_label = f"{reminder_min} минут"

                # === Telegram ===
                tg_text = (
                    f"🔔 *Напоминание!*\n\n"
                    f"📌 *{title}*\n"
                    f"📅 {dt_str}\n"
                    f"⏰ Начало через *{time_label}*\n"
                )
                if description:
                    tg_text += f"💬 _{description}_"

                tg_ok = send_telegram_message(tg_text)

                # === Email (только если настроен) ===
                email_ok = False
                if YOUR_EMAIL:
                    email_html = f"""
                    <html><body style="font-family:Arial,sans-serif">
                    <h2>🔔 Напоминание о задаче</h2>
                    <table style="border-collapse:collapse;width:100%;max-width:500px">
                      <tr>
                        <td style="padding:10px;font-weight:bold;background:#f0f0f0">Задача:</td>
                        <td style="padding:10px">{title}</td>
                      </tr>
                      <tr>
                        <td style="padding:10px;font-weight:bold;background:#f0f0f0">Дата и время:</td>
                        <td style="padding:10px">{dt_str}</td>
                      </tr>
                      <tr>
                        <td style="padding:10px;font-weight:bold;background:#f0f0f0">До начала:</td>
                        <td style="padding:10px"><b>{time_label}</b></td>
                      </tr>
                      {"<tr><td style='padding:10px;font-weight:bold;background:#f0f0f0'>Комментарий:</td><td style='padding:10px'>" + description + "</td></tr>" if description else ""}
                    </table>
                    </body></html>
                    """
                    email_ok = send_email(
                        YOUR_EMAIL,
                        f"🔔 CalBot: {title} — через {time_label}",
                        email_html
                    )

                if tg_ok or email_ok:
                    sent[reminder_key] = {
                        "title": title,
                        "sent_at": now.isoformat(),
                        "minutes_before": reminder_min,
                    }
                    logger.info(f"Напоминание отправлено: {title} (за {reminder_min} мин)")

    save_sent_reminders(sent)


def main():
    """Бесконечный цикл планировщика."""
    logger.info("Scheduler CalBot запущен ✅")
    _auth_error_notified = False
    while True:
        try:
            check_and_send_reminders()
            _auth_error_notified = False
        except RuntimeError as e:
            # Ошибка авторизации Google — уведомляем пользователя один раз
            logger.error(f"Ошибка авторизации Google: {e}")
            if not _auth_error_notified:
                send_telegram_message(
                    f"⚠️ *Требуется повторная авторизация Google*\n\n"
                    f"Выполни на сервере:\n"
                    f"`cd /root/calbot && source venv/bin/activate && python google_api.py`"
                )
                _auth_error_notified = True
        except Exception as e:
            logger.error(f"Ошибка в планировщике: {e}")
        time.sleep(SCHEDULER_INTERVAL)


if __name__ == "__main__":
    main()
