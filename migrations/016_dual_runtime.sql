-- First-class runtime identity for native adapters and Blackbox V2 schemes.

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_registry' AND column_name = 'runtime_type') = 0,
    'ALTER TABLE t_scheme_registry ADD COLUMN runtime_type VARCHAR(32) NOT NULL DEFAULT ''native_adapter'' AFTER task_type',
    'SELECT 1'
);
PREPARE runtime_stmt FROM @ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_versions' AND column_name = 'runtime_type') = 0,
    'ALTER TABLE t_scheme_versions ADD COLUMN runtime_type VARCHAR(32) NOT NULL DEFAULT ''native_adapter'' AFTER scheme_version',
    'SELECT 1'
);
PREPARE runtime_stmt FROM @ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'runtime_type') = 0,
    'ALTER TABLE t_scheme_runs ADD COLUMN runtime_type VARCHAR(32) NOT NULL DEFAULT ''native_adapter'' AFTER scheme_version',
    'SELECT 1'
);
PREPARE runtime_stmt FROM @ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_versions' AND column_name = 'algorithm_version') = 0,
    'ALTER TABLE t_scheme_versions ADD COLUMN algorithm_version VARCHAR(64) NULL AFTER runtime_type',
    'SELECT 1'
);
PREPARE runtime_stmt FROM @ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_versions' AND column_name = 'contract_version') = 0,
    'ALTER TABLE t_scheme_versions ADD COLUMN contract_version VARCHAR(32) NULL AFTER algorithm_version',
    'SELECT 1'
);
PREPARE runtime_stmt FROM @ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_versions' AND column_name = 'runtime_profile') = 0,
    'ALTER TABLE t_scheme_versions ADD COLUMN runtime_profile VARCHAR(64) NULL AFTER contract_version',
    'SELECT 1'
);
PREPARE runtime_stmt FROM @ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_versions' AND column_name = 'environment_fingerprint') = 0,
    'ALTER TABLE t_scheme_versions ADD COLUMN environment_fingerprint VARCHAR(64) NULL AFTER runtime_profile',
    'SELECT 1'
);
PREPARE runtime_stmt FROM @ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_versions' AND column_name = 'data_snapshot_id') = 0,
    'ALTER TABLE t_scheme_versions ADD COLUMN data_snapshot_id VARCHAR(128) NULL AFTER environment_fingerprint',
    'SELECT 1'
);
PREPARE runtime_stmt FROM @ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'data_snapshot_id') = 0,
    'ALTER TABLE t_scheme_runs ADD COLUMN data_snapshot_id VARCHAR(128) NULL AFTER input_artifact_id',
    'SELECT 1'
);
PREPARE runtime_stmt FROM @ddl;
EXECUTE runtime_stmt;
DEALLOCATE PREPARE runtime_stmt;
