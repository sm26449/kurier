#!/usr/bin/env bash
# deploy.sh — upgrade a running Kurier to the checked-out code, with a
# verified database backup and an image rollback tag taken first.
#
#   scripts/deploy.sh <label>                       # asks before changing anything
#   scripts/deploy.sh <label> --yes                 # no prompt
#   scripts/deploy.sh <label> --dry-run             # preflight + plan only
#   scripts/deploy.sh <label> --runtime-dir=DIR     # deploy into a separate runtime directory
#
# <label> names the deploy (e.g. "1.10.0"): image tag kurier:<label>, rollback
# tag kurier:rollback-pre-<label>, backup file names.
#
# Layouts:
#   in-repo (default) — .env and the compose project live in this checkout.
#   runtime dir       — --runtime-dir=DIR or KURIER_RUNTIME_DIR=DIR: .env,
#                       compose.local.yaml and backups/ live in DIR; compose
#                       files and deploy/ are synced there from this checkout
#                       (old copies saved to DIR/backups/), and KURIER_IMAGE in
#                       DIR/.env is pointed at the new image (old .env saved).
#
# Steps: 1 preflight (health, version, outbox) · 2 backup (mysqldump of the
# four tables, gzip-verified; skipped without MySQL) · 3 rollback tag ·
# 4 build kurier:<label> · 5 docker compose up (recreates what changed) ·
# 6 verify (version, health, startup log).
#
# MySQL for the backup: KURIER_MYSQL_CONTAINER (env or .env — an external
# server's container), else the bundled `mysql` service. New migrations are NOT applied here —
# apply them first (docs/DEPLOY.md, "Upgrade"); Kurier logs which are missing.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=""; YES=0; DRY=0; RT="${KURIER_RUNTIME_DIR:-}"
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;;
    --dry-run) DRY=1 ;;
    --runtime-dir=*) RT="${a#*=}" ;;
    -*) echo "unknown option: $a" >&2; exit 2 ;;
    *) LABEL="$a" ;;
  esac
done
[[ -n "$LABEL" ]] || { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[[ "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "label: letters, digits, . _ - only" >&2; exit 2; }

DIR="${RT:-$SRC}"
cd "$DIR"
[[ -f .env ]] || { echo "no .env in $DIR" >&2; exit 1; }
envval() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
PORT="$(envval KURIER_PORT)"; PORT="${PORT:-5080}"
URL="http://127.0.0.1:$PORT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NEW_IMAGE="kurier:$LABEL"
WANT_VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/src/__init__.py")"
say() { printf '\n== %s\n' "$*"; }

say "1. Preflight ($DIR)"
CID="$(docker compose ps -q kurier 2>/dev/null || true)"
if [[ -n "$CID" ]]; then
  OLD_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$CID")"
  echo "running: $(docker inspect -f '{{.Name}}' "$CID" | tr -d /) image=$OLD_IMAGE"
  echo "version: $(curl -fsS -m 5 "$URL/" || echo '?')"
  echo "health:  $(curl -fsS -m 10 "$URL/api/health" || echo '?')"
else
  OLD_IMAGE=""; echo "kurier is not running in this compose project (first deploy here)"
fi
echo "target:  $NEW_IMAGE (code version $WANT_VERSION) from $SRC"

say "Plan"
echo "- backup MySQL tables to $DIR/backups/kurier-predeploy-$LABEL-$STAMP.sql.gz"
[[ -n "$OLD_IMAGE" ]] && echo "- tag $OLD_IMAGE as kurier:rollback-pre-$LABEL"
echo "- docker build -t $NEW_IMAGE $SRC"
[[ -n "$RT" ]] && echo "- sync compose*.yaml + deploy/ + migrations/ + scripts/backup.sh into $DIR, set KURIER_IMAGE=$NEW_IMAGE in $DIR/.env (backups first)"
echo "- docker compose up -d --no-build (kurier and any overlay services), then verify"
(( DRY )) && { echo; echo "dry run: nothing changed"; exit 0; }
if (( ! YES )); then read -r -p "Proceed? [y/N] " ok; [[ "$ok" == y* ]] || { echo aborted; exit 1; }; fi

mkdir -p backups
say "2. Backup"
MYSQL_C="${KURIER_MYSQL_CONTAINER:-$(envval KURIER_MYSQL_CONTAINER)}"
[[ -n "$MYSQL_C" ]] || MYSQL_C="$(docker compose ps -q mysql 2>/dev/null || true)"
DB="$(envval MYSQL_DATABASE)"; DB="${DB:-sms_gateway}"
DBUSER="$(envval MYSQL_USER)"; DBUSER="${DBUSER:-sms_gateway}"
DBPASS="$(envval MYSQL_PASSWORD)"
if [[ -n "$MYSQL_C" && -n "$DBPASS" ]]; then
  OUT="backups/kurier-predeploy-$LABEL-$STAMP.sql.gz"
  docker exec -e MYSQL_PWD="$DBPASS" "$MYSQL_C" mysqldump -u"$DBUSER" --single-transaction \
    --no-tablespaces --skip-lock-tables "$DB" sms_log sms_outbox sms_idempotency sms_inbox \
    2>/dev/null | gzip > "$OUT"
  gzip -t "$OUT" && [[ $(zcat "$OUT" | grep -c 'CREATE TABLE') -ge 1 ]] \
    || { echo "backup failed or empty: $OUT" >&2; exit 1; }
  echo "ok: $OUT ($(du -h "$OUT" | cut -f1))"
else
  echo "skipped (no MySQL container or no MYSQL_PASSWORD)"
fi

if [[ -n "$OLD_IMAGE" ]]; then
  say "3. Rollback tag"
  docker tag "$OLD_IMAGE" "kurier:rollback-pre-$LABEL" && echo "kurier:rollback-pre-$LABEL"
fi

say "4. Build"
docker build -q -t "$NEW_IMAGE" "$SRC" >/dev/null && echo "$NEW_IMAGE"

if [[ -n "$RT" ]]; then
  say "Sync runtime files"
  SNAP="backups/runtime-pre-$LABEL-$STAMP"; mkdir -p "$SNAP"
  cp -a .env "$SNAP/.env"; cp -a compose*.yaml deploy migrations scripts "$SNAP/" 2>/dev/null || true
  cp -a "$SRC"/compose.yaml "$SRC"/compose.mysql.yaml "$SRC"/compose.proxy.yaml \
        "$SRC"/compose.traefik.yaml "$SRC"/compose.backup.yaml "$SRC"/compose.local.example.yaml .
  mkdir -p scripts && cp -a "$SRC/scripts/backup.sh" scripts/backup.sh
  rm -rf deploy migrations && cp -a "$SRC/deploy" deploy && cp -a "$SRC/migrations" migrations
  if grep -q '^KURIER_IMAGE=' .env; then
    sed -i "s|^KURIER_IMAGE=.*|KURIER_IMAGE=$NEW_IMAGE|" .env
  else
    printf '\nKURIER_IMAGE=%s\n' "$NEW_IMAGE" >> .env
  fi
  echo "old files in $SNAP; KURIER_IMAGE=$NEW_IMAGE"
fi

say "5. Recreate"
if [[ -z "$RT" ]]; then export KURIER_IMAGE="$NEW_IMAGE"; fi
# All services of the selected overlays (e.g. backup); compose only recreates
# what changed — a bundled MySQL is left alone unless its config changed.
docker compose up -d --no-build

say "6. Verify"
for _ in $(seq 1 60); do curl -fsS -m 2 "$URL/" >/dev/null 2>&1 && break; sleep 1; done
GOT="$(curl -fsS -m 5 "$URL/" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
echo "version: ${GOT:-none} (want $WANT_VERSION)"
echo "health:  $(curl -fsS -m 10 "$URL/api/health" || echo FAIL)"
CID="$(docker compose ps -q kurier)"
ERRS="$(docker logs --since 2m "$CID" 2>&1 | grep -E 'ERROR|Traceback' || true)"
[[ -z "$ERRS" ]] && echo "startup log: clean" || { echo "startup log errors:"; echo "$ERRS"; }
[[ "$GOT" == "$WANT_VERSION" && -z "$ERRS" ]] || {
  echo; echo "VERIFY FAILED. Roll back: set KURIER_IMAGE=kurier:rollback-pre-$LABEL in .env, docker compose up -d --no-build kurier" >&2
  exit 1; }
echo; echo "deployed $NEW_IMAGE. Add a release line to $DIR/README.md."
