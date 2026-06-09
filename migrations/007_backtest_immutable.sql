-- Immutable backtest run schema switch.
-- Idempotent: safe to run more than once on bond_db.

UPDATE t_backtest_runs
SET backtest_run_id = id
WHERE backtest_run_id IS NULL;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_backtest_runs'
          AND index_name = 'uk_backtest_run_scope'
    ) > 0,
    'ALTER TABLE t_backtest_runs DROP INDEX uk_backtest_run_scope',
    'SELECT 1'
);
PREPARE backtest_immutable_stmt FROM @ddl;
EXECUTE backtest_immutable_stmt;
DEALLOCATE PREPARE backtest_immutable_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_backtest_runs'
          AND index_name = 'uk_backtest_run_id'
    ) = 0,
    'ALTER TABLE t_backtest_runs ADD UNIQUE KEY uk_backtest_run_id (backtest_run_id)',
    'SELECT 1'
);
PREPARE backtest_immutable_stmt FROM @ddl;
EXECUTE backtest_immutable_stmt;
DEALLOCATE PREPARE backtest_immutable_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_backtest_runs'
          AND index_name = 'idx_backtest_runs_latest_scope'
    ) = 0,
    'ALTER TABLE t_backtest_runs ADD INDEX idx_backtest_runs_latest_scope (benchmark_id, scheme_id, data_source, start_date, end_date, id)',
    'SELECT 1'
);
PREPARE backtest_immutable_stmt FROM @ddl;
EXECUTE backtest_immutable_stmt;
DEALLOCATE PREPARE backtest_immutable_stmt;

CREATE OR REPLACE VIEW v_latest_backtest_run AS
SELECT r.*
FROM t_backtest_runs r
JOIN (
    SELECT benchmark_id, scheme_id, data_source, start_date, end_date, MAX(id) AS id
    FROM t_backtest_runs
    GROUP BY benchmark_id, scheme_id, data_source, start_date, end_date
) latest
    ON r.id = latest.id;
