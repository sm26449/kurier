# Architecture

Kurier is one FastAPI process. It **transports** SMS between your services
and a Teltonika RutOS router, in both directions, and keeps enough state
to do that reliably. It never interprets message text.

```
              ┌──────────────────────────── Kurier ────────────────────────────┐
 REST ───────▶│ routers/sms.py ─┐                                              │
 MQTT ───────▶│ services/mqtt.py┴─▶ SMSService ─▶ TeltonikaClient ──REST/ubus──┼──▶ router
              │                      │  ▲  │            ▲                       │
              │      IdempotencyCache┘  │  └─▶ Outbox ─┘ (retry worker)        │
              │                         │                                       │
 MQTT ◀───────│ mqtt.publish_inbox ◀─ InboxService ◀── poll every 20 s ─────────┼─── router
 webhook ◀────│                        (archive → deliver → delete from SIM)    │
              │ Housekeeping (daily prune)        storage/ ────────────────────┼──▶ MySQL
              │ /ui (static) · /api/health · /api/sms/{status,metrics,…}       │
              └────────────────────────────────────────────────────────────────┘
```

## Modules

| Module | Responsibility |
|---|---|
| `routers/sms.py`, `routers/health.py` | HTTP API; status-code mapping; API-key check |
| `services/sms_service.py` | send pipeline: options, text preparation, reserve → router or outbox, counters, status/metrics snapshot |
| `services/send_limits.py` | rate windows (normal minute/hour, critical hour) and the dedup window, reserved in one step; `budget()` |
| `services/send_history.py` | last 100 attempts in memory (+ `sms_log` write-through); `stats()`, `search()` |
| `services/teltonika.py` | router client: REST login/send, ubus session + `gsmctl`, status from `gsmctl -E` |
| `services/outbox.py` | durable queue for SMS the router didn't take; backoff worker; per-message TTL |
| `services/inbox.py` | inbound transport: poll → archive → MQTT/webhook → delete from modem |
| `services/idempotency.py` | `idempotency_key` replay cache, conflict detection |
| `services/mqtt.py` | MQTT in/out, Last Will, retained status/metrics |
| `services/mqtt_status.py` | connection flag MQTT writes and the send service reads (no reference cycle) |
| `services/stats.py` | pure aggregation of the send history (per day, per type) |
| `services/housekeeping.py` | daily retention pruning |
| `wiring.py` | `build_services()`: picks MySQL or memory for each store, creates the services and links them; `main.py`'s lifespan only starts and stops them |
| `storage/` | one module per table family (`history`, `outbox`, `idempotency`, `inbox`), each with a MySQL and an in-memory store; `db.py`: connection, column probes |
| `gsm7.py`, `phone.py` | GSM-7 transliteration/segment counting; E.164 normalization |
| `ui/` | status page (static HTML/CSS/JS): `core.js` helpers, `now.js` tab Now, `insights.js` statistics + search, `app.js` wiring, `i18n.js` texts |

## Send pipeline

1. **Validate leniently** (`SendSMSRequest`): only `message` is required;
   `phone` is an alias for `number`; severity words map to two budgets;
   optional metadata is sanitized or clamped, never rejected.
2. **Idempotency** — with `idempotency_key`, a key already accepted returns
   the stored result (`replayed`); concurrent requests with one key share
   one execution; a key reused with a different request → 422.
3. **Recipients** — the number, or `SMS_DEFAULT_RECIPIENTS`, normalized
   to E.164; every recipient is attempted.
4. **Text** — GSM-7 transliteration, optional cap to N segments.
5. **Reserve** — rate limit and dedup are checked *and reserved* in one
   synchronous step (no `await` between), so concurrent duplicates can't
   both pass. Critical: own hourly budget, no dedup.
6. **Send** — if the outbox already holds messages, queue behind them
   (order + instant answer); otherwise try the router. On any router
   failure → outbox, `202`.
7. **Record** — history row (MySQL write-through on a background thread),
   counters, billed segments from the router's `sms_used`.

## Inbound pipeline

Every `SMS_INBOX_POLL_SECONDS`: list the modem inbox → for each message
compute a fingerprint (sender | modem timestamp | text) → `INSERT IGNORE`
into `sms_inbox` (exactly once) → deliver to MQTT and the webhook while
the message is inside the delivery window → delete it from the modem.
Outside the window it is marked `stale`: kept, never delivered. With the
in-memory store a message is deleted only after delivery.

## Data model (MySQL)

| Table | Content | Migration |
|---|---|---|
| `sms_log` | every send attempt: number, text, priority, outcome, detail, source, type, segments | 001, 003 |
| `sms_outbox` | queued SMS: status, attempts, next attempt, expiry, last error | 002, 003 |
| `sms_idempotency` | idempotency key → request fingerprint + results (24 h) | 003 |
| `sms_inbox` | received SMS: fingerprint, sender, text, modem date, flags, delivery state | 004 |

Columns added by later migrations are probed at startup: the code runs
before and after a migration is applied.

## Design decisions

| Decision | Why |
|---|---|
| Transport only | Bots, commands and alert policy belong to the producer/consumer; the gateway stays small and predictable. |
| `202` + outbox when the router is down | The alert that matters most ("internet down") is sent exactly when the path is shaky. An accepted SMS must not be lost. |
| Health always `200`, state in the body | Monitors such as Klopot's `http_poll` drop non-2xx responses — a 503 would hide `modem_reachable: false`. |
| Optional metadata never rejects | Producers fill fields from templates; one odd value must not lose an alert. |
| Separate critical budget, no dedup for critical | A noisy normal alert must not block a critical one; critical still has a ceiling. |
| `dedup_key` instead of text | Alert texts carry changing values ("SOC 14.1 %"); the alert's identity doesn't change. |
| Idempotency conflict → 422 | Reusing a key for a different message is a producer bug; a silent replay would drop the second message. |
| Inbound delivery window | An SMS command sent during an outage must not be executed an hour later. |
| Status from `gsmctl -E` | The modem's per-value queries fail 25–65 % of the time; its JSON cache answers reliably in one call. |
| GSM-7 transliteration | Diacritics or one em dash switch an SMS to UCS-2 (70 chars per part): 5 parts instead of 3 for a typical alert. |
| MySQL, optional | State shared with the rest of the stack and its backups; without it everything still works, in memory. |
