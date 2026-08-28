-- Registry owner 的一次性历史权威回填；运行时不保留第二份 owner 映射。
DROP TEMPORARY TABLE IF EXISTS `_bfl_registry_owner_guard_021`;
DROP TEMPORARY TABLE IF EXISTS `_bfl_registry_owner_021`;

CREATE TEMPORARY TABLE `_bfl_registry_owner_021` (
    `scheme_id` VARCHAR(64) NOT NULL,
    `owner` VARCHAR(64) NOT NULL,
    PRIMARY KEY (`scheme_id`),
    CONSTRAINT `chk_bfl_registry_owner_021` CHECK (
        `owner` <> ''
        AND `owner` = TRIM(`owner`)
        AND LOWER(`owner`) NOT IN ('--', 'unknown', '待定')
        AND `owner` NOT REGEXP '[<>[:cntrl:]]'
    )
) ENGINE=InnoDB;

INSERT INTO `_bfl_registry_owner_021` (`scheme_id`, `owner`) VALUES
    ('cgb_a4_fundseason_10y__h1__10Y', 'rl'),
    ('cgb_a4_fundseason_1y__h1__1Y', 'rl'),
    ('cgb_a4_fundseason_3y__h1__3Y', 'rl'),
    ('cgb_a4_fundseason_5y__h1__5Y', 'rl'),
    ('cgb_a4_fundseason_7y__h1__7Y', 'rl'),
    ('cgb_causal_wk_1y__h1__1Y', 'wg'),
    ('cgb_causal_wk_1y_v128__h1__1Y', 'wg'),
    ('cgb_causal_wk_3y__h1__3Y', 'wg'),
    ('daily_10y_lgbm_10y04_0629__h1__10Y', 'rl'),
    ('daily_1y_xgb_1y13_0629__h1__1Y', 'rl'),
    ('daily_5y_2_v28__h5__5Y', 'rl'),
    ('daily_5y_lgbm_5y10_0629__h1__5Y', 'rl'),
    ('daily_7y_1_v28__h5__7Y', 'rl'),
    ('five_y_factor_rule_online_v1__h1__5Y', 'liwei'),
    ('five_y_t5_lgbm_3y_anti_lag252_b8_v1__h5__5Y', 'lw'),
    ('five_y_t5_lgbm_3y_z_anti180_b12_v1__h5__5Y', 'lw'),
    ('five_y_t5_xgb_spr_3y1y_b8_v1__h5__5Y', 'lw'),
    ('liwei_0616_10y01_cons_say_k3_div_k10__h5__10Y', 'lw'),
    ('liwei_0616_10y01_full_oos_k3_div_k10__h5__10Y', 'lw'),
    ('liwei_0616_10y02_cons_say_k3_div_k5__h5__10Y', 'lw'),
    ('liwei_0616_5y01_full_oos_k3_div_k10__h5__5Y', 'lw'),
    ('liwei_0616_5y_auc_static_all_k3_div_k10__h5__5Y', 'lw'),
    ('liwei_0616_5y_auc_yearly_all_k3_div_k10__h5__5Y', 'lw'),
    ('liwei_0616_5y_ic_yearly_all_k3_div_k10__h5__5Y', 'lw'),
    ('liwei_0616_7y01_cons_say_k3_div_k10__h5__7Y', 'lw'),
    ('liwei_0616_7y03_cons_all_k3_div_k8__h5__7Y', 'lw'),
    ('liwei_0616_cons_sda_k3_div_k10__h5__5Y', 'lw'),
    ('m0_annual_avg_sf_10y_v1__h1__10Y', 'fengrl'),
    ('m0_annual_avg_sf_1y_v1__h1__1Y', 'fengrl'),
    ('m0_annual_avg_sf_3y_v1__h1__3Y', 'fengrl'),
    ('m0_annual_avg_sf_5y_v1__h1__5Y', 'fengrl'),
    ('m0_annual_avg_sf_7y_v1__h1__7Y', 'fengrl'),
    ('m0_monthly_avg_mid_10y_v1__h1__10Y', 'fengrl'),
    ('m0_monthly_avg_mid_1y_v1__h1__1Y', 'fengrl'),
    ('m0_monthly_avg_mid_3y_v1__h1__3Y', 'fengrl'),
    ('m0_monthly_avg_mid_5y_v1__h1__5Y', 'fengrl'),
    ('m0_monthly_avg_mid_7y_v1__h1__7Y', 'fengrl'),
    ('m0_quarterly_avg_10y_v1__h1__10Y', 'fengrl'),
    ('m0_quarterly_avg_1y_v1__h1__1Y', 'fengrl'),
    ('m0_quarterly_avg_3y_v1__h1__3Y', 'fengrl'),
    ('m0_quarterly_avg_5y_v1__h1__5Y', 'fengrl'),
    ('m0_quarterly_avg_7y_v1__h1__7Y', 'fengrl'),
    ('m0_weekly_avg_10y_v1__h1__10Y', 'fengrl'),
    ('m0_weekly_avg_1y_v1__h1__1Y', 'fengrl'),
    ('m0_weekly_avg_3y_v1__h1__3Y', 'fengrl'),
    ('m0_weekly_avg_5y_v1__h1__5Y', 'fengrl'),
    ('m0_weekly_avg_7y_v1__h1__7Y', 'fengrl'),
    ('monthly_10y_rf_top5_0629__h30__10Y', 'rl'),
    ('monthly_1y_rf_top30_0629__h30__1Y', 'rl'),
    ('monthly_5y_knn_top20_0629__h30__5Y', 'rl'),
    ('one_y_t1_quote_state_hv_v1__h1__1Y', 'lw'),
    ('one_y_t5_liq_excess_a_v1__h5__1Y', 'lw'),
    ('one_y_t5_liq_excess_a_w252_l7_v1__h5__1Y', 'lw'),
    ('one_y_t5_liq_excess_a_w350_l7_v1__h5__1Y', 'lw'),
    ('one_y_t5_liq_excess_b_w252_l7_v1__h5__1Y', 'lw'),
    ('one_y_t5_xgb_10y_streak_anti7_b8_v1__h5__1Y', 'lw'),
    ('one_y_t5_xgb_7y_cond_rev20_b12_v1__h5__1Y', 'lw'),
    ('one_y_t5_xgb_spr_zrev_10y5y_b12_v1__h5__1Y', 'lw'),
    ('seven_y_current55_lgbm_001_v2__h1__7Y', 'lw'),
    ('seven_y_current55_lgbm_002_v2__h1__7Y', 'lw'),
    ('seven_y_t5_lgbm_bf_z_anti40_b8_v1__h5__7Y', 'lw'),
    ('seven_y_t5_xgb_7y_rv_rev20_b0_v1__h5__7Y', 'lw'),
    ('seven_y_t5_xgb_bf_z_anti40_b0_v1__h5__7Y', 'lw'),
    ('t1_daily__h1__10Y', 'rl'),
    ('t1_daily__h1__5Y', 'rl'),
    ('t5_daily__h5__10Y', 'rl'),
    ('t5_daily__h5__3Y', 'rl'),
    ('t5_daily__h5__5Y', 'rl'),
    ('t5_daily__h5__7Y', 'rl'),
    ('ten_y_factor_level_ensemble_v1__h1__10Y', 'liwei'),
    ('ten_y_t5_curve_spread_adapt90_v1__h5__10Y', 'lw'),
    ('ten_y_t5_maj3_k3_ic_static_v1__h5__10Y', 'lw'),
    ('ten_y_t5_maj4_k3_ic_static_v1__h5__10Y', 'lw'),
    ('ten_y_t5_maj4_k3_ic_yearly_v1__h5__10Y', 'lw'),
    ('ten_y_t5_say_k5_sharpe_static_v1__h5__10Y', 'lw'),
    ('ten_y_t5_short_amp_adapt756_v1__h5__10Y', 'lw'),
    ('three_y_adyn_lb1_k3_v1__h1__3Y', 'lw'),
    ('three_y_adyn_lb2_k1_v1__h1__3Y', 'lw'),
    ('three_y_t5_lgbm_7yanti_b12_v2__h5__3Y', 'lw'),
    ('three_y_t5_xgb_fxlead_b8_v2__h5__3Y', 'lw'),
    ('three_y_t5_xgb_tp_5y1y_b12_v2__h5__3Y', 'lw'),
    ('wavg_10y_gapflip_v5__h1__10Y', 'rl'),
    ('wavg_1y_gapflip_v5__h1__1Y', 'rl'),
    ('wavg_3y_gapflip_v5__h1__3Y', 'rl'),
    ('wavg_5y_gapflip_v5__h1__5Y', 'rl'),
    ('wavg_7y_gapflip_v5__h1__7Y', 'rl'),
    ('weekly_10y_d_overlay_0529__h6__10Y', 'rl'),
    ('weekly_10y_hetero_vote_v1__h1__10Y', 'lw'),
    ('weekly_10y_lgbm_point_v1__h1__10Y', 'wg'),
    ('weekly_1y_causal_v1_31_0_standalone__h1__1Y', 'CWG'),
    ('weekly_5y_curve_logit_v1__h1__5Y', 'lw'),
    ('weekly_5y_direct_0529__h6__5Y', 'wg'),
    ('weekly_7y_cross_d_overlay_0529__h6__7Y', 'wg'),
    ('weekly_avg_10y_lgbm_0529__h6__10Y', 'rl'),
    ('weekly_avg_1y_lgbm_0529__h6__1Y', 'rl'),
    ('weekly_avg_5y_lgbm_0529__h6__5Y', 'rl');

CREATE TEMPORARY TABLE `_bfl_registry_owner_guard_021` (
    `violation` TINYINT NOT NULL,
    CONSTRAINT `chk_bfl_registry_owner_guard_021` CHECK (`violation` = 0)
) ENGINE=InnoDB;

INSERT INTO `_bfl_registry_owner_guard_021` (`violation`)
SELECT 1
WHERE (SELECT COUNT(*) FROM `_bfl_registry_owner_021`) <> 96;

INSERT INTO `_bfl_registry_owner_guard_021` (`violation`)
SELECT 1
FROM `t_scheme_registry` AS registry
LEFT JOIN `_bfl_registry_owner_021` AS authority
  ON BINARY authority.`scheme_id` = BINARY registry.`scheme_id`
WHERE authority.`scheme_id` IS NULL
LIMIT 1;

INSERT INTO `_bfl_registry_owner_guard_021` (`violation`)
SELECT 1
FROM `_bfl_registry_owner_021` AS authority
LEFT JOIN `t_scheme_registry` AS registry
  ON BINARY registry.`scheme_id` = BINARY authority.`scheme_id`
WHERE registry.`scheme_id` IS NULL
LIMIT 1;

SET @bfl_registry_owner_add_sql_021 = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE `t_scheme_registry` ADD COLUMN `owner` VARCHAR(64) NULL AFTER `name`',
        'SELECT 1'
    )
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 't_scheme_registry'
      AND column_name = 'owner'
);
PREPARE bfl_registry_owner_add_021
    FROM @bfl_registry_owner_add_sql_021;
EXECUTE bfl_registry_owner_add_021;
DEALLOCATE PREPARE bfl_registry_owner_add_021;

INSERT INTO `_bfl_registry_owner_guard_021` (`violation`)
SELECT 1
FROM `t_scheme_registry` AS registry
INNER JOIN `_bfl_registry_owner_021` AS authority
  ON BINARY authority.`scheme_id` = BINARY registry.`scheme_id`
WHERE registry.`owner` IS NOT NULL
  AND (
      registry.`owner` = ''
      OR registry.`owner` <> TRIM(registry.`owner`)
      OR LOWER(registry.`owner`) IN ('--', 'unknown', '待定')
      OR registry.`owner` REGEXP '[<>[:cntrl:]]'
      OR BINARY registry.`owner` <> BINARY authority.`owner`
  )
LIMIT 1;

UPDATE `t_scheme_registry` AS registry
INNER JOIN `_bfl_registry_owner_021` AS authority
  ON BINARY authority.`scheme_id` = BINARY registry.`scheme_id`
SET registry.`owner` = authority.`owner`;

ALTER TABLE `t_scheme_registry`
    MODIFY COLUMN `owner` VARCHAR(64) NOT NULL AFTER `name`;

DROP TEMPORARY TABLE `_bfl_registry_owner_guard_021`;
DROP TEMPORARY TABLE `_bfl_registry_owner_021`;
