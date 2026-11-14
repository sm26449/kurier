-- Extended send contract (v1.6.0): source/type labels, billed segments,
-- per-message outbox expiry and idempotency keys.
-- Run as a MySQL admin, ONCE (MySQL 8 has no ADD COLUMN IF NOT EXISTS),
-- then restart the gateway. Until then the gateway keeps working: it
-- detects the missing columns/table and falls back (logged at startup).
ALTER TABLE sms_log
  ADD COLUMN source   VARCHAR(32) NULL AFTER priority,
  ADD COLUMN type     VARCHAR(64) NULL AFTER source,
  ADD COLUMN segments SMALLINT    NULL AFTER type;

ALTER TABLE sms_outbox
  ADD COLUMN expires_at DATETIME(3) NULL AFTER next_attempt_at,
  ADD COLUMN source     VARCHAR(32) NULL AFTER priority,
  ADD COLUMN type       VARCHAR(64) NULL AFTER source;

CREATE TABLE IF NOT EXISTS sms_idempotency (
  idem_key   VARCHAR(128) NOT NULL,
  created_at DATETIME(3)  NOT NULL,
  response   JSON         NOT NULL,
  PRIMARY KEY (idem_key),
  KEY idx_sms_idempotency_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
