# Changelog

All notable changes to Kurier are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.15.0] — 2026-11-17

### Added
- **Watchdogs** ([ADR-0001](docs/adr/0001-watchdogs.md)): a service checks
  in with `POST /api/watchdogs/{name}/ping` (`timeout_sec`, optional
  `status: fail` + `detail`, `to`); no check-in within its timeout, or a
  reported failure, sends a critical SMS, and an SMS again when it is back.
  At most one down SMS per watchdog every 30 min; timers restart with
  Kurier. The answer carries Kurier's modem state, so the caller watches
  Kurier with the same request. `DELETE /api/watchdogs/{name}` forgets one,
  `GET /api/watchdogs` lists them, and the status page shows them.
  New settings `SMS_WATCHDOG_TO`, `SMS_WATCHDOG_MAX`; new table
  `sms_watchdogs` (migration 005, applied by `scripts/deploy.sh`; without
  it the watchdogs are kept in memory).
- `docs/adr/`: architecture decision records.

## [1.14.2] — 2026-11-16

### Added
- Dockerfile `test` stage with the dev dependencies baked in:
  `docker build --target test -t kurier-test .` once, then
  `docker run --rm -v "$PWD":/app kurier-test` runs the suite against the
  mounted checkout. The runtime image is unchanged and stays the default
  build; CI checks both.

### Changed
- Internal layout, no behaviour change: `src/storage.py` is now the
  package `src/storage/` with one module per table family (`history`,
  `outbox`, `idempotency`, `inbox`) plus `db.py`; `from src.storage
  import …` still works. Store selection and service wiring moved from
  `main.py`'s lifespan into `src/wiring.py::build_services()` (covered
  by `tests/test_wiring.py`); the startup log lines are unchanged.
  The send service now gets the inbox and MQTT's connection state
  (`MQTTStatus`) at construction instead of having them attached
  afterwards. Rate and dedup limits moved to `services/send_limits.py`
  and the history with its statistics and search to
  `services/send_history.py`; `sms_service.py` keeps the send path.
  The status page script is split into `core.js` (helpers), `now.js`
  (tab Now), `insights.js` (statistics, search) and `app.js` (wiring);
  the page looks and behaves the same.

## [1.14.1] — 2026-11-15

### Changed
- Dependencies: aiomqtt 2.5.1, pydantic-settings 2.16.2 (tests: pytest
  9.2, pytest-asyncio 1.4.1). MQTT was exercised against a real broker
  with the new client.
- CI: a pull request that touches the image or the release workflow now
  builds the multi-arch image without publishing it; GitHub Actions
  updated to their current major versions. Dependabot no longer proposes
  Python base-image version jumps — the image stays on Python 3.11.

### Added
- `docs/brand/social-preview.png`, the social card rasterised.

## [1.14.0] — 2026-11-15

### Added
- **`./install.sh`**: sets Kurier up by asking a handful of questions
  (router, phone number, database, access, MQTT), generates the secrets,
  checks the router login and JSON-RPC before anything starts (and picks
  HTTPS when the router offers it), writes `.env`, starts the containers
  and offers a test SMS. Run it again to reconfigure: current values are
  the defaults, the previous `.env` is saved, settings added by a newer
  version are filled in. `--advanced` walks through every setting;
  `--yes KEY=value…` runs without questions.

### Changed
- `scripts/deploy.sh` and `scripts/migrate.sh` read single-quoted values
  from `.env` (the installer quotes values with special characters).

## [1.13.0] — 2026-11-15

### Security
- **The API is closed without a key.** With `SMS_API_KEY` empty, every
  `/api/sms/*` request now answers `503` instead of being accepted without
  authentication. To keep the old behaviour on a closed network set
  `SMS_ALLOW_NO_AUTH=true`. `/api/health` is unaffected.
- The gateway container no longer receives `MYSQL_ROOT_PASSWORD` and
  `CF_DNS_API_TOKEN` from the shared `.env` (it never used them).
- FastAPI 0.163 / Starlette 1.10 / Uvicorn 0.54 (the previous Starlette
  0.41 had known advisories); pip upgraded and setuptools removed from the
  image. CI audits the pinned dependencies (`pip-audit`, also weekly);
  Dependabot watches pip, the base image and the Actions.

### Added
- **Published image**: `ghcr.io/sm26449/kurier:<version>` for amd64 and
  arm64, built by a release workflow on every version tag. Set
  `KURIER_IMAGE` and start with `docker compose up -d --no-build`.
- **`scripts/migrate.sh`**: applies pending migrations and records them in
  `sms_schema_migrations`; an existing schema (001–004) is recognised, not
  re-run. `scripts/deploy.sh` shows pending migrations in its plan and
  applies them after the backup (`--no-migrate` to skip).
- **HTTPS to the router**: `TELTONIKA_SCHEME=https`, with
  `TELTONIKA_VERIFY_TLS` for routers that have a trusted certificate.
- Brand assets in `docs/brand/` (wordmark, relay mark, favicon, social
  card), in the same family as Klopot. The status page gets a favicon and
  the mark in its header.

### Documentation
- [docs/INSTALL.md](docs/INSTALL.md): step-by-step install with the router
  settings Kurier needs (JSON-RPC is off by default on RutOS) and the
  expected output of every step.
- [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md): Home Assistant, Node-RED,
  Grafana, Uptime Kuma, Alertmanager, shell, Python, MQTT.
- API.md: endpoint table, metrics and a full MQTT topic reference;
  `.env.example` lists every variable the compose overlays read.
- Issue and pull-request templates.

### Changed
- **MQTT is off when `MQTT_BROKER` is empty**, and empty is now the default
  (it was `mosquitto`). If you relied on the default, set
  `MQTT_BROKER=mosquitto` in `.env`. The status page shows "MQTT not
  configured"; `GET /api/sms/budget` adds `connections.mqtt.enabled`.
- A broker that stays down is logged once, not every 5 seconds (retries
  continue).
- Bundled MySQL and the backup sidecar use `mysql:8.4` (LTS; 8.0 is end of
  life). An existing 8.0 data volume is upgraded in place on the first
  start — take a backup first (`BACKUP_ON_START=true` or
  `scripts/deploy.sh`).

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
