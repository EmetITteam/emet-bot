#!/bin/bash
# backup_indices.sh — щоденний backup ChromaDB індексів
# Cron (від root або emet): 0 2 * * * /opt/emet-bot/backup_indices.sh >> /var/log/emet_backup.log 2>&1

set -euo pipefail

DATA_DIR="/opt/emet-bot/data"
BACKUP_DIR="/opt/emet-bot/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M)
ARCHIVE="$BACKUP_DIR/indices_$TIMESTAMP.tar.gz"
KEEP_DAYS=7

# ENV з .env файлу (для Telegram алерту)
ENV_FILE="/opt/emet-bot/.env"
BOT_TOKEN=""
ADMIN_ID=""
if [ -f "$ENV_FILE" ]; then
    BOT_TOKEN=$(grep '^TELEGRAM_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
    ADMIN_ID=$(grep '^ADMIN_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'")
fi

send_tg() {
    local msg="$1"
    if [ -n "$BOT_TOKEN" ] && [ -n "$ADMIN_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=${ADMIN_ID}" \
            -d "text=${msg}" \
            -d "parse_mode=Markdown" > /dev/null 2>&1 || true
    fi
}

mkdir -p "$BACKUP_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting backup..."

# Архівуємо всі ChromaDB індекси (bind mount — на хості, не в контейнері)
tar -czf "$ARCHIVE" \
    -C "$DATA_DIR" \
    db_index_kb_openai \
    db_index_kb_google \
    db_index_coach_openai \
    db_index_coach_google \
    db_index_certs_openai \
    db_index_certs_google 2>/dev/null || \
tar -czf "$ARCHIVE" \
    -C "$DATA_DIR" \
    $(ls "$DATA_DIR" | grep '^db_index_')

ARCHIVE_SIZE=$(du -sh "$ARCHIVE" | cut -f1)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup OK: $ARCHIVE ($ARCHIVE_SIZE)"

# Видаляємо старіші N+1 архіви
find "$BACKUP_DIR" -name "indices_*.tar.gz" -type f \
    | sort -r | tail -n +$((KEEP_DAYS + 1)) | xargs -r rm -f

REMAINING=$(find "$BACKUP_DIR" -name "indices_*.tar.gz" | wc -l)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cleanup done. Kept $REMAINING archives."

send_tg "✅ *EMET Backup OK* — \`$TIMESTAMP\` ($ARCHIVE_SIZE, зберігається $REMAINING/${KEEP_DAYS} копій)"

exit 0
