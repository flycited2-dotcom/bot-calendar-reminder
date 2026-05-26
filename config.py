# ==============================================
# config.py — настройки Calendar Bot
# ==============================================

# Telegram
TELEGRAM_TOKEN = "8306726797:AAHn_RPjZufTb3eoAPW-JTBwTEbeIQh4TK8"
TELEGRAM_CHAT_ID = "-5216399246"

# OpenAI (Whisper для транскрипции голосовых)
OPENAI_API_KEY = "ВАШ_OPENAI_API_KEY"       # platform.openai.com

# Gmail / почта (оставь пустым "" чтобы отключить email-уведомления)
YOUR_EMAIL = ""

# Google API scopes
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.file",   # для загрузки файлов на Drive
]

# Напоминания (в минутах до события)
REMINDER_MINUTES = [60, 15]

# Интервал планировщика (секунды)
SCHEDULER_INTERVAL = 60

# Часовой пояс
TIMEZONE = "Europe/Moscow"

# Пути к Google Auth
# Service Account (рекомендуется): токен не истекает никогда
SERVICE_ACCOUNT_FILE = "/root/calbot/service_account.json"
# OAuth (fallback): используется если service_account.json отсутствует
CREDENTIALS_FILE = "/root/calbot/credentials.json"
TOKEN_FILE = "/root/calbot/token.json"

# Основной календарь
CALENDAR_ID = "primary"

# Временная папка для скачивания файлов из Telegram
TEMP_DIR = "/root/calbot/tmp"

# Пути к лог-файлам
BOT_LOG = "/root/calbot/bot.log"
SCHEDULER_LOG = "/root/calbot/scheduler.log"
