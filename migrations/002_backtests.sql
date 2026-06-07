-- Historical benchmark reproduction schema.
-- Idempotent: safe to run more than once on bond_db.

CREATE TABLE IF NOT EXISTS t_backtest_runs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    benchmark_id VARCHAR(128) NOT NULL,
    scheme_id VARCHAR(64) NOT NULL,
    data_source VARCHAR(64) NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    status ENUM('running','success','failed','partial') NOT NULL DEFAULT 'running',
    summary JSON DEFAULT NULL,
    report_path VARCHAR(512) DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_backtest_run_scope (benchmark_id, scheme_id, data_source, start_date, end_date),
    INDEX idx_backtest_runs_scheme (scheme_id, data_source),
    INDEX idx_backtest_runs_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_backtest_predictions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT NOT NULL,
    benchmark_id VARCHAR(128) NOT NULL,
    scheme_id VARCHAR(64) NOT NULL,
    target_tenor VARCHAR(16) NOT NULL,
    horizon INT NOT NULL,
    predict_date DATE NOT NULL,
    feature_date DATE DEFAULT NULL,
    target_date DATE DEFAULT NULL,
    label TINYINT DEFAULT NULL,
    predicted_direction TINYINT DEFAULT NULL,
    model_pred TINYINT DEFAULT NULL,
    confidence DOUBLE DEFAULT NULL,
    source_row JSON DEFAULT NULL,
    extra JSON DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_backtest_prediction (run_id, target_tenor, predict_date),
    INDEX idx_backtest_predictions_scope (scheme_id, target_tenor, predict_date),
    INDEX idx_backtest_predictions_run (run_id),
    CONSTRAINT fk_backtest_predictions_run
        FOREIGN KEY (run_id) REFERENCES t_backtest_runs(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_backtest_monthly_metrics (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT NOT NULL,
    benchmark_id VARCHAR(128) NOT NULL,
    scheme_id VARCHAR(64) NOT NULL,
    target_tenor VARCHAR(16) NOT NULL,
    horizon INT NOT NULL,
    month CHAR(7) NOT NULL,
    sample_count INT NOT NULL DEFAULT 0,
    correct_count INT NOT NULL DEFAULT 0,
    accuracy DOUBLE DEFAULT NULL,
    up_precision DOUBLE DEFAULT NULL,
    up_recall DOUBLE DEFAULT NULL,
    down_precision DOUBLE DEFAULT NULL,
    down_recall DOUBLE DEFAULT NULL,
    actual_dist JSON DEFAULT NULL,
    predicted_dist JSON DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_backtest_monthly (run_id, target_tenor, month),
    INDEX idx_backtest_monthly_scope (scheme_id, target_tenor, month),
    CONSTRAINT fk_backtest_monthly_run
        FOREIGN KEY (run_id) REFERENCES t_backtest_runs(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_backtest_reproduction_checks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    benchmark_id VARCHAR(128) NOT NULL,
    check_name VARCHAR(128) NOT NULL,
    status ENUM('success','failed','partial') NOT NULL,
    source_path VARCHAR(512) DEFAULT NULL,
    row_count_csv INT DEFAULT NULL,
    row_count_db INT DEFAULT NULL,
    col_count_csv INT DEFAULT NULL,
    col_count_db INT DEFAULT NULL,
    date_min_csv DATE DEFAULT NULL,
    date_max_csv DATE DEFAULT NULL,
    date_min_db DATE DEFAULT NULL,
    date_max_db DATE DEFAULT NULL,
    csv_only_columns JSON DEFAULT NULL,
    db_only_columns JSON DEFAULT NULL,
    target_max_abs_diff JSON DEFAULT NULL,
    overall_max_abs_diff DOUBLE DEFAULT NULL,
    missing_diff_count BIGINT DEFAULT NULL,
    first_diff JSON DEFAULT NULL,
    report JSON DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_reproduction_check (benchmark_id, check_name, created_at),
    INDEX idx_reproduction_check_name (benchmark_id, check_name),
    INDEX idx_reproduction_check_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
