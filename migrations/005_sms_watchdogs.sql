-- Watchdogs (v1.15.0, docs/adr/0001-watchdogs.md): a service pings
-- POST /api/watchdogs/{name}/ping; no ping within timeout_sec, or a ping
-- with status "fail", sends an SMS. The ping is the registration, so a
-- lost row heals on the next ping.
-- Run as a MySQL admin, then restart the gateway.
CREATE TABLE IF NOT EXISTS sms_watchdogs (
  name          VARCHAR(64)  NOT NULL,          -- [a-z0-9][a-z0-9._-]{0,63}
  timeout_sec   INT          NOT NULL,          -- 60 … 604800
  recipients    VARCHAR(512) NULL,              -- comma-separated E.164; NULL = SMS_WATCHDOG_TO
  state         VARCHAR(8)   NOT NULL,          -- up | down
  last_ping     DATETIME(3)  NOT NULL,          -- UTC
  last_status   VARCHAR(8)   NOT NULL,          -- ok | fail (of the last ping)
  detail        VARCHAR(160) NULL,              -- of the last ping
  down_since    DATETIME(3)  NULL,
  down_reason   VARCHAR(8)   NULL,              -- timeout | fail
  down_sms_at   DATETIME(3)  NULL,              -- last down SMS attempt (30 min flapping limit)
  down_sms_sent TINYINT(1)   NOT NULL DEFAULT 0, -- this outage's down SMS went out
  created_at    DATETIME(3)  NOT NULL,
  PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
