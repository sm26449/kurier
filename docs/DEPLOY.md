# Deploy

## Compose overlays

Choose files with `COMPOSE_FILE` in `.env` (colon-separated):

| Files | What you get |
|---|---|
| `compose.yaml:compose.mysql.yaml` | Kurier + MySQL 8.4 LTS; schema created on first start |
| `…:compose.backup.yaml` | + daily dump of the four tables into `./backups` (**default** with the line above) |
| `…:compose.traefik.yaml` | + Traefik v3 (Let's Encrypt), IP allowlist, read-only Docker socket proxy |
| `compose.yaml:compose.proxy.yaml` | joins a Traefik you already run (Docker or file provider) |
| `compose.yaml` | Kurier only — external MySQL via `MYSQL_*`, or none (in-memory) |
| `+ compose.local.yaml` | your site-local tweaks (gitignored; see `compose.local.example.yaml`) |

`docker compose up -d --build` then uses them. The API listens on
`KURIER_BIND:KURIER_PORT` (default `127.0.0.1:5080`).

### Own Traefik with TLS (`compose.traefik.yaml`)

```
KURIER_HOSTNAME=kurier.example.com
ACME_EMAIL=you@example.com
TLS_MODE=dns01            # or http01
CF_DNS_API_TOKEN=…        # dns01 with Cloudflare (Zone:DNS:Edit)
ALLOWED_CIDRS=127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
```

`http01` needs the hostname reachable from the internet on port 80.
`dns01` doesn't: it also works for a **LAN-only hostname** — a DNS record
pointing at a private IP — which is the recommended setup: HTTPS for the
status page, nothing reachable from outside.

### Behind an existing Traefik (`compose.proxy.yaml`)

```
PROXY_NETWORK=traefik      # that Traefik's Docker network
KURIER_HOSTNAME=kurier.example.com
TRAEFIK_CERTRESOLVER=letsencrypt
```

Kurier joins `PROXY_NETWORK` as `kurier`. A Docker-provider Traefik picks
up the labels; a file-provider Traefik needs
[`deploy/traefik/kurier.example.yml`](../deploy/traefik/kurier.example.yml)
copied into its dynamic directory. Both include an IP allowlist and HSTS.

## Configuration

Every variable, with defaults and comments: [`.env.example`](../.env.example).
The essentials:

| Variable | Purpose |
|---|---|
| `TELTONIKA_HOST`, `TELTONIKA_USER`, `TELTONIKA_PASS` | router access ([TELTONIKA.md](TELTONIKA.md)) |
| `SMS_API_KEY` | **required**: header `X-API-Key` on `/api/sms/*`. Unset → the API answers `503` (`SMS_ALLOW_NO_AUTH=true` opts out) |
| `SMS_DEFAULT_RECIPIENTS` | who gets SMS sent without a number; also the `authorized` senders for inbound SMS |
| `SMS_DEFAULT_COUNTRY_CODE` | prefix for national numbers (`07…` → `+407…`) |
| `SMS_RATE_LIMIT_*`, `SMS_DEDUP_SECONDS` | sending policy |
| `MQTT_*` | optional MQTT in/out; empty `MQTT_BROKER` = off |
| `MYSQL_*` | optional persistence; empty `MYSQL_PASSWORD` = in-memory |
| `SMS_INBOX_WEBHOOK_URL`, `…_TOKEN` | optional push of received SMS |
| `TZ` | local days in the statistics |

## Database

`compose.mysql.yaml` runs `migrations/*.sql` on the first start. For an
existing MySQL, create a database and a user, then apply the migrations
in order as an admin:

```sql
CREATE DATABASE sms_gateway CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'sms_gateway'@'%' IDENTIFIED BY '…';
GRANT SELECT, INSERT, UPDATE, DELETE ON sms_gateway.* TO 'sms_gateway'@'%';
```
```bash
for f in migrations/0*.sql; do mysql -u root -p sms_gateway < "$f"; done
```

The gateway starts without the newest migration (it probes the schema
and logs which file to apply), but some features stay in memory until
it's applied.

## Separate runtime directory (recommended for production)

Keep the source checkout clean and run Kurier from its own directory —
`.env`, `compose.local.yaml` and backups live there, the code doesn't:

```bash
mkdir -p /srv/kurier/backups
cp .env.example /srv/kurier/.env && chmod 600 /srv/kurier/.env   # edit it
scripts/deploy.sh 1.10.0 --runtime-dir=/srv/kurier --dry-run
scripts/deploy.sh 1.10.0 --runtime-dir=/srv/kurier
```

[`scripts/deploy.sh`](../scripts/deploy.sh) does: preflight (health,
version) → MySQL backup of the four tables (`KURIER_MYSQL_CONTAINER` for an
external server, else the bundled `mysql`) → rollback tag
`kurier:rollback-pre-<label>` → `docker build -t kurier:<label>` → sync
`compose*.yaml`, `deploy/`, `migrations/` into the runtime directory and set
`KURIER_IMAGE` (old files saved under `backups/`) → recreate the `kurier`
service → verify version, health and the startup log. Without
`--runtime-dir` it deploys in the checkout itself. It does not apply
migrations.

Rollback: `KURIER_IMAGE=kurier:rollback-pre-<label>` in `.env`, then
`docker compose up -d --no-build kurier`.

## Upgrade

```bash
git pull
# apply any migrations/*.sql newer than your schema (CHANGELOG says which)
# with a runtime directory: scripts/deploy.sh <version> --runtime-dir=… (does the rest)
docker compose exec -T mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' \
  < migrations/00X_….sql          # bundled MySQL
docker compose up -d --build
curl -s 127.0.0.1:5080/ ; curl -s 127.0.0.1:5080/api/health
```

Restarting is safe: the outbox, idempotency keys and the inbound archive
are in MySQL; queued SMS resume.

## Backup

Everything worth keeping is in four tables (`sms_log`, `sms_outbox`,
`sms_idempotency`, `sms_inbox`). `compose.backup.yaml` adds a `backup`
service that dumps them daily at `BACKUP_TIME` (local `TZ`) into
`./backups/kurier-<UTC>.sql.gz`, verifies each archive, keeps
`BACKUP_KEEP_DAYS` (14) and writes the last result to
`backups/last-backup.txt`. It uses the gateway's MySQL user and works with
the bundled or an external MySQL (on another network: attach the service to
it in `compose.local.yaml`). `BACKUP_ON_START=true` dumps once at start —
handy to check the setup. `scripts/deploy.sh` also dumps before every deploy.

Restore into an empty database:

```bash
gunzip -c backups/kurier-….sql.gz | mysql -u root -p sms_gateway
```

Copy `./backups` off the host with the rest of your backups. The `.env`
holds the secrets — back it up separately.

## Other services calling Kurier

In the same Docker network use the service name (`http://kurier:5080`),
or give the container a fixed name in `compose.local.yaml` if existing
clients use one. Clients on other hosts need `KURIER_BIND=0.0.0.0` (and
should still go through the API key).
