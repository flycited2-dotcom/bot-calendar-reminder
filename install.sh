#!/bin/bash
# ==============================================
# install.sh — установка CalBot на VPS
# Запускать: bash install.sh
# ==============================================

set -e

echo "=== CalBot installer ==="

# 1. Создаём директорию
mkdir -p /home/alex/calbot
cd /home/alex/calbot

# 2. Python venv
python3 -m venv venv
source venv/bin/activate

# 3. Зависимости
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "✅ Зависимости установлены"

# 4. Права на файлы
chmod 600 /home/alex/calbot/config.py

# 5. Копируем systemd сервисы
cp /home/alex/calbot/calbot.service /etc/systemd/system/calbot.service
cp /home/alex/calbot/calbot-scheduler.service /etc/systemd/system/calbot-scheduler.service

# 6. Активируем сервисы
systemctl daemon-reload
systemctl enable calbot
systemctl enable calbot-scheduler

echo ""
echo "=== Следующие шаги ==="
echo ""
echo "1. Отредактируй config.py:"
echo "   nano /home/alex/calbot/config.py"
echo "   - TELEGRAM_TOKEN (от @BotFather)"
echo "   - TELEGRAM_CHAT_ID (от @userinfobot)"
echo ""
echo "2. Положи credentials.json в /home/alex/calbot/"
echo "   (скачать с console.cloud.google.com)"
echo ""
echo "3. Первая авторизация Google:"
echo "   cd /home/alex/calbot && source venv/bin/activate"
echo "   python google_api.py"
echo "   (откроется ссылка — пройди авторизацию, создастся token.json)"
echo ""
echo "4. Запуск сервисов:"
echo "   systemctl start calbot"
echo "   systemctl start calbot-scheduler"
echo ""
echo "5. Проверка логов:"
echo "   journalctl -u calbot -f"
echo "   journalctl -u calbot-scheduler -f"
