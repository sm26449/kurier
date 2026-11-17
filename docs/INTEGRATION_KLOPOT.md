# Integration with Klopot

Klopot is a self-hosted notification service (rules → templates →
channels). Kurier plugs into it in four places: Klopot **sends** SMS
through Kurier, Klopot **receives** SMS through Kurier, Klopot
**monitors** the SMS path, and Kurier **watches Klopot** (an SMS when
Klopot stops checking in). Klopot 1.3.0 and later has a dedicated `kurier`
SMS provider that does the first and the last; older Klopot (1.0.1 builds
from November 2026 onward) uses its generic `http` provider and has no
watchdog.

## 1. Sending — SMS channel

### Klopot 1.3.0+: `provider: kurier`

Channel `kind: sms`:

```json
{
  "provider": "kurier",
  "phone": "+40…",
  "kurier_url": "http://kurier:5080",
  "api_key_ref": "SMS_GATEWAY_API_KEY",
  "dedup_key": "{type}:{rule_id}",
  "ttl_seconds": 21600,
  "max_segments": 3,
  "watchdog": true,
  "watchdog_timeout_sec": 180
}
```

For every recipient (comma-separated in `phone`), Klopot posts to
`{kurier_url}/api/sms/send`: `number`, `message`, `severity`,
`idempotency_key` = delivery key + `:` + phone, `source: "klopot"`,
`type`, and `dedup_key` (the template, rendered), `ttl_seconds` and
`max_segments` when the channel sets them. Klopot counts any `2xx`
(including `202`, queued in Kurier's outbox) and `409` (duplicate, already
sent or queued) as delivered. Its channel health check reads `{kurier_url}/api/health`.
The `watchdog*` fields belong to [section 4](#4-watchdog--kurier-watches-klopot).

### Older Klopot: `provider: http`

Channel `kind: sms`, `provider: http`:

```json
{
  "provider": "http",
  "phone": "+40…",
  "url": "http://kurier:5080/api/sms/send",
  "method": "POST",
  "content_type": "application/json",
  "auth_kind": "header", "auth_header": "X-API-Key", "auth_value_ref": "SMS_GATEWAY_API_KEY",
  "health_url": "http://kurier:5080/api/health",
  "success_statuses": [409],
  "body_template": "{\"phone\": \"{phone | jsonstr}\", \"message\": \"{text | jsonstr}\", \"severity\": \"{severity | jsonstr}\", \"dedup_key\": \"{type | jsonstr}:{rule_id | jsonstr}\", \"idempotency_key\": \"{idempotency_key | jsonstr}:{phone | jsonstr}\", \"ttl_seconds\": 21600, \"max_segments\": 3, \"source\": \"klopot\", \"type\": \"{type | jsonstr}\"}"
}
```

What each field buys (with either provider):

| Field | Effect in Kurier |
|---|---|
| `severity` | `critical` uses the critical budget (no dedup), so a noisy normal alert can't block it |
| `dedup_key` = type + rule | repeats of one alert with changing values are recognised; rules sharing a template stay distinct. For `/v1/fire` (no rule) it renders `<type>:[?rule_id]` — stable per type |
| `idempotency_key` = delivery key **+ phone** | a Klopot retry after a timeout is replayed, not re-sent. The phone suffix is required: Klopot sends one request per recipient with the same delivery key, and Kurier rejects a key reused for a different number (422) |
| `ttl_seconds` | how long an alert may wait in Kurier's outbox |
| `max_segments` | encoding-aware truncation instead of a fixed character cut |
| `success_statuses: [409]` | a duplicate is already sent/queued — don't retry it |

With the `http` provider every string goes through `| jsonstr`: the
rendered body must be valid JSON or Klopot drops the SMS. The `kurier`
provider builds the JSON itself.

Klopot's delivery side complements it: `429`/`503` are deferred by
exactly `Retry-After` (no attempt spent, breaker untouched), other `4xx`
are permanent, `202` (queued in Kurier's outbox) counts as delivered.

## 2. Receiving — webhook input

1. Klopot: an input `kind: sms_inbox` with `auth_token_ref` naming an env
   var that holds a random token (allowlisted in Klopot's secret refs).
2. Kurier `.env`:
   ```
   SMS_INBOX_WEBHOOK_URL=http://klopot:5090/v1/inputs/<input id>/webhook
   SMS_INBOX_WEBHOOK_TOKEN=<same token>
   ```
3. Klopot variables on that input: `body`, `from`, and `raw.authorized`.

Rules that act on SMS commands **must require `authorized`**; for actions
that change state, confirm with a one-time code sent back by SMS (sender
ids can be spoofed). Kurier never delivers messages older than its
delivery window, so an old command can't be replayed after an outage.
The MQTT path (`sms-gateway/inbox`) works in parallel.

## 3. Monitoring the SMS path

An `http_poll` input on `http://kurier:5080/api/health` every 2 min, with
variables bound **to that input** (Klopot only accepts values from the
input a variable is bound to):

| Variable | JSON path |
|---|---|
| `sms.modem_reachable` | `modem_reachable` |
| `sms.sim_ok` | `sim_ok` |
| `sms.outbox_pending` | `outbox_pending` |
| `sms.outbox_oldest_age_sec` | `outbox_oldest_age_seconds` |

Rules (route to a channel that is **not** SMS, e.g. Telegram):

| Rule | Condition |
|---|---|
| modem down | `(sms.modem_reachable == False OR sms.sim_ok == False) AND var_age("sms.modem_reachable") < 600`, ×3 at 60 s |
| gateway silent | `var_age("sms.modem_reachable") > 900 AND var_age("sms.modem_reachable") < 86400` |
| SMS not leaving | `sms.outbox_oldest_age_sec > 600 AND var_age("sms.outbox_oldest_age_sec") < 600`, ×2 at 120 s, inhibited by "modem down" |

`/api/health` always answers `200` — a non-2xx would make `http_poll`
drop the body and the "modem down" rule would never see the value.

## 4. Watchdog — Kurier watches Klopot

Klopot cannot report its own outage through itself. Every `kurier` SMS
channel with `"watchdog": true` pings a
[watchdog](API.md#watchdogs--post-apiwatchdogsnameping) of its own
(Klopot ADR-0003, Klopot 1.3.0+):

```
POST http://kurier:5080/api/watchdogs/klopot-<channel>/ping   every 60 s
{"timeout_sec": 180, "status": "ok" | "fail", "detail": "<why, when fail>"}
```

- `<channel>` is the channel name in Kurier's alphabet (lower case, other
  characters → `-`), e.g. `klopot-sms_main`. `timeout_sec` is the
  channel's `watchdog_timeout_sec` (default 180).
- Klopot stops (process, container) → after `timeout_sec` without a ping
  Kurier sends `⚠ klopot-<channel>: no check-in since …`; `✅ … is back`
  when the pings resume. Recipients: the channel's `watchdog_to` (sent as
  `to` on every ping) if set, else Kurier's `SMS_WATCHDOG_TO`, else
  `SMS_DEFAULT_RECIPIENTS`.
- Klopot alive but not working (an essential component down, the rule
  engine stuck, its outbox not draining) → it pings `status: fail` and the
  SMS carries the reason at once.
- The same answer carries Kurier's modem state (`modem.registered`,
  `signal_strength`, `sim_state`, `operator`), so Klopot watches Kurier
  with the same request (its `kurier_unreachable` / `kurier_modem_down`
  alerts) — route those to a channel that is **not** SMS.
- Klopot never deletes its watchdog. Before a planned Klopot stop longer
  than the timeout, `DELETE /api/watchdogs/klopot-<channel>` (the next
  ping registers it again), or expect the SMS.

## Verifying

- Send: Klopot channel test → Kurier history shows `source=klopot`; a
  second identical delivery shows `replayed: true`.
- Receive: send an SMS from an authorized phone → Klopot variable
  `sms.inbox.body` updates within ~20 s; a POST without the token → 401.
- Watchdog: the status page (tab *Now*, *Watchdogs*) lists
  `klopot-<channel>` as up with its last check-in; stop Klopot for 3 min →
  the SMS arrives, start it → `✅ … is back`.
- Klopot's activity log (Deliveries / Requests) records every request to
  and from Kurier with status and headers.

