#!/bin/bash
# backup_indices.sh — щоденний backup PostgreSQL + ChromaDB індексів
# Cron: 0 2 * * * /opt/emet-bot/scripts/backup_indices.sh >> /var/log/emet_backup.log 2>&1

set -euo pipefail

DATA_DIR="/opt/emet-bot/data"
BACKUP_DIR="/opt/emet-bot/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M)
INDICES_ARCHIVE="$BACKUP_DIR/indices_$TIMESTAMP.tar.gz"
PG_DUMP_FILE="$BACKUP_DIR/pgdump_$TIMESTAMP.sql.gz"
KEEP_DAYS=7

ENV_FILE="/opt/emet-bot/.env"
BOT_TOKEN=""
ADMIN_ID=""
PG_USER=""
PG_DB=""
if [ -f "$ENV_FILE" ]; then
    # tr -d '\r' — на випадок CRLF line endings у .env (Windows)
    BOT_TOKEN=$(grep '^TELEGRAM_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"\047\r')
    ADMIN_ID=$(grep '^ADMIN_ID=' "$ENV_FILE" | cut -d= -f2- | tr -d '"\047\r')
    PG_USER=$(grep '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2- | tr -d '"\047\r')
    PG_DB=$(grep '^POSTGRES_DB=' "$ENV_FILE" | cut -d= -f2- | tr -d '"\047\r')
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

# 1. PostgreSQL dump через docker exec
PG_OK=0
if docker exec emet_postgres pg_dump -U "${PG_USER:-emet}" "${PG_DB:-emet_bot}" 2>/dev/null | gzip > "$PG_DUMP_FILE"; then
    PG_SIZE=$(du -sh "$PG_DUMP_FILE" | cut -f1)
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] PG dump OK: $PG_DUMP_FILE ($PG_SIZE)"
    PG_OK=1
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] PG dump FAILED"
    rm -f "$PG_DUMP_FILE"
    PG_SIZE="ERROR"
fi

# 2. ChromaDB індекси (bind mount — на хості, не в контейнері)
INDEX_OK=0
if tar -czf "$INDICES_ARCHIVE" -C "$DATA_DIR" $(cd "$DATA_DIR" && ls -d db_index_*) 2>/dev/null; then
    INDEX_SIZE=$(du -sh "$INDICES_ARCHIVE" | cut -f1)
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Indices OK: $INDICES_ARCHIVE ($INDEX_SIZE)"
    INDEX_OK=1
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Indices tar FAILED"
    rm -f "$INDICES_ARCHIVE"
    INDEX_SIZE="ERROR"
fi

# 3. Ротація — лишаємо останні N комплектів
find "$BACKUP_DIR" -maxdepth 1 -name "indices_*.tar.gz" -type f | sort -r | tail -n +$((KEEP_DAYS + 1)) | xargs -r rm -f
find "$BACKUP_DIR" -maxdepth 1 -name "pgdump_*.sql.gz"  -type f | sort -r | tail -n +$((KEEP_DAYS + 1)) | xargs -r rm -f

REMAINING_IDX=$(find "$BACKUP_DIR" -maxdepth 1 -name "indices_*.tar.gz" | wc -l)
REMAINING_PG=$(find "$BACKUP_DIR" -maxdepth 1 -name "pgdump_*.sql.gz" | wc -l)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cleanup done. Indices: $REMAINING_IDX, PG dumps: $REMAINING_PG"

# 4. Сповіщення в Telegram — тільки помилки або підсумок раз на тиждень
if [ "$PG_OK" = 1 ] && [ "$INDEX_OK" = 1 ]; then
    # Тиха неділя — підсумковий звіт
    if [ "$(date +%u)" = "7" ]; then
        send_tg "✅ *EMET Backup* (тижневий звіт) — індекси \`$INDEX_SIZE\` × $REMAINING_IDX, БД \`$PG_SIZE\` × $REMAINING_PG"
    fi
    exit 0
else
    send_tg "⚠️ *EMET Backup FAILED* \`$TIMESTAMP\` — PG=$PG_OK indices=$INDEX_OK"
    exit 1
fi
