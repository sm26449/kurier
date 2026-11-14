-- Send history (v1.2.1).
--
-- Migrations run in order against the gateway's database (MYSQL_DATABASE):
--   mysql -u root -p <database> < migrations/001_sms_log.sql   (then 002, 003, …)
-- compose.mysql.yaml applies them automatically on the first start.
-- The gateway's MySQL user needs only SELECT, INSERT, UPDATE, DELETE:
--   CREATE USER 'sms_gateway'@'%' IDENTIFIED BY '…';
--   GRANT SELECT, INSERT, UPDATE, DELETE ON <database>.* TO 'sms_gateway'@'%';

CREATE TABLE IF NOT EXISTS sms_log (
  id       BIGINT NOT NULL AUTO_INCREMENT,
  ts       DATETIME(3) NOT NULL,
  number   VARCHAR(32) NOT NULL,
  message  TEXT NOT NULL,
  priority VARCHAR(16) NOT NULL,
  success  TINYINT(1) NOT NULL,
  detail   TEXT,
  PRIMARY KEY (id),
  KEY idx_sms_log_ts (ts DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
