# 实盘预测目标区间起点设计

## 目标

因子实验室的“实盘预测目标区间”从方案部署日期之后首个实际预测的
`target_date` 开始显示。部署当天不计入，即只接受
`predict_date > deployed_at` 的记录。

## 现状

前端仅从 `phaseRanges` 中寻找 `scheduled_live`。因此有效的
`gray_live` 记录不会形成区间起点，并被显示为“待产生”。这将信号来源审计
字段误用为用户可见的实盘区间规则。

## 设计

- 在前端从方案的 `liveRows` 中筛选 `predictDate > deploymentDate` 的记录。
- 按 `predictDate` 升序选择首条记录，使用其 `targetDate` 作为区间起点。
- 不区分 `gray_live` 与 `scheduled_live`；两者保留为记录的来源审计字段。
- 若不存在部署后的实盘记录，继续显示“实盘预测目标区间：待产生”。
- 不修改 Dashboard API、数据库数据、历史预测阶段、launchd 或算法。

## 测试设计

扩展现有前端 Node 契约测试，覆盖：

- 部署后的 `gray_live` 首条记录可给出目标区间起点；
- 部署后的 `scheduled_live` 记录可给出目标区间起点；
- 部署当天及之前的记录不计入；
- 没有部署后记录时仍显示“待产生”。

## 完成标准

- 区间起点只由部署后首个实盘预测的目标日决定；
- `prediction_phase` 不再参与该展示判断；
- 聚焦前端测试、相关前端契约测试及 `git diff --check` 通过；
- 改动仅限前端展示逻辑和对应测试。
