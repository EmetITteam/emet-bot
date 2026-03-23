#!/bin/bash
# deploy.sh — безпечний деплой EMET Bot
# Зберігає локальні зміни (порт 5000:5000), тягне нові коміти, відновлює
# Використання: bash /opt/emet-bot/deploy.sh

set -euo pipefail
cd "$(dirname "$0")"

echo "[deploy] $(date '+%Y-%m-%d %H:%M:%S') — починаємо..."

# Зберігаємо локальні зміни (наприклад, порт 5000:5000 у docker-compose.yml)
git stash

# Тягнемо зміни
git pull

# Відновлюємо локальні зміни
git stash pop || true

# Записуємо маркер деплою (бот надішле сповіщення при запуску)
echo "$(date '+%Y-%m-%d %H:%M:%S')" > data/deploy_marker.txt

# Перебудовуємо і перезапускаємо контейнери
docker compose up -d --build

echo "[deploy] $(date '+%Y-%m-%d %H:%M:%S') — готово."
