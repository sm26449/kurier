# Changelog

All notable changes to Kurier are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [1.12.0] — 2026-11-15

### Added
- `compose.backup.yaml`: daily dump of the four tables into `./backups`
  (`BACKUP_TIME`, `BACKUP_KEEP_DAYS`), verified, with the last result in
  `backups/last-backup.txt`. In the default `COMPOSE_FILE`.
- CI (GitHub Actions): tests, compose overlays, shellcheck, image build.

### Changed
- The container runs as an unprivileged user (UID 10001).
- `scripts/deploy.sh` starts every service of the selected overlays and
  syncs `scripts/backup.sh` into a runtime directory.

### Documentation
- MQTT `sms-gateway/send` bypasses the API key — restrict it with broker
  ACLs (SECURITY.md, API.md).
- Long received SMS arrive as separate parts (TELTONIKA.md).

## [1.11.0] — 2026-11-15

### Added
- Status page in **English and Romanian**: follows the browser's language,
  switchable at the top right (remembered). Dates, numbers and plurals
  follow the language.

### Fixed
- Romanian copy: counts from 20 up take "de" ("30 de zile", not "30 zile").

## [1.10.0] — 2026-11-15

First public release. The project is now **Kurier** (formerly
`sms-gateway`); API paths `/api/sms/*`, MQTT topics `sms-gateway/*`,
`SMS_*` variables and table names are unchanged.

### Added
- Compose overlays: `compose.yaml` + `compose.mysql.yaml` (bundled MySQL,
  schema on first start), `compose.traefik.yaml` (own Traefik, Let's
  Encrypt HTTP-01/DNS-01, IP allowlist, read-only socket proxy),
  `compose.proxy.yaml` (behind an existing Traefik), `compose.local.yaml`
  for site tweaks.
- `scripts/deploy.sh`: database backup, rollback tag, build, recreate,
  verify — in the checkout or into a separate runtime directory.
- Documentation (README EN/RO, docs/), SECURITY, CONTRIBUTING; Apache-2.0.

### Changed
- Generic defaults: `MQTT_BROKER=mosquitto`, `MYSQL_HOST=mysql`,
  `MYSQL_DATABASE=sms_gateway`, `TZ=UTC`. Set them in `.env` if you relied
  on the old ones.
- The API binds to loopback unless `KURIER_BIND` says otherwise.
- Migrations no longer select a database; apply them to yours.

## 1.0 – 1.9 — before the public release (2026-03 → 2026-11)

Developed as a private service; the detailed history is kept in a private
archive. In short, what those versions brought:

- **Send** over REST and MQTT through a Teltonika RutOS router (REST send,
  `gsmctl` over ubus for reads), API key, E.164 normalization.
- **Policy**: rate limits with a separate critical budget, duplicate
  suppression by alert key, idempotent retries (`idempotency_key`),
  lenient optional metadata (never a 422 for a bad label).
- **Durable outbox**: an SMS the router can't take is accepted (`202`),
  stored and retried with backoff until delivered or expired.
- **Cost**: GSM-7 transliteration (5 → 3 parts for a typical alert),
  billed segments recorded per SMS.
- **Inbound**: every received SMS archived once, delivered once to MQTT and
  an optional webhook within a delivery window, then removed from the SIM.
- **Observability**: health as data (always `200`), metrics, MQTT
  availability/status/metrics, status page with sending budget,
  statistics and history search.
- **Router robustness**: status from `gsmctl -E` (per-value queries fail
  often), UTC SMS timestamps, ubus session recovery.

### Database migrations

| File | Adds | Since |
|---|---|---|
| `001_sms_log.sql` | send history | 1.2 |
| `002_sms_outbox.sql` | outbox | 1.4 |
| `003_extended_contract.sql` | source/type/segments, per-message TTL, idempotency keys | 1.6 |
| `004_sms_inbox.sql` | inbound archive | 1.7 |
