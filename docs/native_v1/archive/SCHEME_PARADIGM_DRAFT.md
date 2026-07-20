# 历史归档：SCHEME_PARADIGM.md

> 文档状态：HISTORICAL
> 适用运行时：native_adapter（冻结前历史）
> 目标读者：历史审计人员
> 最后核验日期：2026-07-19
> 原文件 SHA256：`ff8654ca2cc0e2874c2f4a8c1a04befb0b28ce02a7e3b4e68720538b10d63792`
> 当前替代文档：[../README.md](../README.md)

本文件保留冻结前原文；仅将相对链接迁移到归档后的有效路径。不得用于新增方案或当前验收。

## 原始正文

# 方案生命周期目标态提案（DRAFT）

**更新日期**: 2026-07-14
**状态**: 目标态设计提案，非现行强制规范。
**定位**: 描述方案从「取数 → 消费 → 保存 → 输出」的候选目标范式，供后续平台能力改造评审。本文不会自动改变现有方案接口、目录结构、source benchmark 或 harness gate。
**现行契约优先级**: [SCHEME_CONTRACT.md](../../architecture/SCHEME_CONTRACT.md)、[PREDICTION_SEMANTICS.md](../../architecture/PREDICTION_SEMANTICS.md)、[SOURCE_ALGORITHM_FIDELITY.md](../../architecture/SOURCE_ALGORITHM_FIDELITY.md)、[CODE_ARCHITECTURE.md](../../architecture/CODE_ARCHITECTURE.md) 与 [HARNESS_ARCHITECTURE.md](../../architecture/HARNESS_ARCHITECTURE.md) 共同构成当前可执行规则；与本文冲突时，以这些现行文档和机器 gate 为准。

> 本文中的 **必须 / 禁止 / 应当** 只描述目标态验收标准。只有对应接口、迁移、gate、测试和现行契约同步落地后，条款才可转为 fail-closed 规则。不得使用本文尚未落地的 `train/predict`、`SnapshotGate`、`DeterminismGate` 或目录要求拒绝当前方案。
>
> source-backed 方案迁移仍受算法保真分级约束：L0 平台 I/O 适配可以评审，L1 必须证明未移动算法锚点，L2 算法内部改动默认禁止。本文不得被用来重写原始训练、窗口、投票、fallback、阈值或内部 score 映射。
> 业务语义前置依赖：
> - 日期语义（`predict_date` / `feature_date` / `target_date` / `prediction_phase`）以 [PREDICTION_SEMANTICS.md](../../architecture/PREDICTION_SEMANTICS.md) 为准。
> - 分层边界（输入单点 / 写库单点 / core 纯净）以 [CODE_ARCHITECTURE.md](../../architecture/CODE_ARCHITECTURE.md) 与 [HARNESS_ARCHITECTURE.md](../../architecture/HARNESS_ARCHITECTURE.md) 为准。
> - 方案身份（`base_scheme_id` vs registry composite `scheme_id`）以 [SCHEME_CONTRACT.md](../../architecture/SCHEME_CONTRACT.md) 为准。

---

## 0. 范式总纲

### 0.1 唯一总纲（一句话）

> **目标态框架只按「数据流 + 生命周期 + 契约」调度，不按具体模型类型分支。完成平台迁移后的新范式方案，才使用 `train` / `predict` 纯钩子。**

现行方案继续遵守 `predict.py::run(predict_date) -> list[PredictionRecord]`。目标态要求框架不得针对 `xgb`、`lgbm`、投票、回归等模型类型编写不同执行分支；通用审计字段中的描述性值（例如 `source_component="rule_vote"`）不属于模型分支，允许保留。

### 0.2 落地状态

| 能力 | 当前状态 | 本文角色 |
|---|---|---|
| `shared.input_artifacts` 唯一输入入口 | 已落地、强制 | 必须保持 |
| `predict.py::run(predict_date)` + `PredictionRecord` | 已落地、强制 | 迁移期间兼容 |
| source fidelity L0/L1/L2 与 CompareGate | 已落地、强制 | 不得被目标态覆盖 |
| `train/predict` 双钩子、param store | 未落地 | 候选目标态 |
| `SnapshotGate` / `DeterminismGate` | 未落地 | 设计名，不是现有 gate |
| per-scheme `check_data/model_parameter/output/backtest` | 未统一落地 | 目录迁移提案 |

### 0.3 四段生命周期

| 段 | 名称 | 唯一动作 | 产物 |
|:--:|------|----------|------|
| ① | 取数 | DB 直出内存 `DataFrame`，单点经公共层 | 内存 df |
| ② | 消费 | 单一 `normalize` → 无损 canonical 快照（check_data）→ 内存注入 core | check_data CSV + 内存 df |
| ③ | 保存 | `model_parameter` = 配置（产物，gitignore）；模型确定性重训、不落盘 | model_parameter/ |
| ④ | 输出 | core 只返回富 `PredictionRecord`；框架做 output 留痕 + 写库单点 + run_mode 路由 | output/ + DB 行 |

### 0.4 框架 vs 算法 边界总表（贯穿四段）

| 框架 / 公共层 / harness 拥有（任何方案不变） | 算法 core 拥有（黑盒，随方案变） |
|---|---|
| ① 取数：`shared.data_service` 唯一 DB 导出 | 消费 df 的哪些列、怎么变换 |
| ② 快照：`normalize` 落盘、hash、无损往返、check_data 目录 | —— |
| ③ 存储：`model_parameter` 版本化 / gitignore / manifest / 时点选择 | 「配置」是什么（超参 / 权重 / 系数 / 空） |
| ③ 确定性：seed 注入、输入 hash 钉定、行为级 gate | `train` 怎么选配置；`predict` 怎么算 |
| ④ 持久化：`PredictionRecord` schema、写库单点、run_mode 路由、output 留痕 | direction/prob 怎么算、`feature_attribution` 内部形态 |
| ④ 语义编码：方向词、tenor/task_type/frequency、`target_date` 推导、factor→code、去重/upsert | —— |
| 点时 `feature_date`（`shared.calendar_service`） | 模型类型（全程不可见） |

---

## 1. ①取数

**1.1** 当前与目标态都必须保持 `shared.input_artifacts` 为算法输入唯一入口；它内部调用 `shared.data_service` 从 DB 导出并生成 `InputArtifact`。adapter（`predict.py`）和 backtest runner **禁止**绕过 `input_artifacts` 自拼 SQL、自连 DB、自读源表。此即「输入单点」不变量（[CODE_ARCHITECTURE.md](../../architecture/CODE_ARCHITECTURE.md)）。

**1.2** 方案**必须**在 `config.yaml` 的 `input_spec` 声明取数口径（`data_version` / `required_columns` / 频率 / 必要的 `weekly_variant` / `auxiliary_inputs`），使 harness 无需读算法即可校验输入（字段级契约见 [SCHEME_CONTRACT.md](../../architecture/SCHEME_CONTRACT.md) §1）。

**1.3** 取数的时间截止**必须**是点时的 `feature_date`，由 `shared.calendar_service` 按 `predict_date` 与 run_mode 解析；**禁止**算法层自行做「当前日 −1 天」之类的临时位移。

---

## 2. ②消费：内存 df → 无损快照 → 注入 core

### 2.1 数据通路

当前合法路径是 `shared.input_artifacts.build_*_input_artifact(...) -> InputArtifact.dataframe`，并由公共层负责 canonical CSV、hash、coverage 和 metadata。现行实现会把公共层产出的 CSV 读回后交给算法；任何取消读回的改造必须先证明 dtype、NaN、排序和浮点往返完全等价，再同步修改 InputGate 与回归测试。

目标态候选路径如下：

```
artifact = input_artifacts.build_*(...)     # ① 唯一输入入口，内部调用 data_service
df_raw   = artifact.dataframe
df_norm  = normalize(df_raw)                # 唯一归一化（纯函数：dtype / 排序 / NaN）
save_csv(df_norm, check_data/{date}.csv)    # 落 canonical 无损快照
content_hash = sha256(file_bytes)           # 审计钉定
inject(df_norm) → core.train / core.predict # 直接内存注入，禁止读回
```

**2.2 `normalize` 必须是幂等纯函数**，在落盘前调用一次；core 消费的就是其输出。同一方案的 live 与 backtest **必须**用同一个 `normalize`。

**2.3 目标态禁止不必要的「写完再读回」**。但现行 `InputArtifact.dataframe` 仍以公共层读回结果为权威输入；在 SnapshotGate 和等价性迁移完成前，不得由单个方案自行删除读回。

**2.4 canonical 快照必须无损往返**：CSV 以足够精度保存（`float_format="%.17g"`，float64 可精确还原）+ 复现读取时显式指定 dtype。保证「从 check_data 复现的 df」≡「实盘内存 df」，从而读回是可证明的 no-op。

**2.5** 目标态可把方案级 `check_data/` 作为实盘输入快照视图。当前权威运行期位置仍是 `backtest_artifacts/runtime_inputs/{scheme_id}/`，路径由 `shared.artifact_paths` 管理；未完成仓库级迁移前不得另建第二套权威输入来源。

**2.6 core 零 DB、零文件 IO**：core 只吃传入的 `DataFrame`，**禁止**自己读写任何文件或连库（[CODE_ARCHITECTURE.md](../../architecture/CODE_ARCHITECTURE.md) core 纯净不变量）。

**2.7** harness **必须**提供一条快照无损断言（设计名 `SnapshotGate`）：`read_back(check_data_csv).equals(df_norm)`，只在测试/CI 跑，不进实盘热路径，用以守住 2.4 不被改坏。

---

## 3. ③保存：model_parameter = 配置（产物），模型确定性重训不落盘

### 3.1 model_parameter 的精确定义

**3.1.1** `model_parameter` **只存「选出来的配置」**（超参、阈值、选中因子、投票权重、回归系数等小体量数据），**禁止**作为权威态持久化「训练好的模型二进制」并在预测时加载它来出数。

**3.1.2** 「模型」**必须**由 `配置 + ②的 canonical 快照 + 固定 seed` **确定性重训**得到，临时存在、用完即弃、不落盘、不进 git。

> 该铁律消灭一整类未来函数 bug：因为没有持久化模型可供「glob 最新一个」错选，回测任意历史时点都不会加载到用未来数据训练的模型。

**3.1.3** `model_parameter/` 整目录**必须** gitignore。它是产物，回测前由 `train` 确定性重建。

### 3.2 两个纯钩子（全部扩展面）

完成目标态平台迁移的新范式 core 才要求实现两个纯函数（零 DB、零文件 IO、在 `(df, config, seed)` 给定下确定性）：

```
train(df_snapshot, cutoff_date, ctx)            -> 写出 config 产物（可为 no-op / 空 {}）
predict(df_snapshot, config, cutoff_date, ctx)  -> list[PredictionRecord]
```

**3.3 config 对框架完全不透明**：框架只对 config 做 hash / version / store，**禁止**解析其内部结构。config 的语义只有 core 知道。

**3.4 配置存取必须经公共层 param store**（设计 API：`save(cutoff, config, manifest)` / `resolve_config(feature_date, policy)`）。时点选择集中在公共层，**禁止**算法自己 `glob` 目录取「最新」。

**3.5 manifest 必须随每份 config 落盘**，记录：`input_content_hash`（来自 ②）、`seed`、`code_version`、`cutoff_date`、`produced_at`，使「哪份输入产出哪份配置」可证。

### 3.3 配置选择的时点策略（方案声明）

**3.6** 配置回测取用策略**必须**在 `config.yaml` 显式声明，框架两种都提供：

- **策略 A（生产忠实 / 默认）**：回测各历史日都用「当前这份 config」，只对**数据**做点时重训。等价于现生产「超参周期性重选、视为准平稳」的真实行为。
- **策略 B（严格点时）**：config 按 `cutoff_date` 版本化，`resolve_config(feature_date)` 取 `cutoff ≤ feature_date` 中最大者。更诚实但偏离生产、成本高。

**3.7** 默认 **A**。范式的职责是**忠实复制生产算法的点时口径**，**禁止**为「更正确」擅自加严而改变 benchmark 数值。是否切 B 由方案按自身生产语义声明。

### 3.4 三入口

方案**必须**提供三个 bash 入口（对应生命周期，命名含语义即可）：模型更新（`train`/`update`）、实盘预测（`predict`，`run_mode=live`）、历史回测（`run_mode=backtest`）。回测开跑时 `model_parameter` 不存在（gitignore），**必须**先 `train` 重建；同一次回测内已训练的 cutoff 盘上复用（run-local cache，不入库、不进 git）。

---

## 4. ④输出：core 返回富记录，框架负责一切持久化

### 4.1 边界（一句话）

> **`core.predict()` 只返回结构化的 `list[PredictionRecord]`（纯内存，不写文件、不碰库）；DB 写库与 output 留痕全部由框架做。**

**4.2 禁止算法层做以下任一**（均为生产已存在、必须消灭的反模式）：

1. ❌ 算法层 `cursor.execute` / `commit` 直接写库（违反写库单点）；
2. ❌ 算法层查 `api_wind_indicators_all`、读 factor_map 之类做 factor→code 命名（算法越界碰 DB / 命名）；
3. ❌ 算法层硬编码业务 schema：方向词（多/空/平）、`frequency="D10Y"`、目标表名。

### 4.3 PredictionRecord 携带内容

当前字段名必须以 `shared.models.PredictionRecord` 为准。尚未进入 dataclass 的富审计字段先放在 `extra`，不得由本文虚构新的必填列：

```
PredictionRecord:
  # 身份（scheme_id 填 base scheme id，框架/registry 解释）
  scheme_id, target_tenor, horizon
  predict_date, feature_date, target_date     # target_date 由框架按 calendar 推导并校验
  # 预测主体
  predicted_direction  # -1/0/1；"多/空/平" 措辞由框架映射
  confidence           # 可选
  model_version, scheme_version, prediction_phase
  # 留痕（model-agnostic，可空，当前放在 extra）
  extra.feature_attribution
  extra.diagnostics
  extra.*               # 方案必填键及 signal policy 审计字段，按 SCHEME_CONTRACT §3
```

**4.4 「选用因子」必须抽象成 model-agnostic 的 `feature_attribution`**，**禁止**在框架契约层绑定 SHAP。xgb 给 shap、回归给 |coef|、投票给权重、规则模型给空——框架只负责持久化，不解释其语义。

### 4.4 output/ 落盘约定

框架（**非算法**）把返回记录 dump 成留痕，目录对齐 check_data，整目录 gitignore（产物，可复现）：

```
schemes/{scheme_id}/output/{predict_date}/
  prediction.json          # 方向 + 概率（与入库同源的权威副本）
  feature_attribution.json # 选用因子 + 贡献
  shape.json               # 输入 / 特征 / 样本 / 窗口
  manifest.json            # provenance: input_content_hash(②) + config_hash(③) + seed + code_version
```

**4.5 provenance 链必须钉死**：`snapshot_hash → config_hash → output`。路线甲（确定性）下重跑逐位复现 output 与入库行。

### 4.6 写库单点 + run_mode 路由

```
core.predict() -> records ─► 框架 ─► run_mode=live     : scheduler.repository  → t_scheme_predictions
                                 └► run_mode=backtest : backtests.repository  → t_backtest_*
```

**4.7** 同一份 records，框架按 run_mode 路由到不同表，**算法永远不知道写哪张表**。upsert / 去重（按 base `scheme_id + target_tenor + horizon + target_date`）、方向词映射、tenor/task_type/frequency、factor→indicators_code 归一，**全部在框架 / registry / repository**。入库写 base `scheme_id`，前端业务身份由 registry composite `scheme_id` 表达。

---

## 5. 扩展性契约（模型无关性证明）

### 5.1 目标态入库 = 声明 4 项 + 实现 2 个纯钩子

**候选声明（config.yaml）**：① 复用现行 `input_spec`（取哪些列/频率/回看）② identity（base scheme / tenors / horizon / task_type）③ determinism（seed；配置选择策略 A/B）④ model_parameter（会写哪些配置文件名）。新增字段必须先进入 `SCHEME_CONTRACT` 与 config schema，不能只按本文直接写入。
**实现（core）**：`train` 与 `predict` 两个纯函数。

不碰框架一行。

### 5.2 四类异构算法落进同一契约（证明）

| 算法 | config 是什么 | train | predict |
|------|---------------|-------|---------|
| XGBoost | 超参 + 阈值 | 搜参 → 写超参 | 读超参 → 窗口上重训 xgb → 预测 |
| 投票 / 集成 | 成员配置 + 权重 | 选权重 / 成员 → 写 | 建各成员 → 投票 → 预测 |
| stepwise 回归 | 选中变量(+系数) | 逐步选元 → 写变量集 | 在变量集上拟合 / 套用 → 预测 |
| 纯因子规则 | 空 `{}` | no-op | df 直接算信号 → 方向 |

模型形态天差地别，但框架走 `train → store(config) → resolve(config) → predict → output → 写库`，中间是什么模型框架不关心——这就是 daily 范式可拓展性的落点。

---

## 6. 确定性契约（路线甲）

**6.1** `train` 与 `predict` **必须**在 `(df_snapshot, config, seed)` 给定下完全确定（固定 seed、固定数据口径）。否则每次重训 benchmark 漂移，回测不可复现。

**6.2** harness **必须**提供一条行为级确定性 gate（设计名 `DeterminismGate`）：同输入跑两次 → config hash 一致、`PredictionRecord` 一致。该 gate **不看模型实现**，对任何模型一视同仁。

**6.3** 回测复现 = 从 check_data 快照按同一 `normalize`/dtype 读入 → `train` 重建 config → `predict`。全链路 hash 可追（②→③→④）。

---

## 7. 目录结构基线

### 7.1 方案目录（就近原则 + live / backtest 分离）

```
schemes/{scheme_id}/
├── config.yaml              # 声明：input_spec / identity / determinism / model_parameter / 策略A|B（受 SCHEME_CONTRACT 约束）
├── predict.py               # adapter：暴露 train(cutoff) 与 predict/run(predict_date)，只委托给 core，零业务逻辑
├── core/                    # 纯算法：零 DB、零文件 IO、零跨方案 import；实现 train/predict
├── scripts/                 # 三入口 bash：模型更新 / 实盘预测 / 历史回测
├── benchmarks/              # 入库档(checked-in)：方案专属 canonical 输入档 / 对照真值（用于 ② / §6 对齐校验）
├── check_data/              # 产物(gitignore)：每日 live 归一化输入快照 {predict_date}.csv（②）
├── model_parameter/         # 产物(gitignore)：配置 + manifest（③）
├── output/                  # 产物(gitignore)：live 预测留痕 {predict_date}/ 方向+因子+shape+manifest（④的 live 分支）
├── backtest/                # 产物(gitignore)：回测专属，跟 live 完全分离
│   ├── inputs/{predict_date}.csv          # 回测输入快照（②的回测分支）
│   └── output/{run_id}/{predict_date}/... # 回测产物（④的 backtest 分支）
└── logs/                    # 产物(gitignore)：按日期分类的算法内部细节
```

> `check_data` / `model_parameter` / `output` / `backtest` / `logs` 五个目录**必须** gitignore——它们都是可由「快照 + 配置 + 固定 seed」确定性重建的产物。`benchmarks/` 是 checked-in 的入库档（方案 onboarding 时随 PR 进入仓库），不在 gitignore 之列。

### 7.2 为什么 live `output/` 与 `backtest/` 必须分开

- 范式 §4.6 的 run_mode 路由（live → `t_scheme_predictions` / backtest → `t_backtest_*`）在**目录层面**也要分流，避免 review 时混淆。
- live 是每日单点产物，backtest 一次跑可能跨数年、产物数量级远大于 live；混目录会让 `output/` 被某次回测的几千个 `predict_date` 撑爆，看不清当日 live 留痕。
- backtest 可能因为改 config 重跑多次，`{run_id}` 那层是必要的对比维度；live 没有这层。

### 7.3 仓库级目录收敛（与方案目录配套）

方案目录就近化之后，仓库根目录跟着收敛：

| 现状 | 范式落地后 | 说明 |
|------|------------|------|
| `benchmarks/`（顶层） | 删除（残留归档进 `docs/legacy_sources/`） | per-scheme `schemes/{scheme_id}/benchmarks/` 已是事实标准；顶层那份是早期跨方案大对比的历史档 |
| `backtest_artifacts/runtime_inputs/{scheme_id}/` | 并入 `schemes/{scheme_id}/backtest/inputs/` | 就近 + 与 live `check_data/` 对称 |
| `backtest_artifacts/backtests/{scheme_id}/` | 并入 `schemes/{scheme_id}/backtest/output/{run_id}/` | 就近 + 加 `{run_id}` 区分多次回测 |
| `backtest_artifacts/`（顶层） | 目标态评估是否删除 | 当前仍是 `shared.artifact_paths` 管理的权威运行期目录；只有全量迁移、兼容读取和清理策略完成后才能删除 |
| `backtests/_base_runner.py` / `repository.py` / `weekly_base_runner.py` | **保留**（写库单点的回测分支实现） | bash 入口最终汇到这里的 `repository.write(records)`，**全仓库只有一份回测写库代码**。可改名/搬位置（如挪进 `shared/`），但**禁止**消失或被 per-scheme bash 各自 `cursor.execute` 取代 |
| `backtests/{scheme_id}_reproduction.py` | 范式钉死后**变薄**（不归零） | ② normalize 与 ④ records 收敛后 glue 大幅减少；scheme-specific 配置仍需要落点，可下沉到 `schemes/{scheme_id}/backtest/runner.py` 或保持在顶层 `backtests/` 由公共 runner 派发 |
| `backend/` | **保留**（与算法生命周期完全正交） | FastAPI 服务 + 静态前端托管，算法写完 `t_scheme_predictions` 之后由 backend 读出来给前端 iframe 看；范式 §4 的边界止于写库，backend 不在范式管辖范围 |

> 该收敛是**目标态**。具体迁移属平台能力改造，须按 [sop/SCHEME_ONBOARDING_T0.md](../../sop/SCHEME_ONBOARDING_T0.md) 的"普通入库 vs 平台改造"边界单独评审，不与单个方案入库混合执行。

---

## 8. 目标态强约束清单（设计）

本表混合了已落地不变量与尚未实现的候选 gate；是否已生效以 §0.2 和现行 harness 为准，不得仅凭本表执行 fail-closed。

| # | 约束 | 守护点（设计） |
|:--:|------|----------------|
| C1 | 输入只经 `shared.input_artifacts`；其内部统一调用 `data_service`；adapter/runner 不自拼 DB | StaticGate |
| C2 | core 零 DB、零文件 IO | StaticGate（AST） |
| C3 | 框架/公共层/harness 不按具体模型类型分支 | StaticGate（目标态） |
| C4 | `normalize` 单一、幂等、live==backtest | SnapshotGate |
| C5 | canonical 快照无损往返；禁止读回 | SnapshotGate |
| C6 | `model_parameter` 只存配置、不存权威模型；整目录 gitignore | StaticGate + repo 检查 |
| C7 | config 存取经公共 param store；禁止算法 glob 取最新 | StaticGate |
| C8 | manifest 完整（input_hash/seed/code_version/cutoff/produced_at） | DryRunGate |
| C9 | `core.predict` 只返回 records，不写文件、不写库 | StaticGate + DryRunGate |
| C10 | 写库单点 + run_mode 路由；算法不知表名 | StaticGate |
| C11 | `feature_attribution` 不绑 SHAP（model-agnostic 形态） | 契约 review |
| C12 | train/predict 确定性（同输入两跑一致） | DeterminismGate |
| C13 | provenance 链 snapshot_hash→config_hash→output 完整 | DryRunGate |

---

## 9. 与现有文档的关系

- 本文是**生命周期与数据流的目标态提案**，不是新增方案前置强制读物。新增方案仍以 [sop/SCHEME_ONBOARDING_T0.md](../../sop/SCHEME_ONBOARDING_T0.md) 和现行契约为准。
- [SCHEME_CONTRACT.md](../../architecture/SCHEME_CONTRACT.md) 是 config / predict.py / core 的当前字段级机器契约；本文 §3 的 `train` 钩子与 §4 的富化字段只有在 SCHEME_CONTRACT、schema、gate 和迁移同步完成后才生效。
- [PREDICTION_SEMANTICS.md](../../architecture/PREDICTION_SEMANTICS.md) 定义本文反复引用的日期语义。
- [HARNESS_ARCHITECTURE.md](../../architecture/HARNESS_ARCHITECTURE.md) / [CODE_ARCHITECTURE.md](../../architecture/CODE_ARCHITECTURE.md) 定义本文依赖的分层与 gate 边界；§8 中的 SnapshotGate / DeterminismGate 为本文新增的设计性 gate，须按这两份文档的 harness 边界落地。

> 本文描述候选目标范式。具体 gate 实现、目录迁移与现有 `run(predict_date)` 单钩子契约的迁移，必须按平台能力改造流程单独设计、验证和授权；在此之前不得改变现有方案验收结论。
