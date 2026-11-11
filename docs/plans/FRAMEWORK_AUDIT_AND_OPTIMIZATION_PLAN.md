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
| D3 | liwei_0616_5y01 历史替代：该方案在两端每日运行（Mac 115 条预测，最新 2026-11-11），替代投影在每次 Dashboard 请求中生效。删除会改变页面展示的历史，属功能变更；保留，除非业务决定改为落库 | 业务决定 |
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

`t_input_artifacts` 与 `t_scheme_run_log` 仍有代码读写，不在本次范围（前者随 D3 处理）。

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

