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
