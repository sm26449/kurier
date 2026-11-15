# Contributing to Kurier

Thanks for picking this up. Kurier is small on purpose: it **transports**
SMS between your services and a Teltonika router, in both directions, and
never interprets the text. Please keep it that way — alert logic, bots and
commands belong in the producer/consumer (e.g. Klopot).

## Dev setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

Or without a local Python, in the image itself:

```bash
docker build -t kurier-dev .
docker run --rm -u 0 -v "$PWD":/app -w /app kurier-dev \
  sh -c "pip install -q -r requirements-dev.txt && pytest -q"
```

Tests use fakes for the router (`httpx.MockTransport`), MQTT and storage;
no hardware needed. MySQL-specific code is exercised against a throwaway
`mysql:8.4` container (see the integration scripts described in
docs/OPERATIONS.md, "Testing against MySQL").

## Rules of thumb

- **Contracts are stable.** API paths (`/api/sms/*`), MQTT topics
  (`sms-gateway/*`), environment variables (`SMS_*`) and table names are
  used by deployed clients. Add, don't rename; optional fields only.
- **An alert must never be lost to metadata.** Optional request fields are
  sanitized or clamped, never a 422 (docs/API.md).
- **Health is data.** `/api/health` always answers 200; state is in the body.
- **Schema changes** ship as a new numbered file in `migrations/` and the
  code must keep working before it is applied (probe columns at startup —
  see `storage.py`). `scripts/migrate.sh` applies each file once, by name:
  never edit a released migration, add a new one.
- **Untrusted text in the UI** goes through `textContent`, never HTML;
  no inline script or style (CSP). `tests/test_ui.py` enforces it.
- **UI text** lives in `src/ui/i18n.js`, English and Romanian with the
  same keys (tested); never hard-code a sentence in `app.js`.
- Every behaviour change ships with a test; every user-visible change gets
  a CHANGELOG entry.

## Commits

Conventional Commits in English: `type(scope): description`
(`feat`, `fix`, `docs`, `refactor`, `test`, `build`, `chore`).

