#!/usr/bin/env bash
# Postgres backup / restore for the deployed stack.
#
# Purpose: a known-good restore point before the demo. The journeys and alerts
# tables accumulate for the whole deployment window, so if something corrupts or
# a migration misbehaves there is otherwise no way back.
#
# Runs pg_dump INSIDE the postgres container, so it needs no client tools on the
# VM and no published database port (the production compose publishes none).
#
# Usage:
#   ./scripts/backup_db.sh                    # write backups/oil-<utc>.sql.gz
#   ./scripts/backup_db.sh restore FILE.sql.gz
#
# Cron it daily (VM crontab), keeping the output out of the repo:
#   0 3 * * * cd /opt/oil && ./scripts/backup_db.sh >> /var/log/oil-backup.log 2>&1

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
SERVICE="${POSTGRES_SERVICE:-postgres}"
BACKUP_DIR="${BACKUP_DIR:-backups}"
KEEP="${KEEP:-14}"          # how many dumps to retain

# Match the compose defaults; override via the environment if they differ.
DB_USER="${POSTGRES_USER:-oil}"
DB_NAME="${POSTGRES_DB:-oil}"

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

do_backup() {
  mkdir -p "$BACKUP_DIR"
  # UTC, and colon-free so the name is valid on every filesystem.
  local stamp out
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  out="$BACKUP_DIR/oil-$stamp.sql.gz"

  echo "[backup] dumping $DB_NAME -> $out"
  # No TTY (-T): this must work under cron. Failures propagate via pipefail, and
  # writing to a .partial file first means an interrupted dump never looks valid.
  compose exec -T "$SERVICE" pg_dump -U "$DB_USER" -d "$DB_NAME" \
    | gzip > "$out.partial"
  mv "$out.partial" "$out"
  echo "[backup] wrote $(du -h "$out" | cut -f1) $out"

  # Retention: keep the newest $KEEP dumps.
  local stale
  stale="$(ls -1t "$BACKUP_DIR"/oil-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) || true)"
  if [ -n "$stale" ]; then
    echo "[backup] pruning $(echo "$stale" | wc -l) old dump(s)"
    echo "$stale" | xargs -r rm --
  fi
}

do_restore() {
  local file="${1:-}"
  [ -n "$file" ] || { echo "usage: $0 restore FILE.sql.gz" >&2; exit 2; }
  [ -f "$file" ] || { echo "[restore] no such file: $file" >&2; exit 2; }

  # Destructive: the dump recreates the schema over live data.
  echo "[restore] This OVERWRITES the '$DB_NAME' database from $file."
  read -r -p "[restore] Type the database name ('$DB_NAME') to confirm: " reply
  [ "$reply" = "$DB_NAME" ] || { echo "[restore] aborted"; exit 1; }

  # Stop the writers first so consumers can't interleave with the restore.
  echo "[restore] stopping backend + consumers"
  compose stop backend ai-service injector mock-services >/dev/null

  echo "[restore] restoring"
  gunzip -c "$file" | compose exec -T "$SERVICE" psql -U "$DB_USER" -d "$DB_NAME"

  echo "[restore] restarting services"
  compose start mock-services ai-service backend injector >/dev/null
  echo "[restore] done"
}

case "${1:-backup}" in
  backup)  do_backup ;;
  restore) shift; do_restore "$@" ;;
  *) echo "usage: $0 [backup|restore FILE.sql.gz]" >&2; exit 2 ;;
esac
