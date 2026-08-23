# Dashboard 详情行资源预算调整设计

**日期：** 2026-08-23

## 现象与根因

Mac3 激活五个 M0 weekly-average 方案并分别写入 84 条回测和 1 条 gray live 后，Dashboard 总详情行数达到 20,179。`backend.factor_lab_dashboard.MAX_DETAIL_ROWS=20_000` 在 canonical payload 编码前 fail-closed，公开 API 因此返回 `503 dashboard_data_unavailable`。

只读诊断进程临时把行数上限设为 25,000 后，同一数据库快照成功生成 75 个方案的 Dashboard：raw JSON 为 1,121,481 bytes，gzip 为 73,441 bytes，分别低于既有 1,500,000 和 100,000 bytes 上限。因此数据合同、查询、Registry、回测和 live 行均有效，唯一过期的是行数保护值。

## 决策

将 `MAX_DETAIL_ROWS` 从 20,000 提高到 25,000。保持以下行为不变：

- 不截断、不分页，Dashboard 继续返回完整 canonical snapshot；
- `MAX_RAW_JSON_BYTES=1_500_000` 不变；
- `MAX_GZIP_JSON_BYTES=100_000` 不变；
- 超过 25,000 行仍 fail-closed；
- 不修改查询、排序、周覆盖诊断、API schema 或前端。

25,000 为当前 20,179 行提供有限余量，同时按当前实测密度仍与 raw/gzip 双重预算大致对齐。它不是取消资源保护。

## 验证

1. 新增边界测试：25,000 行允许编码，25,001 行抛出 `DashboardDataError`。
2. 完整测试套件通过。
3. 从新精确提交构建可重复 archive，ECS 先晋级并复验 Dashboard，再用同一 archive 晋级 Mac3。
4. Mac3 `/api/factor-lab/dashboard` 返回 200；五个 M0 DashboardGate 全部通过。

## 回滚边界

源码可通过 previous release 回滚，但在五个 M0 仍 active 且保留其回测时，回滚到 20,000 行上限会重新使 Dashboard 503。Registry、回测和 prediction 不随源码 symlink 回滚。
