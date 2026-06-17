# Bond Factor Lab 架构评审报告

> 历史审计快照：本报告保留 2026-06-09 当时的评审语境和问题清单，正文中的旧周度方案、serving pointer、旧 API 形态、旧 run/UK 设想等描述不代表当前实现状态。
>
> **不要把本文中的接口、表结构或流程片段当作当前 SOP 示例复制。** 当前权威状态以 [CURRENT_STATUS.md](CURRENT_STATUS.md)、[PREDICTION_SEMANTICS.md](PREDICTION_SEMANTICS.md)、[SCHEME_CONTRACT.md](SCHEME_CONTRACT.md) 和 [sop/SCHEME_ONBOARDING_T0.md](sop/SCHEME_ONBOARDING_T0.md) 为准。
>
> 审阅对象：`bond-factor-lab.zip`  
> 审阅目标：面向“国债因子实盘测试与方案对比平台”的工程架构、分层边界、方案入库流程、回测复现流程、实盘调度流程与 harness 控制面进行评审。  
> 核心定位：Bond Factor Lab 不应只是一个预测脚本集合，而应成为“方案进入生产前的灰度实验室”。

---

## 1. 总体结论

Bond Factor Lab 当前的分层方向是正确的，已经具备以下基础雏形：

- `shared/` 作为公共输入与 DB 读取层；
- `schemes/` 作为算法方案层；
- `scheduler/` 作为实盘预测调度层；
- `backtests/` 作为历史回测复现层；
- `backend/` 与 `frontend/` 作为展示与交互层；
- `harness/` 作为架构边界和入库流程守门工具。

但是，从“几十个方案持续灰度、自动比较、可审计、可复现、可挂载实盘”的最终目标看，当前工程还没有达到平台级状态。主要问题不是某个函数或模块的局部实现，而是：

```text
方案入库、回测复现、实盘挂载、授权写库、结果追溯之间，还没有形成一个不可绕过的闭环。
```

因此，Bond Factor Lab 后续应从“工程脚本集合”升级为一个完整的 **Scheme Lifecycle Control Plane**，即方案生命周期控制平台。

目标生命周期应为：

```text
算法交付
  -> 方案隔离归档
  -> 输入合同校验
  -> 静态边界校验
  -> 单测与 dry-run
  -> 原始回测复现
  -> 新旧输出对比
  -> 授权写入回测库
  -> API / 前端可见性验证
  -> 授权进入 live shadow
  -> 定时任务激活
  -> 实盘观察期
  -> 生产候选 / 淘汰 / 冻结
```

---

## 2. 当前工程值得肯定的设计

### 2.1 分层意识正确

当前项目已经明确区分：

| 层级 | 当前定位 | 评价 |
|---|---|---|
| `shared/` | 公共输入、公共服务、DB 读取 | 方向正确，应继续强化为唯一输入构造层 |
| `schemes/` | 算法方案层 | 方向正确，但需要更严格的方案合同与静态边界 |
| `backtests/` | 历史回测复现 | 必要模块，但需要不可变 run 与复现比较机制 |
| `scheduler/` | 实盘调度 | 方向正确，但需要 scheme activation 状态机 |
| `backend/` | FastAPI 服务与前端数据接口 | 需要强化只读边界和 admin 权限隔离 |
| `frontend/` | 原生 HTML/CSS/JS 实验室界面 | 适合早期快速迭代 |
| `harness/` | 强约束 gate | 方向非常关键，应升级为平台控制面 |

### 2.2 `harness` 方向非常关键

对于你的目标，`harness` 不应只是测试脚本，而应该是唯一可以批准以下动作的控制面：

```text
方案能否入库
回测结果能否持久化
方案能否进入 shadow
方案能否挂载 scheduler
方案能否 active
方案能否回滚或冻结
```

未来几十个方案并行时，平台是否可靠，主要取决于 `harness` 是否足够强。

---

## 3. 主要问题与风险

## 3.1 发布包混入敏感与非工程内容

上传包中包含不应出现在评审包或发布包中的内容，例如：

```text
.env
.git/
__pycache__/
.DS_Store
backtest_artifacts/
reports/
```

其中 `.env` 包含数据库连接信息，这属于严重风险。即使 `.gitignore` 已经忽略这些内容，只要打包方式仍然是手工 zip 项目根目录，就仍然可能泄露。

### 风险

- 数据库凭据泄露；
- 评审包污染历史产物；
- 测试与真实产物边界混乱；
- 无法判断哪些文件是源码、哪些文件是运行结果；
- 未来方案归档包可能携带密钥、缓存、报告、数据样本或本地路径。

### 建议

立即处理：

```text
1. 轮换 `.env` 中已经暴露过的数据库密码；
2. 禁止手工 zip 项目根目录；
3. 新增 scripts/export_clean_repo.sh；
4. 新增 PackageGate / SecretGate；
5. 所有对外评审包、方案包、归档包禁止包含 .env、.git、reports、artifacts、cache。
```

建议新增 gate：

```text
SecretGate
PackageGate
QuarantineGate
```

---

## 3.2 文档、代码、产物三者不一致

当前工程中，文档和报告中出现了一些 weekly 方案，例如类似：

```text
weekly_10y_d_overlay
weekly_5y_direct_production
weekly_7y_cross_d_overlay
```

但 `schemes/` 下实际可见方案主要是：

```text
schemes/t1_daily
schemes/t5_daily
```

同时，`reports/` 和 `backtest_artifacts/` 中存在 weekly 相关产物，但对应方案代码并不完整。

### 风险

平台未来必须回答：

```text
这条预测是谁生成的？
使用哪个 scheme？
哪个 version？
哪个 config？
哪次 harness run 批准？
输入数据 hash 是什么？
能否复现？
```

如果“报告里有、文档里有、代码里没有”，则会导致：

- 前端展示失真；
- 回测复现失败；
- scheduler registry 不可信；
- DB 中 scheme_id 无法追溯到代码；
- 方案比较结果无法审计。

### 建议

建立强制不变量：

```text
任何出现在 reports / DB / frontend / scheduler registry 中的 scheme_id，
必须能在 schemes/{scheme_id}/manifest.yaml 或 config.yaml 中找到对应方案定义。
```

建议新增 gate：

```text
RegistryConsistencyGate
ArtifactOwnershipGate
ReportCodeConsistencyGate
```

---

## 3.3 `harness` 没有被一等打包

当前 `pyproject.toml` 的 package include 包含：

```text
shared*
backend*
scheduler*
schemes*
backtests*
```

但没有包含：

```text
harness*
```

### 风险

如果按 package 安装，可能出现：

```text
python -m harness ... 不可用
```

这说明 `harness` 还没有被当作一等模块。对于该项目，这是错误的。`harness` 是平台控制面，不是可选脚本。

### 建议

修改 package include：

```toml
[tool.setuptools.packages.find]
include = [
  "shared*",
  "backend*",
  "scheduler*",
  "schemes*",
  "backtests*",
  "harness*"
]
```

同时拆分依赖环境：

```text
requirements-service.txt       # FastAPI / scheduler / DB
requirements-harness.txt       # pytest / ruff / mypy / pydantic / jsonschema
requirements-algo.txt          # 算法运行依赖
requirements-dev.txt           # 本地开发
```

长期建议引入：

```text
uv.lock / poetry.lock / conda-lock
```

---

## 3.4 当前 harness 仍偏骨架，没有形成完整闭环

当前 `harness` 已具备一些 gate，例如：

```text
static
input
unit
dry-run
backtest
api
live
```

但缺少几个关键控制点。

### 3.4.1 缺少 CompareGate

自动流程中应有：

```text
static -> input -> unit -> dry-run -> baseline reproduction -> compare -> backtest -> api
```

当前最关键的缺口是：

```text
算法原始输出 vs 平台归档后输出
```

如果没有 CompareGate，只能证明“平台能跑”，不能证明“平台跑的是同一个方案”。

### 3.4.2 缺少真正的 ActivationGate

当前 activation 逻辑尚未完整实现。你的最终目标是：

```text
方案通过验证后，进入 scheduler 实盘预测队列。
```

因此需要一个明确的 gate：

```text
ActivationGate
```

它负责：

```text
paused -> shadow -> active -> paused / retired
```

### 3.4.3 授权 token 机制不足

当前授权 token 更像简单的 JSON 编码，不足以承担“写库 / 挂实盘 / 激活”的安全边界。

建议升级为：

```text
SignedAuthorizationToken
```

字段至少包括：

```text
action: backtest_persist / live_shadow / activate / rollback
scheme_id
scheme_version
predict_date 或 date_range
harness_run_id
issued_by
issued_at
expires_at
nonce
hmac_signature
```

强制规则：

```text
token 只能使用一次
token 必须绑定 action
token 必须绑定 scheme_id
token 必须绑定 scheme_version
token 必须绑定 harness_run_id
token 不能跨环境使用
token 必须有 TTL
token 必须有签名校验
```

---

## 3.5 StaticGate 还不够强

### 3.5.1 扫描范围应覆盖 `core/**/*.py`

未来方案可能是文件夹结构：

```text
schemes/foo/core/models/model.py
schemes/foo/core/features/factor.py
schemes/foo/core/utils/io.py
```

StaticGate 不应只扫描一层文件，而应扫描：

```text
schemes/{scheme_id}/core/**/*.py
```

### 3.5.2 `legacy_*.py` 不应成为逃逸口

可以允许 legacy 文件存在，但必须规定：

```text
legacy 文件不得被 active predict.py / core import
legacy 文件不得访问 DB
legacy 文件不得写文件
legacy 文件不得跨方案 import
legacy 文件只能作为归档证据
```

### 3.5.3 `predict.py` 应保持薄 adapter

当前 `predict.py` 中存在通过 `shared.data_service` 或类似方式构造 DB engine 的迹象。原则上这会削弱分层边界。

建议规则：

```text
schemes/*/core/**:
  - 禁止 DB
  - 禁止 shared.data_service
  - 禁止 shared.db_config
  - 禁止 sqlalchemy
  - 禁止 pandas.read_sql
  - 禁止文件写入
  - 禁止跨方案 import

schemes/*/predict.py:
  - 只能调用 shared.input_artifacts
  - 不允许直接调用 shared.data_service
  - 不允许自行 create engine
  - 不允许自行决定 DB 查询逻辑
  - 可以做 PredictionRecord 适配
```

建议引入统一上下文：

```python
PredictionContext(
    scheme_id: str,
    scheme_version: str,
    predict_date: date,
    input_artifact: InputArtifact,
    calendar_artifact: CalendarArtifact,
    config: SchemeConfig,
)
```

方案只接收 context，不自己构造 DB engine。

---

## 3.6 输入产物缺少完整可追溯信息

当前已有 `InputArtifact` 概念，这是正确方向。但灰度实验室需要更完整的数据指纹。

### 建议记录字段

```text
artifact_id
artifact_path
content_hash
schema_hash
data_version
source_table
source_query_hash
min_date
max_date
row_count
column_count
null_ratio
duplicate_key_count
source_watermark
created_at
created_by
```

### 需要回答的问题

平台必须能够回答：

```text
为什么同一个 scheme_id 同一个 predict_date，今天和昨天跑出来不一样？
是代码变了？
是 config 变了？
是 DB 历史数据回填了？
是交易日历变了？
还是依赖包版本变了？
```

因此建议新增：

```text
t_input_artifacts
```

并让预测结果和回测结果引用：

```text
artifact_id 或 input_artifact_hash
```

---

## 3.7 DB 写入模型不适合灰度实验室

当前预测表更像按：

```text
scheme_id + target_tenor + predict_date
```

做唯一约束。这样容易导致同一方案同一天重跑时覆盖旧值。

### 风险

对于生产系统，覆盖 latest 可能可以接受；但对于灰度实验室，这是不够的。实验室必须保留每一次运行：

```text
第一次跑了什么？
第二次修 bug 后跑了什么？
哪个 run 通过 harness？
哪个 run 被前端展示？
哪个 run 被废弃？
```

### 建议改为两层模型

#### 原始不可变运行层

```text
t_scheme_runs
t_scheme_run_predictions
```

每次运行生成一个：

```text
run_id
```

预测结果写入：

```text
run_id + scheme_id + scheme_version + target_tenor + predict_date
```

不覆盖历史。

#### 服务展示层

使用 view 或 pointer 表表达“当前前端展示版本”：

```text
v_latest_approved_predictions
```

或：

```text
t_scheme_prediction_serving_pointer
```

这样既能保留历史 run，又能让前端展示 latest approved 结果。

---

## 3.8 回测运行也应不可变

当前回测结果如果按：

```text
benchmark_id + scheme_id + data_source + start_date + end_date
```

进行合并或覆盖，不适合长期审计。

回测是证据，不是缓存。每次回测都应产生新的：

```text
backtest_run_id
```

建议 `t_backtest_runs` 字段包括：

```text
backtest_run_id
benchmark_id
scheme_id
scheme_version
code_hash
config_hash
input_artifact_hash
data_version
start_date
end_date
run_mode: no_persist / persist / reproduction / comparison
harness_run_id
status
created_at
```

---

## 3.9 API 层存在副作用与权限风险

### 3.9.1 GET API 不应触发写库

当前存在读接口触发 registry sync 的风险。对于平台架构，原则应为：

```text
所有 GET 接口必须 read-only。
```

Registry sync 应只能由：

```text
harness
admin CLI
migration job
```

执行。

### 3.9.2 手动 trigger 接口需要强保护

类似：

```text
POST /api/schemes/{scheme_id}/trigger
```

这种接口会触发预测任务，必须是 admin API，不能裸露给普通前端。

建议：

```text
普通前端只能读
手动触发必须走 admin API
admin API 必须有 RBAC 或 token 保护
trigger 必须生成 run_id
trigger 必须写 run_log
trigger 必须标记 triggered_by
trigger 必须通过 harness authorization 或 emergency override
```

同时，CORS 不建议长期使用：

```text
allow_origins=["*"]
```

---

## 3.10 Scheduler 还没有完全平台化

当前 scheduler 已经能跑任务，但还需要补足：

| 问题 | 建议 |
|---|---|
| weekly actuals updater 挂载不清晰 | 每种 frequency / horizon 都要有 actuals updater |
| 运行时反复 discover / sync registry | 启动时构建 registry snapshot |
| subprocess 输出依赖 stdout JSON | 改为 JSONL protocol 或 result.json 文件 |
| 缺少 per-scheme lock | 防止同一方案同一日期重复并发运行 |
| 缺少 run_id 级幂等语义 | 每次运行生成新 run_id，不静默覆盖 |
| active 状态来自 config | 应由 DB activation state + config version 决定 |
| 缺少生命周期状态 | 增加 draft / validated / shadow / active / paused / retired |

建议 scheduler 执行前加载只读快照：

```text
t_scheme_versions
t_scheme_activation
```

然后只执行：

```text
active schemes
shadow schemes
```

不要在 API 或普通任务执行时自动扫描目录并写 registry。

---

## 4. 推荐目标架构

建议调整为：

```text
bond-factor-lab/
  shared/
    input_artifacts.py
    calendar_artifacts.py
    data_service.py
    db.py
    contracts.py

  schemes/
    {scheme_id}/
      manifest.yaml
      config.yaml
      predict.py
      core/
      tests/
      benchmarks/
      docs/
      requirements.lock

  backtests/
    runners/
    repository.py
    contracts.py

  scheduler/
    registry_loader.py
    executor.py
    jobs.py
    locks.py

  backend/
    main.py
    read_services.py
    admin_services.py

  frontend/
    index.html
    app.js
    styles.css

  harness/
    gates/
    contracts/
    authorization.py
    orchestrator.py
    reports/
    cli.py

  migrations/
  scripts/
  docs/
```

核心原则：

```text
shared/       负责输入合同和数据读取
schemes/      只负责算法逻辑和薄适配
backtests/    负责历史复现和只写 t_backtest_*
scheduler/    负责实盘运行，不做验证
backend/      默认只读，admin 写入必须授权
harness/      唯一可以批准入库、写回测、挂实盘、激活方案的控制面
```

---

## 5. Scheme Package Contract

算法同学未来可能交付：

```text
一个 py 文件
一个文件夹
```

但平台不应直接接收为正式方案。建议统一先进入：

```text
incoming/{request_id}/
```

然后由 Codex / 工程人员整理成标准方案结构：

```text
schemes/{scheme_id}/
  manifest.yaml
  config.yaml
  predict.py
  core/
    model.py
    features.py
    signal.py
  tests/
    test_contract.py
    test_regression.py
  benchmarks/
    original_backtest_summary.json
    original_predictions_sample.csv
    expected_metrics.json
  docs/
    onboarding.md
    algorithm_note.md
```

### 5.1 `manifest.yaml` 示例

```yaml
scheme_id: tsy_daily_t5_macro_v1
owner: quant_team_a
created_by: alice
description: "5 trading-day treasury yield direction predictor"
asset_class: treasury_yield
frequency: daily
horizon:
  unit: trading_day
  value: 5
target_tenors:
  - 3Y
  - 5Y
  - 7Y
  - 10Y
target_rule: direction_5d
data_contract:
  input_builder: shared.input_artifacts.build_daily_input_artifact
  data_version: daily_v1
  required_columns:
    - trade_date
    - target_tenor
    - value
backtest_contract:
  benchmark_id: original_research_2026_06
  start_date: "2023-01-01"
  end_date: "2025-12-31"
  expected_metrics_file: schemes/tsy_daily_t5_macro_v1/benchmarks/expected_metrics.json
runtime_contract:
  entrypoint: schemes.tsy_daily_t5_macro_v1.predict:run
  timeout_seconds: 300
  memory_mb: 2048
  allowed_outputs:
    - prediction_records
```

### 5.2 `config.yaml` 示例

```yaml
status: paused
frequency: daily
cron: "30 15 * * 1-5"
predict_after_market_close: true
target_tenors:
  - 3Y
  - 5Y
  - 7Y
  - 10Y
prediction:
  output_type: direction
  confidence_required: true
```

建议：

```text
manifest.yaml 描述方案身份、合同、输入、回测、运行边界；
config.yaml 描述运行参数和调度配置；
不要让 config.yaml 同时承担所有职责。
```

---

## 6. Harness 工程设计建议

`harness` 应从脚本升级为完整控制面。建议包含以下 15 个 gate。

---

### Gate 0：IntakeGate

目的：检查算法交付包是否可接收。

检查项：

```text
是否包含 README / algorithm note
是否声明期限、标的、horizon、frequency
是否声明回测区间
是否声明原始回测结果
是否包含原始预测样本
是否包含外部依赖说明
```

输出：

```text
reports/harness/{scheme_id}/{harness_run_id}/00_intake.json
```

---

### Gate 1：QuarantineGate

目的：防止脏内容进入工程。

必须禁止：

```text
.env
.git/
*.pem
*.key
*.pkl 中未知来源模型
大体积数据文件
DB dump
个人路径
绝对路径
__pycache__/
.DS_Store
notebook checkpoint
```

---

### Gate 2：ContractGate

目的：验证 scheme 合同。

检查：

```text
scheme_id 命名合法
scheme_id 与目录名一致
manifest.yaml 存在
config.yaml 存在
predict.py 存在
core/ 存在
horizon 合法
frequency 合法
target_tenors 合法
required_columns 非空
backtest 区间明确
原始 benchmark 文件存在
```

建议 `scheme_id` 命名规则：

```text
{asset_or_tenor}_{frequency}_{horizon}_{short_method}_{version}
```

例如：

```text
tsy_daily_t5_macro_v1
tsy_weekly_10y_overlay_v1
```

---

### Gate 3：StaticBoundaryGate

目的：强制架构边界。

`schemes/{id}/core/**` 禁止：

```text
sqlalchemy
pymysql
psycopg2
pandas.read_sql
shared.data_service
shared.db_config
shared.repository
scheduler
backend
backtests
schemes.other_scheme
requests
urllib
subprocess
os.system
open(..., "w")
Path.write_text
Path.write_bytes
pickle.load
joblib.load 未声明模型来源
```

`schemes/{id}/predict.py` 只允许：

```text
shared.input_artifacts
shared.contracts
shared.calendar_artifacts
schemes.{id}.core
```

---

### Gate 4：DependencyGate

目的：验证运行环境。

检查：

```text
service env 可启动
harness env 可启动
algo env 可 import
scheme requirements.lock 是否存在
是否使用未锁定依赖
是否 import 了本地不存在的包
```

---

### Gate 5：InputArtifactGate

目的：验证输入数据合同。

检查：

```text
必须通过 shared.input_artifacts 构造输入
data_version 正确
required_columns 全部存在
row_count > 0
predict_date 覆盖正确
min/max date 合法
交易日历合法
空值率低于阈值
重复 key 数为 0
输入 artifact hash 生成成功
source watermark 记录成功
```

建议配置：

```yaml
input_quality:
  min_rows: 500
  max_null_ratio:
    value: 0.02
    factor_x: 0.05
  duplicate_keys:
    - trade_date
    - target_tenor
```

---

### Gate 6：UnitGate

每个方案应在 manifest 中声明测试：

```yaml
tests:
  - schemes.tsy_daily_t5_macro_v1.tests.test_contract
  - schemes.tsy_daily_t5_macro_v1.tests.test_regression
```

UnitGate 执行：

```text
方案自带测试
平台通用 contract tests
输出 schema tests
target_date rule tests
tenor coverage tests
```

---

### Gate 7：DryRunGate

目的：单日运行，但不允许产生 DB 持久化副作用。

检查：

```text
predict.py 能正常执行
返回 PredictionRecord 列表
target_tenor 覆盖完整
predict_date 正确
target_date 正确
signal 合法
confidence 合法
extra 字段合法
stdout/stderr 协议合法
无 DB 写入
无非授权文件写入
```

建议不要依赖 stdout 纯 JSON，可改为：

```text
runner 捕获日志到 stderr
stdout 只允许 JSONL protocol
或使用临时 result.json 输出
```

---

### Gate 8：BaselineReproductionGate

目的：复现算法同学原始回测结果。

读取：

```text
schemes/{id}/benchmarks/original_backtest_summary.json
schemes/{id}/benchmarks/original_predictions_sample.csv
```

比较：

```text
样本数量
日期范围
tenor 覆盖
direction 序列
score / confidence 容差
核心指标容差
```

建议容差配置：

```yaml
reproduction_tolerance:
  exact_match:
    - predict_date
    - target_tenor
    - signal
  numeric_abs_tolerance:
    confidence: 1.0e-8
    predicted_change: 1.0e-6
  metric_relative_tolerance:
    accuracy: 0.001
    hit_ratio: 0.001
```

---

### Gate 9：CompareGate

目的：比较“原始方案”与“平台归档方案”。

它要回答：

```text
平台归档有没有改变算法行为？
```

输出：

```text
comparison_summary.json
comparison_diff.csv
```

比较维度：

```text
总预测条数
缺失日期
缺失 tenor
新增日期
新增 tenor
方向一致率
confidence 最大差异
confidence 平均差异
指标差异
```

通过标准示例：

```text
direction_match_rate == 100%
missing_records == 0
extra_records == 0
max_confidence_abs_diff <= 1e-8
metric_accuracy_abs_diff <= 0.001
```

---

### Gate 10：BacktestNoPersistGate

目的：执行完整回测，但不写 DB。

输出：

```text
backtest_summary.json
backtest_predictions.parquet 或 csv
metrics.json
```

这一步用于前端预览和人工审查。

---

### Gate 11：BacktestPersistGate

目的：授权后写入 `t_backtest_*`。

必须要求授权 token：

```text
action = backtest_persist
scheme_id = 当前方案
scheme_version = 当前版本
harness_run_id = 当前 run
```

并检查 DB delta：

```text
只允许写 t_backtest_*
不允许写 t_scheme_predictions
不允许写 t_scheme_run_log
不允许写业务源表
```

---

### Gate 12：ApiReadOnlyGate

目的：确认后端和前端能看到该方案的回测结果。

检查：

```text
/api/backtests/factor-lab 是否返回该 scheme
/api/schemes 是否只读
API 数据与 DB 数据一致
前端需要的字段齐全
```

重点：API gate 不应该自动 sync registry，不应该写 DB。

---

### Gate 13：LiveShadowGate

目的：方案进入实盘 shadow，但不是生产。

可采用两种设计。

方案 A：同表加 run_type：

```text
t_scheme_predictions.run_type = shadow / active / manual
```

方案 B：shadow 独立表：

```text
t_scheme_shadow_predictions
```

更建议方案 A，因为前端对比更自然，但必须有 serving pointer。

LiveShadowGate 检查：

```text
只允许写当前 scheme_id
只允许写当前 predict_date
必须生成 run_id
必须关联 harness_run_id
必须关联 input_artifact_hash
不能覆盖旧 run
```

---

### Gate 14：ActivationGate

目的：将 shadow 方案变为 scheduler active。

ActivationGate 应执行：

```text
status: paused -> active
activation row 写入 t_scheme_activation
scheduler registry 验证
cron 表达式验证
下一次运行时间计算
actuals updater 验证
frontend 展示状态更新
```

需要授权：

```text
action = activate
```

---

### Gate 15：ObservationGate

目的：观察期监控。

例如新方案进入 shadow 后观察 20 个交易日：

```text
coverage >= 95%
预测任务成功率 >= 95%
无 schema drift
无 input freshness failure
direction accuracy 不显著劣化
confidence calibration 正常
```

通过后，方案才进入生产候选。

---

## 7. 推荐数据库模型

### 7.1 `t_scheme_versions`

```sql
scheme_id
scheme_version
code_hash
config_hash
manifest_hash
git_commit
status              -- draft / validated / shadow / active / paused / retired
created_by
created_at
approved_by
approved_at
```

### 7.2 `t_harness_runs`

```sql
harness_run_id
scheme_id
scheme_version
stage
status
started_at
finished_at
triggered_by
project_root
git_commit
code_hash
config_hash
report_uri
```

### 7.3 `t_harness_gate_results`

```sql
harness_run_id
gate_name
status
started_at
finished_at
summary_json
report_uri
```

### 7.4 `t_input_artifacts`

```sql
artifact_id
scheme_id
scheme_version
predict_date
frequency
data_version
artifact_uri
content_hash
schema_hash
source_watermark
row_count
min_date
max_date
created_at
```

### 7.5 `t_scheme_runs`

```sql
run_id
scheme_id
scheme_version
run_type             -- dry_run / shadow / active / manual
predict_date
status
harness_run_id
input_artifact_id
started_at
finished_at
records_expected
records_returned
records_written
error_message
```

### 7.6 `t_scheme_predictions`

建议从“按 scheme/date 覆盖”改成“按 run_id 不可变记录”：

```sql
prediction_id
run_id
scheme_id
scheme_version
target_tenor
predict_date
target_date
signal
confidence
extra_json
created_at
```

### 7.7 serving pointer

用于前端读取当前展示版本：

```sql
t_scheme_serving_pointer
```

字段示例：

```sql
scheme_id
target_tenor
predict_date
serving_run_id
serving_status       -- approved / hidden / deprecated
updated_by
updated_at
```

也可以建立视图：

```text
v_latest_approved_predictions
```

---

## 8. 推荐完整 SOP

## Step 1：算法交付

算法同学交付：

```text
原始 py / folder
算法说明
期限 + 标的
预测频率
预测 horizon
回测区间
原始回测结果
原始预测样本
依赖说明
```

禁止直接放入：

```text
schemes/
```

必须先进入：

```text
incoming/{request_id}/
```

---

## Step 2：隔离扫描

执行：

```bash
python -m harness gate intake --request-id <request_id>
python -m harness gate quarantine --request-id <request_id>
```

检查：

```text
无密钥
无 .git
无 .env
无 DB dump
无大文件
无绝对路径
无本地个人目录
```

---

## Step 3：方案标准化归档

整理为：

```text
schemes/{scheme_id}/manifest.yaml
schemes/{scheme_id}/config.yaml
schemes/{scheme_id}/predict.py
schemes/{scheme_id}/core/
schemes/{scheme_id}/benchmarks/
schemes/{scheme_id}/tests/
```

默认：

```yaml
status: paused
```

不能 active。

---

## Step 4：合同校验

执行：

```bash
python -m harness gate contract --scheme-id <scheme_id>
```

验证：

```text
scheme_id
manifest
config
entrypoint
horizon
frequency
target_tenors
backtest range
benchmark files
```

---

## Step 5：静态边界校验

执行：

```bash
python -m harness gate static --scheme-id <scheme_id>
```

必须保证：

```text
core 不访问 DB
core 不写文件
core 不跨方案 import
predict.py 只走 shared.input_artifacts
不访问 backend / scheduler
不访问其他 schemes
```

---

## Step 6：输入产物校验

执行：

```bash
python -m harness gate input \
  --scheme-id <scheme_id> \
  --predict-date YYYY-MM-DD
```

验证：

```text
输入数据能构造
schema 正确
数据版本正确
覆盖区间正确
无严重空值
无重复 key
生成 input_artifact_hash
```

---

## Step 7：单测

执行：

```bash
python -m harness gate unit --scheme-id <scheme_id>
```

包括：

```text
方案自带测试
平台通用 contract tests
输出 schema tests
target_date rule tests
tenor coverage tests
```

---

## Step 8：dry-run

执行：

```bash
python -m harness gate dry-run \
  --scheme-id <scheme_id> \
  --predict-date YYYY-MM-DD
```

要求：

```text
返回预测
不写 DB
不污染源表
输出合法 PredictionRecord
```

---

## Step 9：复现原始回测

执行：

```bash
python -m harness gate reproduce \
  --scheme-id <scheme_id>
```

对比：

```text
原始 benchmark
平台重跑结果
```

通过后才能进入下一步。

---

## Step 10：新旧输出比较

执行：

```bash
python -m harness gate compare \
  --scheme-id <scheme_id>
```

检查：

```text
direction 一致率
confidence 差异
指标差异
缺失样本
新增样本
```

这是确认“算法行为没有被平台归档改坏”的核心证据。

---

## Step 11：回测 no-persist

执行：

```bash
python -m harness gate backtest \
  --scheme-id <scheme_id> \
  --mode no-persist
```

产生报告，但不写库。

---

## Step 12：授权写入回测库

发行授权：

```bash
python -m harness auth issue \
  --scheme-id <scheme_id> \
  --scheme-version <version> \
  --action backtest_persist \
  --harness-run-id <harness_run_id> \
  --issued-by <reviewer>
```

写入：

```bash
python -m harness gate backtest \
  --scheme-id <scheme_id> \
  --mode persist \
  --authorize <token>
```

约束：

```text
只能写 t_backtest_*
不能写 t_scheme_predictions
不能写业务源表
```

---

## Step 13：API / 前端验证

执行：

```bash
python -m harness gate api --scheme-id <scheme_id>
```

检查：

```text
后端能读到方案
前端矩阵能展示方案
指标字段完整
DB 与 API 一致
无 GET 写库副作用
```

---

## Step 14：授权进入 live shadow

发行授权：

```bash
python -m harness auth issue \
  --scheme-id <scheme_id> \
  --scheme-version <version> \
  --action live_shadow \
  --predict-date YYYY-MM-DD \
  --harness-run-id <harness_run_id> \
  --issued-by <reviewer>
```

执行：

```bash
python -m harness gate live \
  --scheme-id <scheme_id> \
  --predict-date YYYY-MM-DD \
  --authorize <token>
```

要求：

```text
生成 run_id
写入 shadow / lab prediction
不覆盖历史 run
关联 harness_run_id
关联 input_artifact_hash
```

---

## Step 15：激活 scheduler

发行授权：

```bash
python -m harness auth issue \
  --scheme-id <scheme_id> \
  --scheme-version <version> \
  --action activate \
  --harness-run-id <harness_run_id> \
  --issued-by <reviewer>
```

执行：

```bash
python -m harness activate \
  --scheme-id <scheme_id> \
  --scheme-version <version> \
  --authorize <token>
```

激活后：

```text
status = active
scheduler registry 更新
cron 生效
frontend 状态更新
```

---

## Step 16：观察期

观察：

```text
任务成功率
预测覆盖率
actuals 回填率
direction accuracy
confidence calibration
异常日志
数据 freshness
```

达到标准后，方案成为生产候选。

---

## 9. 改造优先级路线图

## P0：必须立即修复

```text
1. 轮换 .env 中暴露过的 DB 密码
2. 提供 clean export 脚本，禁止 .env / .git / artifacts / reports 被打包
3. pyproject.toml 加入 harness*
4. 文档、schemes、reports、DB registry 对齐；weekly 方案要么补代码，要么移除声明
5. GET API 去掉写库副作用
6. trigger API 加认证授权，或先禁用
7. harness authorization 改成 HMAC + TTL + one-time + action-bound
8. 实现 CompareGate
9. 实现 ActivationGate
10. scheduler 挂载 weekly actuals updater
```

## P1：平台核心能力

```text
1. 引入 scheme_version
2. 引入 run_id
3. 预测结果改为不可变 run 写入
4. 增加 input_artifact_hash
5. 增加 t_harness_runs / t_harness_gate_results
6. StaticGate 扫描 core/**/*.py
7. StaticGate 检查文件写入、网络、subprocess、pickle/joblib、跨方案 import
8. InputGate 增加 freshness / coverage / null ratio / duplicate key 检查
9. BacktestPersistGate 严格限制只写 t_backtest_*
10. LiveGate 检查写入行必须属于当前 scheme_id + predict_date + run_id
```

---

## 10. 必须坚持的架构硬规则

### 规则一：方案不能自己读 DB

```text
任何 scheme 的算法输入必须来自 shared.input_artifacts。
```

即使 `predict.py` 是 adapter，也不应自己构造 DB engine。

---

### 规则二：core 永远纯净

```text
schemes/{id}/core/** 只能做纯计算。
```

禁止：

```text
DB
文件写入
网络
scheduler
backend
其他 schemes
环境变量读取
```

---

### 规则三：任何实盘预测必须有 run_id

没有 `run_id` 的预测不可接受。

前端展示的每个数字都应该能追溯到：

```text
scheme_id
scheme_version
run_id
harness_run_id
input_artifact_hash
code_hash
config_hash
predict_date
created_at
```

---

### 规则四：回测结果不可覆盖

回测是证据，不是缓存。

同一个方案同一个区间可以跑多次，但每次都应该生成新的：

```text
backtest_run_id
```

---

### 规则五：API 默认只读

展示系统不要顺手做 sync、trigger、activation。

```text
GET API 不写库
POST admin API 必须授权
scheduler 写库必须留痕
harness 是唯一控制面
```

---

### 规则六：harness 报告必须可审计

每个 gate 都应输出：

```text
status
start/end time
scheme_id
scheme_version
code_hash
config_hash
input_artifact_hash
DB delta
stdout/stderr tail
failure reason
report path
```

并写入：

```text
t_harness_runs
t_harness_gate_results
```

---

## 11. 推荐最终工作流图

```text
                ┌────────────────────┐
                │ Algorithm Delivery │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │ incoming quarantine │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │ Scheme Normalizer   │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │ Harness Gates       │
                │ static/input/unit   │
                │ dry/repro/compare   │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │ Backtest Persist    │
                │ only t_backtest_*   │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │ Live Shadow         │
                │ run_id + artifact   │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │ Activation Gate     │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │ Scheduler Active    │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │ Frontend Comparison │
                └────────────────────┘
```

---

## 12. 最终建议

Bond Factor Lab 的核心价值不是“多跑几个预测脚本”，而是建立一套生产前的量化方案灰度实验基础设施。它必须做到：

```text
每个方案可归档；
每次回测可复现；
每条预测可追溯；
每次写库可授权；
每个 active 状态可审计；
每个前端展示结果能回到代码版本、输入数据和 harness run。
```

因此，下一阶段最重要的工程重点不是继续增加新方案，而是先把 `harness` 升级为真正的控制面：

```text
方案不能绕过 harness 入库；
回测不能绕过 harness 持久化；
实盘不能绕过 harness 挂载；
前端展示不能脱离 run_id、scheme_version、input_artifact_hash 和 authorization record。
```

只有这样，Bond Factor Lab 才能从一个单方案/少方案 PoC，升级为可以承载几十个国债收益率预测方案的生产前灰度实验室。
