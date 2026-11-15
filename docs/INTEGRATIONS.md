# Integrations — sending SMS from your tools

Everything here calls one endpoint:

```
POST http://<kurier>:5080/api/sms/send
X-API-Key: <SMS_API_KEY>
Content-Type: application/json

{"message": "text"}
```

Only `message` is required and **unknown JSON fields are ignored**, so a
tool whose webhook body already has a `message` field works unchanged.
Without `number`, the SMS goes to `SMS_DEFAULT_RECIPIENTS`. The fields
worth adding when the tool lets you shape the body:

| Field | Why |
|---|---|
| `severity: "critical"` | uses the separate critical budget, so a noisy normal alert can't block it |
| `dedup_key` | a stable name for the alert (`disk_full_nas`): repeats within 5 minutes are dropped even if the text changes |
| `source`, `type` | labels shown in the history and statistics |
| `max_segments` | cap the cost of long texts (or set `SMS_MAX_SEGMENTS` once for every sender) |

Answers: `200` sent · `202` queued (router unreachable, Kurier retries) ·
`409` duplicate · `429` rate-limited. Treat `202` and `409` as success in
tools that let you choose. Full contract: [API.md](API.md).

`<kurier>` is `kurier` for containers on the same Docker network, the
host's LAN address for other machines (`KURIER_BIND=0.0.0.0`), or your
HTTPS hostname ([DEPLOY.md](DEPLOY.md)).

The snippets for third-party tools follow each tool's documentation at the
time of writing. The shell and Python ones, and the request bodies the
other tools produce, were sent to a running Kurier; the tools themselves
were not set up for this guide. Corrections welcome.

## Shell / cron

```bash
#!/bin/sh
# sms "text" [critical]
curl -fsS -m 20 -X POST "http://127.0.0.1:5080/api/sms/send" \
  -H "X-API-Key: $KURIER_API_KEY" -H 'Content-Type: application/json' \
  -d "$(jq -n --arg m "$1" --arg s "${2:-info}" \
        '{message: $m, severity: $s, source: "cron"}')"
```

`jq` builds the JSON so quotes and newlines in the text can't break it.

## Python

```python
import httpx

API_KEY = "…"  # SMS_API_KEY

def sms(text: str, *, critical: bool = False, dedup_key: str | None = None) -> None:
    r = httpx.post(
        "http://127.0.0.1:5080/api/sms/send",
        headers={"X-API-Key": API_KEY},
        json={"message": text,
              "severity": "critical" if critical else "info",
              "dedup_key": dedup_key, "source": "my-script"},
        timeout=20,
    )
    if r.status_code not in (200, 202, 409):   # sent, queued, duplicate
        r.raise_for_status()
```

## MQTT

With `MQTT_BROKER` set, the same JSON on topic `sms-gateway/send` sends an
SMS — no API key, so restrict the topic with broker ACLs
([SECURITY.md](../SECURITY.md)). Outcomes come back on `sms-gateway/sent`.

```bash
mosquitto_pub -h mosquitto -t sms-gateway/send \
  -m '{"message": "Pump stopped", "severity": "critical", "dedup_key": "pump_stopped"}'
```

## Home Assistant

`configuration.yaml` (key in `secrets.yaml` as `kurier_api_key`):

```yaml
rest_command:
  kurier_sms:
    url: http://<kurier>:5080/api/sms/send
    method: post
    content_type: application/json
    headers:
      X-API-Key: !secret kurier_api_key
    payload: >-
      {"message": {{ message | to_json }},
       "severity": {{ severity | default("info") | to_json }},
       "source": "home-assistant"}
```

In an automation:

```yaml
action:
  - action: rest_command.kurier_sms
    data:
      message: "Freezer door open for 10 minutes"
      severity: critical
```

`to_json` quotes and escapes the text. Received SMS reach Home Assistant
over MQTT: an MQTT trigger on `sms-gateway/inbox`, with a condition on
`trigger.payload_json.authorized`.

## Node-RED

A **function** node in front of an **http request** node (method *set by
msg.method*, URL empty, return *a parsed JSON object*):

```js
msg.method = "POST";
msg.url = "http://<kurier>:5080/api/sms/send";
msg.headers = { "X-API-Key": env.get("KURIER_API_KEY") };
msg.payload = { message: String(msg.payload), source: "node-red" };
return msg;
```

An object payload is sent as JSON. `msg.statusCode` afterwards is 200, 202,
409 or 429. With MQTT enabled, an **mqtt out** node on `sms-gateway/send`
with the same object does the same without the key.

## Grafana Alerting

A **Webhook** contact point. Grafana's default body already carries a
top-level `message`, so no template is needed:

- URL: `http://<kurier>:5080/api/sms/send`, method `POST`
- *Extra Headers*: `X-API-Key` = your key
- *Message*: keep it short — it is the SMS text, e.g.
  `{{ .CommonAnnotations.summary }}`

Provisioned:

```yaml
apiVersion: 1
contactPoints:
  - orgId: 1
    name: kurier-sms
    receivers:
      - uid: kurier_sms
        type: webhook
        settings:
          url: http://<kurier>:5080/api/sms/send
          httpMethod: POST
          headers:
            X-API-Key: <key>
          message: '{{ .CommonAnnotations.summary }}'
```

Grafana's default message is long (labels, links): set
`SMS_MAX_SEGMENTS=2` in Kurier or write your own *Message*. Recent Grafana
versions also offer a *Custom Payload* if you want `severity` and
`dedup_key` in the body. Route "the SMS path is broken" alerts to a
contact point that is **not** SMS.

## Uptime Kuma

Notification type **Webhook**:

- Post URL: `http://<kurier>:5080/api/sms/send`
- Request Body: **Custom Body**
  ```
  {"message": {{ msg | json }}, "source": "uptime-kuma", "type": "{{ name }}"}
  ```
- Additional Headers:
  ```json
  {"X-API-Key": "<key>"}
  ```

The default body has no `message` field, hence the custom one.

## Prometheus Alertmanager

The default webhook payload has no `message` field. Alertmanager 0.32+
can template the body (marked experimental in its documentation):

```yaml
receivers:
  - name: kurier
    webhook_configs:
      - url: http://<kurier>:5080/api/sms/send
        http_config:
          http_headers:
            X-API-Key:
              secrets: ["<key>"]
        payload:
          message: '{{ .CommonAnnotations.summary }}'
          severity: '{{ .CommonLabels.severity }}'
          source: alertmanager
```

On older versions put a small adapter between the two, or let Grafana or
Klopot do the notifying.

## Klopot

Send, receive and monitor, with idempotent retries:
[INTEGRATION_KLOPOT.md](INTEGRATION_KLOPOT.md).

## Receiving SMS in your tools

| Path | Use |
|---|---|
| MQTT `sms-gateway/inbox` | any MQTT consumer (Home Assistant, Node-RED) |
| Webhook `SMS_INBOX_WEBHOOK_URL` | an HTTP endpoint of yours; token sent in `SMS_INBOX_WEBHOOK_AUTH_HEADER`; retried until it answers `2xx` |
| `GET /api/sms/inbox` | polling, or looking at the archive |

Act on a received SMS only when `authorized` is `true`, and for anything
that changes state confirm with a code sent back to the number — sender
ids can be forged. Payload: [API.md](API.md#receive--modem--mqtt--webhook).
