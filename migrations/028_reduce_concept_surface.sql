-- bfl-migration: rerunnable
-- Reduce the public concept surface:
--   * t_scheme_runs.prediction_phase: gray_live -> gap_fill (rows are rewritten; published facts are untouched).
--   * t_scheme_runs.run_type and t_backtest_runs.run_mode were constants ('active' / 'persist'); drop them.
--   * t_scheme_versions.status loses the never-used 'validated' and 'shadow'.
-- Every statement checks information_schema first, so an interrupted run can be re-executed safely.

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'prediction_phase' AND column_type LIKE '%gap_fill%') > 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs MODIFY COLUMN prediction_phase ENUM(''gray_live'',''gap_fill'',''scheduled_live'') DEFAULT NULL'
);
PREPARE concept_028_01 FROM @ddl;
EXECUTE concept_028_01;
DEALLOCATE PREPARE concept_028_01;

UPDATE t_scheme_runs SET prediction_phase = 'gap_fill' WHERE prediction_phase = 'gray_live';

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'prediction_phase' AND column_type LIKE '%gray_live%') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs MODIFY COLUMN prediction_phase ENUM(''gap_fill'',''scheduled_live'') DEFAULT NULL'
);
PREPARE concept_028_02 FROM @ddl;
EXECUTE concept_028_02;
DEALLOCATE PREPARE concept_028_02;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'run_type') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP COLUMN run_type'
);
PREPARE concept_028_03 FROM @ddl;
EXECUTE concept_028_03;
DEALLOCATE PREPARE concept_028_03;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_backtest_runs' AND column_name = 'run_mode') = 0,
    'SELECT 1',
    'ALTER TABLE t_backtest_runs DROP COLUMN run_mode'
);
PREPARE concept_028_04 FROM @ddl;
EXECUTE concept_028_04;
DEALLOCATE PREPARE concept_028_04;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_versions' AND column_name = 'status' AND (column_type LIKE '%validated%' OR column_type LIKE '%shadow%')) = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_versions MODIFY COLUMN status ENUM(''draft'',''active'',''paused'',''retired'') NOT NULL DEFAULT ''draft'''
);
PREPARE concept_028_05 FROM @ddl;
EXECUTE concept_028_05;
DEALLOCATE PREPARE concept_028_05;
