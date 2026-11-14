# Teltonika router

Kurier talks to a Teltonika router running **RutOS 7.x** with an
SMS-capable modem. Tested on a **RUT241** (MeigLink SLM750-VE modem, LTE).
Other RutOS routers with the same APIs should work; reports welcome.

## How Kurier talks to the router

| Operation | Interface | Call |
|---|---|---|
| Login | REST | `POST /api/login` → bearer token (renewed automatically) |
| Send SMS | REST | `POST /api/messages/actions/send` `{data: {number, message, modem}}` |
| Login | ubus JSON-RPC | `POST /ubus` `session.login` → session id |
| Inbox, read, delete | ubus `file.exec` | `/usr/sbin/gsmctl -S -l all` / `-S -r <i>` / `-S -d <i>` |
| Modem status | ubus `file.exec` | `gsmctl -E` (JSON), `-z` (SIM), `-i` (IMEI) |

Both are needed ("hybrid" mode, detected at startup and again later if
the router was down): the REST API sends SMS, but on the RUT241 its
read endpoints answer `403`, so reads go through `gsmctl` over ubus.
With REST only, sending works and inbox/status don't (`api_mode: rest`
in `/api/health`).

## Account

`TELTONIKA_USER` / `TELTONIKA_PASS` must be able to:

- use the REST API for **Messages** (send);
- open a ubus session and run **`file.exec` on `/usr/sbin/gsmctl`**.

The admin account can. A dedicated, restricted user is better practice;
make sure it keeps both permissions — if `gsmctl` isn't allowed, sending
still works but inbound SMS and modem status stay empty.

Set `TELTONIKA_MODEM` to the modem id (`1-1` on single-modem routers).

## Quirks we handle (and why)

| Behaviour | Seen on | What Kurier does |
|---|---|---|
| SMS timestamps from `gsmctl -S -l` are **UTC** (no zone in the text) | RUT241 | `SMS_MODEM_TIMEZONE=UTC` (default); a future timestamp is ignored |
| Per-value queries (`gsmctl -q`, `-o`, `-t`) answer `ERROR: Couldn't retrieve data` 25–65 % of the time | RUT241 | status comes from `gsmctl -E` (daemon JSON cache, reliable, one call); never stores an error as a value; one retry, then last good value |
| `gsmctl -E` reports temperature in tenths of °C | RUT241 | divided by 10 (`modem_temperature`) |
| Expired ubus session answers `{"result": [6]}` (one element) | RutOS | re-login once, then fail |
| Modem reuses inbox indexes after deletion | — | inbound SMS identified by sender + timestamp + text, not by index |
| SIM storage fills up (tens of messages) | — | messages deleted from the modem after archiving |
| `send` can answer HTTP 200 with `{"success": false}` | RutOS | treated as a failure |
| The send response reports billed parts (`data.sms_used`) | RutOS | recorded per SMS (`segments`) |
| Diacritics / `—` switch an SMS to UCS-2 (70 chars per part) | GSM | GSM-7 transliteration before sending |

## Check from the Docker host

```bash
curl -s http://127.0.0.1:5080/api/health          # modem_reachable, sim_ok, api_mode
curl -s -H "X-API-Key: $SMS_API_KEY" http://127.0.0.1:5080/api/sms/status
```

`api_mode: hybrid`, `sim_state: Inserted` and a signal value mean the
router side is complete.
