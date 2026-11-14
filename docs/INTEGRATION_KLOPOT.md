# Integration with Klopot

Klopot is a self-hosted notification service (rules → templates →
channels). Kurier plugs into it in three places: Klopot **sends** SMS
through Kurier, Klopot **receives** SMS through Kurier, and Klopot
**monitors** the SMS path. Requires Klopot with the SMS `http` provider
and per-delivery idempotency keys (Klopot 1.0.1 builds from November 2026
onward).

## 1. Sending — SMS channel

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

What each field buys:

| Field | Effect in Kurier |
|---|---|
| `severity` | `critical` uses the critical budget (no dedup), so a noisy normal alert can't block it |
| `dedup_key` = type + rule | repeats of one alert with changing values are recognised; rules sharing a template stay distinct. For `/v1/fire` (no rule) it renders `<type>:[?rule_id]` — stable per type |
| `idempotency_key` = delivery key **+ phone** | a Klopot retry after a timeout is replayed, not re-sent. The phone suffix is required: Klopot sends one request per recipient with the same delivery key, and Kurier rejects a key reused for a different number (422) |
| `ttl_seconds` | how long an alert may wait in Kurier's outbox |
| `max_segments` | encoding-aware truncation instead of a fixed character cut |
| `success_statuses: [409]` | a duplicate is already sent/queued — don't retry it |

Every string goes through `| jsonstr`: the rendered body must be valid
JSON or Klopot drops the SMS.

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

## Verifying

- Send: Klopot channel test → Kurier history shows `source=klopot`; a
  second identical delivery shows `replayed: true`.
- Receive: send an SMS from an authorized phone → Klopot variable
  `sms.inbox.body` updates within ~20 s; a POST without the token → 401.
- Klopot's activity log (Deliveries / Requests) records every request to
  and from Kurier with status and headers.

