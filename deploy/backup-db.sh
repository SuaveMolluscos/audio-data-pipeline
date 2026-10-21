#!/bin/sh
set -eu

DB_PATH="/var/lib/music-crawler/tracks.db"
BACKUP_DIR="/var/lib/music-crawler/backups"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_PATH="$BACKUP_DIR/tracks-$TIMESTAMP.sqlite"

mkdir -p "$BACKUP_DIR"

/opt/music-crawler/.venv/bin/python - "$DB_PATH" "$BACKUP_PATH" <<'PY'
import sqlite3
import sys

source_path, backup_path = sys.argv[1:]
source = sqlite3.connect(source_path)
destination = sqlite3.connect(backup_path)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
PY

find "$BACKUP_DIR" -type f -name 'tracks-*.sqlite' -mtime +30 -delete
printf 'Backup created: %s\n' "$BACKUP_PATH"
