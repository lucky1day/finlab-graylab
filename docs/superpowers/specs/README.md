# Superpowers 设计记录索引

**文档状态**：`HISTORICAL`

**目标读者**：平台架构、实现和审计人员

**最后核验日期**：2026-07-30

| 设计记录 | 范围 |
|---|---|
| [Blackbox V2 周历平台输入设计](2026-07-26-blackbox-v2-week-calendar-input-design.md) | DataBridge 三文件父快照、版本化周历制品、组合身份和来源 provenance |
| [因子排行文本溢出](2026-07-27-factor-ranking-overflow-design.md) | 方案名、准确率和备注列的稳定布局契约 |
| [仓库文档保守清理](2026-07-29-conservative-document-cleanup-design.md) | 第一阶段只删除历史兼容页和 archive 副本，并同步修正入口、索引与文档门禁 |
| [cgb_causal_wk_1y 周度方案生产入库](2026-07-30-cgb-causal-wk-1y-production-onboarding-design.md) | 1Y 周度 Blackbox V2 的输入、历史/灰度分区、写库、激活和自然调度边界 |
| [DataBridge 周历自测与平台部署对齐](2026-07-30-databridge-calendar-self-test-alignment-design.md) | 上游四文件自测、平台同代验收、组合输入身份和生产 generation 滚动规则 |
