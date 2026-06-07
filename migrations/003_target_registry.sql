-- Y target registry for display labels and future cross-asset targets.
-- Idempotent: safe to run more than once on bond_db.

CREATE TABLE IF NOT EXISTS t_target_registry (
    target_code VARCHAR(64) NOT NULL PRIMARY KEY,
    display_name VARCHAR(128) NOT NULL,
    asset_class VARCHAR(64) NOT NULL DEFAULT 'bond',
    target_type VARCHAR(64) NOT NULL DEFAULT 'active_treasury',
    sort_order INT NOT NULL DEFAULT 0,
    status ENUM('active','paused','archived') NOT NULL DEFAULT 'active',
    extra JSON DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_target_registry_status_sort (status, sort_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

ALTER TABLE t_scheme_predictions
    MODIFY target_tenor VARCHAR(64) NOT NULL;

ALTER TABLE t_scheme_actuals
    MODIFY tenor VARCHAR(64) NOT NULL;

ALTER TABLE t_backtest_predictions
    MODIFY target_tenor VARCHAR(64) NOT NULL;

ALTER TABLE t_backtest_monthly_metrics
    MODIFY target_tenor VARCHAR(64) NOT NULL;

INSERT INTO t_target_registry
    (target_code, display_name, asset_class, target_type, sort_order, status, extra)
VALUES
    ('3Y', '3Y国债活跃', 'bond', 'active_treasury', 30, 'active', JSON_OBJECT('legacy_tenor', '3Y')),
    ('5Y', '5Y国债活跃', 'bond', 'active_treasury', 50, 'active', JSON_OBJECT('legacy_tenor', '5Y')),
    ('7Y', '7Y国债活跃', 'bond', 'active_treasury', 70, 'active', JSON_OBJECT('legacy_tenor', '7Y')),
    ('10Y', '10Y国债活跃', 'bond', 'active_treasury', 100, 'active', JSON_OBJECT('legacy_tenor', '10Y'))
ON DUPLICATE KEY UPDATE
    display_name = VALUES(display_name),
    asset_class = VALUES(asset_class),
    target_type = VALUES(target_type),
    sort_order = VALUES(sort_order),
    status = VALUES(status),
    extra = VALUES(extra),
    updated_at = CURRENT_TIMESTAMP;
