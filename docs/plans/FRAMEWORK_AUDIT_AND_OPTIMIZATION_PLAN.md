# 灰度实验室框架审查与可维护性治理计划

**文档状态** `CURRENT`（执行中的计划，不授予生产操作权限）
**整理日期** 2026-11-11（Asia/Shanghai）
**适用基线** `codex/develop` @ `d9a28960`

## 1. 目标与硬约束

目标：让框架可维护、出错可定位。只优化效率和稳定性，**不改变任何功能**。

以下约束优先于本计划中的任何条目：

1. **前端展示零修改。** `frontend/` 不改动。Dashboard V7 响应、HTTP 路由、CLI 命令与参数、数据库 schema、业务键、日期和统计语义均作为兼容基线。
2. **两台机器的定时任务保持稳定。** Mac3（launchd）和 ECS（systemd）上的每个定时任务行为不变：入口、退出码、stdout JSON 摘要、写库语义、失败归类和重试策略都不变。新增的日志只走 stderr 或 logger。
3. **不改数据。** 不做 DDL、历史数据变更、Registry 变更、激活或调度安装。凡是涉及这些的条目，只列在第 5 节，等待单独授权。
4. **不改算法字节。** W4 九套 Native 与所有 Blackbox 交付文件不改；`week_id` 和交易日历逻辑只报告不改。
5. **合入 `codex/develop` 不等于部署。** 生产仍按[部署手册](../../deploy/README.md)的 immutable release 流程，另行核验和晋级。

## 2. 上一轮审查（B01–B10）的落地情况

2026-11-09 的审查发现已全部合入 `codex/develop`，各条目的原始描述见 Git 历史中本文的上一版。

| 范围 | 提交 |
| --- | --- |
| B01 DataBridge 提交点、B02 启动围栏 | `a1e9dc6d` |
| B03 内存探针、B04 取消结算、B05 运行视图清理、B06 名单非阻塞读取、B07 预算、B08 列表乱序、B10 空指标 | `e385289f` |
| B09 脚本策略摘要抽取 | `77053c08` |
| F 别名与绝对路径策略升级 | `c0f3670f` |
| E CI 与重复实现精简 | `1a600bde` |

## 3. 本轮审查结论（2026-11-11，只读）

覆盖 scheduler、shared、harness/scripts、backend/frontend、migrations/deploy/docs、schemes/tests 六个区域。

总体判断：**几乎没有死代码，重量主要来自下面五类。**

| 类型 | 证据 | 量级 |
| --- | --- | --- |
| ① 一次性迁移恢复代码常驻 | `migrations/_schema.py` 4.4k 行（手写的 016 基线结构和 017 账本指纹）；`_recovery_state.py` 1k 行；`runner.py` 中 017–025 的逐版本 inspect/recover；`apply_migrations.py` 有 16 个恢复模式 | 约 8–9k 行 |
| ② 一次性兼容层进入运行时读路径 | 为单个方案 `liwei_0616_5y01_full_oos_k3_div_k10`（333 条事实）做的历史替换，分布在 backend 查询、两个 gate 和 `history_cleanup`；DataBridge v1/v2/四文件 current 升级分支；v2 生成缓存 | 约 2–3k 行，另有 3.3k 行证据 JSON |
| ③ 同一不变量多层重复校验 | Native 一次实盘运行要校验身份或目标 5–6 次；Dashboard payload 在后端出口、前端解码、前端提交时各校验一遍；补缺 plan 在同进程生成后又重新规范化；runtime_view 复制文件做 4 次 stat | 约 2–3k 行 |
| ④ 不可达的设计 | `historical_as_of` 快照模式无生产调用方；`process_started`/`process_fence` 仅测试传入；`canonical_config_trusted` 在生产调用中恒为 True；`repository.py` 导出了只有测试使用的兼容入口；多期限 `deliveries:` 只服务 2 个 paused 方案；4 个管理员端点前端不调用 | 约 1.5k 行 |
| ⑤ 失败难以定位 | `one_shot_prediction_runner` 没有 logger；`_execute_candidate` 的 `except Exception` 只保留错误码；未预期异常一律归为 `configuration_error`；`harness/gates/base.guarded_result` 只保留 `str(exc)`；`refresh_data_bridge_current` 静默 `return False` | 维护成本最高的一项 |

结构问题：

- **职责混杂的大文件：** `harness/gates/data_consistency_gate.py`（3.6k 行，实际是 4 个 gate 加一层取数）、`shared/data_bridge/refresh.py`（2.4k 行）、`scheduler/executor.py` 与 `scheduler/persistence/predictions.py`（各约 1.8k 行）。
- **重复实现：** 交易日历和三日期语义有 5 份；日期规范化约 10 处；`_is_sha256`、规范 JSON、fsync、原子写各 3–5 份；DB Engine 工厂 3 份，且时区设置不一致。
- **层次错位：**
  - gray replay 执行器放在 scheduler，唯一调用方却是 harness；
  - DataBridge gate 状态机写在 `scripts/refresh_data_bridge_current.py`；
  - 持久化层硬编码了 `t1_daily`/`t5_daily`；
  - 月度入口 `run_close_predictions` 先完整跑一遍发现和预检，再调用 launchd 入口重跑一遍。
- **文档：** 已被取代的草案 `DETERMINISM_HOTFIX_ACTIVATION_DRAFT` 仍在树中（已删除）；`architecture/parity/five_y_factor_rule_online_v1.md` 写着“待落补丁”，需要确认是否已结案后再处理；`CURRENT_STATUS.md` 已演变为发布日志；`TODO.md` 中有过期的观察项。

## 4. 可直接实施的批次（满足第 1 节约束）

每批单独提交。每批先跑完整非 MySQL pytest 和 Node 状态机测试，结果与基线一致后再推进。

### 批次 1：可定位性（只增加诊断信息，不改输出合同）

- one-shot 入口加 logger。`_execute_candidate` 和 CLI 兜底分支用 `logger.exception` 把 traceback 写到 stderr。stdout JSON、退出码、失败码保持不变。
- harness 的 `guarded_result`、gap plan 内部错误和 `refresh_data_bridge_current` 的静默分支，同样把 traceback 写到 stderr 或 logger，结构化结果不变。
- 补上 `scheduler/executor.py` 和 `harness/cli.py` 中缺失的 `Mapping` import。
- **验收：** 现有测试全部通过；新增测试注入异常，断言 stderr 中含 traceback，且 stdout 摘要与改动前逐字段相等。

### 批次 2：删除不可达代码（无生产调用方）

- 删除 executor 的 `historical_as_of` 快照模式，以及只服务于它的参数、校验函数和死分支。
- `repository.py` 门面不再导出只有测试使用的名字（尾部修复整套、4 个兼容入口版 `upsert_*_actuals`、私有函数），同步更新钉住这些名字的 facade 测试。
- 删除 `shared/period_average_buckets.detect_spring_boundary` 及其测试。
- **验收：** 用 grep 证明删除项没有生产调用方；完整测试通过；`python -m harness --help` 和 one-shot `--help` 冒烟通过。

### 批次 3：结构收敛（行为等价的重构）

每项单独提交，回归测试必须覆盖该项涉及的入口。

- **3A** 两套几乎相同的进程组启动和清理代码（`executor._run_process_group` 与 `blackbox_v2_runner._run_process`）合并到 `scheduler.process_control`。由 `tests/test_process_lifecycle.py` 兜底。
- **3B** 新建 `shared/_fs.py`（稳定读取、fsync、原子写）和 `shared/_digest.py`（规范 JSON、sha256 判断），替换上述重复实现。
- **3C** `data_consistency_gate` 按 snapshot/completeness/lineage/facts/representation 拆成子模块，对外接口不变。
- **3D** `shared/data_bridge/refresh.py` 拆分为 errors/manifest/store/refresh。截止键解析下沉为纯函数，消除 authority 与 `_input_artifacts` 之间的循环导入。
- **3E** 名字叫 legacy、实际在跑 v3 生产链路的 `allow_legacy_v1_period_fallback` 改名为 `allow_period_cutoff_carry_forward`，补充说明，行为不变。
- **3F** 测试支撑：共享 sqlite schema 夹具（目前 `t_scheme_registry` 有 6 份手写 DDL）和公共 fakes。

Dashboard 后端拆分（sql/rules/payload/service）等工作区中进行中的 rank-IC 改动合入后再做，且不改变响应字节。

### 批次 4：文档整理

- 删除已被取代的草案。
- `CURRENT_STATUS.md` 每条只保留"版本、包、核验日期、回执链接"。
- 关闭 `TODO.md` 中已过期的条目。
- `docs/native_v1/SCHEME_CONTRACT.md` 改名，避免与共享契约同名。

## 5. 暂缓项（涉及功能、数据或治理，需单独授权）

| 编号 | 内容 | 前置条件 |
| --- | --- | --- |
| D1 | ~~迁移 026：删除 017 账本表和其他退役表~~ 代码已完成（`8d64fdde`），**生产执行待办**，见第 8 节 | 第 8 节 |
| D2 | ~~迁移基线压缩~~ 已完成（`8d64fdde`，2026-11-11 用户授权） | — |
| D3 | ~~liwei_0616_5y01 历史替代~~ 2026-11-11 退役：读回两端 `t_scheme_predictions` 中指定来源 `liwei_0616_5y01_full_oos_k3_div_k10_bbv2` 均为 0 行，投影在每次请求中都解析为空，Dashboard 输出不变。删除 backend 替代投影、DataConsistency 快照投影、`shared/prediction_history_replacement.py` 及已于 2026-09-17 执行完毕的一次性清理工具（`scripts/cleanup_replaced_prediction_history.py`、`scheduler/persistence/history_cleanup.py`）。迁移清单、纠正版证据 JSON 与 Harness 的 `legacy_migration` / live-only 基线验收仍在使用，保留 | — |
| D4 | ~~删除 DataBridge v1/v2/四文件 current 兼容~~ 代码已完成（`8fdd9711`，净删约 180 行）：2026-11-11 读回两端 current 均为 v3 五文件、state 带 `publication_manifest_version=data-bridge-current-v3`；新代码以 strict_read_only 校验两端生产 current 均通过，旧格式改为 fail closed。四文件快照视图（legacy_v1 Blackbox 输入）保留。随下一个 release 部署 | — |
| D5 | ~~下线 42 个 paused 方案~~ **撤销**：Blackbox 配置中的 `status: paused` 只是 Intake 初始声明，生效状态由数据库 Registry 决定（`lifecycle.resolve_database_lifecycle`）。2026-11-11 读回两端 Registry 159 个 target 全部 active，这 42 个方案均有生产预测（27 个在 11 月内有新预测，其余为周/月频）。`deliveries:` 多期限路径服务于运行中的 t1/t5，保留 | — |
| D6 | `prediction_input_quality_v1.json`（1.7 万行，全部为 log_only）改为从 config 推导或删除 | 业务确认是否保留这项审计 |
| D7 | 身份校验收敛为"一次预检加一次事务内复核"，合并 Native/Blackbox 结算；删除 capability 防护 | 确认治理意图；补"运行中 Registry 变更被拒绝"的事务测试 |
| D8 | 失败归类统一（Native 未激活 skipped 与 Blackbox 未批准 failed）；预检失败改走专用 repository 入口 | 会改变 run 记录语义 |
| D9 | `run_close_predictions` 去掉重复预检；launchd/systemd runner 合并 | 会改变调度入口，需在两端完成自然运行验证 |
| D10 | Dashboard 三层校验收敛、计时代码简化、删除前端不读的字段、删除未用的管理员端点 | 会改变前端或 HTTP 合同 |
| D11 | 日历和日期实现合并 | 受 `week_id` 不改约束限制 |

## 6. 验证方法

- **本机：** Python 3.13 venv（与 CI 版本一致）跑 `python -B -m pytest -q -m "not mysql_integration"`；Node 22+ 跑 `node --test tests/frontend_*.test.js`。基线为 1168 passed、56 pass。
- **每批必须零新增失败。** 删除项附 grep 证据；重构项保持对外签名和输出逐字段相等。
- **合入：** 在独立分支提交，fast-forward 合入 `codex/develop` 后推送。推送前扫描提交范围内是否有凭据。不触碰 `master`。

## 7. 执行记录

| 日期 | 批次 | 提交 | 结果 |
| --- | --- | --- | --- |
| 2026-11-11 | 计划 | `3a3319cc` | 落盘本文 |
| 2026-11-11 | 1 可定位性 | `283ffc8b` | one-shot、harness gate、gap plan、DataBridge gate 写入把 traceback 写入 stderr；stdout 摘要、退出码、失败码不变；补上缺失的 `Mapping` import |
| 2026-11-11 | 2 不可达代码 | `f834e195` | 删除 `historical_as_of` 快照模式 |
| 2026-11-11 | 3A | `070795c0` | Native/Blackbox 共用 `process_control.start_process_group` |
| 2026-11-11 | 3C | `8130ca23` | `data_consistency_gate` 从 3586 行拆为 565 行加 `harness/gates/consistency/` 7 个模块；72 个顶层块逐字节一致（仅 `PROJECT_ROOT` 层级随目录调整） |
| 2026-11-11 | 3E | `e90db00b` | 标明 cutoff 承接是现行 v3 producer 语义（只加注释，未改名） |
| 2026-11-11 | 顺带修复 | `456f0215` | `harness/blackbox_v2/gates.py` 引用了已删除的异常类，配置变更时报 NameError；改为抛 ValueError 并给出原意文案，gate 仍按失败处理 |

| 2026-11-11 | 4 文档 | `af789d97` | 删除已被取代的 `DETERMINISM_HOTFIX_ACTIVATION_DRAFT`；文档中心增加本计划入口 |
| 2026-11-11 | 2 不可达代码 | `5f325824` | 删除 lifecycle 中未使用的局部变量及 harness 中两处未使用 import |
| 2026-11-11 | 3D | `fc32b5d9` | `shared/data_bridge/refresh.py` 从 2412 行拆为 1509 行，另加 `errors.py`、`publication_manifest.py`、`filesystem.py`；70 个顶层块逐字节一致；`refresh` 保留全部原有名字和测试补丁点（`DataBridgeStore`、`_ensure_before_deadline`、`_logger` 不搬）。唯一可见差异：异常类的 `__module__` 变为 `shared.data_bridge.errors`，仓库内无代码依赖它 |

本批验证：非 MySQL pytest 1170 passed（基线 1168，新增 2 项）；Node 56 pass；release archive 两次构建 sha256 一致；各定时入口 `--help` 冒烟通过；pyflakes 全框架无未定义名。

本批评估后不做的项：

- `detect_spring_boundary` 是交易日历春节边界的不变量测试入口，受日历不改约束。
- `upsert_*_actuals` 兼容入口被多处测试当作便捷入口使用，删除收益小于改测试的成本。
- 尾部修复（tail repair）是 CODE_ARCHITECTURE §3.2 记载的运维能力，不属于死代码。
- `_RETIRED_GRAY_GAP_EXTRA_FIELDS` 会剥离入站记录字段，删除会改变写库内容。
- 3E 不改名：authority 层参数确实只在 legacy manifest 下生效，改名收益有限。
- `process_started`/`process_fence` 仅测试传入，但它们是进程生命周期测试的接缝，暂不删除。

| 2026-11-11 | 3D' Dashboard 后端 | `52426ba2` | 从 semantics 拆出 `factor_lab_dashboard_validation.py`（611 行），从 builder 拆出 `_dto.py`、`_encoding.py`；136 个顶层块逐字节一致；用相同合成数据（1 万事实）在基线 `d9a28960` 与当前代码上各生成响应，结构化比对仅随机 `snapshot_id`（`uuid4`）不同。生产名单读取、诊断 LRU、`_build_summary/_build_detail` 因测试补丁点或模块状态保留原位 |
| 2026-11-11 | CI | `7990a6b4` | 新增未定义名检查（可拦截 `456f0215` 那类 NameError）和定时入口启动冒烟；release job 改为构建两次比对 sha256。job 名称不变 |

经评估不做的项（附证据）：

- **3B 公共工具合并：** `_iso_date` 5 份、`_canonical_json` 3 份互不相同，`_is_sha256` 4 份有 2 种语义，`_fsync_directory` 3 份有 2 种。完全相同的只有几行，合并收益小于语义误合的风险。
- **3F 共享 schema 夹具：** `t_scheme_registry` 的 6 份测试 DDL 是 6 种不同列集合（各测试只建所需列），其他表也类似。统一需要改写各测试的造数，暂缓。
- **migrations/runner 与 intake 的"未使用"导入：** `scripts/apply_migrations.py` 通过 `globals()[handler_name]` 动态分派 16 个恢复函数，pyflakes 报的是误报；intake 按 B09 决定保留导出接口；runner 是迁移门面，生产机运维工具可能直接导入。不清理。
- **`scheduler/persistence/predictions.py` 的 gray gap 拆分：** 测试在该模块上替换 10 个 gray gap 辅助函数，辅助函数又依赖模块内私有读写函数，拆出会形成循环导入并使补丁静默失效。这是写库路径，不做纯搬迁。

## 8. D1/D2 基线压缩与 026 生产执行手册

**已完成（代码，`8d64fdde`）。** 001..025 压缩为 `migrations/baseline/schema_025.sql`，删除逐版本恢复代码（净减约 1 万行）。新增可重跑迁移 026：

- 删除 017 账本 5 张表、`t_harness_runs`、`t_harness_gate_results`、`t_backtest_reproduction_checks`、`v_latest_backtest_run`；
- 删除 `t_scheme_runs` 上 9 个未用列（`schedule_item_id`、`attempt_no`、`trigger_origin`、`queued_at`、`failure_code`、`execution_token`、`process_id`、`process_group_id`、`harness_run_id`）及其外键、CHECK 和索引。

`t_input_artifacts` 与 `t_scheme_run_log` 仍有代码读写，不在本次范围（前者仍被 DataConsistency 血缘检查读取）。

**本机验证（一次性 MySQL 8.4）：**

- 基线与旧文件建出的 025 在 information_schema 层（表、列、索引、外键、CHECK、视图）和参考数据上完全一致；
- 旧 runner 建到 025、再用新 runner 升级，与新库执行"基线 + 026"相比，schema 和 26 条历史逐行一致；
- 026 连续执行两次安全；中途中断后可经 `--inspect-applying` / `--recover-applying` 完成；
- 非 MySQL 测试 1089 passed，MySQL 集成测试 30 passed（新增 6 项迁移合同）。

**Mac3 现场只读读回（2026-11-11）：**

- 001..025 全部 APPLIED（001..016 为基线补录）；
- `t_input_generations` 6 行（最后 2026-08-08），其余 4 张账本表为 0 行；
- `t_harness_runs` 329 行、`t_harness_gate_results` 1802 行（最后 2026-08-23）；
- `t_backtest_reproduction_checks` 9 行。

ECS 读回尚未取得。

**生产执行步骤（每台机器各一次，先 ECS 后 Mac3，需用户授权）：**

1. **构建与安装：** 用含 `8d64fdde` 的提交构建 release，按部署手册安装为候选，暂不切 current。
2. **执行前只读核对：** 读回 `t_schema_migrations`（1..25 全部 APPLIED、无 APPLYING），以及上述表的行数与 `t_scheme_runs` 9 列的非空计数。
3. **选窗口：** 两端触发时间为 06:30、07:03（工作日）、08:30、18:00、19:00、23:45、周六 11:30。在工作日 14:00–17:00 执行，并确认没有在途 run。
4. **备份：** `mysqldump --single-transaction --no-tablespaces` 导出上述 8 张表、`t_scheme_runs` 和 `t_schema_migrations`，存到 runtime 备份目录，记录 sha256。
5. **执行迁移：** 在候选 release 内执行 `scripts/apply_migrations.py --apply --expected-database-name bond_db --expected-server-uuid <只读查得的 UUID>`。
6. **执行后核对：**
   - 历史 1..26 全部 APPLIED；
   - 退役对象不存在；
   - Dashboard 返回 200，且与执行前同口径数据一致；
   - 随后一次自然调度（预测、Actuals、DataBridge）正常完成。
7. **切换与更新状态：** 切换 current，并更新 `CURRENT_STATUS`（schema 改为 026，记录备份位置）。

**兼容性：** 上一个 release 的运行时代码不读写被删除的对象，可以作为回退目标。但旧 release 的 `apply_migrations` 不认识 026 历史，回退后不要用它执行迁移。

**2026-11-11 更正：** 早先审查把配置文件中的 `status: paused` 当成"不调度、不展示"，这是错误判断。Blackbox 方案以数据库 Registry 为准。判断方案是否在用，必须读两端 Registry 与预测事实，不能只看配置文件。

## 9. D3 历史替代层退役（2026-11-11）

- **前提核实：** 读回两端，`liwei_0616_5y01_full_oos_k3_div_k10_bbv2` 在 `t_scheme_predictions` 中没有任何事实（ECS 仅剩 80 条无事实的 run 记录）。原方案名下有完整 115 条实盘与自有成功回测 run，替代投影在每次请求中都返回 None，实际未生效。因此**无需写任何生产数据**。
- **代码删除：** 删除 backend 替代投影、harness 快照中的同类投影、一次性清理工具（`history_cleanup` 与脚本，2026-09-17 已执行），净删约 3200 行。`legacy_prediction_migration` 与 compatibility JSON 保留：DataConsistency 与 Dashboard Gate 仍用它认可 liwei 5Y/h5 的 live-only 范围。
- **展示等价验证：** 将 Mac3 生产数据只读导出到一次性 MySQL，新旧代码分别构建 Summary（159 方案）与 636 个 Detail（每方案最近两个月 × all/backtest），去掉 `snapshot_id`/`generated_at` 后逐字节一致。DataConsistency 完整性与血缘检查在新旧代码下结果一致。
- **既有问题（与本次无关，删除前后相同）：** 在 Mac3 数据副本上，completeness 有 1 个错误、25 个 authority gap；lineage 对 `three_y_t5_xgb_fxlead_b8_v2`（exact `dc4e944f5aec`）报 legacy backtest lineage evidence drift。需另行核查。

## 10. 结果分区统一（2026-11-11 用户决定并已实施）

- 唯一结果分区：`feature_date < 2026-06-01` 为回测，否则为实盘；migration 027 新增 VIRTUAL 生成列 `t_scheme_predictions.result_segment`，与后端展示函数同一常量（测试锁定）。来源字段（`backtest_run_id`/`run_id`/`prediction_phase`）只作审计。
- 上线日期唯一来源为 Registry `deployed_at`（精确到日）。
- 入库改为一次回测到最新数据日 + 激活 + 少量衔接补齐，不再从 2026-06-01 做 gray_live 补缺；DataConsistency 完整性改为"每个应有业务键恰好一条产品事实（不论来源）"。
- 原记录的"32 个方案以回测身份发布分界日后事实"在新模型下合规，无需处理；run 205 的 completeness 误报随之消除。
- 遗留：`tests/test_active_scheme_contracts.py` 以 config `status == active` 选方案，未覆盖 Registry 已生效但 config 仍为 paused 的方案。

## 11. 模块级评估（2026-11-12，只读）

**依赖分层已干净**：`shared`（1.5 万行）← `scheduler`（1.0 万）← `harness`（0.9 万），`backend` 只依赖 `shared`，包级别无循环；跨包导入只有 8 类，其中 `harness → backend` 仅 2 处（Dashboard Gate 复用展示构建）。函数级几乎没有重复（跨模块同实现函数只有 3 处共 20 行），无引用函数、常量、模块、孤儿文件均已清理。因此下面的方向都是**结构性取舍**，不再是"删冗余代码"。

| 方向 | 事实 | 判断 |
|---|---|---|
| W4 Native 源码运行器/证据/适配器（`shared/` 顶层约 3100 行） | 自成一岛，但九个冻结方案的 `predict.py` 直接 `import shared.<x>_predict_adapter`；改路径会改 W4 的 `predict.py` 字节与 exact | **不搬**。边界只在文档中标明为冻结岛 |
| 数据一致性 Gate（`harness/gates` 约 3400 行 + 测试约 2000 行，最大单一子系统） | 只在入库验收（SOP 3.8）手工运行；`facts.py` 以 DB 重新实现一份 Dashboard 聚合再与 API 比对，语义每改一次要改两处 | 有意的独立验证，但维护权重最高。是否瘦身取决于对验收严格度的取舍，**需产品决定** |
| 超大模块（`signal_gap_plan` 2000、`predictions.py` 1780、`lifecycle` 1510、`executor` 1510、`blackbox_v2_runner` 1280、`one_shot_prediction_runner` 1250、`data_bridge/refresh` 1370 行） | 多为合同校验与事务编排；测试大量 `monkeypatch` 模块属性，按职责拆分会引起成片测试改动 | 只有可读性收益，无功能收益；建议只在触及该模块时顺带拆分 |
| 两个 launchd/systemd 薄入口（各约 55 行）、四个 actuals updater 薄模块（58–164 行）、`backtests/` 小包（723 行） | 模块名被已安装的 plist/unit、AGENTS 的写库单点条款引用 | 合并会改变已安装控制面或根规范，收益小，**不做** |
| 发布包含 `tests/`（78 个文件）与 `source_evidence/`（约 12MB） | 两端共用同一 archive 是既定原则；`source_evidence` 是 W4 运行输入 | 不动 |
| 概念面 | 已于迁移 028 收敛；剩余 `gray_replay`（已写入事实的溯源标识）、`t_scheme_registry.status` 的 `archived` 未使用 | 前者属历史事实不能改；后者收益很小 |

## 12. 数据一致性 Gate 瘦身（2026-11-12 用户决定并已实施）

- 决定：保留完整性与血缘检查；展示对账改为复用后端语义。
- 删除 `harness/gates/consistency/facts.py` 与 `frozen_calendar.py`（独立重算聚合与日期口径）及 `common.py` 中随之无引用的名字；新增 `consistency/representation.py`（约 100 行）：HTTP 响应必须等于 `build_factor_lab_dashboard`/`build_factor_lab_dashboard_detail` 在同一数据库上的输出（忽略 `snapshot_id`/`generated_at`）。预期载荷来源以构造参数注入，测试里替换为手写载荷。
- 被删除的独立检查及其替代：业务键重复（库内 `uk_scheme_tenor_target`）、不可变 Actual 存在性（completeness）、run 存在/成功/同方案（lineage）、冲突 Actual（库内约束）。**不再独立验证**的只有：特征日 ≤ 预测日 ≤ 目标日的顺序，以及各任务类型的日期口径（由写入侧与 completeness 的预期键推导保证）；展示聚合口径不再被第二套实现交叉验证，只由 Dashboard 自身测试保护。
- 验证：Mac3 生产库上以进程内 API 分别运行新旧 Gate，两组方案（共 7 个，含多目标、周频、月频、W4、修订方案）的 completeness、lineage 子结果逐字一致，representation 均 PASSED（69 与 160 次 Detail 请求），耗时相同。
- 规模：实现 -1480 行（含 gate 主模块 -205）、测试 -545 行，净增 representation.py 约 100 行。

## 13. 把 W4 九套 V1（Native）迁移到 Blackbox V2，只维护一套系统（2026-11-12 用户提出，评估）

**为什么值得做。** V1 不是一个小岛：它同时占用源码、测试、运维与文档四个面，且是上面第 11 节里所有"无法再删"项的共同原因（W4 `predict.py` 字节固定，导致其依赖路径不能动）。下面是只读统计的下限。

| 面 | 体量 |
|---|---|
| 整文件只服务 Native 的源码 | 约 3400 行（`shared` 的 source 运行器/证据/适配器、`source_runtime_database`、`_native_source`、`native_output`、`_input_artifacts/native`，加 `scripts/run_close_predictions.py`、`scheduler/scheme_runner.py`） |
| 混在其他文件里的 Native 分支 | 约 400 行起（`complete_active_native_run`、`_execute_native_wave`、补跑的 `_run_native_wave`、`active_native_identity_error` 等，按函数名统计的下限） |
| 方案目录与证据 | 9 个 W4 方案目录 72 个文件；`source_evidence/` 207 个文件约 12MB（发布包里两端都带） |
| 测试 | 36 个测试文件提到 Native；整文件专属的约 1500 行（`test_native_executor`、`test_close_prediction_runner`、`test_native_input_artifacts`、`test_source_runner_database_isolation` 等），其余是夹在通用测试里的 Native 用例 |
| 运维面 | Mac3 月度 launchd 任务（`run_close_predictions`，仅 W4 月频/周均值）、源库凭据配置、`forecast_env` conda 环境、`BFL_SOURCE_DB_CONFIG*`、`legacy_native_scheme_ids` 清单与 Gate/Dashboard 的 legacy 血缘分支 |
| 文档 | `docs/native_v1/`、`NATIVE_V1_MAINTENANCE_SOP.md`、`MAC_ECS_PARITY_DRAFT.md`（358 行）及 parity 台账 |

**技术上可行的依据。** Blackbox V2 合同已支持 W4 用到的全部任务类型（`T+1`、`monthly`、`weekly_average`，见 `shared/task_specs.py`）；根规范已规定迁移方式："同算法运行时升级保留原 base/Registry ID，以真实新 exact 区分版本，只切未来唯一 Writer，不重算、覆盖或搬删既有历史"（[保真与迁移边界](../architecture/SOURCE_ALGORITHM_FIDELITY.md)）。ECS 不运行 W4，迁移只影响 Mac3。

**迁移后能删什么、必须留什么。** 可删：上表中"整文件只服务 Native"的源码、Native 执行分支、W4 方案目录的 `core/`/`predict.py`、`source_evidence/`、月度 launchd 任务与源库凭据/conda 环境、专属测试与文档。必须留（历史只读）：`t_scheme_versions`/`t_scheme_runs` 里 `native_adapter` 的既有行与其 exact（insert-only 事实不能改），因此 Gate 的 legacy 血缘判定、冻结清单 `deploy/unversioned_backtest_facts_v1.json` 与 Dashboard 对旧事实的读取要保留，预计约 500 行。

**分阶段（每一步独立授权、独立回退）。**
1. **试点一套。** 选 `daily_*_0629`（日频 T+1，三套之一）。由算法方交付 `{scheme_id}.py` + `.json`（把源码包的预测逻辑改为读 DataBridge 五文件），保持原 base ID，走 SOP 第 4 节「同 ID 修订」：持久化回测到最新数据日 → 激活 → 单日补跑。
2. **同输入对等验证。** 在 Mac3 上让 Native 与新 V2 对同一批历史日（建议最近 20–40 个交易日）产出，逐日比对方向；差异必须逐条解释（源码包读库 vs DataBridge 导出的口径差异是主要风险）。通过才切 Writer。
3. **切 Writer。** 同一窗口内：新 exact 成为唯一 active，Native 版本退役（Registry 保留历史）；自然运行首个窗口以真实宿主时钟的 run 为准。
4. **复制到其余八套。** 月频三套、周均值三套在对应调度窗口各自试点；周均值与月频的 Actual 链路（`period_average`/`monthly` updater）保留，因为 V2 同样产出这两类任务。
5. **删除 Native 代码与运维面。** 九套全部切完并观察至少一个完整周期后，一次性删除上表可删部分，并同步 AGENTS、部署矩阵、`onboarding_policy_v1.json`（`native_v1_mode` 改为 retired）与文档。

**需要您决定的事。**
1. **谁产出九套 V2 交付？** Blackbox 内部保真按根规范由上游负责；若由我们代为包装，则须按"源算法保真"逐套证明与源码包等价（并且命中「内嵌运行时外壳」类交付时要先停下报告，见入库 skill）。
2. **等价标准。** 方向逐日完全一致，还是允许在已解释的口径差异内不一致？这决定了能否切 Writer。
3. **历史处理。** 默认保留既有 Native 事实不动，只切未来；若希望新 exact 也回填同一历史区间，需要单独授权。

### 13.1 试点前置核查结果（2026-11-12，`daily_5y_lgbm_5y10_0629`，只读）

结论：**按第 13 节方案 1 无法由我们单方完成，须先解决"算法只有二进制"这个前置问题。**

- 算法核心是上游以 CPython 3.13 darwin arm64 编译的 `.so` 交付（`fixed_lgbm`、`strategy_signals`、`combo_overlay`、特征构建、`data_service`、`db_writer` 等）；可读的 `run.py`/`run_daily.py` 均为 "Auto-generated encrypted entry shim"。批次 manifest 自述"platform treats it as a source-original binary runner"。仓库里没有算法源码，因此不能"改写"成 V2 的单文件可读脚本，也不能在不具备源码的情况下声称保真。
- 该二进制与数据库深度耦合：`data_service` 从 MySQL 读 `api_wind_*` 并重建 `check_data`，`run_daily.sh` 直接用 pymysql 查 `t_trade_calendar`，`db_writer` 写预测表（dry-run 只跳过写）。而 V2 沙箱只提供 `--data-dir` 文件、禁止联网与连库。要让它在 V2 里运行，必须替换其编译的数据层，而该接口没有文档。
- V2 合同对交付的限制：目录只含两个普通文件，脚本通过静态策略（禁 `subprocess`、`sqlalchemy`、`pymysql`、`socket` 等及 `eval/exec/compile/__import__`）。把编译产物嵌入自解压外壳属于入库 skill 所说"内嵌运行时外壳"，须停止并经明确批准与隔离解包审计，且仍然解决不了上一条的数据库耦合。
- **安全问题（已确认存在，未做任何修改）：** 三个源码包（`daily_0629`、`monthly_0629`、`model_muti_0529/weekly_average_0529`）的 `forecast_project/db_config.py` 各含一份明文 root 数据库口令，自提交 `0120988e` 起进入仓库历史。不能直接改文件：源码包的树哈希是 W4 冻结身份的一部分。处置应是轮换该口令（若仍有效）并确认生产只通过外置配置文件取凭据。

可选路径见对话回复；选定前不改动任何代码。

### 13.2 包一层二进制：可行性已验证（2026-11-13，`daily_0629` 三套）

用户决定：W4 本来就是黑盒，把二进制包装成 V2 交付，只保证每天能产出预测。验证过程与结论：

1. **抓取二进制的全部 SQL。** 重放工具新增 `--general-log`，在一次性库上真实运行一次。二进制只读 9 张源表：`api_wind_daily`、`api_wind_derivative_daily`、`api_wind_weekly`、`api_wind_derivative_weekly`、`api_wind_monthly`、`api_wind_derivative_monthly`、`api_wind_date`、`t_trade_calendar`、`api_wind_indicators_all`（完整目录，26 列），且都是 `SELECT <列> FROM <表> [WHERE indicators_code IN (...) [AND indicators_value IS NOT NULL] [AND rdate <= 日期]]` 这一族；不写库（dry-run）。日频两张长表请求同一组 773 个指标代码，两表键无重叠。
2. **DataBridge 不能直接喂它。** DataBridge 的 `daily_output.csv` 覆盖全部 773 个代码，但是平台加工后的宽表：与数据库原始长表相比，仅 DataBridge 有 1 万行、仅数据库有 6.7 万行，另有 39 万行数值不同（134/773 个代码几乎所有日期都不同，量级可差几十倍）。`factor_catalog.csv` 只有 3 列，不含二进制读取的目录元数据。因此**必须向它提供原始长表**，否则输入变了，预测就不是原算法。
3. **无数据库原型成功。** 在 `forecast_env`（Python 3.13.12，与 `forecast_env_blackbox_v1` 同版本且同样带 lightgbm/xgboost/shap）里，进程内拦截 `pandas.read_sql*`、`sqlalchemy.create_engine`、`pymysql`，用上述 9 张表的导出回答它的查询（未知形态一律报错），运行 `daily.run_daily 2026-11-12 --dry-run`：44 秒跑完整套流程。D1Y/D5Y/D10Y 三套的方向与 `prob_up`（完整双精度）与生产当日发布的事实**逐位一致**。原型见 `docs/plans/attachments/w4_daily_0629_nodb_prototype.py`。
4. **体量。** 二进制包 3.6MB（`.so` 约 3.5MB）；V2 交付目录限 64MiB、两个文件，可容纳自解压外壳。

**还没解决的（决定设计）：**
- **原始数据如何进入 V2 沙箱。** 现有 V2 输入只有 DataBridge 标准文件，runner 会校验文件集合。需要新增一份由平台生成、内容寻址并随快照封存的"原始源表导出"（9 张表的相关切片，约 100–200MB），仅对声明需要它的交付挂载。这是平台侧新增，约数百行，但替代的是 Native 的 DB 直读输入链路。
- **外壳形态。** 交付的 `.py` 须自解压二进制并安装上述拦截层；属于入库 skill 所说"内嵌运行时外壳"，须经批准（本次用户已批准）并做隔离解包审计。
- **三套共享一次运行。** 二进制一次跑完 D1Y/D5Y/D10Y；若拆成三个独立交付会各自重跑 44 秒（可接受），或在外壳里缓存。
- **多日对等验证与回测口径。** 下一步用原型对最近若干交易日逐日对比生产事实，再设计 V2 backtest 如何驱动二进制。

### 13.3 多日对等验证结果（2026-11-13）

用无数据库原型对 `daily_0629` 三套在 2026-11-02 至 11-11 共 8 个交易日、24 个事实逐日运行并与生产当时发布的事实比较（方向与 `prob_up` 双精度）：**20 个逐位一致，4 个不一致**（11-02 D1Y、11-03 D10Y、11-04 D1Y、11-05 D5Y），全部集中在较早日期，11-06 之后全部一致。对这 4 个日期，用**原生路径（真实数据库）在同一份当前数据上重跑**，方向与 `prob_up` 与无数据库结果**逐位相同**。结论：差异来自生产当时之后源数据被修订（上游 `*-lastday` 任务会重拉最近一天并替换），不是无数据库层造成的；无数据库层在全部对比中等价于原生路径。该事实同时说明：以"当前数据 + 截止日"回放历史，与当时实盘产出本来就可能不同，V2 回测口径（`current_snapshot_as_of`）对 W4 同样适用，不应以历史实盘事实作为逐日回放的真值。

### 14. 原始源表旁路产物（`raw_wind_v1`）平台设计

**目标。** 让声明需要原始数据的 Blackbox V2 交付，在沙箱里拿到与二进制原先从数据库读到的内容逐位相同的输入；其余方案不受任何影响；输入仍只经 `shared.input_artifacts`，并随快照封存、可追溯。

**约束（已核实）。**
1. DataBridge 的 `candidate/current` 目录必须**正好是五个数据文件**（`shared/data_bridge/filesystem.py`、`validation.py`），原始数据不能放进去。
2. 沙箱数据目录必须与 `BlackboxInputBundle.expected_filenames` **严格相等**，并按 producer 的 stat 指纹封存（`runtime_view.py`）。
3. `BlackboxInputBundle` 已有 `combined_snapshot_id` / `parent_snapshot_id` 两个预留字段，可承载"基础快照 + 旁路产物"的组合身份。
4. 二进制对 9 张源表的读取形态已用 general_log 确定（见下表），全部是只读 `SELECT`。

| 表 | 导出列 | 过滤 |
|---|---|---|
| `api_wind_daily`、`api_wind_derivative_daily`、`api_wind_monthly` | `rdate, indicators_code, indicators_value` | `indicators_value IS NOT NULL` |
| `api_wind_weekly`、`api_wind_derivative_weekly` | `rdate, week_id, indicators_code, indicators_value` | 同上 |
| `api_wind_derivative_monthly` | `rdate, month_id, indicators_code, indicators_value` | 同上 |
| `api_wind_date` | `rdate, week_id` | 两列非空，按 `rdate` 排序 |
| `t_trade_calendar` | `rdate, trade_flag` | 按 `rdate` 排序 |
| `api_wind_indicators_all` | 全部 27 列 | 无 |

截止日由二进制自己在 SQL 里按运行日截断，所以导出**不按日期裁剪**（只去掉 NULL 值行），与它原先看到的一致。

**实测（Mac3 生产库副本）。** 9 张表全列全量 CSV 共 408MB、读写 25 秒，逐列往返相等；只导出上表列后约 110MB 量级（待实现后实测）。每次运行复制该旁路文件的开销可忽略。

**设计。**
1. **导出器** `shared/data_bridge/raw_wind.py`（新增，约 150 行）：在与 DataBridge 同一 REPEATABLE READ 一致性快照里按上表读取，确定性写 CSV（UTF-8、LF、固定列序、按主键稳定排序、空值为空串），生成 `raw_wind_manifest.json`（每文件 sha256/行数/列、源库 `server_uuid` 不入库、关联的 DataBridge `generation_id` 与 `refresh_date`）。产物放在 DataBridge 目录**之外**：`<runtime>/data-bridge/raw/<generation_id>/`，目录内容寻址并只读。
2. **生产者钩子**：`scripts/refresh_data_bridge_current.py` 在已有的 `prepare_blackbox_generation_snapshot` 之后调用导出器；**失败不影响标准 DataBridge 发布**，只在 state 记录 `raw_wind: unavailable`，声明需要它的交付按 BLOCKED 处理（fail-closed 只针对这些方案）。
3. **交付合同**：Metadata 增加可选字段 `input_profile`（`standard` 缺省 | `raw_wind_v1`），参与 exact；不声明者行为完全不变。
4. **输入组装**：`compose_blackbox_input_bundle(..., input_profile=)` 在 `raw_wind_v1` 下把 `expected_filenames` 扩展为标准文件加 `raw_wind__<表名>.csv` 九个文件；`combined_snapshot_id = sha256(parent_snapshot_id + raw_manifest_sha256)`；运行视图沿用现有 stat 指纹封存与拷贝校验；`data_snapshot_id` 证据写组合身份。
5. **交付外壳**（`daily_5y_lgbm_5y10_0629.py`，须经批准并做隔离解包审计）：自解压二进制包到工作目录，读取 `raw_wind__*.csv`，安装与原型相同的 `pandas.read_sql*`/`create_engine`/`pymysql` 拦截层（未知查询一律报错），进程内调用 `daily.run_daily --dry-run`，再把选中行映射为 V2 输出（映射逻辑复用现有 `daily_0629_predict_adapter` 的方向规则）。
6. **回测**：`gate backtest` 对该交付使用最新数据日的同一份旁路产物；二进制按各请求日期自行截断。

**验证闸门（每步独立通过才进入下一步）。**
- G1 CSV 往返：导出→读回的 DataFrame 与 `pandas.read_sql` 结果在 dtype 与值上逐列相等。（已对全量 9 表通过；实现后对只导出列再验证）
- G2 外壳无数据库复现：旁路 CSV 喂外壳，复现原型已验证的 24 项对比。
- G3 封存与篡改：旁路文件被改、缺失、多出时运行视图拒绝；`expected_filenames` 与实际严格相等。
- G4 平台单测与 MySQL 集成测试；不声明 `input_profile` 的方案 Dashboard 摘要与重放逐位不变。
- G5 在生产库副本上用候选 release 走 V2 全链路（持久化回测→激活→补跑），与 Native 对同一日期的方向、`prob_up` 逐位一致。
- G6 DataBridge 06:30 窗口耗时与磁盘占用在预算内（旁路产物按 generation 保留最近两代）。

**风险与对策。** 旁路导出耗时拖长 DataBridge 刷新 → 放在标准发布之后、失败不阻断；磁盘占用 → 只保留 current 与 previous 两代；二进制对 Python 3.13 darwin arm64 的绑定 → 与 `forecast_env_blackbox_v1` 同版本，环境指纹已纳入 exact；外壳静态策略 → 经批准的内嵌运行时例外，隔离解包审计后入库。

### 14.1 实施进度

- **已完成（2026-11-13，未发布，无生产影响）：** ① 合同字段 `input_profile`（`standard` 缺省 | `raw_wind_v1`），严格校验、测试与上游合同文档；② 导出器 `shared/data_bridge/raw_wind.py`：9 张表按二进制实际读取的列确定性导出，内容寻址、只读，manifest 记录每列类型、行数、字节数与 sha256，附参考读取实现 `read_raw_wind_frame`；单元测试 12 项加 MySQL 集成测试（DECIMAL/DATETIME/JSON/NULL 与流式游标）。
- **G1 在真实数据上通过：** Mac3 生产库副本上导出 16.6 秒，合计 108MB（日 61.2、日衍生 29.3、周 4.6、周衍生 10.0、月 0.5、月衍生 0.3、日期/日历/目录 2.0）；9 张表读回后与 `pandas.read_sql` 的 dtype 与值逐位相同。
- **格式约定（交付外壳须自行实现等价读取）：** UTF-8、LF、标准 CSV；`\N` 表示 NULL，以反斜杠开头的字符串多加一个反斜杠；浮点按精确解析（逐值 `float()`，不得用 pandas 默认快速解析器）；每列类型见 manifest。
- **输入组装与生产者钩子已完成（2026-11-13，G3/G4）：**
  - `BlackboxInputBundle` 增加 `input_profile`、`raw_wind`；`compose_blackbox_input_bundle(..., input_profile=, raw_wind=)` 在 `raw_wind_v1` 下把文件集合扩展为标准文件加 9 个 `raw_wind__*.csv`，`combined_snapshot_id = snapshot-combined-<sha24(profile, parent, artifact_id)>`；`standard` 行为与身份不变；旁路产物的 `generation_id/refresh_date` 必须与 ready 快照一致。
  - 运行视图：旁路文件边复制边按 manifest 校验 sha256 与字节数（沿用独立文件、`O_NOFOLLOW`、`O_EXCL`、nlink=1），目录内容严格等于期望集合；可信校验识别旁路文件与 profile，伪造 profile/文件集/组合身份均被拒绝；篡改后不留 active 视图。
  - 就绪标记 `<artifacts>/blackbox_v2/raw_wind/ready.json`（generation 配对）；`prepare_raw_wind_sidecar`（一致性快照导出→校验→写标记→保留最新两代）、`invalidate_raw_wind_sidecar`、`get_ready_raw_wind_artifact`（轻量核对身份与文件大小）、`raw_wind_for_profile`（standard 返回 None，raw_wind_v1 未就绪抛 `RawWindUnavailableError`）。执行器两处与回测 gate 按 `blackbox_input_profile(cfg)`（一个方案的各 delivery 必须同 profile）取用。
  - producer（`scripts/refresh_data_bridge_current.py`）：发布开始时先失效旁路标记；发布与 ready 快照封存之后，**仅当本部署（按 `BFL_DEPLOYMENT_TARGET` 过滤）有声明 `raw_wind_v1` 的方案**才导出，失败只记录、不阻断标准发布。目前两端都没有这类方案，所以生产行为不变，ECS 也不会多导出。
  - 验证：单元测试 10 项（组合规则、物化、篡改、伪造、就绪配对、保留策略、profile 一致性、钩子隔离）；MySQL 集成测试 30 项（含 `prepare_raw_wind_sidecar`）；全量 987 项通过。Mac3 生产库副本端到端：生产者准备 ready 快照与旁路产物 16.5 秒；`legacy_v1` 运行视图 13 个文件、`algorithm_managed` 14 个文件，合计 133MB，物化 0.1 秒。
- **下一步：** 交付外壳（G2）：自解压二进制包、读取 `raw_wind__*.csv`、安装无数据库拦截层、进程内运行并映射为 V2 输出；用无数据库原型已验证的 24 项对比复现，再走 V2 全链路对照 Native（G5）。

### 14.2 交付外壳（G2，2026-11-13）

**产物与位置。** 模板 `deploy/blackbox_v2/w4_wrapper/shell_template.py`（可读、可单测的普通模块）、生成器 `build_delivery.py`（确定性：同源码包与参数生成字节相同的 `{scheme_id}.py` + `.json`；生成时即用平台静态策略 `validate_delivery_script` 与 `load_metadata` 校验，任何一项不过都不写出）。`daily_5y_lgbm_5y10_0629` 的交付约 984KB（二进制包 lzma 压缩后 base64 内嵌）。

**设计决定与依据。**
- **口令不进入交付。** 源码包 `db_config.py` 含明文 root 口令；生成器换成"键齐全但无凭据"的占位（二进制启动时会校验这些键存在，空字典会报 `database config missing required keys`），主机用不可解析的 `database.disabled.invalid`，即使拦截层有遗漏也不可能连到真实库。交付文件经程序比对不含该口令。
- **子进程与目录上限。** 二进制会自行拉起子进程写大量中间文件（`candidate_stdout.log`、`backtest/5y10/*.csv` 等，单次 D5Y 运行约 203MB、41 个文件），无法在进程内限制写入；运行器对运行目录是**运行中轮询**。因此仅对 `raw_wind_v1` 通过 `profile_for_input_profile` 把 `max_run_dir_bytes` 放宽到 4GiB（时限、内存、网络/数据库禁用不变），并把 manifest 随旁路文件一起放进数据目录（外壳据此还原列类型、校验哈希）。
- **严格白名单假模块。** 二进制只向 `sqlalchemy` 要 `create_engine`、向 `sqlalchemy.engine` 要 `URL`，从不触碰 `pymysql`；外壳用 `sys.meta_path` 查找器放行这两个名字，其余一律 `AttributeError: database access is disabled`。`pandas.read_sql*` 只回答 SELECT 选列加 `IN`/`IS NOT NULL`/比较/`BETWEEN`/`ORDER BY` 这一族，未知形态一律报错。
- **运行日取 `target_date`。** 原生适配器以运行日为被预测日；V2 回测请求里 `predict_date = feature_date`，被预测日是 `target_date`。外壳以 `target_date` 为运行日，并要求源行的 `prediction_date == feature_date`、`rdate == target_date`，方向取 `pred_label/candidate_pred/vote_pred/model_pred` 中第一个非空整数（与原生适配器同规则）。
- **二进制有模块级状态。** 每个请求之后清除其模块（变异检验：禁用清除后测试失败），保证结果与请求顺序、同进程内次数无关；`backtest` 用 `spawn` 多进程（上限 6），每个 worker 独立解压与装载数据。
- **批量回测入口不可用。** `daily.run_backtest` 在一个月区间上已写出 5GB，且是 source-original 批处理；V2 回测改为逐请求走实盘入口（与活跃实盘同一路径）。

**验证。** 桩二进制包端到端测试 10 项（生成确定性与策略合规、无口令、无数据库运行且 stdout 为空、运行目录只剩输出、回测与单次预测一致且与请求顺序无关、非交易日/数据被篡改/日期非法/多余字段/源日期不一致均非 0 退出且不写部分结果、外壳装载的表与平台参考读取实现逐列相等、未知查询与数据库访问 fail-closed）；真实二进制与真实数据（Mac3 生产库副本导出的旁路产物，`forecast_env_blackbox_v1` Python 3.13.12，`python -I` 与受控环境）：`predict` 2026-11-12 用时 29 秒，方向 -1，与生产当日事实一致；`backtest` 8 个请求（11-02 至 11-11）69 秒、运行目录峰值 1.15GB，8/8 与已对过账的无数据库原型逐日一致。按约 460 个请求估算回测约 66 分钟，在 7200 秒预算内。

**还没做（下一步）。** ① 将 `daily_5y_lgbm_5y10_0629` 的 config 与交付落到 `schemes/`，走 SOP 第 4 节的同 ID 修订（需确认从 `native_adapter` 到 `blackbox_v2` 的 exact 修订路径在 lifecycle/activation 中是否被支持）；② `intake-blackbox` 与各 Gate 对 `raw_wind_v1` 的适配；③ 在生产库副本上用候选 release 走 V2 全链路（持久化回测、激活、补跑）并与 Native 对照（G5）；④ 复制到另外两套 daily，再处理 monthly 与 weekly_average 两个二进制包。
