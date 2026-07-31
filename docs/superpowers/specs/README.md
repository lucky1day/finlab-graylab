# Superpowers 设计记录索引

**文档状态**：`HISTORICAL`

**目标读者**：平台架构、实现和审计人员

**最后核验日期**：2026-07-31

| 设计记录 | 范围 |
|---|---|
| [Blackbox V2 周历平台输入设计](2026-07-26-blackbox-v2-week-calendar-input-design.md) | DataBridge 三文件父快照、版本化周历制品、组合身份和来源 provenance |
| [因子排行文本溢出](2026-07-27-factor-ranking-overflow-design.md) | 方案名、准确率和备注列的稳定布局契约 |
| [仓库文档保守清理](2026-07-29-conservative-document-cleanup-design.md) | 第一阶段只删除历史兼容页和 archive 副本，并同步修正入口、索引与文档门禁 |
| [cgb_causal_wk_1y 周度方案生产入库](2026-07-30-cgb-causal-wk-1y-production-onboarding-design.md) | 1Y 周度 Blackbox V2 的输入、历史/灰度分区、写库、激活和自然调度边界 |
| [DataBridge 周历自测与平台部署对齐](2026-07-30-databridge-calendar-self-test-alignment-design.md) | 上游四文件自测、平台同代验收、组合输入身份和生产 generation 滚动规则 |
| [cgb_causal_wk_1y 已激活版本替换](2026-07-30-cgb-causal-wk-1y-version-replacement-design.md) | L0 周历输入更新、exact version 切换、历史与灰度原子替换边界 |
| [cgb_causal_wk_3y 周度方案生产入库](2026-07-30-cgb-causal-wk-3y-production-onboarding-design.md) | 3Y CV2 两文件 Intake、算法保真、历史/灰度写库、调度隔离和前端验收 |
| [1Y T+1 Blackbox 生产灰度入库](2026-07-30-one-y-t1-production-gray-design.md) | PR #19 修复、两文件交付生命周期、历史/灰度分区和既有 launchd 日频执行边界 |
| [week_id=200951 权威日历边界修复](2026-07-31-week-200951-calendar-boundary-repair-design.md) | 跨年周双表最小补数、数据库身份围栏、历史复现和 benchmark 漂移拆分 |
| [3Y ADYN T+1 双方案生产灰度入库](2026-07-31-three-y-adyn-t1-production-gray-design.md) | PR #21 两文件交付、exact 生命周期、历史/灰度分区和既有 daily-gray 日频执行边界 |
