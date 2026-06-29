-- Allow weekly point and weekly average actuals to coexist.
-- Idempotent: safe to run more than once on bond_db.

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_weekly_actuals'
          AND index_name = 'uk_weekly_actual_predict'
    ) > 0,
    'ALTER TABLE t_scheme_weekly_actuals DROP INDEX uk_weekly_actual_predict',
    'SELECT 1'
);
PREPARE weekly_average_actuals_stmt FROM @ddl;
EXECUTE weekly_average_actuals_stmt;
DEALLOCATE PREPARE weekly_average_actuals_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_weekly_actuals'
          AND index_name = 'uk_weekly_actual_predict_rule'
    ) = 0,
    'ALTER TABLE t_scheme_weekly_actuals ADD UNIQUE KEY uk_weekly_actual_predict_rule (tenor, predict_date, target_rule)',
    'SELECT 1'
);
PREPARE weekly_average_actuals_stmt FROM @ddl;
EXECUTE weekly_average_actuals_stmt;
DEALLOCATE PREPARE weekly_average_actuals_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_weekly_actuals'
          AND index_name = 'idx_weekly_actual_target_rule'
    ) = 0,
    'ALTER TABLE t_scheme_weekly_actuals ADD INDEX idx_weekly_actual_target_rule (tenor, target_date, target_rule)',
    'SELECT 1'
);
PREPARE weekly_average_actuals_stmt FROM @ddl;
EXECUTE weekly_average_actuals_stmt;
DEALLOCATE PREPARE weekly_average_actuals_stmt;
