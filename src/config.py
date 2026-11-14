from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Teltonika RUT241
    teltonika_host: str = "192.168.1.1"
    teltonika_user: str = "admin"
    teltonika_pass: str = ""
    teltonika_modem: str = "1-1"

    # MQTT
    mqtt_broker: str = "mosquitto"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_pass: str = ""

    # SMS rate limiting
    sms_rate_limit_hour: int = 20
    sms_rate_limit_minute: int = 3
    sms_dedup_seconds: int = 300
    # Hard ceiling for priority=critical (which bypasses the normal
    # limits and dedup) so a buggy caller can't drain the SIM credit.
    sms_rate_limit_critical_hour: int = 30

    # Text: transliterate to GSM-7 (ăâîșț→aaist, —→-, …) so a message
    # costs 160/153-char parts instead of 70/67 (UCS-2). Cap the parts
    # per message (0 = no cap); a request's max_segments overrides.
    sms_gsm7_transliterate: bool = True
    sms_max_segments: int = 0

    # Idempotency keys are remembered this long (memory + MySQL).
    sms_idempotency_ttl_seconds: int = 86400

    # Inbound SMS (modem → archive → MQTT sms-gateway/inbox + optional webhook).
    sms_inbox_poll_seconds: int = 20
    sms_inbox_delete_after_archive: bool = True
    # Delivery window, measured from when the SMS was sent: a message is
    # delivered (MQTT + webhook) only while younger than this; older →
    # marked stale, kept in the archive/API, never delivered — so nothing
    # acts on an old command, whatever the outage was (modem, broker, webhook).
    sms_inbox_stale_seconds: int = 600
    sms_inbox_retention_days: int = 90
    sms_inbox_webhook_url: str = ""          # e.g. Klopot /v1/inputs/{id}/webhook
    sms_inbox_webhook_token: str = ""
    sms_inbox_webhook_auth_header: str = "X-Input-Token"  # or "Authorization" (Bearer)
    # gsmctl prints SMS timestamps in UTC on the RUT241 (verified 2026-11-14:
    # "Sat Nov 14 20:01:19 2026" for an SMS sent at 22:01 EET).
    sms_modem_timezone: str = "UTC"

    # Outbox: SMS that can't reach the router are accepted (HTTP 202),
    # stored, and retried with exponential backoff until delivered or
    # older than the TTL. Durable when MySQL + table sms_outbox exist.
    sms_outbox_enabled: bool = True
    sms_outbox_ttl_seconds: int = 6 * 3600
    sms_outbox_poll_seconds: int = 30
    sms_outbox_max_backoff_seconds: int = 600

    # Housekeeping (daily): drop old send history and finished outbox rows.
    # Daily mysqldump backups keep the long-term record.
    sms_history_retention_days: int = 90
    sms_outbox_retention_days: int = 30
    sms_default_recipients: str = ""

    # API auth — when set, /api/sms/* requires header `X-API-Key`.
    # Empty (default) keeps the legacy unauthenticated mode for
    # backwards compat with deployments that haven't configured a key
    # yet. The operator-facing docs recommend setting this since the
    # port (5080) is published to the host network.
    sms_api_key: str = ""

    # Default country dial code for phone normalization. E.164 numbers
    # with explicit ``+`` pass through; bare national-format numbers
    # like ``0722...`` get this prefix replacing the leading 0.
    sms_default_country_code: str = "+40"

    # Persistent storage (MySQL): send history, outbox, idempotency keys,
    # inbound archive. Schema in migrations/. Empty MYSQL_PASSWORD (or
    # sms_history_enabled=False) = in-memory only, nothing survives a restart.
    sms_history_enabled: bool = True
    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "sms_gateway"
    mysql_password: str = ""
    mysql_database: str = "sms_gateway"

    # App
    log_level: str = "INFO"
    tz: str = "UTC"  # TZ env — local days for statistics (e.g. Europe/Bucharest)

    @property
    def default_recipients_list(self) -> list[str]:
        if not self.sms_default_recipients:
            return []
        return [r.strip() for r in self.sms_default_recipients.split(",") if r.strip()]

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
