# T1 Daily SHAP 退役与精确版本发布设计

**文档状态**：`HISTORICAL`

**目标读者**：平台开发和历史审计人员

**最后核验日期**：2026-08-02

## 目标

完成僵尸代码清理 P0 的最后一项：退役运行图不可达的
`schemes/t1_daily/core/shap_analysis.py`，同时保持 Native V1 源算法保真、精确版本
身份和 launchd daily-gray 控制面的 fail-closed 约束。

本设计只完成开发分支上的可验证发布单元，不将代码删除等同于生产激活。生产数据库
版本激活、installed plist、`launchctl` reload 和生产进程重启必须在独立维护窗口取得
明确授权后执行。

## 已确认事实

- `shap_analysis.py` 在当前 prediction、backtest、API、frontend 和 scheduler 运行图中
  不可达；模块内符号没有仓库外部调用入口或动态发现约定。
- 原始 runner 在形成 prediction、probability 和 vote 后才计算 SHAP；SHAP 不参与模型
  方向、投票、fallback 或 score 映射。
- 当前平台不写 `t_shap`；该表仅作为受保护的历史只读表存在。
- 当前文件 SHA-256 为
  `34597d21b02cfa852c8cadcbcb93ab5f47a17f52f3a400e8557dcfc79fddcdaf`。
- 当前 `t1_daily` 精确版本为 `bdf54ed4cfc0`；删除该文件后代码哈希变为
  `f86ef896620f3793df6bfaefe8063776f2203762f3e20ff61c1d32336be340e5`，
  精确版本变为 `7898b9e47a9a`，config hash 保持不变。
- `deploy/daily_gray_launchd_policy_v1.json` 当前冻结旧版本；裸删会导致 policy 与
  strict discovery 漂移，并在算法或写库前 fail-closed。

## 方案选择

采用“证据归档 + 运行时代码删除 + 精确 policy 更新 + 回归门禁”的协调发布方案。

未采用的方案：

1. 裸删模块：会破坏 daily-gray 精确身份，不能部署。
2. 永久保留模块：规避版本变化，但无法完成本轮僵尸代码治理目标。
3. 将 SHAP 从代码哈希排除：削弱 Native bundle 完整性，违反精确版本设计。
4. 改名为方案内 `core/legacy_*.py.txt`：虽然是已有 legacy 证据惯例且不会进入
   `core/**/*.py` 哈希，但仍把退役实现留在 active 方案目录，不符合本轮清理目标。
5. 只依赖 Git history：可以恢复原字节，但不能在当前证据批次中直接核验来源与退役
   哈希，因此不作为本次首选。

## 变更设计

### 1. 原始证据归档

将退役模块的原始字节以非运行扩展名归档到
`source_evidence/benchmark_batches/model_muti_0529/retired/t1_daily/shap_analysis.py.source`。
在批次 `manifest.json` 中记录相对路径、SHA-256、原运行路径和退役原因，并在批次
README 中说明：该文件只用于审计和历史解释输出复核，不是 runtime input，不允许被
方案 import。

这是对 `model_muti_0529` 批次 manifest 的显式扩展，不冒充已有字段。新增字段采用
`retired_artifacts` 数组；该 manifest 当前没有 runtime loader，测试负责约束字段结构、
文件哈希和零运行时依赖。选择批次级证据目录而不是 active `core/`，是为了让运行树只
保留仍参与执行的代码。

归档文件必须与删除前模块逐字节一致。不得把个人路径、数据库凭据或未纳管的原始包
一起纳入仓库。

### 2. 运行时代码退役

删除 `schemes/t1_daily/core/shap_analysis.py`。不修改 `predict.py`、
`lgbm_predictor.py`、特征、窗口、模型参数、投票、fallback、score 映射、输入 artifact
或 PredictionRecord 结构；不删除 Python `shap` 依赖，也不清理其它零引用符号。

该变更分级为 L0：退役已停止消费的解释输出实现，不改变预测算法。验收表述只能是
“预测运行图与数值口径不变”；由于当前 CompareGate 不覆盖 SHAP explanation，不能
宣称新版本仍产生或复现平台 SHAP 输出。

### 3. 精确版本与 launchd policy

将 `deploy/daily_gray_launchd_policy_v1.json` 中 `t1_daily.scheme_version` 更新为
`7898b9e47a9a`。策略的 label、entrypoint、执行数量、目标数量、runtime type、task type、
horizon、tenor 和 execution class 全部保持不变。

仓库 policy 更新只表示候选发布包内部自洽，不表示 installed plist 或生产数据库已
完成切换。

### 4. 自动化门禁

先增加会在旧仓库状态失败的治理测试，再实施删除：

- 断言运行时 SHAP 模块不存在；
- 断言归档文件存在、SHA-256 精确且不位于 runtime tree；
- 断言 manifest 的原路径、归档路径和 SHA-256 与文件一致；
- 断言 strict discovery 得到 `t1_daily@7898b9e47a9a`；
- 断言 repository daily-gray policy 能加载且冻结同一版本；
- 断言 `t1_daily` 的 config hash 和非 SHAP runtime 文件未被本发布单元意外改动。

随后运行相关单测、Native 静态/单元/无持久化 Gate，以及仓库全量测试。任何预测方向、
内部数值、输入 artifact、registry composite identity 或调度基数变化均阻断发布。

### 5. 执行期 ApiReadinessGate 阻塞修正

执行 `all --check-only` 时，Native `ApiReadinessGate` 对
`/api/backtests/factor-lab` 发起了未过滤请求；现场响应约 2.86 MB，因正确触发 probe 的
1 MiB fail-closed 响应上限而阻塞。相同端点按
`benchmark_id=model_muti_0529` 过滤后约 621 KB，证明后端过滤、展示语义和 1 MiB 防护均
无需修改；该阻塞不是后端 API 缺陷。

Gate 已经从最新成功的 `t_backtest_runs` 行取得发布验收上下文，因此 readiness probe
应携带该行的 `benchmark_id`，并在证据中记录 `latest_backtest_benchmark_id`；仅当该字段
缺失时回退到原未过滤 URL。通用 `ApiGate`、Blackbox `data_source` 过滤、后端响应形状和
响应大小上限保持不变。此前“不修改 Native Gate”的边界只约束 SHAP 退役发布单元本身；
本节记录的是为解除实际验收阻塞而单独授权的窄范围 follow-up。

## 发布与回滚边界

开发分支完成条件：代码、证据、policy 和测试构成单一提交序列，所有离线门禁通过，
且工作区无无关产物。不得在该阶段声称生产已激活。

生产切换需要独立核验并授权：

1. 为 `t1_daily@7898b9e47a9a` 生成绑定同版本的一次性 activation authorization；
2. 经 Native activation 流程将精确版本写为 active，并保持两个 composite registry
   identity active；
3. 在避开 07:00 调度的维护窗口核对仓库 policy、installed plist、loaded label 和
   当前生产进程；
4. reload/restart 后验证 strict discovery、policy、DB active version 与 launchd
   控制面一致；
5. 对下一次真实 occurrence 核对版本、run fence、prediction linkage 和可见性。

若生产切换前任一门禁失败，保持旧代码和旧 policy 运行。若切换后需要回滚，必须作为
另一次精确版本切换恢复 `bdf54ed4cfc0` 对应代码与 policy；不得只回滚单个文件或单独
修改数据库状态。

## 非目标

- 不处理 `docs/TODO.md` 中独立的“日频真实 ledger 收口”生产 P0；
- 不应用 migration 018，不补历史信号，不生成 production authority；
- 不操作生产数据库、installed plist、`launchctl` 或服务进程；
- 不修改 Native 算法逻辑，不扩展 SHAP 展示功能；
- 不删除 `t_shap` 历史数据或保护规则。
