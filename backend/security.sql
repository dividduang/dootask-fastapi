-- New-install security state. Original business tables remain source-compatible.
-- Passwords and bearer material never occupy the source's 50-character hash column.
-- Deliberate new-install fix: preserve full IPv6 peers rather than truncate at 20.
ALTER TABLE users MODIFY last_ip VARCHAR(45) NULL DEFAULT '',
  MODIFY line_ip VARCHAR(45) NULL DEFAULT '', MODIFY created_ip VARCHAR(45) NULL DEFAULT '';
CREATE TABLE auth_credentials (
  userid BIGINT UNSIGNED NOT NULL PRIMARY KEY,
  password_hash VARCHAR(255) NOT NULL,
  generation CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  updated_at DATETIME NOT NULL,
  CONSTRAINT auth_credentials_user_fk FOREIGN KEY(userid) REFERENCES users(userid) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE auth_sessions (
  token_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  userid BIGINT UNSIGNED NOT NULL,
  device_id BIGINT UNSIGNED NOT NULL,
  generation CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  email VARCHAR(100) NOT NULL,
  expires_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL,
  revoked_at DATETIME NULL,
  INDEX auth_sessions_user(userid, revoked_at),
  CONSTRAINT auth_sessions_user_fk FOREIGN KEY(userid) REFERENCES users(userid) ON DELETE CASCADE,
  CONSTRAINT auth_sessions_device_fk FOREIGN KEY(device_id) REFERENCES user_devices(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE auth_pgp_keys (
  key_id VARCHAR(160) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  public_key TEXT NOT NULL,
  private_cipher MEDIUMBLOB NOT NULL,
  expires_at DATETIME NOT NULL,
  INDEX auth_pgp_expiry(expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE auth_limits (
  scope CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  expires_at DATETIME NOT NULL,
  INDEX auth_limits_expiry(expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE auth_captchas (
  captcha_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  answer_digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  expires_at DATETIME NOT NULL,
  INDEX auth_captcha_expiry(expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
