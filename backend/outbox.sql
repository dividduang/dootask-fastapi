CREATE TABLE backend_outbox (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 kind VARCHAR(120) NOT NULL,
 payload LONGTEXT NOT NULL,
 dedupe_key VARCHAR(191) NULL,
 available_at DATETIME(6) NOT NULL,
 attempts INT UNSIGNED NOT NULL DEFAULT 0,
 completed_at DATETIME(6) NULL,
 last_error TEXT NULL,
 created_at DATETIME(6) NOT NULL,
 UNIQUE KEY outbox_dedupe (dedupe_key),
 KEY outbox_ready (completed_at,available_at,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE backend_deliveries (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 event_id BIGINT UNSIGNED NOT NULL,
 userid BIGINT UNSIGNED NOT NULL,
 fd VARCHAR(80) NOT NULL DEFAULT '',
 reconnect TINYINT NOT NULL DEFAULT 1,
 msg_id VARCHAR(80) NOT NULL,
 payload LONGTEXT NOT NULL,
 scope LONGTEXT NULL,
 ignore_fds TEXT NOT NULL,
 acknowledged_at DATETIME(6) NULL,
 transport_sent_at DATETIME(6) NULL,
 discarded_at DATETIME(6) NULL,
 discard_reason VARCHAR(32) NULL,
 expires_at DATETIME(6) NOT NULL,
 created_at DATETIME(6) NOT NULL,
 UNIQUE KEY delivery_recipient (event_id,userid,fd),
 UNIQUE KEY delivery_msg (msg_id),
 KEY delivery_pending (userid,transport_sent_at,expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE backend_operations (
 request_id VARCHAR(80) NOT NULL PRIMARY KEY,
 userid BIGINT UNSIGNED NOT NULL,
 fd VARCHAR(80) NOT NULL,
 result LONGTEXT NULL,
 expires_at DATETIME(6) NOT NULL,
 created_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
