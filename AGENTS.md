# AGENTS.md — Kurier

Instructions for coding agents (Claude Code, Cursor, Codex, …). Humans:
[CONTRIBUTING.md](CONTRIBUTING.md) covers the same rules in prose.

Kurier is one FastAPI process that **transports** SMS between other
services and a Teltonika RutOS router, in both directions. It never
interprets SMS text: bots, commands and alert policy belong to the
producers/consumers (e.g. Klopot).

> **Host-specific facts** (a running production instance, its runtime
> directory, credentials, integrations) are never in this repo. If a
> `CLAUDE.local.md` or `AGENTS.local.md` exists next to this file, read
> it before touching anything live.

## One project, several names

Search for all of them; they are the same thing.

| Name | Where it appears |
|---|---|
| **Kurier** | product, image `ghcr.io/sm26449/kurier`, compose project `kurier`, UI title |
| `sms-gateway` | historical repo/dir name, MQTT topics `sms-gateway/*`, logger names, some container names |
| `SMS_*` | env settings (a wire contract — never rename) |
| `sms_*` | MySQL tables (`sms_log`, `sms_outbox`, `sms_idempotency`, `sms_inbox`, `sms_schema_migrations`) |
| **Klopot** = `alertd` | the main consumer: notification service (package/process `alertd`, code in its own repo under `app/`) |

## Start here (don't read all of `src/`)

`cat src/**/*.py` is ~55 KB and overflows tool output. Read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (diagram + *Modules*), then
only what the task needs:

| To understand… | Open |
|---|---|
| how the process is assembled (which store, which service, who links to whom) | `src/wiring.py` → `open_stores()`, `build_services()`; `src/main.py` → `lifespan` only starts/stops |
| one outbound SMS | `routers/sms.py::send_sms` → `services/sms_service.py::SMSService.send` → `services/teltonika.py::TeltonikaClient.send_sms`; router down → `services/outbox.py` |
| one inbound SMS | `services/inbox.py::InboxService` (poll → archive → MQTT / webhook → delete from modem) |
| a table | `migrations/NNN_*.sql` + `src/storage/<history|outbox|idempotency|inbox>.py` (MySQL and in-memory store side by side); `src/storage/db.py` = connection + column probes |
| a setting | `src/config.py` + its comment in `.env.example` |
| the HTTP contract | [docs/API.md](docs/API.md) (status codes, fields, MQTT topics) |
| the router's quirks | [docs/TELTONIKA.md](docs/TELTONIKA.md) |
| the status page | `src/ui/index.html`, `app.js` (fetch → render → charts, one file), `i18n.js`, `app.css` |

Everything else:

| Area | Files |
|---|---|
| MQTT | `src/services/mqtt.py` (topics `sms-gateway/*`, Last Will, retained status/metrics) |
| Text | `src/gsm7.py` (transliteration, segments), `src/phone.py` (E.164) |
| Errors | `src/errors.py` (`describe_error` for router/HTTP failures) |
| Deploy | `compose.yaml` + overlays (`compose.mysql.yaml`, `compose.backup.yaml`, `compose.proxy.yaml`, `compose.traefik.yaml`), `deploy/traefik/`, `install.sh`, `scripts/deploy.sh`, `scripts/migrate.sh`, `scripts/backup.sh` |
| CI | `.github/workflows/ci.yml` (tests, `pip-audit`, compose overlays, shellcheck, installer, image), `release.yml` (multi-arch image on a tag; build-only on PRs touching the image), `dependabot.yml` |
| User docs | `docs/INSTALL.md`, `docs/INTEGRATIONS.md`, `docs/INTEGRATION_KLOPOT.md`, `docs/API.md`, `docs/DEPLOY.md`, `docs/TELTONIKA.md`, `docs/OPERATIONS.md`; brand rules `docs/brand/README.md` |

`SMSService.inbox` and `SMSService.mqtt` are **set after construction**
by `wiring.build_services()` (they report state in `/api/sms/status`);
they are `None` in unit tests that build an `SMSService` directly.

## Tests

```sh
docker build -t kurier-dev .
docker run --rm -u 0 -v "$PWD":/app -w /app kurier-dev \
  sh -c "pip install -q -r requirements-dev.txt && pytest -q -p no:cacheprovider"
```

No local Python needed. Narrow with `pytest -q tests/test_x.py -k name`
while iterating; run the full suite (~3 s) before you finish.

| File | Layer | Fakes / fixtures |
|---|---|---|
| `test_api.py` | HTTP only: auth, status codes, aliases | `api` fixture (TestClient + `dependency_overrides`, fake service) |
| `test_sms.py` | send pipeline: rate limit, dedup, priorities, history | `FakeTeltonikaClient`, `make_service()` |
| `test_contract.py` | extended contract: GSM-7, segments, severity, `dedup_key`, idempotency, TTL | in-memory stores |
| `test_outbox.py` | queue on router failure, backoff, expiry | `MemoryOutboxStore` |
| `test_inbox.py` | inbound, exactly once | `FakeModem`, `MemoryInboxStore`, webhook via `httpx.MockTransport` |
| `test_teltonika.py` | router client | `httpx.MockTransport` |
| `test_observability.py` | metrics, MQTT publishing, housekeeping, async history | publish capture, `FakeHistory` |
| `test_insights.py` | budget, connections, stats, search | — |
| `test_wiring.py` | store choice and service links | no MySQL |
| `test_ui.py` | CSP, assets, no HTML injection, EN/RO key parity | — |

`conftest.py` only zeroes the router retry delay. SQL is checked against
a throwaway `mysql:8.4` (docs/OPERATIONS.md).

## Other commands

| Task | Command |
|---|---|
| Compose config check | in a runtime dir with a `.env`: `docker compose config -q` (the source has no `.env` on purpose) |
| Status page screenshots | Playwright/Chromium against `/ui`, API key via `localStorage["smsgw.apiKey"]`. Take one screenshot per change you want to judge; each one costs context |
| Migrations | `scripts/migrate.sh [--status] [--runtime-dir=DIR]` — applies pending `migrations/*.sql` once, recorded in `sms_schema_migrations` |
| Deploy | `scripts/deploy.sh <label> [--runtime-dir=DIR] [--dry-run] [--no-migrate]` — backup, migrations, rollback tag, build `kurier:<label>`, recreate, verify. **Ask before running it on a live host** |
| Installer | `./install.sh [--advanced] [--yes KEY=value…] [--no-start] [--skip-router-check]` — writes `.env` from `.env.example`, checks the router, starts the stack. Refuses to run while a compose project `kurier` runs from another directory; test it only in a scratch copy with its own `COMPOSE_PROJECT_NAME` |
| Shell lint | `docker run --rm -v "$PWD":/s:ro koalaman/shellcheck:stable -S warning /s/install.sh /s/scripts/*.sh` |

## Rules that agents have broken before

**Behaviour**
- `/api/health` **always answers 200**; state is in the body, outbox
  fields are numbers, never null. (Monitors drop non-2xx and would lose
  `modem_reachable: false`.) Don't describe or change it as "503 when the
  router is down".
- The API **fails closed**: no `SMS_API_KEY` → `/api/sms/*` answers 503
  unless `SMS_ALLOW_NO_AUTH=true`. The status page key is the same
  `SMS_API_KEY`.
- Optional request metadata **never causes a 422**: sanitize or clamp
  (`SendSMSRequest._normalize`). Only an empty message or an idempotency
  conflict is refused.
- Rate limit + dedup are **reserved in one synchronous step** — no
  `await` between check and reserve. `critical` has its own hourly budget
  and no dedup.
- Router answers that start with `ERROR` are not values: one retry, then
  the last good value. Status comes from `gsmctl -E`.
- Modem SMS timestamps are **UTC** (`SMS_MODEM_TIMEZONE`).
- Inbound: archive once (fingerprint), deliver only within
  `SMS_INBOX_STALE_SECONDS` of the send time, delete from the modem after
  archiving.
- Blocking I/O (MySQL) never on the event loop: history via its writer
  thread, other stores via `asyncio.to_thread`.

**Contracts**
- `/api/sms/*`, MQTT `sms-gateway/*`, `SMS_*`, table names: add optional
  fields, never rename.
- Schema change = new numbered file in `migrations/`; the code probes
  columns/tables and must work **before** the migration is applied (fall
  back to memory with a warning naming the migration).
- New setting → `.env.example` with a comment above it (`install.sh`
  reads defaults and the `--advanced` list from that file).

**Other services**
- Before stating how a consumer (Klopot, Node-RED, …) uses Kurier, check
  its code or config. Don't infer it from Kurier's docs. Klopot's SMS
  channel is `app/channels/sms.py` in the Klopot repo; its inbound
  webhook kind is `sms_inbox`.

**UI** (`src/ui/`)
- Untrusted text only via `textContent`; no inline script/style; no
  external resources (strict CSP, must work offline).
- Every sentence lives in `i18n.js`, English and Romanian with the same
  keys. Romanian numerals 20+ take "de": "7 zile", "30 de zile",
  "101 SMS-uri", "493 de SMS-uri".

## Safety

- A compose project named `kurier` may be running production on the
  machine you are on. **Never** run `docker compose up`, `install.sh` or
  `scripts/deploy.sh` in this checkout. To try the stack, export the
  tracked files to a scratch directory and set `COMPOSE_PROJECT_NAME`,
  `KURIER_PORT` and an unroutable `TELTONIKA_HOST` (e.g. `192.0.2.1`).
- Never print `.env` values or API keys; test for presence instead.
- Sending a real SMS costs money and wakes someone up: ask first.
- Don't chain long `sleep`s; poll with a bounded loop.

## Changes, commits, releases

- Every behaviour change ships with a test and a CHANGELOG entry.
  CHANGELOG entries say **what changed for the user**, briefly, not the
  development story.
- Conventional Commits in English. **No AI/tool mention, no trailers**
  (no `Co-Authored-By`). Use the git identity already configured; don't
  override it with `-c user.email=…`.
- Commit only when asked; one commit per agreed phase. Push, tag,
  release and deploy are separate approvals.
- Release: bump `src/__init__.py` + CHANGELOG → push → CI green →
  `git tag -a vX.Y.Z -m "Kurier X.Y.Z"` → push the tag (publishes
  `ghcr.io/sm26449/kurier`) → `gh release create` with the CHANGELOG
  section.
