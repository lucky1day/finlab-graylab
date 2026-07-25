-- Allow a ledger run to exist between claim and confirmed child-process start.
-- The legacy default remains intact for non-ledger run creators.
--
-- MySQL 8 uses atomic DDL for this single ALTER. Replaying the target
-- definition is idempotent; an unexpected source definition fails before DDL.

SET @schedule_run_started_at_source_shape = (
    SELECT COUNT(*) = 1
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 't_scheme_runs'
      AND column_name = 'started_at'
      AND column_type = 'datetime'
      AND LOWER(is_nullable) = 'no'
      AND UPPER(column_default) = 'CURRENT_TIMESTAMP'
      AND UPPER(extra) = 'DEFAULT_GENERATED'
);

SET @schedule_run_started_at_target_shape = (
    SELECT COUNT(*) = 1
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 't_scheme_runs'
      AND column_name = 'started_at'
      AND column_type = 'datetime(6)'
      AND LOWER(is_nullable) = 'yes'
      AND UPPER(column_default) = 'CURRENT_TIMESTAMP(6)'
      AND UPPER(extra) = 'DEFAULT_GENERATED'
);

SET @ddl = IF(
    (
        @schedule_run_started_at_source_shape
        + @schedule_run_started_at_target_shape
    ) = 1,
    'SELECT 1',
    'SELECT (SELECT guard_message FROM (SELECT ''unexpected t_scheme_runs.started_at definition'' AS guard_message UNION ALL SELECT ''unexpected t_scheme_runs.started_at definition'') AS migration_guard)'
);
PREPARE schedule_run_started_at_stmt FROM @ddl;
EXECUTE schedule_run_started_at_stmt;
DEALLOCATE PREPARE schedule_run_started_at_stmt;

ALTER TABLE t_scheme_runs
    MODIFY COLUMN started_at
        DATETIME(6) NULL DEFAULT CURRENT_TIMESTAMP(6);
