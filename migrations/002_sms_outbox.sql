-- Durable outbox (v1.4.0): SMS accepted while the router was unreachable,
-- retried with backoff until delivered or expired (SMS_OUTBOX_TTL_SECONDS).
-- Run as a MySQL admin (sms_gateway has no CREATE grant), then restart
-- the gateway so it switches from the in-memory outbox to this table.
CREATE TABLE IF NOT EXISTS sms_outbox (
  id              BIGINT NOT NULL AUTO_INCREMENT,
  created_at      DATETIME(3) NOT NULL,
  updated_at      DATETIME(3) NOT NULL,
  number          VARCHAR(32) NOT NULL,
  message         TEXT NOT NULL,
  priority        VARCHAR(16) NOT NULL,
  dedup_key       CHAR(16) NOT NULL,
  status          VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending | sent | expired | cancelled
  attempts        INT NOT NULL DEFAULT 0,
  next_attempt_at DATETIME(3) NOT NULL,
  last_error      TEXT,
  PRIMARY KEY (id),
  KEY idx_sms_outbox_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
