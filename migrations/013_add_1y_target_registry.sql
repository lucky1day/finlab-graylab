-- Add 1Y active treasury as a business-visible target.
-- Idempotent: safe to run more than once on bond_db.

INSERT INTO t_target_registry
    (target_code, display_name, asset_class, target_type, sort_order, status, extra)
VALUES
    ('1Y', '1Y国债活跃', 'bond', 'active_treasury', 10, 'active', JSON_OBJECT('legacy_tenor', '1Y'))
ON DUPLICATE KEY UPDATE
    display_name = VALUES(display_name),
    asset_class = VALUES(asset_class),
    target_type = VALUES(target_type),
    sort_order = VALUES(sort_order),
    status = VALUES(status),
    extra = VALUES(extra),
    updated_at = CURRENT_TIMESTAMP;
