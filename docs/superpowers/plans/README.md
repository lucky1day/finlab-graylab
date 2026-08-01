# Superpowers 实施计划索引

**文档状态**：`HISTORICAL`

**目标读者**：平台实现、测试和审计人员

**最后核验日期**：2026-08-01

| 计划 | 范围 |
|---|---|
| [Blackbox 平台输入与 FengRL 月度入库](2026-07-26-blackbox-platform-inputs-fengrl-monthly-onboarding.md) | 平台输入 registry、零写入 check-only 与五方案串行技术入库 |
| [因子排行文本溢出修复](2026-07-27-factor-ranking-overflow.md) | 固定排行榜列宽并约束长方案名和备注 |
| [仓库文档保守清理](2026-07-29-conservative-document-cleanup.md) | 删除历史兼容页和 archive 副本，修正现行入口、索引和文档门禁 |
| [cgb_causal_wk_1y 周度生产入库](2026-07-30-cgb-causal-wk-1y-production-onboarding.md) | 两文件 Intake、周度 scheduler 精确准入、技术 Gate 和生产写库切换 |
| [DataBridge 周历自测与平台部署对齐](2026-07-30-databridge-calendar-self-test-alignment.md) | 上游日历下载、平台同代 Gate、日期语义收口与文档契约验证 |
| [cgb_causal_wk_1y 已激活版本替换](2026-07-30-cgb-causal-wk-1y-version-replacement.md) | 周历输入更新、版本原子切换、历史与灰度替换、旧业务数据清理 |
| [cgb_causal_wk_3y 周度方案生产入库](2026-07-30-cgb-causal-wk-3y-production-onboarding.md) | 3Y CV2 两文件 Intake、生产激活、历史与灰度写库、前端验收和调度隔离 |
| [1Y T+1 Blackbox 生产灰度入库](2026-07-30-one-y-t1-production-gray-onboarding.md) | PR #19 修复、日频两文件交付、历史与灰度写库、前端验收及既有 launchd 挂载 |
| [week_id=200951 权威日历边界修复](2026-07-31-week-200951-calendar-boundary-repair.md) | 跨年周双表受控补数、只读验收、问题拆分和状态更新 |
| [WAVG GAPFLIP V5 五个周平均方案生产入库](2026-07-31-wavg-gapflip-v5-production-onboarding.md) | PR #20 合并、五方案 exact admission、生产激活、历史与灰度写库、前端验收和调度隔离 |
| [3Y ADYN T+1 双方案生产灰度入库](2026-07-31-three-y-adyn-t1-production-gray-onboarding.md) | PR #21 合并、双方案 exact 生命周期、历史与连续灰度写库、API/公网验收和既有 daily-gray 挂载 |
| [日度调度冗余清理第一批](2026-08-01-daily-scheduler-first-cleanup.md) | 测试先行删除孤立 capacity admission 代码、专属测试和失真日度草案，并验证生产行为边界不变 |
| [日度离线 Capacity Gate 退役](2026-08-01-daily-capacity-gate-retirement.md) | 测试先行迁移 policy v2 常量、删除离线 gate/CLI，并收敛为 attestation 专属测试 |
