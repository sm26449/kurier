#!/usr/bin/env bash
# migrate.sh — apply the database migrations that are still missing.
#
#   scripts/migrate.sh                      # show what is applied / pending, ask, apply
#   scripts/migrate.sh --status             # show only; exit 3 if something is pending
#   scripts/migrate.sh --yes                # no prompt
#   scripts/migrate.sh --runtime-dir=DIR    # the .env lives in DIR (see deploy.sh)
#
# Applied files are recorded in the table sms_schema_migrations. On a
# database that predates this script (or was created by compose.mysql.yaml
# on its first start) migrations 001–004 are recognised by what they
# created and recorded without being run again.
#
# Runs as the MySQL admin *inside the MySQL container*, using that
# container's own MYSQL_ROOT_PASSWORD: KURIER_MYSQL_CONTAINER (env or .env —
# an external server's container), else the bundled `mysql` service.
# A MySQL that is not a container on this host: apply migrations/*.sql by
# hand, in order (docs/DEPLOY.md, "Database").
#
# MySQL DDL is not transactional: if a file fails half-way, fix the cause,
# finish that file by hand, then record it:
#   INSERT INTO sms_schema_migrations (filename) VALUES ('00X_….sql');
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
YES=0; STATUS=0; RT="${KURIER_RUNTIME_DIR:-}"
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;;
    --status) STATUS=1 ;;
    --runtime-dir=*) RT="${a#*=}" ;;
    *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
  esac
done

DIR="${RT:-$SRC}"
cd "$DIR"
[[ -f .env ]] || { echo "no .env in $DIR" >&2; exit 1; }
# Value of KEY in .env, without the quotes install.sh puts around special characters.
envval() {
  local v; v="$(grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true)"
  if [[ "$v" == \'*\' && ${#v} -ge 2 ]]; then v="${v:1:${#v}-2}"; v="${v//\\\'/\'}"; fi
  printf '%s' "$v"
}

MIG="$SRC/migrations"
[[ -d "$MIG" ]] || MIG="$DIR/migrations"
DB="$(envval MYSQL_DATABASE)"; DB="${DB:-sms_gateway}"
[[ "$DB" =~ ^[A-Za-z0-9_]+$ ]] || { echo "MYSQL_DATABASE: letters, digits, _ only" >&2; exit 1; }
MYSQL_C="${KURIER_MYSQL_CONTAINER:-$(envval KURIER_MYSQL_CONTAINER)}"
[[ -n "$MYSQL_C" ]] || MYSQL_C="$(docker compose ps -q mysql 2>/dev/null || true)"
[[ -n "$MYSQL_C" ]] || {
  echo "no MySQL container found (bundled 'mysql' service not running, KURIER_MYSQL_CONTAINER not set)." >&2
  echo "Apply $MIG/*.sql by hand, in order — docs/DEPLOY.md, \"Database\"." >&2
  exit 1; }

# SQL on stdin → tab-separated rows on stdout, as the container's MySQL admin.
sql() {
  docker exec -i "$MYSQL_C" sh -c \
    'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B "$0"' "$DB"
}
ask() { printf '%s\n' "$1" | sql; }

ask "SELECT 1" >/dev/null || {
  echo "cannot run SQL as root in container $MYSQL_C (database $DB)" >&2; exit 1; }

has_table()  { [[ "$(ask "SELECT COUNT(*) FROM information_schema.tables
                          WHERE table_schema = DATABASE() AND table_name = '$1'")" == 1 ]]; }
has_column() { [[ "$(ask "SELECT COUNT(*) FROM information_schema.columns
                          WHERE table_schema = DATABASE() AND table_name = '$1'
                            AND column_name = '$2'")" == 1 ]]; }

# Migrations older than the tracking table, recognised by their result.
legacy_applied() {
  case "$1" in
    001_sms_log.sql)           has_table sms_log ;;
    002_sms_outbox.sql)        has_table sms_outbox ;;
    003_extended_contract.sql) has_table sms_idempotency && has_column sms_log segments \
                                 && has_column sms_outbox expires_at ;;
    004_sms_inbox.sql)         has_table sms_inbox ;;
    *) return 1 ;;
  esac
}

RECORDED=""
has_table sms_schema_migrations && RECORDED="$(ask "SELECT filename FROM sms_schema_migrations")"
recorded() { grep -qxF "$1" <<<"$RECORDED"; }

PENDING=(); BASELINE=()
echo "database $DB in container $(docker inspect -f '{{.Name}}' "$MYSQL_C" | tr -d /)"
for f in "$MIG"/[0-9]*.sql; do
  [[ -e "$f" ]] || continue
  name="$(basename "$f")"
  [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "unexpected file name: $name" >&2; exit 1; }
  if recorded "$name"; then
    echo "  applied   $name"
  elif legacy_applied "$name"; then
    echo "  applied   $name (found in the schema, not yet recorded)"; BASELINE+=("$name")
  else
    echo "  PENDING   $name"; PENDING+=("$name")
  fi
done

if (( STATUS )); then
  (( ${#PENDING[@]} == 0 )) || exit 3
  exit 0
fi
if (( ${#PENDING[@]} == 0 && ${#BASELINE[@]} == 0 )); then echo "nothing to do"; exit 0; fi
if (( ${#PENDING[@]} > 0 && ! YES )); then
  read -r -p "Apply ${#PENDING[@]} pending migration(s)? Take a backup first. [y/N] " ok
  [[ "$ok" == y* ]] || { echo aborted; exit 1; }
fi

ask "CREATE TABLE IF NOT EXISTS sms_schema_migrations (
       filename   VARCHAR(128) NOT NULL,
       applied_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
       PRIMARY KEY (filename)
     ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
record() { ask "INSERT IGNORE INTO sms_schema_migrations (filename) VALUES ('$1')"; }

for name in "${BASELINE[@]}"; do record "$name"; done
for name in "${PENDING[@]}"; do
  echo "applying $name"
  sql < "$MIG/$name" || {
    echo "FAILED in $name — nothing recorded for it. See the header of this script." >&2; exit 1; }
  record "$name"
done
echo "done: ${#PENDING[@]} applied, ${#BASELINE[@]} recorded"
echo "Restart Kurier if it is already running, so it picks up the new schema:"
echo "  docker compose up -d --force-recreate kurier   (scripts/deploy.sh does this itself)"
