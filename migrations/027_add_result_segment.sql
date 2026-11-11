-- bfl-migration: rerunnable
-- Add the single public result partition: feature_date < 2026-06-01 is backtest, otherwise live.
-- VIRTUAL generated column (no table rebuild); provenance columns stay audit-only.
-- The boundary must equal backend FACTOR_LAB_LIVE_FEATURE_START_DATE (guarded by tests).

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_predictions' AND column_name = 'result_segment') = 0,
    'ALTER TABLE t_scheme_predictions ADD COLUMN result_segment ENUM(''backtest'',''live'') GENERATED ALWAYS AS (CASE WHEN feature_date IS NULL THEN NULL WHEN feature_date < ''2026-06-01'' THEN ''backtest'' ELSE ''live'' END) VIRTUAL AFTER feature_date',
    'SELECT 1'
);
PREPARE add_result_segment_027 FROM @ddl;
EXECUTE add_result_segment_027;
DEALLOCATE PREPARE add_result_segment_027;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 't_scheme_predictions' AND index_name = 'idx_scheme_result_segment') = 0,
    'ALTER TABLE t_scheme_predictions ADD INDEX idx_scheme_result_segment (scheme_id, result_segment)',
    'SELECT 1'
);
PREPARE add_result_segment_index_027 FROM @ddl;
EXECUTE add_result_segment_index_027;
DEALLOCATE PREPARE add_result_segment_index_027;
