# Changelog

All notable changes to Kurier (formerly sms-gateway) are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/).

## [1.10.0] — 2026-11-15

### Kurier: new name, self-contained deployment, public documentation

The project is now **Kurier** (repo, image, compose service, status page).
Wire contracts are unchanged: API paths `/api/sms/*`, MQTT topics
`sms-gateway/*`, `SMS_*` variables and table names stay as they were.

- **Compose overlays** like the sibling projects: `compose.yaml` (base) +
  `compose.mysql.yaml` (bundled MySQL 8, migrations applied on first
  start), `compose.traefik.yaml` (own Traefik v3: Let's Encrypt HTTP-01 or
  DNS-01, IP allowlist, read-only socket proxy), `compose.proxy.yaml`
  (behind an existing Traefik, Docker or file provider —
  `deploy/traefik/kurier.example.yml`), `compose.local.yaml` for site
  tweaks (gitignored). The API binds to loopback unless `KURIER_BIND` says
  otherwise. Replaces `docker-compose.yml`.
- **Generic defaults**: `MQTT_BROKER=mosquitto`, `MYSQL_HOST=mysql`,
  `MYSQL_DATABASE=sms_gateway`, `TZ=UTC`. Existing installs that relied on
  the old defaults must set them in `.env`. Migrations no longer name a
  database (`USE …`); apply them to yours.
- **Documentation**: README (EN/RO), docs/ARCHITECTURE, DEPLOY, TELTONIKA
  (router prerequisites and measured quirks), OPERATIONS, API,
  INTEGRATION_KLOPOT; SECURITY, CONTRIBUTING; `.env.example` covers every
  variable.
- **License**: Apache-2.0.

## [1.9.2] — 2026-11-14

### fix: modem status from `gsmctl -E` (reliable, one call)

1.9.1's retry did not hold: measured on the RUT241, `gsmctl -q` fails
25–65 % of the time even 1–3 s apart, so right after a start the signal
stayed unknown. `gsmctl -E` (the modem daemon's JSON cache) answered 8/8
and carries signal (RSSI/RSRP/RSRQ/SINR), operator, network, **band** and
**modem temperature** in one call. Status now reads `-E` (+ `-z` for the
SIM state that feeds `/api/health`, `-i` once); the per-value commands are
only a fallback. Router calls per status read: 7 → 2. New fields `band`,
`modem_temperature` in `/api/sms/status`, MQTT status and metrics; the
status page shows band and temperature.

## [1.9.1] — 2026-11-14

### fix: flaky modem reads; UI sent thread dates

- The modem intermittently answers a status query with
  `ERROR: Couldn't retrieve data` (seen 1 in 8 for `gsmctl -q`). That
  answer was stored as the status for 30 s: no signal in metrics/MQTT and
  "unknown signal" on the status page — and an error text could have been
  shown as e.g. the operator. Now an `ERROR`/empty answer is never a value:
  one retry, then the last good value (10 min; SIM state 60 s, as it feeds
  `/api/health`).
- Status page: sent messages after the first showed absolute dates
  (`Array.map` passed the index as the `absolute` flag).

## [1.9.0] — 2026-11-14

### feat(ui): sending budget, statistics, search, confirmed actions

- **Acum:** live sending budget (3/min, 20/h normal, 30/h critical: used,
  free, when a slot frees), MQTT / webhook / history-store state; a
  two-step "send a test SMS" (unique idempotency key per click) and
  "cancel" on queued messages.
- **Statistici:** SMS per local day (sent vs blocked/failed, stacked,
  hover/arrow-key readout, table view), period 7/30/90 days, noisiest
  alert types with billed segments. Palette checked with the dataviz
  validator (chroma, colour-blind separation, contrast) in light and dark;
  sent colour retuned to `#1a6fa3` / `#2a72a8`.
- **Căutare:** the full MySQL history by text/number, outcome, alert type,
  critical only and date range, paged.
- API: `GET /api/sms/budget`, `GET /api/sms/stats`, `GET /api/sms/history/search`;
  metrics add `rate_*_used` and `mqtt_connected`.
- Tests: 128 → 142 (+ MySQL integration for search/stats).

## [1.8.0] — 2026-11-14

### feat: status page `/ui`

Read-only page for daily operation: the SMS path (gateway → router → SIM →
network with signal bars → outbox) with a plain-language summary, counters
since start, the outbox with each retry's last error, and sent/received
messages as a phone-style thread (critical, queued, blocked, duplicate,
stale, delivery targets, billed segments).

- Static files (`src/ui/`), no build, no external resources — it works
  when the internet is down. Strict CSP, `nosniff`, no framing.
- Data from the existing API with the operator's `SMS_API_KEY`, kept in
  the browser; received SMS text (untrusted) is rendered as text only.
- Each section renders on its own: during a router outage the page still
  shows what it can (found while testing: one 502 blanked the page).
- Light/dark, phone layout, keyboard focus, reduced motion respected.

## [1.7.2] — 2026-11-14

### fix: one delivery window for inbound SMS; optional metadata never rejects

- **Inbound:** `stale` was only checked when a message was archived; the
  webhook then retried for 1 h and MQTT without limit. Found live: `TEST 2`
  (sent 20:02:49) reached Klopot at 20:11:46 when the webhook was enabled.
  Now both paths deliver only while the SMS is younger than
  `SMS_INBOX_STALE_SECONDS` (10 min) since it was *sent*; afterwards it is
  marked stale (archive/API only, `inbox_expired` metric).
  `SMS_INBOX_FORWARD_TTL_SECONDS` removed. Payload adds `age_seconds`.
- **Send contract:** optional fields no longer cause a 422 — labels are
  sanitized, `ttl_seconds`/`max_segments` clamped (numeric strings
  accepted), keys and text truncated. Producers filling them from templates
  (Klopot `body_template`) can't lose an alert to a bad value. Only an
  empty `message` or an idempotency conflict is refused.
- Tests: 113 → 122.

## [1.7.1] — 2026-11-14

### fix(inbox): modem timestamps are UTC

The first live inbound test was archived as `stale` and not delivered: the
RUT241 prints SMS timestamps in UTC (`Sat Nov 14 20:01:19 2026` for an SMS
sent at 22:01 EET) and 1.7.0 read them as Europe/Bucharest, making every
message look 2 h old. `SMS_MODEM_TIMEZONE` now defaults to `UTC`, and a
timestamp in the future is ignored instead of trusted.

## [1.7.0] — 2026-11-14

### feat: reliable inbound transport (modem → MQTT / webhook)

The gateway's job is transport in both directions; acting on SMS text is
the consumers' job (Klopot has its own bot). Before: the inbox was polled
every 2 min, never archived or emptied (SIM fills, indexes reused),
delivery to MQTT was best-effort and lost across restarts.

- Every received SMS is archived once in `sms_inbox`
  (`migrations/004_sms_inbox.sql`, unique fingerprint), then deleted from
  the modem (`SMS_INBOX_DELETE_AFTER_ARCHIVE`).
- Delivered once to MQTT `sms-gateway/inbox` (QoS 1, retried while the
  broker is down) and to an optional webhook (`SMS_INBOX_WEBHOOK_URL`,
  token header, retried ≤1h) — payload compatible with Klopot's
  `sms_inbox` input.
- Flags for consumers: `authorized` (sender ∈ `SMS_DEFAULT_RECIPIENTS`),
  `stale` (older than 10 min on arrival → archived, never delivered, so
  an old command is never acted on).
- Poll every 20 s (was 120 s), independent of the MQTT connection.
- `/api/sms/inbox` now serves the archive; metrics `inbox_*`; daily prune
  (90 days).
- Tests: 100 → 111.

## [1.6.0] — 2026-11-14

### feat: extended send contract, GSM-7 cost reduction, idempotency

Driven by `sms_log` data: on 2026-11-13 a container-down storm hit the
hourly limit (21 of 31 attempts blocked, Klopot retrying 429s every
minute); every alert arrived as `normal`; changing values defeated dedup;
each alert cost 5 SMS parts (UCS-2).

- **Contract** ([docs/API.md](docs/API.md)) — all optional, unknown
  fields ignored, same on HTTP and MQTT: `severity` (→ priority, lenient),
  `dedup_key` (alert identity, survives changing text), `idempotency_key`
  (retries replay the first accepted result: no second SMS, no rate slot;
  `Idempotent-Replayed` header; same key + different request → 422),
  `ttl_seconds` (per-message outbox expiry), `max_segments`
  (encoding-aware truncation), `source` / `type` (stored in history).
- **GSM-7 transliteration** (default on): ăâîșț → aaist, — → -, … → ...;
  a 320-char alert drops from 5 to 3 parts. `segments` per send (from the
  router's `sms_used`) in history, response and `segments_sent` metric.
- Normal hourly limit 10 → 20 (`SMS_RATE_LIMIT_HOUR`).
- `migrations/003_extended_contract.sql`: `sms_log.source/type/segments`,
  `sms_outbox.expires_at/source/type`, table `sms_idempotency`. Schema is
  probed at startup — the gateway runs before and after the migration.
- [docs/KLOPOT_PROPOSAL.md](docs/KLOPOT_PROPOSAL.md): `body_template` and
  small Klopot changes (expose the delivery idempotency key, honour
  `Retry-After` on 429, treat 409 as delivered).
- Tests: 66 → 100.

## [1.5.1] — 2026-11-14

### fix: health always answers 200 (regression from 1.3.0)

`/api/health` returned 503 when the router was unreachable (1.3.0).
Klopot's `http_poll` input drops responses >= 400, so rule
`sms_modem_down_v1` could never see `modem_reachable: false` — the one
value it alerts on. Health is a data contract: always 200, state in the
body.

- New field `outbox_oldest_age_seconds` (0 when empty) for an
  "SMS not leaving" alert; `outbox_pending` is now always numeric.
  Klopot treats a `null` comparison as an eval error and auto-disables a
  rule after 5 of them.
- The same field is in `/api/sms/metrics`.

## [1.5.0] — 2026-11-14

### feat: observability, housekeeping, non-blocking history

- **Metrics:** counters since start (`sent`, `failed`, `queued`,
  `blocked_rate_limit`, `blocked_duplicate`, `outbox_delivered/expired/
  cancelled`), `outbox_pending` and last known signal (RSSI/RSRP/RSRQ/
  SINR) via `GET /api/sms/metrics` and retained MQTT `sms-gateway/metrics`
  (flat numeric JSON for Telegraf — snippet in README).
- **MQTT:** Last Will + retained `sms-gateway/availability`
  (`online`/`offline`); `status` retained, published on connect (was
  first after 5 min) and now includes RSRP/SINR/RSRQ; QoS 1.
- **Inbox:** no republish of old messages after restart (first poll
  seeds); messages identified by index+sender+date since the modem reuses
  indexes; first poll after 10s instead of 2 min.
- **Housekeeping:** daily prune of `sms_log` (90 days) and finished
  `sms_outbox` rows (30 days); was never called before.
- **History writes off the event loop:** one background writer thread —
  a slow or unreachable MySQL no longer stalls sends.
- **Pinned dependencies** (pydantic drifted 2.13 → 2.14 between builds).
- Graceful MQTT/outbox shutdown awaits its tasks and flushes history.

## [1.4.0] — 2026-11-14

### feat: durable outbox with retry

Closes the 2026-11-11 gap: with the router unreachable for 40+ minutes,
"INTERNET DOWN" and its escalation failed and were lost.

- A send that fails at the router is **accepted with `202`**
  (`queued: true`, `outbox_id`) and stored in the `sms_outbox` table.
- Background worker retries with exponential backoff (30s → 10 min cap),
  critical first. A round stops at the first connection failure.
- Items older than **6h** (`SMS_OUTBOX_TTL_SECONDS`) are marked `expired`
  and never sent late.
- While anything is pending, new SMS go straight to the outbox: order is
  kept and callers get an answer in milliseconds.
- Dedup also rejects copies of a message that is already queued.
- `GET /api/sms/outbox`, `DELETE /api/sms/outbox/{id}`; `/api/health`
  reports `outbox_pending`; MQTT `sms-gateway/sent` publishes the final
  outcome (`from_outbox`, `status: sent|expired`).
- Falls back to an in-memory outbox when MySQL or the table is missing.
- Router read timeout 15s → 10s and parallel REST/ubus login, so a hung
  router answers `202` in ~5s, inside Klopot's 15s client timeout.
- `storage.py`: shared `MySQLConnection`; removed unused `stats()`.
- SQL schema now versioned in `migrations/` (`001_sms_log`, `002_sms_outbox`).

**Deploy:** apply `migrations/002_sms_outbox.sql` as a MySQL admin, then
restart. Note: a send that timed out may have reached the modem anyway;
the retry can then produce a duplicate (preferred over a lost alert).

## [1.3.0] — 2026-11-14

### fix: send-path correctness and hardening

- **Phone normalization actually applied.** Numbers are normalized to
  E.164 and validated *before* sending (previously only the dedup key
  was normalized). Invalid numbers → HTTP 400, nothing sent.
- **`phone` accepted as alias for `number`.** Klopot's SMS channel posts
  `{"phone": ...}`, which was silently ignored — messages only reached
  the right person because `SMS_DEFAULT_RECIPIENTS` matched.
- **Router down at startup no longer sticks.** `api_mode` stayed `None`
  forever, routing sends through the gsmctl fallback and keeping
  `/api/health` degraded until restart. Now re-detected lazily.
- **Router rejection detected.** HTTP 200 with `{"success": false}`
  from RutOS now raises instead of being logged as sent.
- **ubus session expiry handled.** ubus errors are single-element
  results (`[6]`); the old `len(result) >= 2` check never matched, so
  re-login never happened. Retry is now bounded to one attempt.
- **No race between concurrent sends.** Rate limit + dedup are checked
  and reserved atomically; failed sends release the dedup entry but
  keep the rate slot. `Retry-After` is now computed, not fixed.
- **Multiple recipients:** every recipient is attempted; the request
  only fails if all of them failed.
- **Critical ceiling:** `SMS_RATE_LIMIT_CRITICAL_HOUR` (default 30).
- **Ops:** `/api/health` → 503 when the router is unreachable;
  `get_status` cached 30s (was 7 router calls per health check);
  httpx request logs silenced; empty error messages (timeouts) now show
  the exception type; Teltonika/httpx errors map to 502 instead of 500;
  constant-time API key comparison; `/history?limit` bounded to 1–100.
- **Tests:** 9 → 43 (phone, concurrency, Teltonika client via
  `httpx.MockTransport`, HTTP layer). `requirements-dev.txt` added.

## [1.2.1] — 2026-05-26

### feat: MySQL-backed persistent history with rehydrate on boot

Closes the audit finding "container restart wipes history +
rate-limit window". Every send (success or failure) is now also
recorded to the `sms_log` table in MySQL. On startup the in-memory deque
is rehydrated from MySQL — so the operator sees the same history
table before and after a restart.

Why MySQL not SQLite: the platform migrated away from SQLite
2026-05-22→23. Every persistent store now lives in a MySQL schema
so the daily mysqldump backup pipeline covers it automatically.

**Schema** (one table, scoped grants):

```sql
CREATE DATABASE sms_gateway;
CREATE TABLE sms_log (id, ts, number, message, priority, success, detail);
CREATE USER 'sms_gateway' GRANT SELECT,INSERT,UPDATE,DELETE ON sms_gateway.*;
```

**New env vars** (defaults match the central MySQL container):
- `MYSQL_HOST`
- `MYSQL_PORT=3306`
- `MYSQL_USER=sms_gateway`
- `MYSQL_PASSWORD=` (required; empty disables persistence)
- `MYSQL_DATABASE`
- `SMS_HISTORY_ENABLED=true`

Opt-in: empty `MYSQL_PASSWORD` keeps legacy in-memory-only mode (no
behavior change for fresh deployments that haven't provisioned the
schema). Connection auto-reconnects via `.ping(reconnect=True)` —
survives the MySQL 28800s idle timeout.

Errors in `record()` and `load_recent()` are logged but never raised;
a transient MySQL outage doesn't break the send path.

## [1.1.0] — 2026-05-26

### feat: API key auth + phone normalization + retry-storm guard

Three audit findings closed in one bundle:

1. **API key auth via `X-API-Key` header.** Opt-in: when `SMS_API_KEY`
   env is set, all `/api/sms/*` routes require the matching header
   (401 otherwise). Public `/`, `/api/health` endpoints unaffected.
   Empty key keeps backwards-compat. Closes the zero-auth surface
   that let any LAN host spend SIM quota.

2. **Phone normalization to E.164.** `+40.../0040.../072...` now all
   hash to the same dedup bucket — was a real foot-gun: same alert
   to same recipient could be sent 3× via different formats.
   Whitespace and dashes stripped. Bare RO numbers (`0...`) get
   `+40` prefix via the new `SMS_DEFAULT_COUNTRY_CODE` setting.

3. **Rate-limit slot consumed on send failure too.** Prevents a
   broken caller from retry-storming uncapped during a modem outage.
   Dedup intentionally NOT recorded on failure so legitimate retry
   post-recovery still lands.

### Verified live

- `401` without key, `200` with correct key (after `SMS_API_KEY` set).
- `_normalize_phone` test: 5 RO variants → `+40712345678`; 3 of those
  produce identical dedup_key (`cbe5006d4753b80a`).
- Public endpoints (`/`, `/api/health`) bypass auth even when configured.
- `_record_rate_only(priority)` skips dedup but consumes rate slot on
  the failure path of `_send_single`.

## [1.0.0] — initial — 2026-03-03

Initial FastAPI microservice wrapping Teltonika RUT241 SMS modem.
