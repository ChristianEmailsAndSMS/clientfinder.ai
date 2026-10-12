#!/usr/bin/env bash
# Off-server copy of the Postgres backups (and the .env file) to Backblaze B2, encrypted on THIS server before it leaves
# (rclone "crypt": names and contents are encrypted; B2 only ever sees scrambled files).
#
#   deploy/offsite_backup.sh            copy new dumps + .env, verify the newest, apply retention, write offsite.json
#
# Runs after the daily local backup (clientfinder-offsite.timer). One-time setup: docs/INFRA.md "Off-server backup".
# Needs: rclone, and an rclone crypt remote (default name "cfcrypt") that points at your B2 bucket.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/clientfinder}"
REMOTE="${OFFSITE_REMOTE:-cfcrypt}"
ENV_FILE="${ENV_FILE:-/root/clientfinder.ai/backend/.env}"
KEEP_DAYS="${OFFSITE_KEEP_DAYS:-60}"
KEEP_ENV_DAYS="${OFFSITE_KEEP_ENV_DAYS:-30}"
RCLONE="${RCLONE_BIN:-rclone}"
STATUS="$BACKUP_DIR/offsite.json"

umask 077
log() { echo "$(date -u +%FT%TZ) offsite: $*" >&2; }
write_status() {  # $1 = true|false, $2 = file name or error text
  mkdir -p "$BACKUP_DIR"
  local msg; msg=$(printf '%s' "$2" | tr -d '"\\' | tr '\n' ' ' | cut -c1-200)
  printf '{"ts":"%s","ok":%s,"detail":"%s","remote":"%s"}\n' "$(date -u +%FT%TZ)" "$1" "$msg" "$REMOTE" > "$STATUS"
}
fail() { log "FAILED: $*"; write_status false "$*"; exit 1; }

command -v "$RCLONE" >/dev/null 2>&1 || fail "rclone is not installed (apt install rclone)"
"$RCLONE" listremotes 2>/dev/null | grep -qx "$REMOTE:" || fail "rclone remote '$REMOTE' is not configured (see docs/INFRA.md, Off-server backup)"

newest=$(ls -1t "$BACKUP_DIR"/clientfinder-*.dump 2>/dev/null | head -n1 || true)
[[ -n "$newest" ]] || fail "no local backup found in $BACKUP_DIR; run deploy/backup.sh first"
name=$(basename "$newest")

log "copying dumps to $REMOTE:db"
"$RCLONE" copy "$BACKUP_DIR" "$REMOTE:db" --include 'clientfinder-*.dump' --transfers 2 --retries 3 --low-level-retries 5 \
  || fail "copy of the dumps failed"

# Prove the newest dump arrived whole: compare sizes through the crypt layer.
"$RCLONE" check "$BACKUP_DIR" "$REMOTE:db" --include "$name" --one-way --size-only >/dev/null 2>&1 \
  || fail "verification failed: $name is missing or a different size off-server"

if [[ -r "$ENV_FILE" ]]; then
  log "copying .env (secrets) to $REMOTE:env"
  "$RCLONE" copyto "$ENV_FILE" "$REMOTE:env/env-$(date -u +%F)" --retries 3 || fail "copy of .env failed"
  "$RCLONE" delete "$REMOTE:env" --min-age "${KEEP_ENV_DAYS}d" >/dev/null 2>&1 || log "warning: could not prune old .env copies"
else
  log "warning: $ENV_FILE not readable; secrets were NOT copied"
fi

"$RCLONE" delete "$REMOTE:db" --min-age "${KEEP_DAYS}d" --include 'clientfinder-*.dump' >/dev/null 2>&1 || log "warning: could not prune old off-server dumps"

write_status true "$name"
log "ok: $name copied and verified"
