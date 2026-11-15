# Kurier — API contract

Base URL: `http://<host>:5080` (in a Docker network: `http://kurier:5080`). Interactive schema: `/docs`.
MQTT topics keep the historical prefix `sms-gateway/`.
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

## Health — `GET /api/health`

Always `200` while the process runs; state in the body. Consumers (Klopot
`http_poll`) read the fields — a non-200 would hide them.

`status` (`healthy` | `degraded` | `unhealthy`), `modem_reachable`,
`sim_ok`, `api_mode`, `uptime_seconds`, `outbox_pending`,
`outbox_oldest_age_seconds` (both numeric, `0` when empty).

## Other endpoints

`GET /api/sms/outbox`, `DELETE /api/sms/outbox/{id}`, `GET /api/sms/history?limit=1..100`,
`GET /api/sms/budget` (live rate-window usage + connection state), `GET /api/sms/stats?days=1..90`, `GET /api/sms/history/search` (filters `q`, `type`, `source`, `status`, `priority`, `since`, `until`, `limit`, `offset`), `GET /api/sms/status` (signal RSSI/RSRP/RSRQ/SINR, operator, network, band, modem temperature, SIM, model, IMEI), `GET /api/sms/metrics` (flat numeric: `sent`, `failed`, `queued`,
`blocked_rate_limit`, `blocked_duplicate`, `segments_sent`,
`idempotent_replays`, `outbox_*`, `inbox_*`, signal), `GET /api/sms/status`.

## Examples

```bash
# Minimal (what Klopot 1.0.1 sends today)
curl -X POST localhost:5080/api/sms/send -H "X-API-Key: $K" -H 'Content-Type: application/json' \
  -d '{"phone": "+40712345678", "message": "Test"}'

# Full
curl -X POST localhost:5080/api/sms/send -H "X-API-Key: $K" -H 'Content-Type: application/json' -d '{
  "phone": "+40712345678", "message": "Container CRITICAL down — mosquitto",
  "severity": "critical", "dedup_key": "container_down_mosquitto_v1",
  "idempotency_key": "dlv-661", "ttl_seconds": 3600, "max_segments": 3,
  "source": "klopot", "type": "container_down_critical"}'
```
