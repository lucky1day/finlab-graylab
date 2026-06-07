-- Weekly actuals schema.
-- Idempotent: safe to run more than once on bond_db.

CREATE TABLE IF NOT EXISTS t_scheme_weekly_actuals (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    tenor VARCHAR(64) NOT NULL,
    feature_week_id INT NOT NULL,
    target_week_id INT NOT NULL,
    predict_date DATE NOT NULL,
    feature_date DATE NOT NULL,
    target_date DATE NOT NULL,
    feature_yield DOUBLE NOT NULL,
    target_yield DOUBLE NOT NULL,
    direction_weekly TINYINT NOT NULL COMMENT '收益率方向: 1=上行/价格空, -1=下行/价格多, 0=平',
    price_signal VARCHAR(8) NOT NULL COMMENT '价格视角: 空/多/平',
    target_rule VARCHAR(128) NOT NULL,
    extra JSON DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_weekly_actual_predict (tenor, predict_date),
    INDEX idx_weekly_actual_target (tenor, target_date),
    INDEX idx_weekly_actual_week (feature_week_id, target_week_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
