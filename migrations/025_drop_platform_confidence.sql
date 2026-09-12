-- Remove only the two platform attributes; retained algorithm audit JSON is untouched.
SET @bfl_025_drop_live = (
    SELECT IF(COUNT(*) = 1,
        'ALTER TABLE `t_scheme_predictions` DROP COLUMN `confidence`',
        'SELECT 1')
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 't_scheme_predictions' AND column_name = 'confidence'
);
PREPARE bfl_025_stmt FROM @bfl_025_drop_live;
EXECUTE bfl_025_stmt;
DEALLOCATE PREPARE bfl_025_stmt;

SET @bfl_025_drop_backtest = (
    SELECT IF(COUNT(*) = 1,
        'ALTER TABLE `t_backtest_predictions` DROP COLUMN `confidence`',
        'SELECT 1')
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 't_backtest_predictions' AND column_name = 'confidence'
);
PREPARE bfl_025_stmt FROM @bfl_025_drop_backtest;
EXECUTE bfl_025_stmt;
DEALLOCATE PREPARE bfl_025_stmt;
