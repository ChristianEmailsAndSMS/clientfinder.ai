#!/usr/bin/env bash
# Daily Postgres backup for Clientfinder.ai, stored on this VPS.
#   deploy/backup.sh            take a backup, verify it by restoring into a scratch DB, apply retention
#   deploy/backup.sh --restore-test FILE   verify an existing dump only
#
# Retention: every backup for 14 days, plus one per week (Sundays) for 8 weeks.
# NOTE: backups on the same disk do not survive losing the VPS. Add an off-box copy (see docs/INFRA.md).
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/clientfinder}"
CONTAINER="${CONTAINER:-clientfinder_pg}"
DB_USER="${DB_USER:-clientfinder}"
DB_NAME="${DB_NAME:-clientfinder}"
DOCKER="${DOCKER_BIN:-docker}"
KEEP_DAILY_DAYS="${KEEP_DAILY_DAYS:-14}"
KEEP_WEEKLY_DAYS="${KEEP_WEEKLY_DAYS:-56}"
VERIFY_DB="cf_verify_$$"

log() { echo "$(date -u +%FT%TZ) backup: $*" >&2; }   # stderr: never captured by $(...)
fail() { log "FAILED: $*"; exit 1; }

verify_restore() {  # $1 = dump file. Prints the restored jobs row count on stdout; non-zero exit on any failure.
  # Runs in its own subshell so the EXIT trap always drops the scratch DB, even when fail() exits.
  (
    local file="$1" rows err
    err=$(mktemp "$BACKUP_DIR/.restore.XXXXXX")
    cleanup() {
      rm -f "$err"
      "$DOCKER" exec "$CONTAINER" psql -U "$DB_USER" -d postgres -qc "DROP DATABASE IF EXISTS $VERIFY_DB" >/dev/null 2>&1 || true
    }
    trap cleanup EXIT
    "$DOCKER" exec "$CONTAINER" psql -U "$DB_USER" -d postgres -qc "CREATE DATABASE $VERIFY_DB" >/dev/null || fail "cannot create scratch DB"
    "$DOCKER" exec -i "$CONTAINER" pg_restore -U "$DB_USER" -d "$VERIFY_DB" --no-owner < "$file" >/dev/null 2>"$err" \
      || fail "pg_restore failed: $(head -c 300 "$err")"
    rows=$("$DOCKER" exec "$CONTAINER" psql -U "$DB_USER" -d "$VERIFY_DB" -Atc "SELECT count(*) FROM jobs") || fail "restored DB has no jobs table"
    [[ "$rows" =~ ^[0-9]+$ ]] || fail "unexpected row count '$rows'"
    echo "$rows"
  )
}

apply_retention() {
  local now f base d age dow
  now=$(date -u +%s)
  for f in "$BACKUP_DIR"/clientfinder-*.dump; do
    [[ -e "$f" ]] || continue
    base=$(basename "$f"); d=${base#clientfinder-}; d=${d:0:10}
    date -u -d "$d" +%s >/dev/null 2>&1 || continue          # skip anything we did not name
    age=$(( (now - $(date -u -d "$d" +%s)) / 86400 ))
    dow=$(date -u -d "$d" +%u)                                # 7 = Sunday
    if (( age > KEEP_WEEKLY_DAYS )) || { (( age > KEEP_DAILY_DAYS )) && (( dow != 7 )); }; then
      rm -f -- "$f" && log "retention: removed $base"
    fi
  done
}

if [[ "${1:-}" == "--restore-test" ]]; then
  [[ -f "${2:-}" ]] || fail "usage: backup.sh --restore-test FILE"
  mkdir -p "$BACKUP_DIR"
  rows=$(verify_restore "$2")
  log "restore test of $2 passed: $rows jobs rows"
  exit 0
fi

umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

stamp=$(date -u +%F_%H%M%S)
final="$BACKUP_DIR/clientfinder-$stamp.dump"
partial="$final.partial"
trap 'rm -f "$partial"' EXIT

log "dumping $DB_NAME from $CONTAINER"
"$DOCKER" exec "$CONTAINER" pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$partial" || fail "pg_dump failed"
[[ -s "$partial" ]] || fail "dump is empty"
"$DOCKER" exec -i "$CONTAINER" pg_restore -l < "$partial" >/dev/null || fail "dump is not a readable archive"

rows=$(verify_restore "$partial")
mv "$partial" "$final"
size=$(stat -c %s "$final")
log "ok: $(basename "$final") ${size} bytes, restore-verified ($rows jobs rows)"

printf '{"ts":"%s","file":"%s","bytes":%s,"verified":true,"jobs_rows":%s}\n' \
  "$(date -u +%FT%TZ)" "$(basename "$final")" "$size" "$rows" > "$BACKUP_DIR/last_run.json"

apply_retention
