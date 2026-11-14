# Security Policy

Kurier can send SMS on your SIM card. Treat its API like a payment
endpoint: keep it off the internet, require an API key, and limit who
can reach it.

## Supported versions

Security fixes are provided for the **latest released minor version** only.

| Version | Supported |
|---------|-----------|
| latest minor | ✅ |
| older | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report privately through GitHub's **[private vulnerability reporting][gh]**
(the *Report a vulnerability* button under the repository's **Security**
tab). That channel is confidential and reaches the maintainer directly.

Please include a description of the issue and its impact, the affected
version or commit, and the minimal steps to reproduce it. You will get an
acknowledgement within a few days; once a fix is ready we will agree on a
disclosure timeline and credit you in the release notes unless you prefer
to stay anonymous. Do not include working exploits, credentials or real
phone numbers beyond what is needed to demonstrate the issue.

[gh]: https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability

## Hardening checklist

- **Set `SMS_API_KEY`.** Without it, anyone who reaches port 5080 can send SMS.
- **Don't expose it publicly.** Bind the port to loopback (default) or put it
  behind a proxy with an IP allowlist (`compose.proxy.yaml`,
  `compose.traefik.yaml` — private ranges by default). A LAN-only hostname
  with a DNS-01 certificate gives HTTPS without any public exposure.
- **Use a dedicated router account** for `TELTONIKA_USER` (docs/TELTONIKA.md),
  not the admin account, and a strong password.
- **Inbound SMS are untrusted input.** Sender ids can be spoofed. Consumers
  acting on commands must require `authorized: true`, and for anything that
  changes state, confirm with a one-time code sent back to the number.
- **MQTT can send SMS too.** Anyone allowed to publish on
  `sms-gateway/send` sends SMS without the API key. Restrict that topic with
  broker ACLs to the clients that need it (or don't give the gateway MQTT
  credentials that can subscribe to it, if you only use REST).
- **The MySQL user needs only** `SELECT, INSERT, UPDATE, DELETE`.
- The container runs as an unprivileged user (UID 10001) with
  `no-new-privileges`.
- Rate limits (`SMS_RATE_LIMIT_*`) cap the damage of a buggy or hostile
  client; keep them.

## What the status page does and doesn't do

`/ui` is static (strict CSP, no third-party resources, no inline script).
It calls the same API with the key you enter; the key stays in that
browser's `localStorage`. Received SMS text is rendered as text only.
