-- Materialize prediction date semantics for live predictions.
-- Idempotent: safe to run more than once on bond_db.

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_predictions'
          AND column_name = 'feature_date'
    ) = 0,
    'ALTER TABLE t_scheme_predictions ADD COLUMN feature_date DATE NULL AFTER target_date',
    'SELECT 1'
);
PREPARE prediction_semantics_stmt FROM @ddl;
EXECUTE prediction_semantics_stmt;
DEALLOCATE PREPARE prediction_semantics_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_predictions'
          AND column_name = 'prediction_phase'
    ) = 0,
    'ALTER TABLE t_scheme_predictions ADD COLUMN prediction_phase ENUM(''gray_live'',''scheduled_live'') NULL AFTER feature_date',
    'SELECT 1'
);
PREPARE prediction_semantics_stmt FROM @ddl;
EXECUTE prediction_semantics_stmt;
DEALLOCATE PREPARE prediction_semantics_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_runs'
          AND column_name = 'prediction_phase'
    ) = 0,
    'ALTER TABLE t_scheme_runs ADD COLUMN prediction_phase ENUM(''gray_live'',''scheduled_live'') NULL AFTER run_type',
    'SELECT 1'
);
PREPARE prediction_semantics_stmt FROM @ddl;
EXECUTE prediction_semantics_stmt;
DEALLOCATE PREPARE prediction_semantics_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_predictions'
          AND index_name = 'idx_scheme_predictions_phase'
    ) = 0,
    'ALTER TABLE t_scheme_predictions ADD INDEX idx_scheme_predictions_phase (scheme_id, prediction_phase, predict_date)',
    'SELECT 1'
);
PREPARE prediction_semantics_stmt FROM @ddl;
EXECUTE prediction_semantics_stmt;
DEALLOCATE PREPARE prediction_semantics_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_runs'
          AND index_name = 'idx_scheme_runs_phase'
    ) = 0,
    'ALTER TABLE t_scheme_runs ADD INDEX idx_scheme_runs_phase (scheme_id, prediction_phase, predict_date)',
    'SELECT 1'
);
PREPARE prediction_semantics_stmt FROM @ddl;
EXECUTE prediction_semantics_stmt;
DEALLOCATE PREPARE prediction_semantics_stmt;
