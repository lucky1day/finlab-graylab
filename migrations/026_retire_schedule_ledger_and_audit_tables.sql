-- bfl-migration: rerunnable
-- Retire the unused daily schedule ledger (017) and the legacy harness/reproduction audit tables.
-- No platform code reads or writes these tables or the t_scheme_runs columns removed here.
-- Every statement checks information_schema first, so an interrupted run can be re-executed safely.

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.table_constraints WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND constraint_name = 'fk_scheme_run_schedule_item' AND constraint_type = 'FOREIGN KEY') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP FOREIGN KEY fk_scheme_run_schedule_item'
);
PREPARE retire_026_01 FROM @ddl;
EXECUTE retire_026_01;
DEALLOCATE PREPARE retire_026_01;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.table_constraints WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND constraint_name = 'fk_scheme_runs_harness_run' AND constraint_type = 'FOREIGN KEY') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP FOREIGN KEY fk_scheme_runs_harness_run'
);
PREPARE retire_026_02 FROM @ddl;
EXECUTE retire_026_02;
DEALLOCATE PREPARE retire_026_02;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.table_constraints WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND constraint_name = 'ck_scheme_run_schedule_failure_code' AND constraint_type = 'CHECK') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP CHECK ck_scheme_run_schedule_failure_code'
);
PREPARE retire_026_03 FROM @ddl;
EXECUTE retire_026_03;
DEALLOCATE PREPARE retire_026_03;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND index_name = 'uk_scheme_runs_schedule_attempt') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP INDEX uk_scheme_runs_schedule_attempt'
);
PREPARE retire_026_04 FROM @ddl;
EXECUTE retire_026_04;
DEALLOCATE PREPARE retire_026_04;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND index_name = 'uk_scheme_runs_execution_token') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP INDEX uk_scheme_runs_execution_token'
);
PREPARE retire_026_05 FROM @ddl;
EXECUTE retire_026_05;
DEALLOCATE PREPARE retire_026_05;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND index_name = 'idx_scheme_runs_harness') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP INDEX idx_scheme_runs_harness'
);
PREPARE retire_026_06 FROM @ddl;
EXECUTE retire_026_06;
DEALLOCATE PREPARE retire_026_06;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'schedule_item_id') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP COLUMN schedule_item_id'
);
PREPARE retire_026_07 FROM @ddl;
EXECUTE retire_026_07;
DEALLOCATE PREPARE retire_026_07;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'attempt_no') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP COLUMN attempt_no'
);
PREPARE retire_026_08 FROM @ddl;
EXECUTE retire_026_08;
DEALLOCATE PREPARE retire_026_08;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'trigger_origin') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP COLUMN trigger_origin'
);
PREPARE retire_026_09 FROM @ddl;
EXECUTE retire_026_09;
DEALLOCATE PREPARE retire_026_09;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'queued_at') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP COLUMN queued_at'
);
PREPARE retire_026_10 FROM @ddl;
EXECUTE retire_026_10;
DEALLOCATE PREPARE retire_026_10;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'failure_code') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP COLUMN failure_code'
);
PREPARE retire_026_11 FROM @ddl;
EXECUTE retire_026_11;
DEALLOCATE PREPARE retire_026_11;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'execution_token') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP COLUMN execution_token'
);
PREPARE retire_026_12 FROM @ddl;
EXECUTE retire_026_12;
DEALLOCATE PREPARE retire_026_12;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'process_id') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP COLUMN process_id'
);
PREPARE retire_026_13 FROM @ddl;
EXECUTE retire_026_13;
DEALLOCATE PREPARE retire_026_13;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'process_group_id') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP COLUMN process_group_id'
);
PREPARE retire_026_14 FROM @ddl;
EXECUTE retire_026_14;
DEALLOCATE PREPARE retire_026_14;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs' AND column_name = 'harness_run_id') = 0,
    'SELECT 1',
    'ALTER TABLE t_scheme_runs DROP COLUMN harness_run_id'
);
PREPARE retire_026_15 FROM @ddl;
EXECUTE retire_026_15;
DEALLOCATE PREPARE retire_026_15;

DROP TABLE IF EXISTS t_schedule_item_targets;
DROP TABLE IF EXISTS t_scheduler_heartbeat;
DROP TABLE IF EXISTS t_schedule_items;
DROP TABLE IF EXISTS t_schedule_occurrences;
DROP TABLE IF EXISTS t_input_generations;
DROP TABLE IF EXISTS t_harness_gate_results;
DROP TABLE IF EXISTS t_harness_runs;
DROP TABLE IF EXISTS t_backtest_reproduction_checks;

DROP VIEW IF EXISTS v_latest_backtest_run;
