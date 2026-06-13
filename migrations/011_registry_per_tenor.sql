-- Split t_scheme_registry into one business scheme row per target tenor.
-- Idempotent: safe to run more than once on bond_db.

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_registry'
          AND column_name = 'base_scheme_id'
    ) = 0,
    'ALTER TABLE t_scheme_registry ADD COLUMN base_scheme_id VARCHAR(64) NULL AFTER scheme_id',
    'SELECT 1'
);
PREPARE registry_per_tenor_stmt FROM @ddl;
EXECUTE registry_per_tenor_stmt;
DEALLOCATE PREPARE registry_per_tenor_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_registry'
          AND column_name = 'target_tenor'
    ) = 0,
    'ALTER TABLE t_scheme_registry ADD COLUMN target_tenor VARCHAR(16) NULL AFTER frequency',
    'SELECT 1'
);
PREPARE registry_per_tenor_stmt FROM @ddl;
EXECUTE registry_per_tenor_stmt;
DEALLOCATE PREPARE registry_per_tenor_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_registry'
          AND column_name = 'deployed_at'
    ) = 0,
    'ALTER TABLE t_scheme_registry ADD COLUMN deployed_at DATE NULL AFTER status',
    'SELECT 1'
);
PREPARE registry_per_tenor_stmt FROM @ddl;
EXECUTE registry_per_tenor_stmt;
DEALLOCATE PREPARE registry_per_tenor_stmt;

DROP TEMPORARY TABLE IF EXISTS tmp_registry_per_tenor_source;

CREATE TEMPORARY TABLE tmp_registry_per_tenor_source AS
SELECT id, scheme_id AS old_scheme_id, name, description, horizon, tenors, frequency,
       schedule_cron, schedule_timezone, status, DATE(created_at) AS deployed_at,
       created_at, updated_at
FROM t_scheme_registry
WHERE base_scheme_id IS NULL;

INSERT INTO t_scheme_registry
    (scheme_id, base_scheme_id, name, description, horizon, tenors, frequency, target_tenor,
     schedule_cron, schedule_timezone, status, deployed_at, created_at, updated_at)
SELECT
    CONCAT(src.old_scheme_id, '__h', src.horizon, '__', jt.target_tenor) AS scheme_id,
    src.old_scheme_id AS base_scheme_id,
    src.name,
    src.description,
    src.horizon,
    JSON_ARRAY(jt.target_tenor) AS tenors,
    src.frequency,
    jt.target_tenor AS target_tenor,
    src.schedule_cron,
    src.schedule_timezone,
    src.status,
    src.deployed_at,
    src.created_at,
    src.updated_at
FROM tmp_registry_per_tenor_source src
CROSS JOIN JSON_TABLE(
    COALESCE(src.tenors, JSON_ARRAY()),
    '$[*]' COLUMNS(target_tenor VARCHAR(16) PATH '$')
) AS jt
WHERE 1 = 1
ON DUPLICATE KEY UPDATE
    base_scheme_id = VALUES(base_scheme_id),
    name = VALUES(name),
    description = VALUES(description),
    horizon = VALUES(horizon),
    tenors = VALUES(tenors),
    frequency = VALUES(frequency),
    target_tenor = VALUES(target_tenor),
    schedule_cron = VALUES(schedule_cron),
    schedule_timezone = VALUES(schedule_timezone),
    status = VALUES(status),
    deployed_at = COALESCE(t_scheme_registry.deployed_at, VALUES(deployed_at));

DELETE r
FROM t_scheme_registry r
JOIN tmp_registry_per_tenor_source src ON src.id = r.id
WHERE r.base_scheme_id IS NULL;

DROP TEMPORARY TABLE IF EXISTS tmp_registry_per_tenor_source;

UPDATE t_scheme_registry
SET deployed_at = DATE(created_at)
WHERE deployed_at IS NULL;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_registry'
          AND column_name = 'base_scheme_id'
          AND is_nullable = 'YES'
    ) = 1,
    'ALTER TABLE t_scheme_registry MODIFY COLUMN base_scheme_id VARCHAR(64) NOT NULL',
    'SELECT 1'
);
PREPARE registry_per_tenor_stmt FROM @ddl;
EXECUTE registry_per_tenor_stmt;
DEALLOCATE PREPARE registry_per_tenor_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_registry'
          AND column_name = 'target_tenor'
          AND is_nullable = 'YES'
    ) = 1,
    'ALTER TABLE t_scheme_registry MODIFY COLUMN target_tenor VARCHAR(16) NOT NULL',
    'SELECT 1'
);
PREPARE registry_per_tenor_stmt FROM @ddl;
EXECUTE registry_per_tenor_stmt;
DEALLOCATE PREPARE registry_per_tenor_stmt;

SET @ddl = IF(
    (
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = 't_scheme_registry'
          AND index_name = 'idx_scheme_registry_base'
    ) = 0,
    'ALTER TABLE t_scheme_registry ADD INDEX idx_scheme_registry_base (base_scheme_id, status)',
    'SELECT 1'
);
PREPARE registry_per_tenor_stmt FROM @ddl;
EXECUTE registry_per_tenor_stmt;
DEALLOCATE PREPARE registry_per_tenor_stmt;
