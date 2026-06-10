-- Switch prediction UK from (scheme_id, target_tenor, predict_date, run_id)
-- to (scheme_id, target_tenor, horizon, target_date) with UPSERT.
-- Idempotent: safe to run more than once on bond_db.

-- 1. Drop old UK (run_id-based, allows multiple runs per target_date)
SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_predictions'
     AND index_name = 'uk_scheme_tenor_predict_run') > 0,
    'ALTER TABLE t_scheme_predictions DROP INDEX uk_scheme_tenor_predict_run',
    'SELECT 1'
);
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 2. Add new UK: each (scheme + tenor + horizon + target_date) unique
SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_predictions'
     AND index_name = 'uk_scheme_tenor_target') = 0,
    'ALTER TABLE t_scheme_predictions ADD UNIQUE KEY uk_scheme_tenor_target (scheme_id, target_tenor, horizon, target_date)',
    'SELECT 1'
);
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 3. Drop run_id index (no longer needed)
SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_predictions'
     AND index_name = 'idx_scheme_predictions_run_id') > 0,
    'ALTER TABLE t_scheme_predictions DROP INDEX idx_scheme_predictions_run_id',
    'SELECT 1'
);
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;
