CREATE TABLE `account_picture_uploads` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `userid` BIGINT UNSIGNED NOT NULL,
  `month` CHAR(6) NOT NULL,
  `name` VARCHAR(255) NOT NULL,
  `download_key` CHAR(64) NOT NULL,
  `created_at` DATETIME NOT NULL,
  PRIMARY KEY (`id`),
  KEY `account_picture_owner_month` (`userid`, `month`),
  UNIQUE KEY `account_picture_download_key` (`download_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Transactional singleton: serializes identity creation and permanently records bootstrap.
CREATE TABLE `account_identity_state` (
  `id` TINYINT UNSIGNED NOT NULL,
  `owner_userid` BIGINT UNSIGNED NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;
INSERT INTO `account_identity_state` (`id`, `owner_userid`) VALUES (1, NULL);
