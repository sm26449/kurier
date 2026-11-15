**What and why**

**Checklist**

- [ ] `pytest -q` passes; a behaviour change has a test
- [ ] CHANGELOG entry for anything a user would notice
- [ ] Wire contracts unchanged (`/api/sms/*`, `sms-gateway/*`, `SMS_*`, table names) — additions only
- [ ] Schema change = a new numbered file in `migrations/`, and the code still starts before it is applied
- [ ] UI text added in both languages in `src/ui/i18n.js`
