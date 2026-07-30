# cgb_causal_wk_1y 已激活版本替换设计

## 1. 目标

将已上线的 `cgb_causal_wk_1y` 替换为 2026-07-30 上游更新交付，
重新生成历史回测和灰度实盘数据，并让前端只展示新版本结果。

本次不创建新方案 ID、不改变 Registry 业务身份、不启用周度 scheduler，
也不修改模型、特征、训练、投票或信号方向逻辑。

## 2. 根因与改动定级

新旧脚本的 AST 差异仅位于周历输入：

- 删除内嵌周历、周号外推和 calendar override 兜底；
- 新增对 `<data-dir>/api_wind_date.csv` 的强制读取；
- 周历缺失、字段非法或请求超出覆盖范围时 fail-closed。

Metadata 字节不变，模型函数不变。在相同 `sample_data + api_wind_date.csv`
和 8 条交付 Request 上，新旧输出逐字节一致。

因此本次属于 L0 平台 I/O 与日期适配，不属于算法内部 L2 改动。生产输入仍由
平台 `api-wind-date-v1` 组合输入提供，不接收上游包内的日历文件作为生产真值。

## 3. 版本和数据边界

- 旧版本：`05022a0eeec7`
- 旧脚本 SHA256：
  `90f3abcc1501eb7173c706fc2ad5d76fda89bf9004c88ee976b98378bbb6d450`
- 新脚本 SHA256：
  `cf79ab53433ba63cb05ab23d5aacf7cc33e698d37f31cbbba0e81688ab646172`
- Metadata SHA256：
  `efc8e03c5db98c33f0b830d62cc4465f7f890b5a783de68fee58e4ae19d4162b`
- 旧历史回测：run `192`，72 条 prediction，17 条 monthly metric
- 旧灰度：9 条 prediction，对应 run `1640..1648`

新代码产生新的 canonical `scheme_version`。旧 `t_scheme_versions` 行不物理删除，
而是转为 `retired`，以保留不可变审计链。用户要求删除的“上个版本数据”解释为：

- 删除旧历史 run 及其逐周、月度和复现检查子记录；
- 删除旧灰度 prediction、run 和 run log；
- 不删除 actual 事实表、Registry 行、Harness 审计和版本身份。

## 4. 切换设计

平台新增最小化的已激活 Blackbox 版本替换事务：

1. 锁定同一 base scheme 的旧、新 exact version 和 Registry；
2. 核对旧版本为唯一 active，新版本与当前代码身份完全一致；
3. 将新版本写为 active，并写入批准人、批准时间和 all-stage 证据；
4. 将旧版本转为 retired；
5. 保持 composite Registry 全程 active；
6. 事务内读回并拒绝多 active、身份漂移或 Registry 漂移。

该事务只解决版本指针切换，不写回测或 live 业务表。

## 5. 回测与灰度替换顺序

切换采用“新结果先成功、旧结果后删除”：

1. 新脚本在当前 active 配置下完成 all-stage recertification；
2. 执行版本替换事务；
3. 持久化新历史回测，确认 canonical latest 和前端已选择新 run；
4. 精确删除旧 run `192`；
5. 对 9 个灰度目标先在库外完成新脚本计算；
6. 每个目标在单事务内删除旧 prediction，写入新 run/prediction/log，
   再删除旧 run/log，避免唯一键冲突和无结果空窗；
7. 最后确认旧版本的回测和 live 业务数据计数均为零。

灰度替换不伪造 scheduler 运行，仍使用 `prediction_phase=gray_live`。
周度 scheduler 按用户当前要求继续保持未挂载状态。

## 6. 前端更新

前端不改静态代码。`/api/backtests/factor-lab` 按 latest-success 规则自动选择
新 run；实盘 API 继续按 active Registry 读取同一 composite scheme。更新通过
数据库读回、API 响应和前端验证脚本完成。

## 7. 验收

- 新交付文件和 Metadata 摘要精确；
- all-stage 七 Gate 通过，且业务表零写入；
- 历史 72 周和灰度 9 周日期、方向、Contract 字段完整；
- 新回测目标全部早于 `2026-06-01`，灰度目标覆盖
  `2026-06-05..2026-07-31`；
- 新版本唯一 active，旧版本为 retired，Registry 仍 active；
- 旧 run `192` 和旧灰度 run/prediction/log 已删除；
- API/前端只返回新回测和新灰度数据；
- scheduler 仍未加载该方案；
- 相关测试、编译、`git diff --check` 通过。

