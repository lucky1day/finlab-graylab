-- Align the backtest latest view with the frontend/API serving semantics.
-- Idempotent: safe to run more than once on bond_db.
-- NOTE: This migration intentionally supersedes the v_latest_backtest_run view
--       definition from 007_backtest_immutable.sql. 007 keeps the append-only
--       backtest schema switch; this file is the final latest-run read contract.

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_backtest_runs'
          AND index_name = 'idx_backtest_runs_latest_success'
    ) = 0,
    'ALTER TABLE t_backtest_runs ADD INDEX idx_backtest_runs_latest_success (benchmark_id, scheme_id, data_source, status, updated_at, id)',
    'SELECT 1'
);
PREPARE backtest_latest_stmt FROM @ddl;
EXECUTE backtest_latest_stmt;
DEALLOCATE PREPARE backtest_latest_stmt;

CREATE OR REPLACE VIEW v_latest_backtest_run AS
SELECT
    id,
    backtest_run_id,
    benchmark_id,
    scheme_id,
    data_source,
    start_date,
    end_date,
    status,
    summary,
    report_path,
    code_hash,
    config_hash,
    input_artifact_hash,
    run_mode,
    created_at,
    updated_at
FROM (
    SELECT
        r.*,
        ROW_NUMBER() OVER (
            PARTITION BY benchmark_id, scheme_id, data_source
            ORDER BY updated_at DESC, id DESC
        ) AS latest_rank
    FROM t_backtest_runs r
    WHERE status = 'success'
) ranked
WHERE latest_rank = 1;
