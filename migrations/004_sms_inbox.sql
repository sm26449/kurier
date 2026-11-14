-- Inbound SMS archive (v1.7.0). Every message read from the modem is
-- stored once (unique fingerprint), delivered once to MQTT
-- sms-gateway/inbox and to the optional webhook (SMS_INBOX_WEBHOOK_URL),
-- then deleted from the modem (SMS_INBOX_DELETE_AFTER_ARCHIVE).
-- Run as a MySQL admin, then restart the gateway.
CREATE TABLE IF NOT EXISTS sms_inbox (
  id           BIGINT NOT NULL AUTO_INCREMENT,
  fingerprint  CHAR(32) NOT NULL,              -- sha256(sender|modem_date|text)[:32]
  received_at  DATETIME(3) NOT NULL,           -- when the gateway archived it (UTC)
  modem_date   DATETIME(3) NULL,               -- modem timestamp (UTC), NULL if unparsable
  modem_date_raw VARCHAR(40) NULL,
  sender       VARCHAR(32) NOT NULL,
  message      TEXT NOT NULL,
  authorized   TINYINT(1) NOT NULL,            -- sender in SMS_DEFAULT_RECIPIENTS
  stale        TINYINT(1) NOT NULL,            -- older than SMS_INBOX_STALE_SECONDS when archived
  published_at DATETIME(3) NULL,               -- MQTT publish confirmed
  forwarded_at DATETIME(3) NULL,               -- webhook POST answered 2xx
  forward_attempts INT NOT NULL DEFAULT 0,
  forward_error VARCHAR(255) NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_sms_inbox_fingerprint (fingerprint),
  KEY idx_sms_inbox_received (received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
