-- Bond Factor Lab initial schema.
-- Idempotent: safe to run more than once on bond_db.

CREATE TABLE IF NOT EXISTS t_scheme_predictions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL,
    target_tenor VARCHAR(16) NOT NULL,
    horizon INT NOT NULL,
    predict_date DATE NOT NULL,
    target_date DATE NOT NULL,
    predicted_direction TINYINT NOT NULL COMMENT '1=涨, -1=跌, 0=平',
    confidence FLOAT DEFAULT NULL,
    model_version VARCHAR(64) DEFAULT NULL,
    extra JSON DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_scheme_tenor_predict (scheme_id, target_tenor, predict_date),
    INDEX idx_scheme_predict_date (scheme_id, predict_date),
    INDEX idx_target_date (target_date),
    INDEX idx_tenor_target_date (target_tenor, target_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_scheme_actuals (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenor VARCHAR(16) NOT NULL,
    trade_date DATE NOT NULL,
    close_yield DOUBLE NOT NULL,
    direction_1d TINYINT DEFAULT NULL COMMENT '1=涨, -1=跌, 0=平',
    direction_5d TINYINT DEFAULT NULL COMMENT '1=涨, -1=跌, 0=平',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_tenor_date (tenor, trade_date),
    INDEX idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_scheme_registry (
    id INT AUTO_INCREMENT PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    description TEXT,
    horizon INT NOT NULL,
    tenors JSON NOT NULL,
    frequency VARCHAR(32) NOT NULL DEFAULT 'daily',
    schedule_cron VARCHAR(64) NOT NULL,
    schedule_timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    status ENUM('active','paused','archived') NOT NULL DEFAULT 'active',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_scheme_run_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL,
    run_date DATE NOT NULL,
    status ENUM('success','failed','skipped','partial') NOT NULL,
    duration_sec FLOAT DEFAULT NULL,
    error_msg TEXT DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_scheme_run (scheme_id, run_date),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
