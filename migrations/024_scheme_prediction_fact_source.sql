-- Collapse product result details into t_scheme_predictions as the only fact source.

SET @bfl_024_add_backtest_run_id = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE `t_scheme_predictions` ADD COLUMN `backtest_run_id` BIGINT NULL AFTER `run_id`',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 't_scheme_predictions'
      AND column_name = 'backtest_run_id'
);
PREPARE bfl_024_stmt FROM @bfl_024_add_backtest_run_id;
EXECUTE bfl_024_stmt;
DEALLOCATE PREPARE bfl_024_stmt;

SET @bfl_024_add_backtest_actual_direction = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE `t_scheme_predictions` ADD COLUMN `backtest_actual_direction` TINYINT NULL AFTER `predicted_direction`',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 't_scheme_predictions'
      AND column_name = 'backtest_actual_direction'
);
PREPARE bfl_024_stmt FROM @bfl_024_add_backtest_actual_direction;
EXECUTE bfl_024_stmt;
DEALLOCATE PREPARE bfl_024_stmt;

UPDATE `t_scheme_runs`
SET `scheme_version` = 'e257093fb4f3'
WHERE ((`run_id` = 15 AND `predict_date` = '2026-06-05')
    OR (`run_id` = 16 AND `predict_date` = '2026-06-08')
    OR (`run_id` = 17 AND `predict_date` = '2026-06-09'))
  AND `scheme_id` = 't5_daily'
  AND `scheme_version` IS NULL
  AND `started_at` >= '2026-06-10 00:01:02'
  AND `started_at` < '2026-06-10 18:53:35';

UPDATE `t_scheme_predictions`
SET `scheme_version` = 'e257093fb4f3'
WHERE ((`run_id` = 15 AND `predict_date` = '2026-06-05')
    OR (`run_id` = 16 AND `predict_date` = '2026-06-08')
    OR (`run_id` = 17 AND `predict_date` = '2026-06-09'))
  AND `scheme_id` = 't5_daily'
  AND `scheme_version` IS NULL
;

INSERT INTO `t_scheme_predictions` (
    `run_id`, `backtest_run_id`, `scheme_version`, `scheme_id`,
    `target_tenor`, `horizon`, `predict_date`, `target_date`,
    `feature_date`, `predicted_direction`, `backtest_actual_direction`,
    `confidence`, `model_version`, `extra`, `created_at`
)
WITH active_runtime AS (
    SELECT `base_scheme_id`, MIN(`runtime_type`) AS `runtime_type`
    FROM `t_scheme_registry`
    WHERE `status` = 'active'
    GROUP BY `base_scheme_id`
    HAVING COUNT(DISTINCT `runtime_type`) = 1
),
ranked_benchmark AS (
    SELECT r.*,
           ROW_NUMBER() OVER (
               PARTITION BY r.`benchmark_id`, r.`scheme_id`, r.`data_source`
               ORDER BY r.`updated_at` DESC, r.`id` DESC
           ) AS benchmark_rank
    FROM `t_backtest_runs` r
    JOIN active_runtime a ON a.`base_scheme_id` = r.`scheme_id`
    WHERE r.`status` = 'success'
      AND r.`data_source` = CASE a.`runtime_type`
          WHEN 'native_adapter' THEN 'framework_db_aligned'
          WHEN 'blackbox_v2' THEN 'blackbox_v2_current_snapshot_as_of'
      END
),
ranked_base AS (
    SELECT b.*,
           ROW_NUMBER() OVER (
               PARTITION BY b.`scheme_id`
               ORDER BY b.`updated_at` DESC, b.`id` DESC
           ) AS base_rank
    FROM ranked_benchmark b
    WHERE b.`benchmark_rank` = 1
),
selected_points AS (
    SELECT p.`run_id` AS `backtest_run_id`,
           NULLIF(JSON_UNQUOTE(JSON_EXTRACT(r.`summary`, '$.scheme_version')), 'null') AS `scheme_version`,
           p.`scheme_id`, p.`target_tenor`, p.`horizon`, p.`predict_date`,
           p.`target_date`, p.`feature_date`, p.`predicted_direction`,
           p.`label` AS `backtest_actual_direction`,
           p.`confidence`,
           JSON_REMOVE(COALESCE(p.`extra`, JSON_OBJECT()), '$.prediction_phase') AS `extra`,
           p.`created_at`
    FROM ranked_base r
    JOIN `t_backtest_predictions` p
      ON p.`run_id` = r.`id`
     AND p.`scheme_id` = r.`scheme_id`
    JOIN `t_scheme_registry` registry
      ON registry.`status` = 'active'
     AND registry.`base_scheme_id` = r.`scheme_id`
     AND registry.`target_tenor` = p.`target_tenor`
     AND registry.`horizon` = p.`horizon`
    WHERE r.`base_rank` = 1
      AND p.`predict_date` >= '2025-01-01'
)
SELECT NULL, s.`backtest_run_id`, s.`scheme_version`, s.`scheme_id`,
       s.`target_tenor`, s.`horizon`, s.`predict_date`, s.`target_date`,
       s.`feature_date`, s.`predicted_direction`,
       s.`backtest_actual_direction`, s.`confidence`,
       s.`scheme_version`, s.`extra`, s.`created_at`
FROM selected_points s
LEFT JOIN `t_scheme_predictions` existing
  ON existing.`scheme_id` = s.`scheme_id`
 AND existing.`target_tenor` = s.`target_tenor`
 AND existing.`horizon` = s.`horizon`
 AND existing.`target_date` = s.`target_date`
WHERE existing.`id` IS NULL;

UPDATE `t_scheme_predictions`
SET `extra` = JSON_REMOVE(`extra`, '$.prediction_phase')
WHERE JSON_CONTAINS_PATH(`extra`, 'one', '$.prediction_phase') = 1;

SET @bfl_024_add_backtest_index = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE `t_scheme_predictions` ADD INDEX `idx_scheme_predictions_backtest_run` (`backtest_run_id`)',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 't_scheme_predictions'
      AND index_name = 'idx_scheme_predictions_backtest_run'
);
PREPARE bfl_024_stmt FROM @bfl_024_add_backtest_index;
EXECUTE bfl_024_stmt;
DEALLOCATE PREPARE bfl_024_stmt;

SET @bfl_024_add_run_fk = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE `t_scheme_predictions` ADD CONSTRAINT `fk_scheme_predictions_run` FOREIGN KEY (`run_id`) REFERENCES `t_scheme_runs` (`run_id`) ON DELETE RESTRICT',
        'SELECT 1'
    )
    FROM information_schema.referential_constraints
    WHERE constraint_schema = DATABASE()
      AND table_name = 't_scheme_predictions'
      AND constraint_name = 'fk_scheme_predictions_run'
);
PREPARE bfl_024_stmt FROM @bfl_024_add_run_fk;
EXECUTE bfl_024_stmt;
DEALLOCATE PREPARE bfl_024_stmt;

SET @bfl_024_add_backtest_fk = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE `t_scheme_predictions` ADD CONSTRAINT `fk_scheme_predictions_backtest_run` FOREIGN KEY (`backtest_run_id`) REFERENCES `t_backtest_runs` (`id`) ON DELETE RESTRICT',
        'SELECT 1'
    )
    FROM information_schema.referential_constraints
    WHERE constraint_schema = DATABASE()
      AND table_name = 't_scheme_predictions'
      AND constraint_name = 'fk_scheme_predictions_backtest_run'
);
PREPARE bfl_024_stmt FROM @bfl_024_add_backtest_fk;
EXECUTE bfl_024_stmt;
DEALLOCATE PREPARE bfl_024_stmt;

SET @bfl_024_add_source_check = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE `t_scheme_predictions` ADD CONSTRAINT `ck_scheme_predictions_source` CHECK (((`run_id` IS NOT NULL) AND (`backtest_run_id` IS NULL) AND (`backtest_actual_direction` IS NULL)) OR ((`run_id` IS NULL) AND (`backtest_run_id` IS NOT NULL) AND (`backtest_actual_direction` IN (-1, 0, 1))))',
        'SELECT 1'
    )
    FROM information_schema.table_constraints
    WHERE constraint_schema = DATABASE()
      AND table_name = 't_scheme_predictions'
      AND constraint_name = 'ck_scheme_predictions_source'
      AND constraint_type = 'CHECK'
);
PREPARE bfl_024_stmt FROM @bfl_024_add_source_check;
EXECUTE bfl_024_stmt;
DEALLOCATE PREPARE bfl_024_stmt;

SET @bfl_024_drop_phase_index = (
    SELECT IF(
        COUNT(*) > 0,
        'ALTER TABLE `t_scheme_predictions` DROP INDEX `idx_scheme_predictions_phase`',
        'SELECT 1'
    )
    FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 't_scheme_predictions'
      AND index_name = 'idx_scheme_predictions_phase'
);
PREPARE bfl_024_stmt FROM @bfl_024_drop_phase_index;
EXECUTE bfl_024_stmt;
DEALLOCATE PREPARE bfl_024_stmt;

SET @bfl_024_drop_phase_column = (
    SELECT IF(
        COUNT(*) = 1,
        'ALTER TABLE `t_scheme_predictions` DROP COLUMN `prediction_phase`',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 't_scheme_predictions'
      AND column_name = 'prediction_phase'
);
PREPARE bfl_024_stmt FROM @bfl_024_drop_phase_column;
EXECUTE bfl_024_stmt;
DEALLOCATE PREPARE bfl_024_stmt;

SET @bfl_024_drop_updated_at = (
    SELECT IF(
        COUNT(*) = 1,
        'ALTER TABLE `t_scheme_predictions` DROP COLUMN `updated_at`',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 't_scheme_predictions'
      AND column_name = 'updated_at'
);
PREPARE bfl_024_stmt FROM @bfl_024_drop_updated_at;
EXECUTE bfl_024_stmt;
DEALLOCATE PREPARE bfl_024_stmt;
