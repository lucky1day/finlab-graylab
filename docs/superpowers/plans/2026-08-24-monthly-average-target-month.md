# 月度平均 target_month 重构实施计划

> **状态：未执行，已被最小方案取代。** 本文保留为被否决的全量重构记录，不是当前待执行计划。现网继续保留 Contract 1.0 `target_date` 兼容指针，并仅在 `monthly_average` 平台/展示边界映射目标月；没有新增 migration 021、`target_month` 物理列或第二套业务键。后续不得按本文清单继续推进，除非用户重新批准 Contract/schema 变更。

> **执行约束：** 本计划按仓库 inline-first 规范由主 agent 逐任务实施并逐项复核。生产数据库、release、服务和历史业务行操作，必须在代码与本地测试完成后另行经过只读现场门禁与明确生产授权。

**目标：** 让 monthly_average 从业务模型、写库、Actuals、回测/实盘分区、API、DashboardGate 到前端统一使用 target_month=YYYY-MM，不再把 target_date 当作月度业务目标，同时保持算法方向、置信度、输入截止和已发布 live 核心结果不变。

**架构：** 平台按 task_type 判别目标身份：monthly_average 使用 target_month，日期型任务继续使用 target_date；本轮不改变 quarterly_average 和 annual_average。现有 Blackbox V2 Contract 1.0 的 target_date 只作为沙箱交付兼容指针完成严格 echo 校验，平台从权威下一 MID 桶标签生成 target_month，兼容指针不再进入月度业务键、Actual join、API 分组或前端展示。

**技术栈：** Python 3.12、FastAPI、SQLAlchemy、MySQL 8、原生 JavaScript、pytest、受控 migration runner、Mac3 launchd、ECS systemd immutable release。

---

## 1. 已确定的设计

### 1.1 月度平均的业务记录

以 2026 年 1 月预测 2026 年 2 月为例：

| 字段 | 值 | 含义 |
|---|---|---|
| predict_date | 2026-01-15 | 业务信号发出日 |
| feature_date | 2026-01-15 | 当前 MID 桶数据截止锚点 |
| target_month | 2026-02 | 下一个完整 MID 桶的业务月份 |
| horizon | 1 | 下一个同类业务桶 |

target_month=2026-02 的完整 MID 桶由 shared.period_average_buckets 解析，例如自然区间 2026-01-16..2026-02-15 及其中的权威交易日集合。预测和 Actual 比较目标桶平均收益率与当前桶平均收益率，不比较某一个目标日。

### 1.2 明确排除

- 前端不得把旧 target_date 截取月份后再加一个月。
- 不再把桶开始日、桶结束日或桶结束后一天包装成月度业务 target_date。
- target_month 不能只放在 extra；它必须是可索引、可唯一约束、可 join 的一等字段。
- 不修改算法方向、置信度、predict_date、feature_date、scheme version、输入 lineage。
- 不删除后重写已发布 live 行，也不允许前端合并隐藏同月重复记录。
- 本轮不顺带修改季度平均、年度平均或普通 monthly。

### 1.3 Contract 1.0 兼容边界

当前五个 M0 月均交付要求七字段 Request 和五字段 Result，其中 target_date 仅验证为 feature_date 后一个自然日并原样 echo，算法方向并不依赖它。本计划不修改五个已验证交付文件。

数据流固定为：

    权威 feature_bucket + target_bucket
      ├─ target_month：从 target_bucket.label 提取，进入平台业务记录
      └─ contract_target_pointer：feature anchor + 1 natural day
           只进入 Blackbox Contract 1.0 临时 Request
           只用于 Result echo 校验
           不进入月度业务键，不供 API/前端使用

如果未来要求交付协议本身也删除 target_date，应另行设计 Contract 1.1 并由上游交付新版本，不与本次平台修复混做。

### 1.4 legacy 行兼容

现有 live 行保持核心事实不变。受控迁移只补充可由权威 MID 桶唯一推导的 target_month：

    旧行：feature_date=2026-01-15, target_date=2026-01-16
    校验：旧指针必须等于 feature_date + 1 natural day
    映射：next MID bucket.label = MID-2026-02
    物化：target_month=2026-02

旧 target_date 暂留为 legacy 审计值，不再参与任何月度业务逻辑。新 monthly_average 行必须写 target_month；数据库物理列 target_date 仍服务其他任务，因此不能从统一表全局删除。

## 2. 完成后必须成立的不变量

1. monthly_average 的预测、Actual、回测和 API 明细均有规范 target_month，格式严格为 YYYY-MM。
2. 月均 live 唯一键为 scheme_id + target_tenor + horizon + target_month。
3. 日期型任务仍使用 scheme_id + target_tenor + horizon + target_date。
4. 新月均记录 target_month 必填，业务层禁止依赖 target_date。
5. legacy 月均记录即使保留旧 target_date，读取、join、分区、统计和展示也只使用 target_month。
6. target_month 必须来自 PeriodBucket.label，不能由前端、部署日期或字符串算术猜测。
7. canonical backtest 使用 target_month < gray_target_month_start。
8. gray/live 使用 target_month >= gray_target_month_start。
9. target_month=2026-06 必须归入 gray/live，不能再次落入两侧空档。
10. 同一方案、期限、horizon、target_month 不得有两个 live 业务结果，也不得同时出现在 canonical backtest 与 live。
11. 目标 MID 桶完整且数据水位覆盖后才产生 Actual；此前为待验证。
12. 页面不出现“目标日”“每日验证表”，月均详情统一为“预测明细”和“目标月”。
13. 其他 task type 行为不变。

## 3. 文件与职责

### 新建

- migrations/021_monthly_average_target_month.sql：增加 nullable target_month、月均唯一索引，并允许统一表中月均新行不写 target_date；不在 migration 中猜测业务映射。
- scripts/materialize_monthly_average_target_month.py：默认只读 inspect；显式 apply 时在数据库身份、数量和摘要门禁后，通过 scheduler.repository 只补 legacy target_month。
- tests/test_monthly_average_target_month.py：目标月、跨年、Contract 兼容、历史/实盘边界测试。
- tests/test_monthly_average_target_month_migration.py：migration 021 的 schema、索引、manifest 和重复 apply 测试。
- tests/test_materialize_monthly_average_target_month.py：物化 dry-run、数据库身份、CAS、重复月和幂等测试。

### 修改

- shared/period_average_buckets.py：目标月份唯一纯计算。
- shared/prediction_context.py：月均 live context 携带 target_month 和单独命名的 contract pointer。
- shared/models.py：PredictionRecord、PeriodAverageActualRecord 支持判别式 target_date/target_month。
- shared/blackbox_v2/history.py：历史月均 candidate 保存 target_month，并按目标月分区。
- shared/blackbox_v2/requests.py：平台目标身份与 Contract 1.0 Request 字段分离。
- scheduler/blackbox_v2_runner.py：echo 后把 target_month 写入平台记录。
- backtests/blackbox_v2.py、backtests/repository.py：月均回测持久化 target_month。
- scheduler/repository.py、scheduler/executor.py：月均 insert-only、重复检查和运行前校验按 target_month。
- shared/actual_facts.py、scheduler/period_average_actuals_updater.py：月均 Actual 按 target_month 生成和持久化。
- backend/services.py：metrics、Actual join、月份归属、phase range。
- backend/factor_lab_dashboard.py、backend/factor_lab_dashboard_semantics.py：Dashboard target identity、排序、去重、overlap。
- harness/gates/dashboard_gate.py：月均目标月连续性和零重叠。
- frontend/aifin-shell.js、frontend/index.html：月均展示和详情语义。
- migrations/release_manifest.json：登记 migration 021。
- 长期架构、SOP、onboarding、状态和根规范文档。

## 4. 实施任务

### Task 1：建立目标月份唯一纯函数

**Files**

- Modify: shared/period_average_buckets.py
- Test: tests/test_monthly_average_target_month.py

- [ ] 先写失败测试，证明 MID-2026-01 的下一桶 MID-2026-02 映射为 2026-02，MID-2026-12 的下一桶映射为 2027-01。
- [ ] 错误 task type、非法 bucket label、非连续 bucket 必须 fail-closed。
- [ ] 最小接口如下：

~~~python
def monthly_target_month(
    feature_bucket: PeriodBucket,
    target_bucket: PeriodBucket,
) -> str:
    """返回下一 MID 桶的规范 YYYY-MM 业务身份。"""
~~~

- [ ] 该函数只读取 target_bucket.label；不得从旧 pointer 或前端月份计算。
- [ ] 验证：

~~~bash
python -m pytest -q tests/test_period_average_buckets.py tests/test_monthly_average_target_month.py
~~~

Expected: PASS。

- [ ] Commit:

~~~bash
git add shared/period_average_buckets.py tests/test_monthly_average_target_month.py
git commit -m "refactor: model monthly average targets as months"
~~~

### Task 2：增加兼容数据库结构

**Files**

- Create: migrations/021_monthly_average_target_month.sql
- Modify: migrations/release_manifest.json
- Test: tests/test_monthly_average_target_month_migration.py
- Test: tests/test_migration_session_contract.py

- [ ] 为以下表增加 CHAR(7) NULL target_month：

    t_scheme_predictions
    t_backtest_predictions
    t_scheme_period_average_actuals

- [ ] 将 t_scheme_predictions.target_date 和 t_scheme_period_average_actuals.target_date 改为 nullable，使新的月均业务行不再需要伪造目标日。
- [ ] 保留原 uk_scheme_tenor_target，继续保护日期型任务。
- [ ] 新增：

~~~sql
UNIQUE KEY uk_scheme_tenor_target_month
  (scheme_id, target_tenor, horizon, target_month)

INDEX idx_backtest_predictions_target_month
  (scheme_id, target_tenor, target_month)

INDEX idx_period_average_actual_target_month
  (tenor, target_month, target_rule)
~~~

- [ ] migration 使用仓库既有 information_schema + PREPARE 幂等模式。
- [ ] migration 只改 schema，不直接 UPDATE 预测、回测或 Actual 行。
- [ ] legacy 兼容期不加“target_date/target_month 必须二选一”的数据库 CHECK，因为旧月均行需要同时保留审计 pointer 和新 target_month；严格判别由应用边界执行。
- [ ] release_manifest 追加真实 021 SHA-256，不得修改 001–020。
- [ ] 空库首次 apply、重复 apply、manifest 漂移测试必须通过。
- [ ] 验证：

~~~bash
python -m pytest -q tests/test_monthly_average_target_month_migration.py tests/test_migration_session_contract.py
~~~
- [ ] Commit:

~~~bash
git add migrations/021_monthly_average_target_month.sql migrations/release_manifest.json tests
git commit -m "feat: add monthly average target month storage"
~~~

### Task 3：分离平台目标与 Contract 1.0 指针

**Files**

- Modify: shared/prediction_context.py
- Modify: shared/models.py
- Modify: shared/blackbox_v2/history.py
- Modify: shared/blackbox_v2/requests.py
- Modify: scheduler/blackbox_v2_runner.py
- Test: tests/test_period_average_requests.py
- Test: tests/test_blackbox_v2_runner.py

- [ ] PeriodAverageLiveContext 增加 target_month 和 contract_target_pointer。月均 target_date=None；季度/年度保持现状。
- [ ] PredictionRecord 改为：

~~~python
@dataclass(frozen=True)
class PredictionRecord:
    scheme_id: str
    target_tenor: str
    horizon: int
    predict_date: str
    predicted_direction: int
    feature_date: str | None = None
    target_date: str | None = None
    target_month: str | None = None
~~~

- [ ] 全部构造调用改用关键字参数，避免字段重排造成静默错位。
- [ ] HistoricalCase 保存 target_month；月均历史 candidate 从相邻 PeriodBucket 获取。
- [ ] Contract 1.0 Request 仍写 contract_target_pointer，Result 仍严格 echo。
- [ ] echo 通过后，runner 构造平台记录：

~~~python
PredictionRecord(
    scheme_id=metadata.scheme_id,
    target_tenor=metadata.target_tenor,
    horizon=metadata.horizon,
    predict_date=result.predict_date,
    feature_date=result.feature_date,
    target_date=None,
    target_month=platform_target_month,
    predicted_direction=result.predicted_direction,
    extra={**extra, "contract_target_pointer": result.target_date},
)
~~~

- [ ] platform_target_month 必须从本次 request_id 对应的受控 context 取得，不得从 Result 的 pointer 猜测。
- [ ] 五个 M0 月均 delivery 文件 git diff 必须为空。
- [ ] 验证：

~~~bash
python -m pytest -q tests/test_blackbox_v2_contracts.py tests/test_period_average_requests.py tests/test_blackbox_v2_runner.py tests/test_monthly_average_target_month.py
~~~

- [ ] Commit:

~~~bash
git add shared scheduler tests
git commit -m "refactor: separate monthly targets from contract pointers"
~~~

### Task 4：回测和 gray/live 按目标月分区

**Files**

- Modify: shared/blackbox_v2/history.py
- Modify: backtests/blackbox_v2.py
- Modify: backtests/repository.py
- Modify: harness/blackbox_v2/gates.py
- Test: tests/test_monthly_average_target_month.py
- Test: tests/test_blackbox_v2_harness_gates.py

- [ ] 写 2026-06 边界回归：最后历史目标月为 2026-05，第一 gray 目标月为 2026-06。
- [ ] 现有 CLI cutoff 进入月均路径时必须是规范月首 YYYY-MM-01，然后转换为 gray_target_month_start=YYYY-MM；非月首输入 fail-closed。
- [ ] 月均分区：

~~~python
is_backtest = target_month < gray_target_month_start
is_live = target_month >= gray_target_month_start
~~~

- [ ] 月均回测行写 target_date=None、target_month=case.target_month。
- [ ] source_row 可保留 contract_target_pointer 作为交付审计，但默认 API 不读取它。
- [ ] 月均 backtest summary 使用 actual_target_month_min、actual_target_month_max、gray_target_month_start，不再把 pointer 范围称为业务范围。
- [ ] backtest.repository INSERT 增加 target_month。
- [ ] 验证 backtest target_month 与 live target_month 交集为空。
- [ ] Commit:

~~~bash
git add shared/blackbox_v2/history.py backtests harness tests
git commit -m "fix: partition monthly average history by target month"
~~~

### Task 5：live repository 改用 month key

**Files**

- Modify: scheduler/repository.py
- Modify: scheduler/executor.py
- Test: tests/test_repository_registry.py
- Test: tests/test_signal_gap_fill.py
- Test: tests/test_signal_gap_plan.py
- Test: tests/test_blackbox_v2_runner.py

- [ ] 建立唯一 target selector：

~~~python
def prediction_target_key(
    task_type: str,
    record: PredictionRecord,
) -> tuple[str, str]:
    if task_type == "monthly_average":
        return "target_month", require_target_month(record.target_month)
    return "target_date", require_iso_date(record.target_date)
~~~

- [ ] duplicate preflight、insert-only、部分重复整批失败、错误信息全部通过该入口。
- [ ] 月均 INSERT 写 target_month，target_date=NULL；不得使用 upsert/update。
- [ ] executor 对月均校验：

    predict_date == 本次业务信号日
    feature_date == 当前 MID 桶 anchor
    target_month == 下一 MID 桶 label 月份
    target_date is None

- [ ] 覆盖同月完整重复 skipped、部分重复失败、缺 target_month、非法 2026-2/2026-13、日期型任务缺 target_date。
- [ ] 验证：

~~~bash
python -m pytest -q tests/test_repository_registry.py tests/test_signal_gap_fill.py tests/test_signal_gap_plan.py tests/test_blackbox_v2_runner.py
~~~

- [ ] Commit:

~~~bash
git add scheduler tests
git commit -m "refactor: key monthly average live rows by target month"
~~~

### Task 6：月均 Actual 按月份生成和 join

**Files**

- Modify: shared/models.py
- Modify: shared/actual_facts.py
- Modify: scheduler/period_average_actuals_updater.py
- Modify: scheduler/repository.py
- Test: tests/test_period_average_actuals.py
- Test: tests/test_scheme_metrics_actual_join.py

- [ ] PeriodAverageActualRecord 增加 target_month: str | None，并允许 target_date=None。
- [ ] monthly_average 从 target_bucket.label 生成 target_month，target_date=None。
- [ ] quarterly_average、annual_average 保持当前 target_date。
- [ ] target bucket 不完整或数据水位不足时不得产生 Actual，即使 target_month 已知。
- [ ] 月均 Actual join key：

    tenor + target_month + target_rule

- [ ] 季度/年度 Actual join key继续是：

    tenor + target_date + target_rule

- [ ] 同一事实键一致方向可折叠，冲突方向必须 fail-closed。
- [ ] 验证：

~~~bash
python -m pytest -q tests/test_period_average_actuals.py tests/test_scheme_metrics_actual_join.py
~~~

- [ ] Commit:

~~~bash
git add shared scheduler tests
git commit -m "refactor: join monthly average actuals by target month"
~~~

### Task 7：更新 metrics API 和 Dashboard canonical payload

**Files**

- Modify: backend/services.py
- Modify: backend/factor_lab_dashboard.py
- Modify: backend/factor_lab_dashboard_semantics.py
- Test: tests/test_scheme_metrics_actual_join.py
- Test: tests/test_period_average_dashboard.py
- Test: tests/test_dashboard_gate.py

- [ ] 月均 metrics 明细以 target_month 返回目标身份：

~~~json
{
  "predict_date": "2026-01-15",
  "feature_date": "2026-01-15",
  "target_month": "2026-02",
  "prediction_phase": "gray_live",
  "predicted_direction": 1,
  "actual_direction": null
}
~~~

- [ ] 建立后端唯一 selector：monthly_average 读取 target_month，其他任务读取 target_date。
- [ ] 月均 SQL Actual join 使用 pa.target_month=p.target_month；季度/年度继续按 target_date。
- [ ] 月份归属、排序、phase range、去重、canonical backtest/live overlap 全部使用 selector。
- [ ] Dashboard compact schema 增加 target_month 槽位。为兼容全局 row_fields 可以保留 target_date 槽位，但月均 validator 和前端不得读取 legacy target_date。
- [ ] 月均缺 target_month、非法格式或重复月份时 DashboardDataError。
- [ ] 日期型任务 target_date 契约必须保持。
- [ ] 验证：

~~~bash
python -m pytest -q tests/test_scheme_metrics_actual_join.py tests/test_period_average_dashboard.py tests/test_dashboard_gate.py tests/test_factor_lab_dashboard_api.py
~~~

- [ ] Commit:

~~~bash
git add backend tests
git commit -m "refactor: expose monthly average target months"
~~~

### Task 8：修改月度平均前端

**Files**

- Modify: frontend/aifin-shell.js
- Modify: frontend/index.html
- Test: tests/test_frontend_overview_contract.py
- Test: tests/test_frontend_live_divider_contract.py

- [ ] 增加严格 YYYY-MM 校验：

~~~javascript
function requireTargetMonth(value, context) {
  var text = String(value || "");
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(text)) {
    throw dashboardDataError(context + " missing canonical target_month");
  }
  return text;
}
~~~

- [ ] 禁止月均 fallback 到 targetDate.slice(0, 7) 或 addMonth(targetDate)。
- [ ] 月均的主表、趋势图、月份筛选、指标、排行区间、phase 分隔、详情打开键、详情排序和重复检测全部使用 targetMonth。
- [ ] 月均详情：

    入口：预测明细
    抽屉标题：2026-02 月度平均预测明细
    列：预测日 | 目标月 | 预测方向 | 实际方向 | 结果

- [ ] 不增加“查看2026-02预测明细”等辅助文案。
- [ ] 不增加固定口径说明。
- [ ] 不出现“每日验证表”“目标日”。
- [ ] 只限定 taskType === monthly_average；其他任务不受影响。
- [ ] 验证：

~~~bash
python -m pytest -q tests/test_frontend_overview_contract.py tests/test_frontend_live_divider_contract.py tests/test_period_average_dashboard.py
~~~

- [ ] Commit:

~~~bash
git add frontend tests
git commit -m "fix: present monthly average targets as months"
~~~

### Task 9：受控物化 legacy target_month

**Files**

- Create: scripts/materialize_monthly_average_target_month.py
- Modify: scheduler/repository.py
- Test: tests/test_materialize_monthly_average_target_month.py

- [ ] 默认 inspect 只允许 SELECT，输出数据库身份、legacy live 行数、非法 pointer、重复月份和 canonical state_digest。
- [ ] 每条 legacy 行必须验证：

    task_type == monthly_average
    feature_date 是唯一 MID bucket anchor
    legacy target_date == feature_date + 1 natural day
    next MID bucket 唯一存在
    target_month 来自 next_bucket.label

- [ ] apply 必须显式提供：

~~~bash
python scripts/materialize_monthly_average_target_month.py --apply --expected-database-name <read-back-name> --expected-server-uuid <read-back-uuid> --expected-state-digest <inspect-digest> --expected-prediction-rows <N>
~~~

- [ ] 写入前再次核对数据库身份、摘要、数量、零冲突和 Writer idle。
- [ ] repository 使用 CAS 只补空字段：

~~~sql
UPDATE t_scheme_predictions
SET target_month = :target_month
WHERE id = :id
  AND target_month IS NULL
  AND target_date = :legacy_target_date
  AND feature_date = :feature_date
~~~

- [ ] affected rows 与预期不一致时整组回滚。
- [ ] 除新增 target_month 和 MySQL 自动更新 updated_at 外，不修改旧 target_date、方向、置信度、phase、run_id、version、extra、created_at 或任何其他字段。
- [ ] 该脚本只物化 legacy live 预测。旧 backtest run 保持完全不可变，由 Task 10 创建的新 canonical run 原生写 target_month；周期 Actuals 经现有 updater 重新生成，不由该脚本改写旧事实。
- [ ] 测试首次成功、重复 no-op、冲突阻断、重复月阻断、digest 漂移阻断、Writer active 阻断、数据库身份错误阻断。
- [ ] 此物化是一次显式授权的数据身份迁移，不扩展普通预测 update 权限。
- [ ] Commit:

~~~bash
git add scripts/materialize_monthly_average_target_month.py scheduler/repository.py tests/test_materialize_monthly_average_target_month.py
git commit -m "feat: materialize legacy monthly average target months"
~~~

### Task 10：新 canonical backtest 与 2026-06 缺口

- [ ] ECS、Mac3 分别只读枚举五个 M0 月均方案的 canonical backtest、gray_live、scheduled_live target_month 集合。
- [ ] 新 canonical backtest 只能包含 target_month < 2026-06。
- [ ] 旧 run 保留不可变审计，不删除、不修改。
- [ ] 新旧可对齐月份的方向、置信度、feature_date、version 必须一致。
- [ ] target_month=2026-06 归入 gray_live。
- [ ] 优先复用 ECS 同一 exact release/version、同一 snapshot/lineage 的已验证核心结果；Mac3 只在 Writer idle 后经 repository insert-only 物化。
- [ ] 允许复制字段仅为 scheme_id、target_tenor、horizon、target_month、predict_date、feature_date、direction、confidence、scheme_version 和必要 extra。
- [ ] 不复制数据库主键、源 run_id、Actuals、backtest metrics 或 Harness 历史。
- [ ] 已有任一 target_month 业务键时整组拒绝。
- [ ] 每个 1Y/3Y/5Y/7Y/10Y 月均方案必须满足：

    max(backtest.target_month) == 2026-05
    min(gray_live.target_month) == 2026-06
    backtest ∩ live == ∅
    目标月份连续
    每个 target_month 恰好一条业务结果

### Task 11：DashboardGate、文档和完整回归

**Files**

- Modify: harness/gates/dashboard_gate.py
- Modify: tests/test_dashboard_gate.py
- Modify: docs/architecture/PREDICTION_SEMANTICS.md
- Modify: docs/architecture/SCHEME_CONTRACT.md
- Modify: docs/sop/BLACKBOX_V2_PLATFORM_ONBOARDING_V1.md
- Modify: docs/sop/BLACKBOX_V2_UPSTREAM_DELIVERY_V1.md
- Modify: docs/blackbox_v2/PRODUCTION_READINESS.md
- Modify: docs/onboarding/README.md
- Deployment closure only: docs/CURRENT_STATUS.md、docs/TODO.md、AGENTS.md、CLAUDE.md

- [ ] DashboardGate 增加 target_month 格式、严格递增、无重复、backtest/live 零重叠、2026-06 无缺口、Actual 到期一致性检查。
- [ ] 文档删除 monthly_average 的旧表述：

    target_date=feature_date+1 是业务目标
    月均按 target_date 分月
    月均按 target_date join Actual
    月均按 target_date 与 gray_target_start 分区
    月均业务键包含 target_date

- [ ] 文档明确：

    monthly_average -> target_month
    date-target tasks -> target_date
    Contract 1.0 pointer -> sandbox compatibility only

- [ ] 月均一次性 batch 按 target_month 与 gray_target_month_start 分流；跨主机复用核对 target_month，不能再用 Contract pointer 充当业务键。
- [ ] 部署闭环后 CURRENT_STATUS/TODO 记录 migration、legacy 物化、新 canonical、2026-06、DashboardGate、公网验收和待自然时钟事项。
- [ ] AGENTS.md 与 CLAUDE.md 同步长期原则并保持字节一致。
- [ ] 验证：

~~~bash
python -m pytest -q
git diff --check
cmp -s AGENTS.md CLAUDE.md
~~~

Expected: 全部通过。

### Task 12：immutable release、ECS 先行、Mac3 后续

- [ ] 稳定提交只进入 codex/develop；master 不动。
- [ ] 从精确提交构建一份确定性 archive，记录 commit、archive SHA-256、manifest SHA-256、source-tree digest。
- [ ] ECS 先只读核对 current/previous、数据库身份、migration 001–020、systemd、Backend 和全部 Writer idle。
- [ ] ECS 只能通过 scripts/apply_migrations.py 和现场读回的 database name/server UUID 应用 021；不得直接执行 SQL。
- [ ] ECS 顺序：

    预安装 release
    聚焦测试
    expected-current CAS 激活
    target_month inspect
    受控 materialize apply
    新 canonical backtest
    补齐 2026-06 gray_live
    Backend / DashboardGate / localhost 前端验收

- [ ] ECS 全部通过后，Mac3 复制同一 immutable archive；不得从更新 Git HEAD 重建。
- [ ] Mac3 重复数据库身份、Writer idle、migration、物化、canonical、缺口和 DashboardGate 验收。
- [ ] 不修改域名、Nginx、DNS、Writer authority、launchd/systemd 调度或 DataBridge 时间。
- [ ] Mac3 公网验收：

    https://bond.finailab.cn/bond-factor-lab/ HTTP 200
    HTML/JS/CSS HTTP 200
    静态摘要与 current release 一致
    月均目标月为 YYYY-MM
    抽屉标题为“2026-02 月度平均预测明细”
    列为“预测日 / 目标月 / 预测方向 / 实际方向 / 结果”
    无“每日验证表”“目标日”和额外辅助文案
    全部 active base/composite DashboardGate 通过

- [ ] 更新闭环状态文档，运行完整测试和文档门禁，形成最终稳定 commit，普通 push origin/codex/develop，并读回远程 SHA 等于本地 HEAD。

## 5. 测试矩阵

| 层级 | 必测内容 | 关键失败条件 |
|---|---|---|
| Bucket | MID 相邻桶、跨年、15 日非交易日 | target_month 不是 next bucket label |
| Contract bridge | Contract 1.0 pointer echo + 平台 target_month | echo 漂移或平台月份缺失 |
| Repository | 月均 month key、日期任务 date key、insert-only | 部分重复、跨 key 重复、格式非法 |
| Backtest | 2026-05 最后历史月 | 2026-06 进入历史或两侧都缺失 |
| Actuals | 完整目标桶才生成 | 未到期提前写 Actual |
| API | 月均返回/使用 target_month | 从 legacy target_date 猜月份 |
| Dashboard | 月份排序、去重、phase 零重叠 | 同月重复或月份断裂 |
| Frontend | YYYY-MM、预测明细、目标月 | 每日/目标日文案残留 |
| Migration | inspect、identity、digest、CAS | 状态漂移或非规范 legacy 行 |
| Regression | 全部 task type | 日期型任务行为变化 |

## 6. 生产前后核对

物化和补齐前后必须保存只读摘要：

    t_scheme_predictions 总数
    五个月均 base scheme 行数
    target_month NULL/非 NULL 数量
    target_month 方向分布
    predict_date / feature_date 边界
    scheme_version、prediction_phase 分布
    t_backtest_runs / t_backtest_predictions 数量
    t_scheme_period_average_actuals 数量
    DataBridge 文件 SHA-256
    active Registry 数量
    Dashboard payload row 数量

允许变化只有：

1. legacy 月均行补充确定性 target_month；
2. 新 immutable canonical backtest run；
3. 原本确实缺失的 target_month=2026-06 insert-only gray live；
4. 与新目标身份对应且目标桶已经完整的 Actuals。

对已有预测行，id、run_id、scheme_id、tenor、horizon、predict_date、feature_date、phase、direction、confidence、model_version、scheme_version、created_at 必须逐行保持。

## 7. Fail-closed 条件

遇到任一条件立即停止：

- legacy target_date 不是 feature_date 后一个自然日；
- feature_date 不能唯一映射 MID bucket anchor；
- next MID bucket 不唯一或 target_month 无法解析；
- 同一 scheme/tenor/horizon 已有重复 target_month；
- backtest 与 live 在同一 target_month 重叠；
- 数据库身份、migration history、release/archive/manifest/source digest 不一致；
- inspect digest 与 apply 前状态不一致；
- DataBridge、预测或 Actuals Writer 正在运行；
- 五个 M0 月均 exact version 或输入 lineage 不一致；
- ECS 未先通过同一 immutable release；
- 距离自然 Writer 窗口不足安全余量；
- DashboardGate 不能覆盖全部 active base/composite 方案。

## 8. 本次 Review 最重要的四项

1. **业务模型：** monthly_average 在平台层彻底使用 target_month，不再使用业务 target_date。
2. **交付兼容：** Contract 1.0 pointer 只留在沙箱 Request/Result echo，不修改五个算法交付文件。
3. **历史兼容：** legacy live 行只补 target_month，旧 target_date 暂留审计但不参与任何月度业务逻辑；不删除、不重写核心结果。
4. **任务范围：** 本轮只修月度平均。季度和年度目标身份后续分别从第一性原理设计。
