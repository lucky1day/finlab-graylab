-- P1 lifecycle schema.
-- Idempotent: safe to run more than once on bond_db.

CREATE TABLE IF NOT EXISTS t_scheme_versions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL,
    scheme_version VARCHAR(64) NOT NULL,
    code_hash VARCHAR(64) NOT NULL,
    config_hash VARCHAR(64) DEFAULT NULL,
    manifest_hash VARCHAR(64) DEFAULT NULL,
    git_commit VARCHAR(64) DEFAULT NULL,
    status ENUM('draft','validated','shadow','active','paused','retired') NOT NULL DEFAULT 'draft',
    created_by VARCHAR(128) DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_by VARCHAR(128) DEFAULT NULL,
    approved_at DATETIME DEFAULT NULL,
    UNIQUE KEY uk_scheme_version (scheme_id, scheme_version),
    INDEX idx_scheme_versions_status (status),
    INDEX idx_scheme_versions_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_harness_runs (
    harness_run_id VARCHAR(128) NOT NULL PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL,
    scheme_version VARCHAR(64) DEFAULT NULL,
    stage VARCHAR(64) NOT NULL,
    status ENUM('running','passed','failed','blocked','error','skipped') NOT NULL DEFAULT 'running',
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME DEFAULT NULL,
    triggered_by VARCHAR(128) DEFAULT NULL,
    project_root VARCHAR(512) DEFAULT NULL,
    git_commit VARCHAR(64) DEFAULT NULL,
    code_hash VARCHAR(64) DEFAULT NULL,
    config_hash VARCHAR(64) DEFAULT NULL,
    report_uri VARCHAR(512) DEFAULT NULL,
    INDEX idx_harness_runs_scheme (scheme_id, started_at),
    INDEX idx_harness_runs_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_harness_gate_results (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    harness_run_id VARCHAR(128) NOT NULL,
    gate_name VARCHAR(128) NOT NULL,
    status ENUM('passed','failed','blocked','error','skipped') NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME DEFAULT NULL,
    summary_json JSON DEFAULT NULL,
    report_uri VARCHAR(512) DEFAULT NULL,
    INDEX idx_gate_results_run (harness_run_id),
    INDEX idx_gate_results_gate (gate_name, status),
    CONSTRAINT fk_gate_results_harness_run
        FOREIGN KEY (harness_run_id) REFERENCES t_harness_runs(harness_run_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_input_artifacts (
    artifact_id VARCHAR(128) NOT NULL PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL,
    scheme_version VARCHAR(64) DEFAULT NULL,
    predict_date DATE NOT NULL,
    frequency VARCHAR(32) NOT NULL,
    data_version VARCHAR(64) DEFAULT NULL,
    artifact_uri VARCHAR(512) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    schema_hash VARCHAR(64) NOT NULL,
    source_watermark VARCHAR(128) DEFAULT NULL,
    row_count INT DEFAULT NULL,
    min_date DATE DEFAULT NULL,
    max_date DATE DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_input_artifact (scheme_id, predict_date, content_hash),
    INDEX idx_input_artifacts_scheme_date (scheme_id, predict_date),
    INDEX idx_input_artifacts_hash (content_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_scheme_runs (
    run_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL,
    scheme_version VARCHAR(64) DEFAULT NULL,
    run_type ENUM('dry_run','shadow','active','manual') NOT NULL DEFAULT 'active',
    predict_date DATE NOT NULL,
    status ENUM('running','success','failed','partial','skipped') NOT NULL DEFAULT 'running',
    harness_run_id VARCHAR(128) DEFAULT NULL,
    input_artifact_id VARCHAR(128) DEFAULT NULL,
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME DEFAULT NULL,
    records_expected INT DEFAULT NULL,
    records_returned INT DEFAULT NULL,
    records_written INT DEFAULT NULL,
    error_message TEXT DEFAULT NULL,
    INDEX idx_scheme_runs_scheme_date (scheme_id, predict_date),
    INDEX idx_scheme_runs_status (status),
    INDEX idx_scheme_runs_harness (harness_run_id),
    INDEX idx_scheme_runs_artifact (input_artifact_id),
    CONSTRAINT fk_scheme_runs_harness_run
        FOREIGN KEY (harness_run_id) REFERENCES t_harness_runs(harness_run_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_scheme_runs_input_artifact
        FOREIGN KEY (input_artifact_id) REFERENCES t_input_artifacts(artifact_id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_scheme_serving_pointer (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    scheme_id VARCHAR(64) NOT NULL,
    target_tenor VARCHAR(64) NOT NULL,
    predict_date DATE NOT NULL,
    serving_run_id BIGINT NOT NULL,
    serving_status ENUM('approved','hidden','deprecated') NOT NULL DEFAULT 'approved',
    updated_by VARCHAR(128) DEFAULT NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_serving_pointer (scheme_id, target_tenor, predict_date),
    INDEX idx_serving_pointer_run (serving_run_id),
    INDEX idx_serving_pointer_status (serving_status),
    CONSTRAINT fk_serving_pointer_run
        FOREIGN KEY (serving_run_id) REFERENCES t_scheme_runs(run_id)
        ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_predictions'
          AND column_name = 'run_id'
    ) = 0,
    'ALTER TABLE t_scheme_predictions ADD COLUMN run_id BIGINT NULL AFTER id',
    'SELECT 1'
);
PREPARE lifecycle_stmt FROM @ddl;
EXECUTE lifecycle_stmt;
DEALLOCATE PREPARE lifecycle_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_predictions'
          AND column_name = 'scheme_version'
    ) = 0,
    'ALTER TABLE t_scheme_predictions ADD COLUMN scheme_version VARCHAR(64) NULL AFTER run_id',
    'SELECT 1'
);
PREPARE lifecycle_stmt FROM @ddl;
EXECUTE lifecycle_stmt;
DEALLOCATE PREPARE lifecycle_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_backtest_runs'
          AND column_name = 'backtest_run_id'
    ) = 0,
    'ALTER TABLE t_backtest_runs ADD COLUMN backtest_run_id BIGINT NULL AFTER id',
    'SELECT 1'
);
PREPARE lifecycle_stmt FROM @ddl;
EXECUTE lifecycle_stmt;
DEALLOCATE PREPARE lifecycle_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_backtest_runs'
          AND column_name = 'code_hash'
    ) = 0,
    'ALTER TABLE t_backtest_runs ADD COLUMN code_hash VARCHAR(64) NULL AFTER report_path',
    'SELECT 1'
);
PREPARE lifecycle_stmt FROM @ddl;
EXECUTE lifecycle_stmt;
DEALLOCATE PREPARE lifecycle_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_backtest_runs'
          AND column_name = 'config_hash'
    ) = 0,
    'ALTER TABLE t_backtest_runs ADD COLUMN config_hash VARCHAR(64) NULL AFTER code_hash',
    'SELECT 1'
);
PREPARE lifecycle_stmt FROM @ddl;
EXECUTE lifecycle_stmt;
DEALLOCATE PREPARE lifecycle_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_backtest_runs'
          AND column_name = 'input_artifact_hash'
    ) = 0,
    'ALTER TABLE t_backtest_runs ADD COLUMN input_artifact_hash VARCHAR(64) NULL AFTER config_hash',
    'SELECT 1'
);
PREPARE lifecycle_stmt FROM @ddl;
EXECUTE lifecycle_stmt;
DEALLOCATE PREPARE lifecycle_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_backtest_runs'
          AND column_name = 'run_mode'
    ) = 0,
    'ALTER TABLE t_backtest_runs ADD COLUMN run_mode ENUM(''no_persist'',''persist'',''reproduction'',''comparison'') NULL AFTER input_artifact_hash',
    'SELECT 1'
);
PREPARE lifecycle_stmt FROM @ddl;
EXECUTE lifecycle_stmt;
DEALLOCATE PREPARE lifecycle_stmt;
