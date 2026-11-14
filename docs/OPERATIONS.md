# Operations

## Status page — `/ui`

| Tab | Shows |
|---|---|
| **Now** | the SMS path hop by hop (gateway → router → SIM → network → queue) with a one-line verdict; the live sending budget per window and when a slot frees; MQTT / webhook / storage state; counters since start; the outbox with each message's last error (cancel button); sent and received SMS as a thread (repeats grouped) |
| **Statistics** | SMS per local day (sent vs blocked/failed) for 7/30/90 days, with a table view; the alert types that send the most, with billed segments |
| **Search** | the full history (`SMS_HISTORY_RETENTION_DAYS`) by text/number, outcome, type, critical only, date range |

Two actions, both two-step: send a test SMS to the default recipient,
cancel a queued SMS. The page asks for `SMS_API_KEY` once and keeps it in
the browser. It speaks **English and Romanian**: it follows the browser's
language and can be switched at the top right (the choice is remembered).

## Signals to watch

| Source | Field | Meaning |
|---|---|---|
| `GET /api/health` (always 200) | `status` | `healthy` / `degraded` (router up, SIM not ready) / `unhealthy` (router unreachable) |
| | `outbox_pending`, `outbox_oldest_age_seconds` | SMS waiting; age of the oldest (0 when empty) |
| MQTT `sms-gateway/availability` (retained, Last Will) | `online` / `offline` | the process is up and connected |
| MQTT `sms-gateway/status` (retained, on connect + every 5 min) | signal, operator, network, band, temperature | modem condition |
| MQTT `sms-gateway/metrics` (retained, every 60 s) / `GET /api/sms/metrics` | flat numbers | see below |
| `GET /api/sms/budget` | per window: used, limit, frees in | how close a burst is to blocking |

Metrics (counters since the last start): `sent`, `failed`, `queued`,
`blocked_rate_limit`, `blocked_duplicate`, `segments_sent`,
`idempotent_replays`, `outbox_pending`, `outbox_oldest_age_seconds`,
`outbox_delivered/expired/cancelled`, `inbox_received/unauthorized/stale/
expired/published/forwarded/forward_failures/deleted`, `rate_*_used`,
`mqtt_connected`, `rssi`, `rsrp`, `rsrq`, `sinr`, `modem_temperature`.

Suggested alerts: router unreachable or SIM not ready for a few minutes
(`modem_reachable`, `sim_ok`); the oldest queued SMS older than 10 min
(`outbox_oldest_age_seconds > 600`); `availability = offline`;
`outbox_expired` or `blocked_rate_limit` increasing. Send these over a
channel other than SMS.

### Telegraf → InfluxDB

```toml
[[inputs.mqtt_consumer]]
  servers = ["tcp://mosquitto:1883"]
  topics = ["sms-gateway/metrics"]
  data_format = "json"
  name_override = "kurier"
```

Counters reset on restart — graph them with `non_negative_difference()`.

## Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| Sends answer `202`, outbox grows | router unreachable → check power/LAN; messages leave automatically when it's back |
| Outbox not draining, router reachable | router refuses (credit, SIM, network) → `GET /api/sms/outbox` shows `last_error` |
| `429` | rate limit; `Retry-After` says when; check the budget on `/ui`. Critical alerts should send `severity: critical` |
| `409` | duplicate within `SMS_DEDUP_SECONDS` (same number + `dedup_key`/text) — already sent or queued |
| `422 … different request` | an `idempotency_key` reused for another message — producer bug |
| `api_mode: rest`, empty inbox/status | the router user can't run `gsmctl` over ubus ([TELTONIKA.md](TELTONIKA.md)) |
| Inbound SMS marked `stale` | older than `SMS_INBOX_STALE_SECONDS` when it could be delivered; check `SMS_MODEM_TIMEZONE` if *every* SMS is stale |
| Webhook `forward_error: HTTP 401` | `SMS_INBOX_WEBHOOK_TOKEN` doesn't match the receiver |
| Startup warning "apply migrations/00X" | schema older than the code; apply that file ([DEPLOY.md](DEPLOY.md)) |
| Status page: "The key you entered … isn't accepted" | wrong key; paste the `SMS_API_KEY` value (or the whole `.env` line) |

## Housekeeping

Daily (first run 5 min after start): `sms_log` older than
`SMS_HISTORY_RETENTION_DAYS` (90), finished outbox rows older than
`SMS_OUTBOX_RETENTION_DAYS` (30), expired idempotency keys, inbound
archive older than `SMS_INBOX_RETENTION_DAYS` (90).

## Backups

`backups/last-backup.txt` says `ok <time> <file>` or `failed <time> <error>`
after each daily run (`compose.backup.yaml`); `docker compose logs backup`
has the history. A failed run usually means the backup service can't reach
MySQL (network, password). Restore: [DEPLOY.md](DEPLOY.md#backup).

## Rotating secrets

- `SMS_API_KEY`: change in `.env`, recreate (`docker compose up -d`), then
  update every client (and re-enter it on `/ui`).
- Webhook token: change on both sides, recreate both.
- Router password: change on the router, then `TELTONIKA_PASS`, recreate.

## Testing against MySQL

Unit tests run without MySQL. To exercise the SQL, start a throwaway
`mysql:8.0`, apply `migrations/*.sql`, create a user with
`SELECT, INSERT, UPDATE, DELETE`, and run the stores
(`MySQLConnection`, `SendHistoryStore`, `MySQLOutboxStore`,
`MySQLInboxStore`, `MySQLIdempotencyStore`) against it from the image
(`PYTHONPATH=/app`). Remove the container afterwards.
