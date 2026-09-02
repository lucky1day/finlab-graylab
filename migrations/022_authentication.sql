-- Independent authentication, revocable sessions and append-only account audit.

CREATE TABLE IF NOT EXISTS `t_auth_users` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `username` VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    `password_hash` VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    `role` ENUM('admin', 'user') NOT NULL,
    `status` ENUM('active', 'disabled') NOT NULL DEFAULT 'active',
    `is_protected_admin` TINYINT(1) NOT NULL DEFAULT 0,
    `must_change_password` TINYINT(1) NOT NULL DEFAULT 1,
    `failed_login_count` INT UNSIGNED NOT NULL DEFAULT 0,
    `login_not_before` DATETIME(6) NULL,
    `password_changed_at` DATETIME(6) NOT NULL,
    `created_by` BIGINT UNSIGNED NULL,
    `disabled_by` BIGINT UNSIGNED NULL,
    `disabled_at` DATETIME(6) NULL,
    `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_auth_users_username` (`username`),
    KEY `idx_auth_users_role_status_id` (`role`, `status`, `id`),
    CONSTRAINT `fk_auth_users_created_by`
        FOREIGN KEY (`created_by`) REFERENCES `t_auth_users` (`id`)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT `fk_auth_users_disabled_by`
        FOREIGN KEY (`disabled_by`) REFERENCES `t_auth_users` (`id`)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `t_auth_sessions` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `user_id` BIGINT UNSIGNED NOT NULL,
    `token_hash` BINARY(32) NOT NULL,
    `expires_at` DATETIME(6) NOT NULL,
    `revoked_at` DATETIME(6) NULL,
    `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_auth_sessions_token_hash` (`token_hash`),
    KEY `idx_auth_sessions_user_active` (`user_id`, `revoked_at`, `expires_at`),
    KEY `idx_auth_sessions_expiry` (`expires_at`, `id`),
    CONSTRAINT `fk_auth_sessions_user`
        FOREIGN KEY (`user_id`) REFERENCES `t_auth_users` (`id`)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS `t_auth_audit_logs` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `event_type` VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    `actor_user_id` BIGINT UNSIGNED NULL,
    `target_user_id` BIGINT UNSIGNED NULL,
    `request_id` VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    `detail` JSON NOT NULL,
    `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (`id`),
    KEY `idx_auth_audit_target_created` (`target_user_id`, `created_at`, `id`),
    KEY `idx_auth_audit_actor_created` (`actor_user_id`, `created_at`, `id`),
    KEY `idx_auth_audit_event_created` (`event_type`, `created_at`, `id`),
    CONSTRAINT `fk_auth_audit_actor`
        FOREIGN KEY (`actor_user_id`) REFERENCES `t_auth_users` (`id`)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT `fk_auth_audit_target`
        FOREIGN KEY (`target_user_id`) REFERENCES `t_auth_users` (`id`)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
