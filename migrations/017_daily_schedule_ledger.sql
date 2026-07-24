-- Daily scheduler occurrence ledger and run fencing.
-- Production execution MUST use scripts/apply_migrations.py. Direct mysql
-- execution lacks the required session/data preflight and definition-level
-- postcondition. With those runner gates, this schema-only migration is
-- idempotent and resumable after an explicitly inspected partial apply.
-- Superseded/abandoned runs keep the existing `failed` status and use failure_code.
-- Every new audit timestamp is stored as UTC in DATETIME(6); callers convert at the boundary.

-- Fail before the first DDL when someone accidentally sources this file through
-- mysql instead of the checksum/history runner. This is defense in depth; the
-- runner still performs the authoritative session/data/schema checks.
SET @ddl = IF(
    COALESCE(@bond_factor_lab_migration_runner_017, '') =
        'daily-ledger-017-v1',
    'SELECT 1',
    'SELECT (SELECT guard_message FROM (SELECT ''migration 017 requires scripts/apply_migrations.py'' AS guard_message UNION ALL SELECT ''migration 017 requires scripts/apply_migrations.py'') AS migration_guard)'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

CREATE TABLE IF NOT EXISTS t_input_generations (
    generation_id VARCHAR(128) NOT NULL PRIMARY KEY,
    generation_type ENUM('native_source','databridge_v1') NOT NULL,
    business_date DATE NOT NULL,
    feature_date DATE NOT NULL,
    readiness_basis ENUM('UPSTREAM_SEAL','CLOCK_CONTRACT') NOT NULL,
    source_commit_token VARCHAR(128) NOT NULL,
    dataset_content_id VARCHAR(128) NOT NULL,
    schema_version VARCHAR(64) NOT NULL,
    exporter_version VARCHAR(64) NOT NULL,
    manifest_uri VARCHAR(512) NOT NULL,
    manifest_sha256 CHAR(64) NOT NULL,
    native_generation_id VARCHAR(128) DEFAULT NULL,
    native_manifest_sha256 CHAR(64) DEFAULT NULL,
    state ENUM('BUILDING','SEALED','INVALIDATED') NOT NULL DEFAULT 'BUILDING',
    sealed_at DATETIME(6) DEFAULT NULL,
    invalidated_at DATETIME(6) DEFAULT NULL,
    invalid_reason VARCHAR(512) DEFAULT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6))
        ON UPDATE CURRENT_TIMESTAMP(6),
    INDEX idx_input_generation_date_state (business_date, generation_type, state),
    INDEX idx_input_generation_dataset (dataset_content_id),
    INDEX idx_input_generation_native (native_generation_id),
    CONSTRAINT fk_input_generation_native
        FOREIGN KEY (native_generation_id)
        REFERENCES t_input_generations(generation_id)
        ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_schedule_occurrences (
    occurrence_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    schedule_key VARCHAR(128) NOT NULL,
    predict_date DATE NOT NULL,
    feature_date DATE NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    policy_sha256 CHAR(64) NOT NULL,
    policy_json JSON NOT NULL,
    registry_digest CHAR(64) NOT NULL,
    completion_state ENUM('PENDING','RUNNING','SUCCESS','FAILED')
        NOT NULL DEFAULT 'PENDING',
    expected_item_count INT NOT NULL,
    expected_target_count INT NOT NULL,
    accepted_target_count INT NOT NULL DEFAULT 0,
    sla_accepted_target_count INT DEFAULT NULL,
    sla_deadline_at DATETIME(6) NOT NULL,
    recovery_cutoff_at DATETIME(6) NOT NULL,
    sla_outcome ENUM('PENDING','MET','BREACHED') NOT NULL DEFAULT 'PENDING',
    sla_evaluated_at DATETIME(6) DEFAULT NULL,
    sla_reason VARCHAR(128) DEFAULT NULL,
    failure_code VARCHAR(64) DEFAULT NULL,
    failure_message TEXT DEFAULT NULL,
    started_at DATETIME(6) DEFAULT NULL,
    completed_at DATETIME(6) DEFAULT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6))
        ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_schedule_occurrence (schedule_key, predict_date),
    INDEX idx_schedule_occurrence_date_state (predict_date, completion_state),
    INDEX idx_schedule_occurrence_sla (sla_outcome, sla_deadline_at),
    CONSTRAINT ck_schedule_occurrence_feature_date CHECK (
        feature_date < predict_date
    ),
    CONSTRAINT ck_schedule_occurrence_failure_code CHECK (
        failure_code IS NULL OR failure_code IN (
            'TRANSIENT_INFRA','TIMEOUT','DATA','CONTRACT','ALGORITHM','RESULT',
            'NATIVE_GENERATION_UNSUPPORTED','GENERATION_BUILD_FAILED',
            'GENERATION_HASH_MISMATCH','GENERATION_INVALIDATED',
            'ABANDONED_FENCE_PENDING_CLEANUP','ABANDONED_ORPHAN_CLEANUP',
            'RECOVERY_CUTOFF_EXPIRED','STALE_ATTEMPT','NO_CROSS_DAY',
            'INVALID_ITEM_STATE'
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_schedule_items (
    item_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    occurrence_id BIGINT NOT NULL,
    base_scheme_id VARCHAR(64) NOT NULL,
    runtime_type VARCHAR(32) NOT NULL,
    scheme_version VARCHAR(64) NOT NULL,
    code_sha256 CHAR(64) NOT NULL,
    config_sha256 CHAR(64) NOT NULL,
    cache_group VARCHAR(128) NOT NULL,
    input_generation_id VARCHAR(128) DEFAULT NULL,
    resource_class VARCHAR(64) NOT NULL,
    internal_workers INT NOT NULL,
    release_offset_minutes INT NOT NULL,
    release_at DATETIME(6) NOT NULL,
    deadline_at DATETIME(6) NOT NULL,
    state ENUM(
        'PENDING','RUNNING','RETRY_WAIT','SUCCESS',
        'FAILED_TERMINAL','ABANDONED','EXPIRED'
    ) NOT NULL DEFAULT 'PENDING',
    sla_status ENUM('PENDING','ON_TIME','LATE') NOT NULL DEFAULT 'PENDING',
    late_reason VARCHAR(128) DEFAULT NULL,
    sla_evaluated_at DATETIME(6) DEFAULT NULL,
    attempt_no INT NOT NULL DEFAULT 0,
    current_run_id BIGINT DEFAULT NULL,
    started_at DATETIME(6) DEFAULT NULL,
    completed_at DATETIME(6) DEFAULT NULL,
    failure_code VARCHAR(64) DEFAULT NULL,
    failure_message TEXT DEFAULT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6))
        ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_schedule_item (occurrence_id, base_scheme_id),
    INDEX idx_schedule_item_occurrence_state (occurrence_id, state),
    INDEX idx_schedule_item_current_run (current_run_id),
    INDEX idx_schedule_item_generation (input_generation_id),
    INDEX idx_schedule_item_release (state, release_at, deadline_at),
    CONSTRAINT fk_schedule_item_occurrence
        FOREIGN KEY (occurrence_id) REFERENCES t_schedule_occurrences(occurrence_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_schedule_item_generation
        FOREIGN KEY (input_generation_id) REFERENCES t_input_generations(generation_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_schedule_item_current_run
        FOREIGN KEY (current_run_id) REFERENCES t_scheme_runs(run_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_schedule_item_failure_code CHECK (
        failure_code IS NULL OR failure_code IN (
            'TRANSIENT_INFRA','TIMEOUT','DATA','CONTRACT','ALGORITHM','RESULT',
            'NATIVE_GENERATION_UNSUPPORTED','GENERATION_BUILD_FAILED',
            'GENERATION_HASH_MISMATCH','GENERATION_INVALIDATED',
            'ABANDONED_FENCE_PENDING_CLEANUP','ABANDONED_ORPHAN_CLEANUP',
            'RECOVERY_CUTOFF_EXPIRED','STALE_ATTEMPT','NO_CROSS_DAY',
            'INVALID_ITEM_STATE'
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_schedule_item_targets (
    target_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    occurrence_id BIGINT NOT NULL,
    item_id BIGINT NOT NULL,
    registry_scheme_id VARCHAR(64) NOT NULL,
    base_scheme_id VARCHAR(64) NOT NULL,
    runtime_type VARCHAR(32) NOT NULL,
    task_type VARCHAR(32) NOT NULL,
    target_tenor VARCHAR(16) NOT NULL,
    horizon INT NOT NULL,
    target_date DATE NOT NULL,
    status ENUM('PENDING','ACCEPTED') NOT NULL DEFAULT 'PENDING',
    accepted_run_id BIGINT DEFAULT NULL,
    accepted_prediction_id BIGINT DEFAULT NULL,
    accepted_at DATETIME(6) DEFAULT NULL,
    visible_at DATETIME(6) DEFAULT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    UNIQUE KEY uk_schedule_target_registry (occurrence_id, registry_scheme_id),
    UNIQUE KEY uk_schedule_target_item (item_id, target_tenor, horizon),
    INDEX idx_schedule_target_occurrence_status (occurrence_id, status),
    INDEX idx_schedule_target_identity
        (base_scheme_id, target_tenor, horizon, target_date),
    INDEX idx_schedule_target_run (accepted_run_id),
    INDEX idx_schedule_target_prediction (accepted_prediction_id),
    CONSTRAINT fk_schedule_target_occurrence
        FOREIGN KEY (occurrence_id) REFERENCES t_schedule_occurrences(occurrence_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_schedule_target_item
        FOREIGN KEY (item_id) REFERENCES t_schedule_items(item_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_schedule_target_run
        FOREIGN KEY (accepted_run_id) REFERENCES t_scheme_runs(run_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_schedule_target_prediction
        FOREIGN KEY (accepted_prediction_id) REFERENCES t_scheme_predictions(id)
        ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS t_scheduler_heartbeat (
    service_name VARCHAR(64) NOT NULL PRIMARY KEY,
    process_id BIGINT NOT NULL,
    host_name VARCHAR(255) NOT NULL,
    state VARCHAR(32) NOT NULL,
    occurrence_id BIGINT DEFAULT NULL,
    heartbeat_at DATETIME(6) NOT NULL,
    details_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6)),
    updated_at DATETIME(6) NOT NULL DEFAULT (UTC_TIMESTAMP(6))
        ON UPDATE CURRENT_TIMESTAMP(6),
    INDEX idx_scheduler_heartbeat_at (heartbeat_at),
    CONSTRAINT fk_scheduler_heartbeat_occurrence
        FOREIGN KEY (occurrence_id)
        REFERENCES t_schedule_occurrences(occurrence_id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_item_targets'
       AND index_name = 'idx_schedule_target_identity') = 0,
    'ALTER TABLE t_schedule_item_targets ADD INDEX idx_schedule_target_identity (base_scheme_id, target_tenor, horizon, target_date)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

-- Existing occurrences cannot be assigned a trustworthy feature date from
-- predict_date alone. Reject a nonempty legacy table before ADD COLUMN; this
-- duplicates the runner preflight so direct SQL execution also fails closed.
SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_occurrences'
       AND column_name = 'feature_date') = 0
    AND (SELECT COUNT(*) FROM t_schedule_occurrences) > 0,
    'SELECT (SELECT guard_message FROM (SELECT ''nonempty t_schedule_occurrences is missing feature_date'' AS guard_message UNION ALL SELECT ''nonempty t_schedule_occurrences is missing feature_date'') AS migration_guard)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_occurrences'
       AND column_name = 'feature_date') = 0,
    'ALTER TABLE t_schedule_occurrences ADD COLUMN feature_date DATE NULL AFTER predict_date',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

-- A present feature_date is still untrusted until legacy NULL/zero dates and
-- the occurrence ordering contract have been checked explicitly.
SET @ddl = IF(
    (SELECT COUNT(*) FROM t_schedule_occurrences
     WHERE feature_date IS NULL
        OR CAST(feature_date AS CHAR) = '0000-00-00'
        OR feature_date >= predict_date) > 0,
    'SELECT (SELECT guard_message FROM (SELECT ''t_schedule_occurrences contains invalid feature_date'' AS guard_message UNION ALL SELECT ''t_schedule_occurrences contains invalid feature_date'') AS migration_guard)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_occurrences'
       AND column_name = 'feature_date'
       AND (
           is_nullable <> 'NO'
           OR LOWER(column_type) <> 'date'
           OR column_default IS NOT NULL
       )) > 0,
    'ALTER TABLE t_schedule_occurrences MODIFY COLUMN feature_date DATE NOT NULL',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.table_constraints
     WHERE constraint_schema = DATABASE()
       AND table_name = 't_schedule_occurrences'
       AND constraint_name = 'ck_schedule_occurrence_feature_date'
       AND constraint_type = 'CHECK') = 0,
    'ALTER TABLE t_schedule_occurrences ADD CONSTRAINT ck_schedule_occurrence_feature_date CHECK (feature_date < predict_date)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_item_targets'
       AND column_name = 'visible_at') = 0,
    'ALTER TABLE t_schedule_item_targets ADD COLUMN visible_at DATETIME(6) NULL AFTER accepted_at',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_input_generations'
       AND column_name = 'native_generation_id') = 0,
    'ALTER TABLE t_input_generations ADD COLUMN native_generation_id VARCHAR(128) NULL AFTER manifest_sha256',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_input_generations'
       AND column_name = 'native_manifest_sha256') = 0,
    'ALTER TABLE t_input_generations ADD COLUMN native_manifest_sha256 CHAR(64) NULL AFTER native_generation_id',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema = DATABASE()
       AND table_name = 't_input_generations'
       AND index_name = 'idx_input_generation_native') = 0,
    'ALTER TABLE t_input_generations ADD INDEX idx_input_generation_native (native_generation_id)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.table_constraints
     WHERE constraint_schema = DATABASE()
       AND table_name = 't_input_generations'
       AND constraint_name = 'fk_input_generation_native'
       AND constraint_type = 'FOREIGN KEY') = 0,
    'ALTER TABLE t_input_generations ADD CONSTRAINT fk_input_generation_native FOREIGN KEY (native_generation_id) REFERENCES t_input_generations(generation_id) ON DELETE RESTRICT',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_occurrences'
       AND column_name = 'sla_accepted_target_count') = 0,
    'ALTER TABLE t_schedule_occurrences ADD COLUMN sla_accepted_target_count INT NULL AFTER accepted_target_count',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_occurrences'
       AND column_name = 'failure_code') = 0,
    'ALTER TABLE t_schedule_occurrences ADD COLUMN failure_code VARCHAR(64) NULL AFTER sla_reason',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_occurrences'
       AND column_name = 'failure_message') = 0,
    'ALTER TABLE t_schedule_occurrences ADD COLUMN failure_message TEXT NULL AFTER failure_code',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

-- Resource-policy fields are frozen identity, so this migration never guesses
-- values for legacy rows. Missing fields are only addable to an empty table.
SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'resource_class') = 0
    AND (SELECT COUNT(*) FROM t_schedule_items) > 0,
    'SELECT (SELECT guard_message FROM (SELECT ''nonempty t_schedule_items is missing resource_class'' AS guard_message UNION ALL SELECT ''nonempty t_schedule_items is missing resource_class'') AS migration_guard)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'resource_class') = 0,
    'ALTER TABLE t_schedule_items ADD COLUMN resource_class VARCHAR(64) NULL AFTER input_generation_id',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM t_schedule_items
     WHERE `resource_class` IS NULL) > 0,
    'SELECT (SELECT guard_message FROM (SELECT ''t_schedule_items contains NULL resource_class'' AS guard_message UNION ALL SELECT ''t_schedule_items contains NULL resource_class'') AS migration_guard)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'resource_class'
       AND column_default IS NOT NULL) > 0
    AND (SELECT COUNT(*) FROM t_schedule_items) > 0,
    'SELECT (SELECT guard_message FROM (SELECT ''t_schedule_items resource_class default requires reconciliation'' AS guard_message UNION ALL SELECT ''t_schedule_items resource_class default requires reconciliation'') AS migration_guard)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'resource_class'
       AND (
           is_nullable <> 'NO'
           OR LOWER(column_type) <> 'varchar(64)'
           OR column_default IS NOT NULL
       )) > 0,
    'ALTER TABLE t_schedule_items MODIFY COLUMN resource_class VARCHAR(64) NOT NULL',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'internal_workers') = 0
    AND (SELECT COUNT(*) FROM t_schedule_items) > 0,
    'SELECT (SELECT guard_message FROM (SELECT ''nonempty t_schedule_items is missing internal_workers'' AS guard_message UNION ALL SELECT ''nonempty t_schedule_items is missing internal_workers'') AS migration_guard)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'internal_workers') = 0,
    'ALTER TABLE t_schedule_items ADD COLUMN internal_workers INT NULL AFTER resource_class',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM t_schedule_items
     WHERE `internal_workers` IS NULL) > 0,
    'SELECT (SELECT guard_message FROM (SELECT ''t_schedule_items contains NULL internal_workers'' AS guard_message UNION ALL SELECT ''t_schedule_items contains NULL internal_workers'') AS migration_guard)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'internal_workers'
       AND column_default IS NOT NULL) > 0
    AND (SELECT COUNT(*) FROM t_schedule_items) > 0,
    'SELECT (SELECT guard_message FROM (SELECT ''t_schedule_items internal_workers default requires reconciliation'' AS guard_message UNION ALL SELECT ''t_schedule_items internal_workers default requires reconciliation'') AS migration_guard)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'internal_workers'
       AND (
           is_nullable <> 'NO'
           OR LOWER(column_type) <> 'int'
           OR column_default IS NOT NULL
       )) > 0,
    'ALTER TABLE t_schedule_items MODIFY COLUMN internal_workers INT NOT NULL',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'release_offset_minutes') = 0
    AND (SELECT COUNT(*) FROM t_schedule_items) > 0,
    'SELECT (SELECT guard_message FROM (SELECT ''nonempty t_schedule_items is missing release_offset_minutes'' AS guard_message UNION ALL SELECT ''nonempty t_schedule_items is missing release_offset_minutes'') AS migration_guard)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'release_offset_minutes') = 0,
    'ALTER TABLE t_schedule_items ADD COLUMN release_offset_minutes INT NULL AFTER internal_workers',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM t_schedule_items
     WHERE `release_offset_minutes` IS NULL) > 0,
    'SELECT (SELECT guard_message FROM (SELECT ''t_schedule_items contains NULL release_offset_minutes'' AS guard_message UNION ALL SELECT ''t_schedule_items contains NULL release_offset_minutes'') AS migration_guard)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'release_offset_minutes'
       AND column_default IS NOT NULL) > 0
    AND (SELECT COUNT(*) FROM t_schedule_items) > 0,
    'SELECT (SELECT guard_message FROM (SELECT ''t_schedule_items release_offset_minutes default requires reconciliation'' AS guard_message UNION ALL SELECT ''t_schedule_items release_offset_minutes default requires reconciliation'') AS migration_guard)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND column_name = 'release_offset_minutes'
       AND (
           is_nullable <> 'NO'
           OR LOWER(column_type) <> 'int'
           OR column_default IS NOT NULL
       )) > 0,
    'ALTER TABLE t_schedule_items MODIFY COLUMN release_offset_minutes INT NOT NULL',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs'
       AND column_name = 'schedule_item_id') = 0,
    'ALTER TABLE t_scheme_runs ADD COLUMN schedule_item_id BIGINT NULL AFTER data_snapshot_id',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs'
       AND column_name = 'attempt_no') = 0,
    'ALTER TABLE t_scheme_runs ADD COLUMN attempt_no INT NULL AFTER schedule_item_id',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs'
       AND column_name = 'trigger_origin') = 0,
    'ALTER TABLE t_scheme_runs ADD COLUMN trigger_origin ENUM(''apscheduler'',''startup_catchup'',''auto_retry'',''operator_recovery'') NULL AFTER attempt_no',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs'
       AND column_name = 'queued_at') = 0,
    'ALTER TABLE t_scheme_runs ADD COLUMN queued_at DATETIME(6) NULL AFTER trigger_origin',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs'
       AND column_name = 'failure_code') = 0,
    'ALTER TABLE t_scheme_runs ADD COLUMN failure_code VARCHAR(64) NULL AFTER error_message',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs'
       AND column_name = 'execution_token') = 0,
    'ALTER TABLE t_scheme_runs ADD COLUMN execution_token VARCHAR(128) NULL AFTER failure_code',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs'
       AND column_name = 'process_id') = 0,
    'ALTER TABLE t_scheme_runs ADD COLUMN process_id BIGINT NULL AFTER execution_token',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs'
       AND column_name = 'process_group_id') = 0,
    'ALTER TABLE t_scheme_runs ADD COLUMN process_group_id BIGINT NULL AFTER process_id',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs'
       AND index_name = 'uk_scheme_runs_schedule_attempt') = 0,
    'ALTER TABLE t_scheme_runs ADD UNIQUE INDEX uk_scheme_runs_schedule_attempt (schedule_item_id, attempt_no)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema = DATABASE() AND table_name = 't_scheme_runs'
       AND index_name = 'uk_scheme_runs_execution_token') = 0,
    'ALTER TABLE t_scheme_runs ADD UNIQUE INDEX uk_scheme_runs_execution_token (execution_token)',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.table_constraints
     WHERE constraint_schema = DATABASE() AND table_name = 't_scheme_runs'
       AND constraint_name = 'fk_scheme_run_schedule_item'
       AND constraint_type = 'FOREIGN KEY') = 0,
    'ALTER TABLE t_scheme_runs ADD CONSTRAINT fk_scheme_run_schedule_item FOREIGN KEY (schedule_item_id) REFERENCES t_schedule_items(item_id) ON DELETE RESTRICT',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

-- Failure codes are an audited vocabulary. These CHECK additions intentionally
-- fail closed when an earlier ledger rollout wrote an old alias: operators must
-- inspect and explicitly migrate those rows; this migration never rewrites them.
SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.table_constraints
     WHERE constraint_schema = DATABASE()
       AND table_name = 't_schedule_occurrences'
       AND constraint_name = 'ck_schedule_occurrence_failure_code'
       AND constraint_type = 'CHECK') = 0,
    'ALTER TABLE t_schedule_occurrences ADD CONSTRAINT ck_schedule_occurrence_failure_code CHECK (failure_code IS NULL OR failure_code IN (''TRANSIENT_INFRA'',''TIMEOUT'',''DATA'',''CONTRACT'',''ALGORITHM'',''RESULT'',''NATIVE_GENERATION_UNSUPPORTED'',''GENERATION_BUILD_FAILED'',''GENERATION_HASH_MISMATCH'',''GENERATION_INVALIDATED'',''ABANDONED_FENCE_PENDING_CLEANUP'',''ABANDONED_ORPHAN_CLEANUP'',''RECOVERY_CUTOFF_EXPIRED'',''STALE_ATTEMPT'',''NO_CROSS_DAY'',''INVALID_ITEM_STATE''))',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.table_constraints
     WHERE constraint_schema = DATABASE()
       AND table_name = 't_schedule_items'
       AND constraint_name = 'ck_schedule_item_failure_code'
       AND constraint_type = 'CHECK') = 0,
    'ALTER TABLE t_schedule_items ADD CONSTRAINT ck_schedule_item_failure_code CHECK (failure_code IS NULL OR failure_code IN (''TRANSIENT_INFRA'',''TIMEOUT'',''DATA'',''CONTRACT'',''ALGORITHM'',''RESULT'',''NATIVE_GENERATION_UNSUPPORTED'',''GENERATION_BUILD_FAILED'',''GENERATION_HASH_MISMATCH'',''GENERATION_INVALIDATED'',''ABANDONED_FENCE_PENDING_CLEANUP'',''ABANDONED_ORPHAN_CLEANUP'',''RECOVERY_CUTOFF_EXPIRED'',''STALE_ATTEMPT'',''NO_CROSS_DAY'',''INVALID_ITEM_STATE''))',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;

SET @ddl = IF(
    (SELECT COUNT(*) FROM information_schema.table_constraints
     WHERE constraint_schema = DATABASE()
       AND table_name = 't_scheme_runs'
       AND constraint_name = 'ck_scheme_run_schedule_failure_code'
       AND constraint_type = 'CHECK') = 0,
    'ALTER TABLE t_scheme_runs ADD CONSTRAINT ck_scheme_run_schedule_failure_code CHECK (schedule_item_id IS NULL OR failure_code IS NULL OR failure_code IN (''TRANSIENT_INFRA'',''TIMEOUT'',''DATA'',''CONTRACT'',''ALGORITHM'',''RESULT'',''NATIVE_GENERATION_UNSUPPORTED'',''GENERATION_BUILD_FAILED'',''GENERATION_HASH_MISMATCH'',''GENERATION_INVALIDATED'',''ABANDONED_FENCE_PENDING_CLEANUP'',''ABANDONED_ORPHAN_CLEANUP'',''RECOVERY_CUTOFF_EXPIRED'',''STALE_ATTEMPT'',''NO_CROSS_DAY'',''INVALID_ITEM_STATE''))',
    'SELECT 1'
);
PREPARE daily_ledger_stmt FROM @ddl;
EXECUTE daily_ledger_stmt;
DEALLOCATE PREPARE daily_ledger_stmt;
