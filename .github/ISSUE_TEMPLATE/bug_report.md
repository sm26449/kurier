---
name: Bug report
about: Something does not work as documented
labels: bug
---

**What happened / what you expected**

**How to reproduce** (request, config that matters — no secrets, no real phone numbers)

**Environment**

- Kurier version (`curl -s 127.0.0.1:5080/`):
- How it runs (compose files in `COMPOSE_FILE`, published image or local build):
- Router model and RutOS version:
- MySQL: bundled / external / none

**Health and logs**

```
curl -s 127.0.0.1:5080/api/health
docker compose logs --tail 50 kurier
```

Security problems: please do **not** open an issue — see SECURITY.md.
