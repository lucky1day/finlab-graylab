-- Optional user profile fields and removal of first-login password forcing.

SET @bfl_auth_full_name_add_sql_023 = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE `t_auth_users` ADD COLUMN `full_name` VARCHAR(100) NULL AFTER `username`',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 't_auth_users'
      AND column_name = 'full_name'
);
PREPARE bfl_auth_full_name_add_023
    FROM @bfl_auth_full_name_add_sql_023;
EXECUTE bfl_auth_full_name_add_023;
DEALLOCATE PREPARE bfl_auth_full_name_add_023;

SET @bfl_auth_organization_add_sql_023 = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE `t_auth_users` ADD COLUMN `organization_name` VARCHAR(200) NULL AFTER `full_name`',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 't_auth_users'
      AND column_name = 'organization_name'
);
PREPARE bfl_auth_organization_add_023
    FROM @bfl_auth_organization_add_sql_023;
EXECUTE bfl_auth_organization_add_023;
DEALLOCATE PREPARE bfl_auth_organization_add_023;

ALTER TABLE `t_auth_users`
    MODIFY COLUMN `must_change_password` TINYINT(1) NOT NULL DEFAULT 0;

UPDATE `t_auth_users`
SET `must_change_password` = 0
WHERE `must_change_password` <> 0;
