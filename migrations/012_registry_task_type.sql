-- Add explicit frontend task type to the registry.
-- Nullable by schema for rolling deployment. App/config/API enforce non-empty values.

SET @add_task_type := (
    SELECT IF(
        NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 't_scheme_registry'
              AND column_name = 'task_type'
        ),
        'ALTER TABLE t_scheme_registry ADD COLUMN task_type VARCHAR(32) NULL AFTER horizon',
        'SELECT 1'
    )
);
PREPARE stmt FROM @add_task_type;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

UPDATE t_scheme_registry
SET task_type = 'T+1'
WHERE (task_type IS NULL OR task_type = '')
  AND frequency = 'daily'
  AND horizon = 1;

UPDATE t_scheme_registry
SET task_type = 'T+5'
WHERE (task_type IS NULL OR task_type = '')
  AND frequency = 'daily'
  AND horizon = 5;

UPDATE t_scheme_registry
SET task_type = 'weekly_point'
WHERE (task_type IS NULL OR task_type = '')
  AND frequency = 'weekly'
  AND horizon = 6;

UPDATE t_scheme_registry
SET task_type = 'monthly'
WHERE (task_type IS NULL OR task_type = '')
  AND frequency = 'monthly';
