#!/usr/bin/env bash
# Kurier database backup loop — runs in the `backup` service (compose.backup.yaml).
#
# Every day at BACKUP_TIME (container local time, TZ) it dumps the four
# Kurier tables to /backups/kurier-<UTC>.sql.gz, verifies the archive and
# deletes dumps older than BACKUP_KEEP_DAYS. BACKUP_ON_START=true also dumps
# once at start (useful to check the setup). The last result is written to
# /backups/last-backup.txt.
#
# Needs only SELECT on the tables (the gateway's MySQL user is enough).
set -uo pipefail

: "${MYSQL_HOST:?}" "${MYSQL_USER:?}" "${MYSQL_PASSWORD:?}" "${MYSQL_DATABASE:?}"
BACKUP_TIME="${BACKUP_TIME:-03:30}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
TABLES="sms_log sms_outbox sms_idempotency sms_inbox"
export MYSQL_PWD="$MYSQL_PASSWORD"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

wait_for_db() {  # up to ~2 min: the database may still be starting
  for _ in $(seq 1 24); do
    mysqladmin -h "$MYSQL_HOST" -P "${MYSQL_PORT:-3306}" -u "$MYSQL_USER" ping >/dev/null 2>&1 && return 0
    sleep 5
  done
  return 1
}

dump() {
  wait_for_db || log "database not answering at $MYSQL_HOST — trying anyway"
  local out tmp
  out="/backups/kurier-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
  tmp="$out.partial"
  if mysqldump -h "$MYSQL_HOST" -P "${MYSQL_PORT:-3306}" -u "$MYSQL_USER" \
       --single-transaction --no-tablespaces --skip-lock-tables \
       "$MYSQL_DATABASE" $TABLES 2>/tmp/dump.err | gzip > "$tmp" \
     && gzip -t "$tmp" && [[ $(zcat "$tmp" | grep -c 'CREATE TABLE') -ge 1 ]]; then
    mv "$tmp" "$out"
    # Daily dumps only (kurier-<UTC>…); pre-deploy dumps (kurier-predeploy-…) are kept.
    find /backups -maxdepth 1 -name 'kurier-[0-9]*.sql.gz' -mtime +"$BACKUP_KEEP_DAYS" -delete
    log "ok $out ($(du -h "$out" | cut -f1))"
    echo "ok $(date -u +%FT%TZ) $out" > /backups/last-backup.txt
  else
    rm -f "$tmp"
    log "FAILED: $(tr '\n' ' ' < /tmp/dump.err | cut -c1-300)"
    echo "failed $(date -u +%FT%TZ) $(tr '\n' ' ' < /tmp/dump.err | cut -c1-200)" > /backups/last-backup.txt
  fi
}

seconds_until() {  # next occurrence of HH:MM, local time
  local now target
  now=$(date +%s)
  target=$(date -d "today $1" +%s)
  (( target <= now )) && target=$(date -d "tomorrow $1" +%s)
  echo $(( target - now ))
}

log "started: daily at $BACKUP_TIME ($(date +%Z)), keep $BACKUP_KEEP_DAYS days, db $MYSQL_DATABASE@$MYSQL_HOST"
[[ "${BACKUP_ON_START:-false}" == "true" ]] && dump
while true; do
  sleep "$(seconds_until "$BACKUP_TIME")"
  dump
  sleep 61  # don't run twice within the same minute
done
