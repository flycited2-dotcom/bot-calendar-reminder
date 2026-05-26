# ==============================================
# google_api.py — Google Calendar + Drive API
# Авторизация: Service Account (рекомендуется) или OAuth токен (fallback)
# ==============================================

import os
import base64
import logging
import mimetypes
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import pytz

from config import (
    SCOPES, CREDENTIALS_FILE, TOKEN_FILE,
    SERVICE_ACCOUNT_FILE, CALENDAR_ID, TIMEZONE, YOUR_EMAIL,
)

logger = logging.getLogger(__name__)

# Scopes для Service Account (без gmail — не работает с личным аккаунтом)
_SA_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.file",
]


def get_google_credentials():
    """
    Приоритет авторизации:
    1. Service Account (не истекает никогда) — если существует service_account.json
    2. OAuth токен (истекает через 7 дней в Testing-режиме) — fallback
    """
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        return service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=_SA_SCOPES
        )

    # OAuth fallback
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Требуется авторизация Google.\n"
                "Вариант 1 (рекомендуется — не истекает):\n"
                "  Создай Service Account в Google Cloud Console,\n"
                "  скачай JSON-ключ и положи в /root/calbot/service_account.json\n"
                "  Поделись календарём с email сервисного аккаунта.\n\n"
                "Вариант 2 (OAuth, истекает через 7 дней):\n"
                f"  cd /root/calbot && source venv/bin/activate && python google_api.py"
            )
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return creds


def get_calendar_service():
    return build("calendar", "v3", credentials=get_google_credentials())


def get_gmail_service():
    return build("gmail", "v1", credentials=get_google_credentials())


def get_drive_service():
    return build("drive", "v3", credentials=get_google_credentials())


# ==============================================
# GOOGLE DRIVE — загрузка вложений
# ==============================================

def _get_or_create_drive_folder(service, folder_name: str) -> str:
    """Найти папку на Drive или создать её."""
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    folder = service.files().create(
        body={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id"
    ).execute()
    logger.info(f"Папка '{folder_name}' создана на Drive")
    return folder["id"]


def upload_to_drive(file_path: str, filename: str, folder_name: str = "CalBot") -> dict:
    """
    Загрузить файл на Google Drive в папку CalBot.
    Возвращает {"id": ..., "link": ..., "name": ...}
    """
    service = get_drive_service()
    folder_id = _get_or_create_drive_folder(service, folder_name)

    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "application/octet-stream"

    uploaded = service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=MediaFileUpload(file_path, mimetype=mime_type, resumable=True),
        fields="id, name, webViewLink",
    ).execute()

    # Открытый доступ по ссылке
    service.permissions().create(
        fileId=uploaded["id"],
        body={"type": "anyone", "role": "reader"},
    ).execute()

    logger.info(f"Файл загружен: {uploaded.get('webViewLink')}")
    return {
        "id": uploaded.get("id"),
        "link": uploaded.get("webViewLink"),
        "name": uploaded.get("name"),
    }


# ==============================================
# GOOGLE CALENDAR
# ==============================================

def create_calendar_event(
    title: str,
    description: str,
    dt: datetime,
    reminder_minutes: list,
    attachments: list = None,
) -> dict:
    """
    Создать событие в Google Calendar.

    :param attachments: список dict {"id": ..., "link": ..., "name": ...} из upload_to_drive()
    """
    service = get_calendar_service()
    tz = pytz.timezone(TIMEZONE)

    if dt.tzinfo is None:
        dt = tz.localize(dt)

    end_dt = dt + timedelta(minutes=30)

    full_description = description or ""
    if attachments:
        full_description += "\n\n📎 Вложения:\n"
        for att in attachments:
            full_description += f"• {att['name']}: {att['link']}\n"

    event_body = {
        "summary": title,
        "description": full_description.strip(),
        "start": {"dateTime": dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in reminder_minutes],
        },
    }

    if attachments:
        event_body["attachments"] = [
            {"fileUrl": att["link"], "title": att["name"], "fileId": att["id"]}
            for att in attachments
        ]

    event = service.events().insert(
        calendarId=CALENDAR_ID,
        body=event_body,
        supportsAttachments=True,
    ).execute()

    logger.info(f"Событие создано: {event.get('htmlLink')}")
    return event


def get_upcoming_events(minutes_ahead: int = 65) -> list:
    service = get_calendar_service()
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    events_result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now.isoformat(),
        timeMax=(now + timedelta(minutes=minutes_ahead)).isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return events_result.get("items", [])


def get_events_for_day(target_date: datetime) -> list:
    service = get_calendar_service()
    tz = pytz.timezone(TIMEZONE)
    naive = target_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    start = tz.localize(naive)
    end = tz.localize(naive.replace(hour=23, minute=59, second=59))
    events_result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return events_result.get("items", [])


def delete_calendar_event(event_id: str) -> bool:
    try:
        get_calendar_service().events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        return True
    except Exception as e:
        logger.error(f"Ошибка удаления события {event_id}: {e}")
        return False


# ==============================================
# GMAIL
# ==============================================

def send_email(to: str, subject: str, body_html: str, attachment_paths: list = None) -> bool:
    """
    Отправить email с опциональными файловыми вложениями.
    """
    try:
        service = get_gmail_service()

        message = MIMEMultipart("mixed")
        message["to"] = to
        message["from"] = YOUR_EMAIL
        message["subject"] = Header(subject, "utf-8").encode()

        alt_part = MIMEMultipart("alternative")
        alt_part.attach(MIMEText(body_html, "html", "utf-8"))
        message.attach(alt_part)

        if attachment_paths:
            for path in attachment_paths:
                if not os.path.exists(path):
                    continue
                mime_type, _ = mimetypes.guess_type(path)
                main_type, sub_type = (mime_type or "application/octet-stream").split("/", 1)
                with open(path, "rb") as f:
                    part = MIMEBase(main_type, sub_type)
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=os.path.basename(path))
                message.attach(part)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(f"Email отправлен на {to}: {subject}")
        return True

    except Exception as e:
        logger.error(f"Ошибка отправки email: {e}")
        return False


if __name__ == "__main__":
    # Запускать вручную для первичной/повторной авторизации Google:
    #   cd /root/calbot && source venv/bin/activate && python google_api.py
    import sys
    logging.basicConfig(level=logging.INFO)
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"❌ Файл {CREDENTIALS_FILE} не найден. Скачай credentials.json из Google Cloud Console.")
        sys.exit(1)
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    print(f"\nОткрой в браузере:\n{auth_url}\n")
    code = input("Вставь код авторизации: ").strip()
    flow.fetch_token(code=code)
    creds = flow.credentials
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"✅ Токен сохранён в {TOKEN_FILE}")
