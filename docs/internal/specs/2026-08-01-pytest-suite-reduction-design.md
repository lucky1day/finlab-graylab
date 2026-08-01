# Pytest 回归套件初步瘦身设计

**文档状态**：`APPROVED`
**确认日期**：2026-08-01
**适用分支**：`codex/audit-bugfixes-20260613`

## 1. 背景与目标

当前 `tests/` 有 204 个测试模块、约 15.3 万行代码，pytest 收集 3356 个 case，
在当前 Mac Studio 上完整运行约 305 秒。这个规模不是由生产表面积自然产生，而是长期把
onboarding、灰度补录、真实回放、历史迁移、逐方案算法矩阵和一次性修复测试全部累积在
默认回归中造成的。

本轮目标是首次大规模瘦身：

- pytest 收集数降到约 1500～1700；
- 测试代码降到约 7～8 万行；
- 同一台 Mac Studio 的完整回归降到约 90 秒；
- 不使用 marker、默认排除或另一个隐藏测试目录伪造缩短结果；
- 不修改生产代码、方案文件、数据库、launchd 或 `master`。

## 2. 耗时证据

完整 `pytest --durations=80` 显示，约 130 个 `daily_real_replay` case 占用了接近三分钟。
其中大量 case 每项等待 2.7～4.6 秒；它们验证的是已经完成的真实回放、operator session、
历史 generation 注册和并发围栏，不是当前 launchd/direct authority 生产日批的默认职责。

逐方案测试共有约 45 个文件、2.15 万行、524 个 case。它们主要重复冻结已入库 Native
方案的算法分支、回测 adapter、gray benchmark 和 cache 构造；Blackbox 平台已经以统一
Contract、discovery、admission 和 runner 管理方案身份，不需要继续为已入库身份保留过程
测试。

## 3. 长期测试模型

默认 pytest 只保留当前平台不变量，不再保存项目实施历史。长期守护分为七类：

1. 架构和静态边界：依赖方向、输入单点、写库单点、Native core 纯净；
2. 生产调度权威：launchd 一次性入口、direct authority、scheduler 不重复挂载；
3. Repository 和数据库身份：写库边界、幂等键、当前 recovery、fail-closed；
4. Actuals：日/周/月 updater 与一次性 CLI；
5. 当前 API/前端契约：composite registry ID、task type、关键 API 与静态页面 smoke；
6. 统一方案契约：Native/Blackbox discovery、版本身份、输入截止和标准输出；
7. 当前迁移入口：migration runner、017/018 recovery 和 operator identity guard。

每条不变量原则上只保留一个正常路径、一个 fail-closed 路径和必要边界值。不再按方案、
tenor、日期、运行阶段重复展开相同平台行为。

## 4. 删除与合并范围

### 4.1 逐方案测试

删除已入库方案专属测试，包括：

- `daily_0529`、`daily_0629`、`daily_5y_2_v28`、`daily_7y_1_v28`；
- `liwei_0616_*` 的逐方案 core、backtest、gray model、cache generation；
- `monthly_0629` 和月度 source runner/reproduction adapter；
- `weekly_*_0529`、weekly average source/backtest/adapter；
- 对应 benchmark builder、prewarm、fingerprint 和 rebuild 过程测试。

用一个紧凑的、数据驱动的 active-scheme contract 测试替代：遍历 active config，只检查
runtime type、入口存在、scheme identity、标准配置可加载，以及 Native benchmark/Blackbox
delivery 契约存在。它不重复算法内部测试矩阵。

### 4.2 已完成的历史运行与补录测试

删除：

- `daily_real_replay_*` 全部测试；
- gray runner、gray gap repository、signal-gap plan/fill/native-artifact 等已完成补录链测试；
- 一次性 certification、post-onboard、benchmark 生成、refactor output compare 测试；
- 已完成 retired-scheme 数据清理脚本的专属测试。

这些生产模块或 admin 脚本若暂时仍在仓库中，不再进入默认长期回归；后续代码清理批次必须
按模块闭环删除它们，不得据此重新扩展默认测试套件。

### 4.3 历史 migration 测试

删除 005～016 的逐文件历史测试。保留：

- `test_apply_migrations.py` 中 operator 参数和数据库身份 guard；
- `test_migration_runner_module.py`；
- 017/018 当前 migration、recovery 和 `started_at` 规则。

### 4.4 通用平台矩阵去重

对以下大类删除重复身份、重复 tenor、重复日期和同构参数组合：

- Blackbox lifecycle/bootstrap/draft/history/gray/live/persistence；
- scheduler、repository、daily runtime；
- frontend/dashboard/performance/http compression；
- executor、DataBridge 和 input generation。

优先删除整个已经被另一个通用测试覆盖的模块；必须保留的文件内部，只保留代表性正常、
拒绝和边界 case。不得把多个相同断言换名保留。

## 5. 分批执行

### 批次 A：低争议删除

先删除逐方案、历史 replay/gap、一次性脚本和旧 migration 测试，并增加紧凑 active-scheme
contract。运行完整 pytest，记录收集数、代码行数和耗时。

### 批次 B：通用矩阵压缩

根据批次 A 剩余数量，对 Blackbox、daily、repository、frontend/API 大文件做代表性 case
收敛，直到达到约 1500～1700 case。每个删除组都必须先列出保留的替代守护，再执行删除。

若批次 A 已达到 90 秒但 case 仍高于目标，继续批次 B；不能用“已经够快”保留明显重复代码。

## 6. 验证与失败处理

每一批均执行：

1. 删除前记录候选 case 数和替代守护；
2. 删除后运行对应核心测试；
3. 运行完整 `python -m pytest -q`；
4. 运行 `compileall`、`git diff --check` 和生产目录边界检查；
5. 记录测试文件数、行数、case 数和 wall time。

若生产不变量缺少替代守护，先补一个紧凑通用测试，再删除重复文件。若完整回归失败，必须
修复替代守护或恢复误删测试；不得把失败测试继续删除来获得绿色结果。

## 7. 不在本轮范围

- 删除或重构 scheduler、ledger、real replay、signal-gap 等生产/运维代码；
- 修改方案算法、benchmark、source evidence 或 active config；
- 修改数据库、installed launchd 或运行中进程；
- 合并 `master` 或推送远程。

本设计和后续实施计划是本轮临时过程记录；执行完成后从工作树删除，只保留 Git 历史。
