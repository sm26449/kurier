# ADR-0001 — Watchdogs: an SMS when a service stops checking in

Status: Accepted
Date: 2026-11-17
Amended: 2026-11-17 — the table is `sms_watchdogs` (Kurier's `sms_*` table names), migration 005

## Context

Kurier sends SMS over the router's LTE modem. That makes it the one alert path
that still works when the internet is down, and the one that does not depend
on the notification service (Klopot) that normally decides what to send. If
Klopot stops — process dead, container stopped, rule engine stuck — nothing
tells anyone: Klopot cannot report its own outage through itself.

The same need comes up elsewhere: a nightly backup that silently stopped
running, a gateway that went quiet. A *dead man's switch* covers all of them:
the service checks in regularly, and the silence is the alarm.

Kurier must stay a small, reliable SMS transport. Whatever it gains here has
to be generic (any service, not only Klopot), passive (it does not probe
anything itself) and cheap to call (one HTTP request, from `curl` in a cron
job as well as from Klopot).

## Decision

**Two endpoints**, under the existing `X-API-Key` (`SMS_API_KEY`):

```
POST   /api/watchdogs/{name}/ping
       {"timeout_sec": 180, "status": "ok" | "fail", "detail": "…",
        "to": ["+40…"]}
  → 200 {"kurier": "ok",
         "modem": {"registered": true, "signal_strength": -71, "sim_state": "ok", "operator": "…"},
         "watchdog": {"state": "up" | "down", "last_ping": "…"}}

DELETE /api/watchdogs/{name}      planned stop or maintenance: forget it, no alert
```

- **The ping is the registration.** The first ping creates the watchdog; every
  ping stores its settings (`timeout_sec`, `to`). Clients keep no state and
  nothing drifts; a lost Kurier database heals on the next ping.
- `name`: `[a-z0-9][a-z0-9._-]{0,63}`. `timeout_sec`: 60 … 7 days.
  `status` defaults to `ok`; `detail` ≤ 160 characters. `to` is optional — the
  default recipients are `SMS_WATCHDOG_TO` (Kurier's config), so a client
  that only checks in needs to know no phone numbers. At most
  `SMS_WATCHDOG_MAX` (100) watchdogs.
- **States**, checked every 15 s:

  | Event | Transition | SMS |
  |-------|------------|-----|
  | no ping for `timeout_sec` | up → down | "⚠ {name}: no check-in since {time}" |
  | ping with `status: fail` | up → down at once | "⚠ {name}: {detail}" |
  | `ok` ping while down | down → up | "✅ {name} is back", only if the down SMS went out |

  At most one down SMS per watchdog every 30 minutes (flapping). After a
  Kurier restart every timer starts from the restart, so a restart never
  alerts everyone at once.
- **Watchdog SMS go through the normal send path** — priority `critical`,
  outbox, rate limits, history — so they are visible on the status page and
  counted like any other SMS. The status page lists the watchdogs and their
  state.
- **Passive only.** Kurier never probes a service (no HTTP checks, pings or
  ports).

## Consequences

- Any service gets an SMS watchdog with one line:
  `curl -X POST -H "X-API-Key: …" …/api/watchdogs/backup-nightly/ping -d '{"timeout_sec":90000}'`.
- Klopot uses it from its `kurier` SMS provider (Klopot ADR-0003): a ping
  every 60 s with its own health in `status`, and Kurier's modem state back
  in the response — the two services watch each other with one request.
- New storage: one table (`watchdogs`: name, timeout, recipients, state,
  last ping, last SMS) and a migration; the 15 s check loop runs with the
  existing background tasks. Docs: `docs/API.md`, `docs/INTEGRATION_KLOPOT.md`,
  the status page.
- Kurier still dies with its host. A watcher outside the host (the router
  itself, or an external service) is a separate decision.

## Alternatives

- **A persistent TCP/WebSocket link with Klopot** — reconnects, half-open
  connections and keep-alives to get right, and usable by Klopot only. A
  periodic HTTP ping says the same with none of it.
- **Kurier probing services (HTTP checks)** — turns an SMS transport into a
  monitoring system, with its configuration and false positives. Out of scope.
- **Separate register + ping calls** — two calls and state to keep in sync;
  a self-registering ping is simpler and heals itself.
- **Building Kurier into Klopot** — removes exactly the independence this
  needs: Klopot down would mean SMS down.
