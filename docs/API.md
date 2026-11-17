# Kurier — API contract

Base URL: `http://<host>:5080` (in a Docker network: `http://kurier:5080`). Interactive schema: `/docs`.
MQTT topics keep the historical prefix `sms-gateway/` ([MQTT](#mqtt)).
Authentication: header `X-API-Key` = `SMS_API_KEY` on every `/api/sms/*` request.
With no key configured the API answers `503` (closed) unless
`SMS_ALLOW_NO_AUTH=true`. `/api/health` and `/` need no key.

## Send — `POST /api/sms/send`

Only `message` is required. Every other field is optional; **unknown fields
are ignored**, so a producer can start sending a field before the gateway
uses it. The same JSON is accepted on MQTT topic `sms-gateway/send` — **without the
API key**: whoever may publish there can send SMS, so restrict the topic with
broker ACLs ([SECURITY.md](../SECURITY.md)).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `message` | string 1–1600 | — | Text. Transliterated to GSM-7 (`SMS_GSM7_TRANSLITERATE`). |
| `number` / `phone` | string | `SMS_DEFAULT_RECIPIENTS` | Normalized to E.164 (`0722…` → `+40722…`); invalid → 400. |
| `priority` | `normal` \| `critical` | `normal` | Budget. Any other value → `normal` (never a 422). |
| `severity` | `info` \| `warning` \| `critical` | — | Used when `priority` is absent: `critical` → critical, else normal. |
| `dedup_key` | string ≤128 | the text | Identifies the alert. At most one SMS per *number + key* per `SMS_DEDUP_SECONDS`, even if the text changes (e.g. a value in it). Not applied to critical. |
| `idempotency_key` | string ≤128 | — | Retry-safe: a request with a key already accepted in the last 24h gets the first result back (`replayed: true`, header `Idempotent-Replayed: true`) — nothing is sent and no rate slot is used. |
| `ttl_seconds` | int 60–86400 | `SMS_OUTBOX_TTL_SECONDS` (6h) | If queued in the outbox, dropped (`expired`) after this long. |
| `max_segments` | int 1–10 | `SMS_MAX_SEGMENTS` (0 = no cap) | Text truncated (ending `...`) to fit this many SMS parts. |
| `source` | label ≤32 | — | Producer, e.g. `klopot`. Stored in history (`sms_log.source`). |
| `type` | label ≤64 | — | Alert type, e.g. the template type. Stored in history. |

Labels match `^[A-Za-z0-9_.:-]+$`. Empty strings count as absent.

### Responses

| Status | Meaning |
|---|---|
| `200` | Sent (to every recipient, or some — see per-item `success`). |
| `202` | At least one recipient queued in the outbox (router unreachable); delivered with retries until sent or expired. Outcome on MQTT `sms-gateway/sent` (`from_outbox: true`). |
| `400` | Invalid phone number / no recipient configured. |
| `401` | Missing or wrong `X-API-Key`. |
| `409` | Duplicate (same number + `dedup_key`/text within the window, or already queued). |
| `422` | Empty / missing `message`, or an `idempotency_key` reused for a different request. |
| `429` | Rate limit; `Retry-After` = seconds until a slot frees. |
| `502` | Router error and outbox disabled. |
| `503` | `SMS_API_KEY` is not configured (the API is closed). |

Optional fields never cause a rejection: labels are sanitized, numbers
clamped (`ttl_seconds` 60–86400, `max_segments` 1–10) or dropped if not
numeric, keys cut to 128 chars, text cut to 1600. Only an empty `message`
(and an idempotency conflict) is refused.

Body: one item per recipient — `success`, `number`, `timestamp`, `detail`,
`queued`, `outbox_id`, `segments` (parts billed, from the router's
`sms_used`), `replayed`.

### Budgets

| Priority | Limits | Dedup |
|---|---|---|
| normal | `SMS_RATE_LIMIT_MINUTE` (3) / `SMS_RATE_LIMIT_HOUR` (20) | yes |
| critical | `SMS_RATE_LIMIT_CRITICAL_HOUR` (30) | no |

A failed send still uses its slot (retry-storm guard); a blocked or replayed
request does not.

### Idempotency semantics

- Remembered: accepted outcomes — sent, queued, or a mix of per-recipient
  results. Not remembered: requests rejected as a whole (429, 409, 400,
  502) — retrying those is exactly what the producer should do.
- Concurrent requests with the same key share one execution.
- Keys live `SMS_IDEMPOTENCY_TTL_SECONDS` (24h) in memory and in
  `sms_idempotency` (survive restarts).
- A key reused with a different number, text or priority is rejected with
  `422` (never a silent replay of the wrong result).
- Use one key per *delivery attempt chain* and per recipient — e.g.
  `<delivery-id>:<phone>`. A notification id alone is NOT enough when the
  producer sends follow-ups (escalations) or several recipients for it.

## Receive — modem → MQTT / webhook

The gateway only **transports** received SMS; it never acts on their text.
Every `SMS_INBOX_POLL_SECONDS` (20) the modem is read; each message is
archived once in `sms_inbox` (unique on sender + modem date + text), then
deleted from the modem (`SMS_INBOX_DELETE_AFTER_ARCHIVE`), and delivered:

| Path | Guarantee |
|---|---|
| MQTT `sms-gateway/inbox` (QoS 1) | once, in order; retried every poll while the broker is down |
| Webhook `SMS_INBOX_WEBHOOK_URL` (POST JSON) | once; retried every poll until a `2xx`; token in `SMS_INBOX_WEBHOOK_AUTH_HEADER` (`X-Input-Token`, or `Authorization` → Bearer) |

**Delivery window:** both paths deliver a message only while it is younger
than `SMS_INBOX_STALE_SECONDS` (10 min) **since it was sent**. Whatever
delays it (modem, broker, webhook down, webhook enabled later), once outside
the window it is marked `stale`, kept in the archive/API and never
delivered (`inbox_expired` metric). Nothing downstream acts on an old command.

Payload (same on both; `from`/`body`/`received_at`/`modem` match Klopot's
`sms_inbox` input, `message`/`timestamp` keep the pre-1.7 MQTT fields):

```json
{"id": 42, "from": "+40712345678", "body": "STATUS", "message": "STATUS",
 "received_at": "2026-11-14T19:40:02+00:00", "timestamp": "2026-11-14T19:40:02+00:00",
 "archived_at": "2026-11-14T19:40:15+00:00", "authorized": true, "stale": false,
 "age_seconds": 13, "modem": "1-1"}
```

- `authorized` — sender is one of `SMS_DEFAULT_RECIPIENTS`. Alphanumeric
  senders (operator promos) are never authorized. **Consumers that act on
  commands must require it.** Sender ids can be spoofed: for control
  actions, confirm with a one-time code sent back to the number.
- `stale` — outside the delivery window (on arrival or later): archived,
  **not delivered**. Messages already on the modem at first start are stale.
- `age_seconds` — seconds since the SMS was sent, at delivery time; a
  consumer can apply a stricter limit.
- `received_at` — modem timestamp converted to UTC. The RUT241 prints it in
  UTC (`SMS_MODEM_TIMEZONE=UTC`, the default); a timestamp in the future is
  ignored (age unknown).

Archive: `GET /api/sms/inbox?limit=1..200`, `GET|DELETE /api/sms/inbox/{id}`
(fields add `received_at`, `authorized`, `stale`, `published`, `forwarded`,
`forward_error`). Kept `SMS_INBOX_RETENTION_DAYS` (90).

## Watchdogs — `POST /api/watchdogs/{name}/ping`

A dead man's switch for other services ([ADR-0001](adr/0001-watchdogs.md)):
the service checks in regularly; silence, or a reported failure, sends an
SMS. Kurier never probes anything itself. `X-API-Key` as for `/api/sms/*`.

```
POST /api/watchdogs/{name}/ping
{"timeout_sec": 180, "status": "ok", "detail": "…", "to": ["+40…"]}
```

| Field | |
|---|---|
| `name` (path) | `[a-z0-9][a-z0-9._-]{0,63}`; otherwise `422` |
| `timeout_sec` | required, 60 … 604800 (7 days): down when no ping arrives for this long |
| `status` | `ok` (default) or `fail`: the service reports a problem, down at once |
| `detail` | text for the `fail` SMS; whitespace collapsed, cut to 160 characters (never refused) |
| `to` | optional, up to 10 numbers (normalized like `number` on send). Omitted or empty: `SMS_WATCHDOG_TO`, else `SMS_DEFAULT_RECIPIENTS` |

**The ping is the registration**: the first one creates the watchdog,
every one stores `timeout_sec` and `to` as sent. At most
`SMS_WATCHDOG_MAX` (100) watchdogs: a ping for a new name beyond that is
`409`. Other answers: `401` bad key, `422` invalid body or `to`.

```json
200 {"kurier": "ok",
     "modem": {"registered": true, "signal_strength": -71, "sim_state": "inserted", "operator": "Orange RO"},
     "watchdog": {"state": "up", "last_ping": "2026-11-17T12:00:00.123Z"}}
```

`modem` comes from Kurier's status cache — a ping never waits for the
router. While watchdogs exist, the cache is kept at most 60 s old. Once it
is older than 3 min (router unreachable) `registered` is `false` and the
other fields are `null`. `registered` = the modem reports an operator and
a signal.

States, checked every 15 s:

| Event | Transition | SMS (priority `critical`, `source=kurier`) |
|---|---|---|
| no ping for `timeout_sec` | up → down | `⚠ {name}: no check-in since {local time}` (`type=watchdog_down`) |
| ping with `status: fail` | up → down at once | `⚠ {name}: {detail}` (`type=watchdog_down`) |
| `ok` ping while down | down → up | `✅ {name} is back` (`type=watchdog_up`), only if the down SMS went out |

- At most **one down SMS per watchdog every 30 min**. An outage that
  starts inside that window gets its SMS when the window ends, if still
  down; its recovery is then announced as usual.
- After a Kurier restart every timer counts **from the restart**; a
  watchdog that was down stays down without a second SMS.
- The SMS go through the normal send path: outbox, critical budget,
  history (search `type=watchdog_down`).

`DELETE /api/watchdogs/{name}` — forget it (planned stop, maintenance), no
alert: `204`, or `404` if unknown. A later ping registers it again.

`GET /api/watchdogs` — every watchdog: `name`, `state`, `timeout_sec`,
`last_ping`, `last_status`, `detail`, `down_since`, `down_reason`
(`timeout` | `fail`), `down_sms_sent`, `recipients`, `created_at`.

```bash
# a nightly job: SMS if it hasn't succeeded for 25 h
curl -fsS -X POST -H "X-API-Key: $SMS_API_KEY" -H "Content-Type: application/json" \
  -d '{"timeout_sec": 90000}' http://kurier:5080/api/watchdogs/backup-nightly/ping
```

## Health — `GET /api/health`

Always `200` while the process runs; state in the body. Consumers (Klopot
`http_poll`) read the fields — a non-200 would hide them.

`status` (`healthy` | `degraded` | `unhealthy`), `modem_reachable`,
`sim_ok`, `api_mode`, `uptime_seconds`, `outbox_pending`,
`outbox_oldest_age_seconds` (both numeric, `0` when empty).

## Other endpoints

All need `X-API-Key`. Times are UTC, ISO 8601.

| Endpoint | Returns |
|---|---|
| `GET /api/sms/outbox` | SMS still waiting for the router, in delivery order: `id`, `number`, `message`, `priority`, `created_at`, `attempts`, `next_attempt_at`, `last_error` (`404` if `SMS_OUTBOX_ENABLED=false`) |
| `DELETE /api/sms/outbox/{id}` | cancels a waiting SMS (`404` if it is not pending any more) |
| `GET /api/sms/history?limit=1..100` | the most recent send attempts (memory, newest first): `number`, `message`, `priority`, `success`, `timestamp`, `detail`, `source`, `type`, `segments` |
| `GET /api/sms/history/search` | the full history in MySQL. Filters: `q` (text or number), `type`, `source`, `status` (`sent` \| `blocked` \| `duplicate` \| `failed` \| `queued`), `priority`, `since`, `until`, `limit` (1–200), `offset` |
| `GET /api/sms/budget` | per window (`normal_minute`, `normal_hour`, `critical_hour`): `used`, `limit`, `frees_in_seconds`; `dedup_seconds`, `dedup_active`; `connections` (`mqtt.enabled/connected/broker`, `webhook.enabled/forwarded/failures`, `history_store`) |
| `GET /api/sms/stats?days=1..90` | `per_day` (local days, `TZ`): `sent`, `blocked`, `duplicate`, `failed`, `queued`, `segments`; the same per alert `type`; `totals` |
| `GET /api/sms/status` | modem: `signal_strength` (RSSI), `rsrp`, `rsrq`, `sinr`, `operator`, `sim_state`, `connection_type`, `band`, `modem_temperature`, `modem_model`, `imei` (`502` if the router is unreachable) |
| `GET /api/sms/metrics` | flat numbers, see [Metrics](#metrics) |
| `GET /api/sms/inbox`, `GET\|DELETE /api/sms/inbox/{id}` | the inbound archive (see [Receive](#receive--modem--mqtt--webhook)) |
| `GET /` | `{"service": "kurier", "version": "…"}` — no key |

`/docs` (Swagger UI) and `/openapi.json` describe the same API and are
served without a key (they contain no data). Swagger UI loads its assets
from a CDN, so `/docs` needs internet access in the browser; the status
page `/ui` does not.

### Metrics

Counters since the last start (they reset on restart), plus current state:

| Keys | Meaning |
|---|---|
| `sent`, `failed`, `queued` | send attempts by outcome |
| `blocked_rate_limit`, `blocked_duplicate` | refused with `429` / `409` |
| `segments_sent` | SMS parts billed (router's `sms_used`) |
| `idempotent_replays` | requests answered from an idempotency key |
| `rate_normal_minute_used`, `rate_normal_hour_used`, `rate_critical_hour_used` | current use of each budget |
| `outbox_pending`, `outbox_oldest_age_seconds` | queue now |
| `outbox_delivered`, `outbox_expired`, `outbox_cancelled` | what happened to queued SMS |
| `inbox_received`, `inbox_unauthorized`, `inbox_stale`, `inbox_expired`, `inbox_published`, `inbox_forwarded`, `inbox_forward_failures`, `inbox_deleted` | inbound SMS |
| `mqtt_connected` | `1` / `0` |
| `rssi`, `rsrp`, `rsrq`, `sinr`, `modem_temperature` | last known signal (absent until first read) |
| `uptime_seconds` | since start |

## MQTT

Off unless `MQTT_BROKER` is set. The topic prefix `sms-gateway/` is fixed:
run one Kurier per broker. All JSON, QoS 1.

| Topic | Direction | Retained | Payload |
|---|---|---|---|
| `sms-gateway/send` | you → Kurier | no | the same JSON as `POST /api/sms/send`. **No API key**: protect the topic with broker ACLs. An invalid payload is logged and dropped |
| `sms-gateway/sent` | Kurier → you | no | the outcome of each SMS requested **over MQTT**, and of every SMS leaving the outbox: `number`, `message`, `timestamp`, `success`, then `queued`, `segments`, `replayed` (sent or queued) or `detail` (refused: rate limit, duplicate, error). From the outbox: `from_outbox: true`, `outbox_id`, `status` (`sent` \| `expired` \| `cancelled`) |
| `sms-gateway/inbox` | Kurier → you | no | a received SMS (see [Receive](#receive--modem--mqtt--webhook)) |
| `sms-gateway/availability` | Kurier → you | yes | `online` / `offline` (plain text; Last Will, so a crash also says `offline`) |
| `sms-gateway/status` | Kurier → you | yes | on connect and every 5 min: `signal`, `rsrp`, `sinr`, `rsrq`, `operator`, `sim_state`, `connection_type`, `band`, `modem_temperature`, `timestamp` |
| `sms-gateway/metrics` | Kurier → you | yes | every 60 s: the [Metrics](#metrics) object |

```bash
mosquitto_pub -h mosquitto -t sms-gateway/send \
  -m '{"message": "Pump stopped", "severity": "critical", "dedup_key": "pump_stopped"}'
mosquitto_sub -h mosquitto -t 'sms-gateway/#' -v
```

## Examples

```bash
# Minimal
curl -X POST localhost:5080/api/sms/send -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"phone": "+40712345678", "message": "Test"}'

# Full
curl -X POST localhost:5080/api/sms/send -H "X-API-Key: $K" -H 'Content-Type: application/json' -d '{
  "phone": "+40712345678", "message": "Container CRITICAL down — mosquitto",
  "severity": "critical", "dedup_key": "container_down_mosquitto_v1",
  "idempotency_key": "dlv-661", "ttl_seconds": 3600, "max_segments": 3,
  "source": "klopot", "type": "container_down_critical"}'
```
