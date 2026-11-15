# Install — step by step

From nothing to a delivered SMS, with what you should see at each step.
About 15 minutes. For the variants (existing Traefik, external MySQL,
separate runtime directory) see [DEPLOY.md](DEPLOY.md) afterwards.

## 0. What you need

- A Linux host with **Docker** and **Compose v2** (`docker compose version`
  prints 2.x). amd64 or arm64 (a Raspberry Pi 4/5 with a 64-bit OS works).
- A **Teltonika router with RutOS 7.x** and an SMS-capable modem, reachable
  from that host (tested on a RUT241).
- A **SIM that can send SMS** (credit or a plan that includes SMS).
- The router's admin password.

## 1. Prepare the router

Do these in the router's WebUI (by default `http://192.168.1.1`). Menu names
are from Teltonika's RUT241 manual; they can differ slightly between
firmware versions.

1. **The SIM works.** *Network → Mobile*: the SIM card state is `Inserted`
   and the router is registered to the operator. If the SIM asks for a PIN,
   enter it under *Network → Mobile → General → SIM configuration → PIN*
   (the router remembers it), or turn the lock off under
   *Utilities → SIM PIN management*.
2. **The router itself can send an SMS.** *Services → Mobile Utilities →
   Messages → Send Messages*: send one to your phone. If this does not
   arrive, fix it first — Kurier uses the same modem and cannot do better.
3. **JSON-RPC is on.** Kurier sends over the router's REST API but *reads*
   (received SMS, signal, SIM state) through JSON-RPC, which is **off by
   default**: *System → Administration → Access Control → General*, edit
   the HTTP (or HTTPS) row and set **Enable JSON-RPC**. On firmware 7.18
   and newer, first install the *JSON-RPC* package from
   *System → Package Manager*. Without it Kurier still sends, but receives
   nothing and shows no modem status (`api_mode: rest`, step 5).
4. **HTTP or HTTPS.** On the same page, note which is enabled. If
   **Redirect to HTTPS** is on (or HTTP access is off), you will set
   `TELTONIKA_SCHEME=https` in step 3.
5. *(Optional)* **A separate user for Kurier**, so you can change its
   password without touching your own: *System → Administration → User
   Settings → System Users → Add New User*. Details and a caveat in
   [TELTONIKA.md](TELTONIKA.md#account).

## 2. Get Kurier

```bash
git clone https://github.com/sm26449/kurier.git
cd kurier
cp .env.example .env
chmod 600 .env
```

## 3. Configure

Generate three secrets (hex, so nothing in them needs quoting):

```bash
for name in SMS_API_KEY MYSQL_PASSWORD MYSQL_ROOT_PASSWORD; do
  echo "$name=$(openssl rand -hex 24)"
done
```

Open `.env` and set these lines (leave the rest as it is):

```
TZ=Europe/Bucharest                 # your time zone

TELTONIKA_HOST=192.168.1.1          # the router
TELTONIKA_USER=admin
TELTONIKA_PASS=<router password>
TELTONIKA_SCHEME=http               # https if the router redirects to HTTPS

SMS_API_KEY=<generated>
SMS_DEFAULT_RECIPIENTS=+40712345678 # your phone, international format; comma-separated for several
SMS_DEFAULT_COUNTRY_CODE=+40        # prefix added to numbers written as 07…

MYSQL_PASSWORD=<generated>
MYSQL_ROOT_PASSWORD=<generated>
```

Two things to know:

- A `$` in a value (a router password, for instance) must be written `$$`
  — Compose treats `$` as a variable.
- `SMS_DEFAULT_RECIPIENTS` is both *who gets an SMS sent without a number*
  and *whose incoming SMS are marked `authorized`*.

## 4. Start

```bash
docker compose up -d --build
```

(Or, without building: add `KURIER_IMAGE=ghcr.io/sm26449/kurier:<version>`
to `.env` and run `docker compose up -d --no-build`.)

This starts three containers: `kurier`, `mysql` (the schema is created on
this first start) and `backup` (a daily dump into `./backups`).

```bash
docker compose ps
```

After about a minute all three are `Up`, and `kurier` and `mysql` say
`(healthy)`.

## 5. Check

```bash
curl -s http://127.0.0.1:5080/api/health
```

```json
{"status":"healthy","modem_reachable":true,"sim_ok":true,"api_mode":"hybrid",
 "uptime_seconds":42.1,"outbox_pending":0,"outbox_oldest_age_seconds":0,"detail":null}
```

| You see | Meaning |
|---|---|
| `"status":"healthy"`, `"api_mode":"hybrid"` | everything works — continue |
| `"api_mode":"rest"` | sending works, reading doesn't: JSON-RPC is off, or this user may not use it (step 1.3) |
| `"status":"degraded"`, `"sim_ok":false` | the router answers but the SIM is not ready (step 1.1) |
| `"status":"unhealthy"`, `"modem_reachable":false` | Kurier cannot log in to the router — see [If it doesn't work](#if-it-doesnt-work) |

## 6. Send the first SMS

```bash
KEY=$(grep '^SMS_API_KEY=' .env | cut -d= -f2-)
curl -s -X POST http://127.0.0.1:5080/api/sms/send \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"message": "Hello from Kurier"}'
```

```json
[{"success":true,"number":"+40712345678","detail":null,"queued":false,"segments":1, …}]
```

`"success":true` and your phone rings. If instead you get `"queued":true`
(HTTP `202`), the router did not take the SMS; Kurier keeps it and retries
— the reason is in `detail`.

## 7. Receive one

Reply to that SMS from your phone. Within about 20 seconds:

```bash
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:5080/api/sms/inbox
```

shows it, with `"authorized":true` (your number is in
`SMS_DEFAULT_RECIPIENTS`). Kurier removes received SMS from the SIM once
they are archived, so the router's own message list stays empty — that is
intended.

## 8. The status page

Open **http://127.0.0.1:5080/ui** in a browser on the host and paste the
API key once. It shows the SMS path hop by hop, the sending budget, what
was sent and received, and has a *send test SMS* button.

From another computer the page is not reachable yet: Kurier listens on
loopback only. See the next section.

## 9. What next

| You want | Do |
|---|---|
| Other machines on the LAN to call the API or open `/ui` | `KURIER_BIND=0.0.0.0` in `.env`, `docker compose up -d`. Everything still needs the API key. Never forward this port from the internet |
| HTTPS and a hostname | [DEPLOY.md](DEPLOY.md) — own Traefik, or behind the one you already run |
| Your alerting / automation to send SMS | [INTEGRATIONS.md](INTEGRATIONS.md) (Home Assistant, Node-RED, Grafana, Uptime Kuma, scripts), contract in [API.md](API.md) |
| MQTT in and out | `MQTT_BROKER=<host>` (+ `MQTT_USER`/`MQTT_PASS`) in `.env`; read the ACL note in [SECURITY.md](../SECURITY.md) |
| To be told when the SMS path itself breaks | [OPERATIONS.md](OPERATIONS.md#signals-to-watch) |
| To upgrade later | `git pull`, then `scripts/deploy.sh <version>` — [DEPLOY.md](DEPLOY.md#upgrade) |
| No database at all | `COMPOSE_FILE=compose.yaml` and leave `MYSQL_PASSWORD` empty: everything works, but history, the queue and received SMS are lost on restart |

After changing `.env`, apply it with `docker compose up -d`.

## If it doesn't work

Start with `docker compose logs --tail 50 kurier`.

| Symptom | Cause → fix |
|---|---|
| `docker compose up` stops with `set MYSQL_PASSWORD in .env` | a required value is empty (step 3) |
| Every API call answers `503 … SMS_API_KEY is not set` | set `SMS_API_KEY`, then `docker compose up -d` |
| `401 invalid or missing X-API-Key` | the header value differs from `SMS_API_KEY` |
| Log: `Teltonika not reachable … neither REST nor ubus responded` | wrong `TELTONIKA_HOST`, wrong user/password, or the router only answers on HTTPS → `TELTONIKA_SCHEME=https`. Test from the host: `curl -sk -o /dev/null -w '%{http_code}\n' http://192.168.1.1/` (a `301`/`302` means it redirects to HTTPS) |
| `api_mode: rest`, inbox and status empty | JSON-RPC disabled or the package missing (step 1.3) |
| `400 No phone number provided` | `SMS_DEFAULT_RECIPIENTS` is empty and the request has no `number` |
| `400` invalid phone number | use `+<country><number>`, or set `SMS_DEFAULT_COUNTRY_CODE` for national numbers |
| `202`, SMS never arrives, router reachable | the router refuses (no credit, SIM, network): `curl -s -H "X-API-Key: $KEY" 127.0.0.1:5080/api/sms/outbox` shows `last_error`; repeat step 1.2 |
| `429` | the rate limit (3 per minute, 20 per hour by default) — `Retry-After` says when; limits in `.env` |
| `409` | the same text to the same number within 5 minutes was already sent |
| Port 5080 already in use | `KURIER_PORT=5081` in `.env` |

More: [OPERATIONS.md](OPERATIONS.md#troubleshooting).

## Remove

```bash
docker compose down          # stop; data stays in the mysql volume and ./backups
docker compose down -v       # also delete the database volume
```
