-- Prediction run_id uniqueness switch.
-- Idempotent: safe to run more than once on bond_db.

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_run_log'
          AND column_name = 'run_id'
    ) = 0,
    'ALTER TABLE t_scheme_run_log ADD COLUMN run_id BIGINT NULL AFTER id',
    'SELECT 1'
);
PREPARE prediction_runid_stmt FROM @ddl;
EXECUTE prediction_runid_stmt;
DEALLOCATE PREPARE prediction_runid_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_predictions'
          AND index_name = 'uk_scheme_tenor_predict'
    ) > 0,
    'ALTER TABLE t_scheme_predictions DROP INDEX uk_scheme_tenor_predict',
    'SELECT 1'
);
PREPARE prediction_runid_stmt FROM @ddl;
EXECUTE prediction_runid_stmt;
DEALLOCATE PREPARE prediction_runid_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_predictions'
          AND index_name = 'uk_scheme_tenor_predict_run'
    ) = 0,
    'ALTER TABLE t_scheme_predictions ADD UNIQUE KEY uk_scheme_tenor_predict_run (scheme_id, target_tenor, predict_date, run_id)',
    'SELECT 1'
);
PREPARE prediction_runid_stmt FROM @ddl;
EXECUTE prediction_runid_stmt;
DEALLOCATE PREPARE prediction_runid_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_predictions'
          AND index_name = 'idx_scheme_predictions_run_id'
    ) = 0,
    'ALTER TABLE t_scheme_predictions ADD INDEX idx_scheme_predictions_run_id (run_id)',
    'SELECT 1'
);
PREPARE prediction_runid_stmt FROM @ddl;
EXECUTE prediction_runid_stmt;
DEALLOCATE PREPARE prediction_runid_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_run_log'
          AND index_name = 'idx_scheme_run_log_run_id'
    ) = 0,
    'ALTER TABLE t_scheme_run_log ADD INDEX idx_scheme_run_log_run_id (run_id)',
    'SELECT 1'
);
PREPARE prediction_runid_stmt FROM @ddl;
EXECUTE prediction_runid_stmt;
DEALLOCATE PREPARE prediction_runid_stmt;
