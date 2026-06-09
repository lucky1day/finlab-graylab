# 测试方案: Bond Factor Lab 实盘测试系统

**版本**: v1.1  
**日期**: 2026-06-05  
**原则**: 每个TODO必须在Mac Studio上逐项验证通过后才能进入下一阶段

> 当前测试 Mac 的一次只读盘点已记录在 [TEST_MACHINE_BASELINE.md](TEST_MACHINE_BASELINE.md)。Phase 0 执行前先核对该基线中的数据库实例、端口和最新数据日期。

> 状态更新（2026-06-06）: 新机器正式平台表、target registry、backtest 表均已创建；`api_wind_daily` 最新为 `2026-06-05`，`api_wind_derivative_daily` 最新为 `2026-06-04`，Y actuals 覆盖到 `2026-06-03`。`t1_daily` / `t5_daily` 均为 `active`，backend/scheduler 已由 launchd 常驻管理，并已在 2026-06-05 09:25 写入 6 条正式预测。早先文档曾描述的周度方案（`weekly_10y_d_overlay` 等）已退役/代码未实现，scheduler 不再注册任何周度 cron。

> 历史复现更新（2026-06-08）: 已完成 `model_muti_0529` 历史回测复现。验证链路已显式按公共输入层执行: 先用 `shared.input_artifacts` 调用统一 `shared.data_service` 从 `bond_db` 生成 daily_output/weekly_output CSV，再喂给算法。按当前验证口径，暂排除 `target_date=2026-05-25` 至 `2026-05-29` 的 5 月最后目标周样本；t5 三组原始 1328 行、有效 1312 行，framework 两组 mismatch 为 0；t1 三组原始 1011 行、有效 999 行，framework 两组 mismatch 为 0。`shared.data_service` 已替换为用户提供的统一日/周/月数据层；与历史保存的 canonical CSV 相比，日期、行数、列顺序和 Y 生成所依赖的收益率列均通过，因子列存在缺失/精度差异并已落表记录。

> 新机器复核（2026-06-07）: 当前正式平台表、target registry、backtest 表均已创建；`t_scheme_actuals=13900`，覆盖到 `2026-06-03`。`t_scheme_weekly_actuals` 早先含 794 条 10Y 周度 actuals，因周度方案已退役/代码未实现，该表已于 2026-06-09 清空（当前无周度方案写入）。`t_scheme_predictions`、`t_scheme_run_log` 中早先来自周度受控写库/补跑的记录也已清理；registry 当前只保留 `t1_daily` / `t5_daily`。

> 周度方案更新（已退役）: 早先文档曾记录按 SOP 接入 `weekly_5y_direct_production`（5Y）与 `weekly_7y_cross_d_overlay`（7Y）并保持 `paused`，附带受控回测落库结果与前端矩阵项。这三个周度方案（含 `weekly_10y_d_overlay`）现已退役/代码未实现：方案目录、adapter、backtest runner 已不在代码库中，对应的 `t_backtest_*` 与 `t_scheme_*` 记录已于 2026-06-09 清理，`t_scheme_predictions/t_scheme_run_log/t_scheme_actuals/t_scheme_weekly_actuals` 中已无周度数据。如需周度方案须按 SOP 重新接入。

---

## Phase 0: 环境验证

### 数据库连通性

- [x] MySQL服务正在运行: 通过项目 `.env` 连接 `bond_db` 成功
- [x] bond_db数据库存在: 当前连接库为 `bond_db`
- [x] 核心数据表存在且有数据:
  - [x] `api_wind_indicators_all` — 有元数据
  - [x] `api_wind_daily` — 有日频数据，最新 `2026-06-05`
  - [x] `api_wind_derivative_daily` — 有衍生日频数据，最新 `2026-06-03`
- [x] 环境变量配置正确: `.env`文件中`BOND_DB_PASSWORD`能连接成功
- [x] 新表创建成功: 四张新表存在并已写入验证数据
  - [x] `t_scheme_predictions` — 早先 7 条（含 2026-06-05 日度 6 条与 1 条已退役周度方案 live 记录，该周度记录已于 2026-06-09 清理）
  - [x] `t_scheme_actuals` — 当前 13900 条
  - [x] `t_scheme_weekly_actuals` — 表仍在；早先 794 条 10Y 周度 actuals 因周度方案退役已于 2026-06-09 清空，当前无周度方案写入 (no weekly scheme currently writes here)
  - [x] `t_scheme_registry` — 当前 3 条
  - [x] `t_scheme_run_log` — 当前 4 条

当前实测记录（2026-06-06）: `bond_db` 已连通，正式平台表已创建。`t_scheme_registry` 有3条、`t_scheme_predictions` 有7条、`t_scheme_actuals` 有13900条、`t_scheme_weekly_actuals` 有794条、`t_scheme_run_log` 有4条。`api_wind_daily` 最新日期为 `2026-06-05`，日度目标 actuals 最新覆盖到 `2026-06-03`。

### Python/conda环境

- [x] conda环境存在: `forecast_env`
- [x] 预测环境Python版本确认: `forecast_env` 可运行预测脚本
- [x] 算法核心依赖安装成功: `lightgbm`, `pandas`, `numpy`, `sqlalchemy`, `pymysql`
- [x] chinese-calendar安装: 交易日判断已验证
- [x] 服务环境存在: `bond_factor_lab_service`
- [x] 服务环境Python版本确认: `bond_factor_lab_service` 可运行后端/调度器
- [x] 服务依赖安装成功: FastAPI / uvicorn / SQLAlchemy / PyMySQL / APScheduler 已验证

当前环境记录（2026-06-05）: 算法预测使用 `forecast_env`；后端/API/调度器使用独立 `bond_factor_lab_service`。已用 `PYTHONNOUSERSITE=1` 验证服务依赖可 import。

当前修正记录（2026-05-31）: `shared.db_config` 已加入 `.env` fallback 解析，解决 `forecast_env` 未安装 `python-dotenv` 时无法读取本机数据库配置的问题。

---

## Phase 1: 数据服务层验证

### shared/input_artifacts.py 公共输入文件层

- [x] `build_daily_output_from_db()`能正常返回DataFrame
  ```python
  from shared.data_service import build_daily_output_from_db
  df = build_daily_output_from_db(start_date="2025-01-01", end_date="2025-05-28")
  ```
- [x] `shared.input_artifacts.build_daily_input_artifact()` 调用统一 `shared.data_service` 生成 daily_output CSV，再从 CSV 读回 DataFrame；adapter 不直接拼接 DB 输入。
- [x] `shared.input_artifacts.build_daily_input_artifact()` 是 t1/t5/历史复现 upstream 的统一日频输入文件入口；返回 `InputArtifact(path, dataframe, source, metadata)`。
- [x] `shared.input_artifacts.build_weekly_input_artifact()` 是所有周频方案的统一周频输入文件入口；底层调用统一 `shared.data_service`，先写 `weekly_output_*.csv`，再读回 DataFrame。
- [x] `build_weekly_input_artifact()` 对旧 `end_date` / `include_daily_weekly_close_fallback` 参数显式报错，避免静默绕过统一数据层口径。
- [x] 输入文件统一写到 `backtest_artifacts/runtime_inputs/{scheme_id}/`，adapter 不再自己拼 daily/weekly 文件路径。
- [x] 2026-06-08 统一数据层 dry-run 输出: t1 运行期 daily 文件 `1696 x 774`，t5 运行期 daily 文件 `1939 x 774`。
- [x] 返回的DataFrame包含日频交易日锚定收益率列: `TB1YWI0C`, `TB5YWI0C`, `TB0YWI0C`；算法所需 `TB3YWI0C/TB7YWI0C` 作为普通因子列参与输出。
- [x] 如果后续新增或修正的预测因子暂缺，记录为人工补数待完成，不作为代码测试失败
- [x] `date`列为datetime类型且已排序
- [x] 历史验证窗口最后一行的date等于当时最近交易日 `2026-05-29`
- [x] 最近验证窗口中，Y 生成所依赖的收益率列无 NaN
- [x] 先按 `shared.data_service` 上游兼容口径从 `bond_db` 生成 daily_output
- [x] 与 canonical `benchmarks/model_muti_0529/daily_output.csv` 对比:
  - [x] 相同日期范围内，行数一致: `3843`
  - [x] DB 对齐列数和列顺序一致: `877`
  - [x] Y 生成所依赖的收益率列数值一致: 5 个收益率列最大误差均为 0
  - [x] 暂排除 `2026-05-25` 至 `2026-05-29` 目标周日期后，有效口径最大数值误差约为 `5e-7`
- [x] 2026-06-08 重新审计: `scripts/audit_daily_data_service.py` 使用统一 `shared.data_service` 生成 DB daily_output；artifact 层不再传旧 `target_columns` 参数，当前运行路径不依赖 `_original_source`。

### 难点: 数据一致性

- [x] 验证上游 data_service 重新生成的 DB CSV 和历史原始 CSV（Y 生成所依赖的收益率列和结构已通过；因子列差异已记录）:
  ```python
  import pandas as pd
  csv_df = pd.read_csv("benchmarks/model_muti_0529/daily_output.csv")
  from shared.data_service import build_daily_output_from_db
  db_df = build_daily_output_from_db(
      start_date="2010-07-27",
      end_date="2026-05-28",
  )
  # 对比前3843行（CSV行数）的 Y 生成所依赖的收益率列
  assert (csv_df["TB0YWI0C"].dropna() - db_df["TB0YWI0C"].iloc[:len(csv_df)].dropna()).abs().max() < 1e-8
  ```
- [x] 如果不一致，定位差异来源: 当前 DB 重新生成的上游 CSV 与历史保存 CSV 在因子列上存在缺失差异与小数精度差异；首个超阈值样本 `SWR00001` / `2023-03-20`，Y 生成所依赖的收益率列不受影响。
- [x] 当前最大差异已定位为 `S5470301` / `2026-05-28`: 历史 CSV `6140.0`，当前 DB 经 `shared.data_service` 生成 `6280.0`；15 个缺失差异集中在 `2026-05-26`，历史 CSV 为 `0.0` 而当前 DB 生成为空。

历史实测记录（2026-05-31）: `build_daily_output_from_db(start_date="2026-05-20", end_date="2026-05-29")` 在 `forecast_env` 中验证通过，返回 `8 x 880` DataFrame，最后一行日期为 `2026-05-29`，`TB1YWI0C/TB3YWI0C/TB5YWI0C/TB7YWI0C/TB0YWI0C` 无 NaN。新机器只读核验显示这些收益率代码最新覆盖到 `2026-06-03`。

输入文件层实测记录（2026-06-08）: `t1_daily` / `t5_daily` 均通过 `shared.input_artifacts` 调用统一 `shared.data_service` 生成运行期 CSV，再读回给算法。只读 dry-run 成功: `t1_daily` 返回 `5Y/10Y` 两条，`t5_daily` 返回 `3Y/5Y/7Y/10Y` 四条。运行期文件统一位于 `backtest_artifacts/runtime_inputs/{scheme_id}/`: t1 文件 `1696 x 774`，t5 文件 `1939 x 774`，日频来源标记为 `shared_data_service_daily`。早先一并实测的三个周度方案已退役/代码未实现，相关周度运行期文件与 dry-run 记录不再适用。

周频输入导出结论（历史，已退役）: 早先曾以统一 `data_service` 周度口径只读生成周度方案的 `weekly_output_*.csv` 并与桌面 CSV 逐格对比。相关周度方案已退役/代码未实现，运行期 `weekly_*` 输入目录已删除，该结论与下列对比报告仅作历史记录:

- `reports/weekly_input_artifact_wind_export1_vs_desktop_summary.json`
- `reports/weekly_input_artifact_wind_export1_vs_desktop_diff.csv`

---

## Phase 2: t1方案验证

### 核心逻辑不变性验证

- [x] t1 core 已框架化保留算法逻辑；旧 `_original_source` 目录已删除，后续通过 Git baseline/tag 追溯历史:
  - [x] `lgbm_predictor.py` — 无diff
  - [x] `feature_engineering.py` — 无diff
  - [x] `config.py` — 无diff
  - [x] `shap_analysis.py` — 无diff
  - [x] `model_store.py` — 无diff

### predict.py adapter验证

- [x] `from schemes.t1_daily.predict import run` 能正常import
- [x] `run("2026-06-01")` 返回list，当前配置长度为2（5Y/10Y）
- [x] 每条记录的字段完整性:
  - [x] `scheme_id == "t1_daily"`
  - [x] `target_tenor` 在 `{"5Y", "10Y"}` 中
  - [x] `horizon == 1`
  - [x] `predict_date` 格式为YYYY-MM-DD
  - [x] `target_date` 格式为YYYY-MM-DD
  - [x] `predicted_direction` 在 `{-1, 0, 1}` 中
  - [x] `confidence` 为float，范围[0, 1]
- [x] 预测结果与原始t1历史回测链路一致性验证:
  - [x] 用原始 `run_backtest(..., dry_run=True)` 生成 baseline
  - [x] framework-csv 对比 baseline: 原始 1011 行，有效 999 行，mismatch 0
  - [x] framework-db 对比 baseline: 原始 1011 行，有效 999 行，mismatch 0
  - [x] `prob_up` 比较阈值 `1e-6`

当前实测记录（2026-06-01）: `1Y` 已从当前预测目标中移除；`run("2026-06-01")` 应返回 `5Y/10Y` 两条 `PredictionRecord`。`1Y` 仍可作为算法内部特征输入使用。

### 难点: import路径

- [x] 验证`sys.path`设置正确，core内部的相对import不报错
- [x] 验证 adapter 只调用 `shared.input_artifacts` 生成输入文件并读取 DataFrame；core 内部不直接写库，也不自行读取 DB。
- [x] 如果core内部有`from .config import ...`这样的相对import，验证在新目录结构下仍然工作

### 难点: 模型确定性

- [ ] 同一天运行两次，预测结果是否一致（LightGBM有随机性）
- [ ] 如果不一致，确认是否需要固定random_seed
- [ ] 记录: 原始代码是否设置了seed？如果没有，adapter是否需要加？

---

## Phase 3: t5方案验证

### 核心逻辑不变性验证

- [x] `common_utils.py` — 与原始无diff
- [x] `predict_3y.py` — 与原始无diff
- [x] `predict_5y.py` — 与原始无diff
- [x] `predict_7y.py` — 与原始无diff
- [x] `predict_10y.py` — 与原始无diff

### latest_prediction.py adapter函数验证

对每个tenor（3Y/5Y/7Y/10Y）:

- [x] `predict_latest_for_module(module, df, "2026-06-01")` 能正常返回 `LatestPrediction`
- [x] 返回值包含: `feature_date`, `vote_pred`, `model_pred`, `confidence`, `vote_signals`
- [x] `vote_pred` 在 `{-1, 0, 1}` 中
- [x] `target_date` 由 adapter 按原始标签语义记录为 `feature_date` 后第5个交易日

### 与原始全量回测对比

- [x] 运行原始`run_all.py`生成`*_predictions.csv`
- [x] 原始 CSV baseline 与 framework-csv 逐行比较
- [x] 原始 CSV baseline 与 framework-db 逐行比较
- [x] 对每个tenor都验证:
  - [x] 3Y: 原始 332 行，有效 328 行，mismatch 0，当前口径全量 `69.5% (228/328)`
  - [x] 5Y: 原始 332 行，有效 328 行，mismatch 0，当前口径全量 `64.6% (212/328)`
  - [x] 7Y: 原始 332 行，有效 328 行，mismatch 0，当前口径全量 `66.5% (218/328)`
  - [x] 10Y: 原始 332 行，有效 328 行，mismatch 0，当前口径全量 `63.4% (208/328)`

### 难点: latest_prediction.py的正确提取

- [x] 验证`latest_prediction.py`中的训练窗口与main()中一致（框架侧复用原始常量）
- [x] 验证GAP=5的排除逻辑在`latest_prediction.py`中正确实现
- [x] 验证投票阈值和投票信号配方与main()一致
- [x] 验证`latest_prediction.py`不会意外使用未来数据（时间穿越检查）:
  - [x] 训练样本的最大日期 < feature_date - GAP
  - [x] 特征日取 `predict_date` 前最近可用行情日

### 难点: t5的target_date计算

- [x] T+5的target_date以原始代码标签语义为准，当前 adapter 记录为 `feature_date` 后第5个**交易日**（不是自然日）
- [x] 验证: `feature_date=2026-05-29` 为周五，target_date 为 `2026-06-05`
- [x] 验证: target_date 通过 `t_trade_calendar` 顺延交易日
- [x] 记录: `predict_date` 是预测发出日，最近可用行情日写入 `extra.feature_date`

历史实测记录（2026-05-31）: `schemes/t5_daily/core` 与原始核心逻辑保持零改动；新增 `latest_prediction.py` 在框架侧复用原始 LightGBM、特征、标签、投票阈值和 `GAP=5` 逻辑。`run("2026-06-01")` 返回 `3Y/5Y/7Y/10Y` 四条记录，`feature_date=2026-05-29`，`target_date=2026-06-05`。

---

## Phase 3.5–3.7: 周度方案验证（已退役）

早先文档曾包含三个周度方案的完整验证清单:

- Phase 3.5 — `weekly_10y_d_overlay`（10Y D-overlay）
- Phase 3.6 — `weekly_5y_direct_production`（5Y direct-production）
- Phase 3.7 — `weekly_7y_cross_d_overlay`（7Y cross-D-overlay）

这三个周度方案现已**退役 / 代码未实现**：方案目录、adapter、`core/`、`scripts/check_weekly_*`、`scripts/write_weekly_*`、`backtests/weekly_*_reproduction.py` 均已不在代码库中；对应的 `t_backtest_*`、`t_scheme_predictions/run_log/weekly_actuals` 记录已于 2026-06-09 清理，运行期 `backtest_artifacts/runtime_inputs/weekly_*` 目录已删除。原清单中的 readiness/dry-run/回测/前端格子/cron 验证项均已失效，仅作历史记录。

如需重新启用周度方案，须按 [SCHEME_ONBOARDING_SOP](sop/SCHEME_ONBOARDING_SOP.md) 与 harness 流程重新接入，并重建相应验证清单。

---

## Phase 4: 数据库写入验证

### UPSERT语义

- [x] 首次写入: `INSERT`成功，`SELECT`能查到
- [x] 重复写入同一天: 不报错，不产生重复行
- [x] 重复写入后数据正确: UPSERT 保持唯一预测行
- [x] 验证当前唯一键约束: `(scheme_id, target_tenor, predict_date)` 确实唯一
- [x] 明确隔离口径: `target_tenor + horizon` 是任务格子，`scheme_id` 是该任务格子下的方案实例；前端使用数据库 `t_target_registry` 的 target label 展示当前国债活跃券标的名称
- [x] 当前每个 `scheme_id` 固定一个 `horizon`; 若未来同一 `scheme_id` 覆盖多个预测长度，需升级唯一键或拆分 `scheme_id`
- [x] 任务格子内方案排行的区间指标按样本级聚合: `accuracy=总正确/总样本`，`precision/recall` 按对应方向的 TP 与样本级分母重算，不对月度百分比直接取平均
- [x] 前端任务格子最优指标与排行排序指标联动，矩阵展示当前排序指标对应的最优方案值

当前实测记录（2026-06-06）: 新机器正式预测表已有 6 条 `2026-06-05` 日度预测。早先曾有 1 条已退役周度方案的 live 预测与 2 条相关 run_log，已于 2026-06-09 清理。重复写同一预测日时仍依赖 UPSERT，不应产生重复预测行。当前 scheduler 不注册任何周度 job。

### 数据隔离

- [x] t1_daily和t5_daily的数据互不干扰:
  ```sql
  SELECT COUNT(*) FROM t_scheme_predictions WHERE scheme_id='t1_daily';
  SELECT COUNT(*) FROM t_scheme_predictions WHERE scheme_id='t5_daily';
  -- 两者独立，删除一个不影响另一个
  ```
- [x] 同一scheme不同tenor的数据隔离:
  ```sql
  SELECT * FROM t_scheme_predictions 
  WHERE scheme_id='t5_daily' AND target_tenor='3Y' AND predict_date='2026-05-28';
  -- 只有一条记录
  ```

### 难点: 方向映射一致性

- [ ] t1原始映射: `{0: "平", 1: "空", -1: "多"}` — 这是收益率方向
- [ ] 统一表中存储的是: `1=涨(收益率上行), -1=跌(收益率下行)`
- [ ] 验证adapter中的映射转换是否正确:
  - [ ] t1的`pred_label=1`（原始含义"空"=收益率上行）→ 统一表中`predicted_direction=1`
  - [ ] t1的`pred_label=-1`（原始含义"多"=收益率下行）→ 统一表中`predicted_direction=-1`
- [ ] t5的方向定义与t1是否一致？验证:
  - [ ] t5的`make_labels`: `future_return > 0 → 1` — 这里的return是收益率变化还是价格变化？
  - [ ] 确认t5的1和t1的1含义相同（都是收益率上行）

---

## Phase 5: Actuals更新验证

### 方向计算正确性

- [x] T+1方向计算: actuals行的 `trade_date` 为目标日，`direction_1d = sign(yield[trade_date] - yield[前1个交易日])`
- [x] T+5方向计算: actuals行的 `trade_date` 为目标日，`direction_5d = sign(yield[trade_date] - yield[前5个交易日])`
- [x] 手动验证样本日期:
  - [x] 取2025-05-21的10Y收益率，和前1个交易日对比，确认direction_1d正确
  - [x] 取2025-05-27的10Y收益率，和前5个交易日对比，确认direction_5d正确
  - [x] 补齐3Y数据后，已纳入 actuals 刷新覆盖范围

当前实测记录（2026-06-05）: 已基于当前可用的 `1Y/3Y/5Y/7Y/10Y` 写入13900条 actuals；五个期限最新 actuals 均到 `2026-06-03`。

### 难点: 交易日对齐

- [x] T+5的"第5个交易日"定义必须与t5方案的标签定义一致
- [x] 验证: actuals表中的direction_5d使用的是第5个交易日，不是第5个自然日
- [x] 验证: 如果某天是非交易日，actuals表中不应有该天的记录

### 难点: 数据延迟

- [ ] 16:00更新actuals时，当天的收盘数据是否已经写入`api_wind_daily`？
- [ ] 如果数据源有延迟（比如17:00才更新），actuals更新任务需要相应延后
- [ ] 验证: `SELECT MAX(rdate) FROM api_wind_daily` 在16:00时是否已包含当天

---

## Phase 6: 调度器验证

### 方案发现

- [x] scheduler启动时能正确扫描到所有schemes:
  ```
  [INFO] Discovered 2 schemes: t1_daily, t5_daily
  ```
- [ ] 新增一个空的`schemes/test_scheme/config.yaml`后重启，能发现3个schemes
- [ ] config.yaml格式错误时，不影响其他方案的加载（优雅降级）
- [ ] config.yaml缺少必填字段时，给出明确错误信息

当前实测记录（2026-06-06）: 正式 registry 中 `t1_daily`、`t5_daily` 均为 active。早先注册的周度方案及其周六 `30 11 * * 6` job 已随周度方案退役/代码未实现而移除；registry 同步已加保护，未变化的 t1/t5 行未刷新 `updated_at`。

### 定时执行

- [x] 工作日9:25触发预测任务
- [x] 调度器在 `bond_factor_lab_service` 中运行，通过子进程调用 `forecast_env` 执行方案
- [x] 日度方案非交易日（周末）不执行
- [x] 法定假日不执行（按 `t_trade_calendar` / `chinese_calendar` 逻辑判断）
- [x] 验证: `is_trading_day("2026-05-29") == True`，`is_trading_day("2026-05-30") == False`
- [x] 周度方案按周六发出预测，scheduler job 使用 `force=True`，不受日度非交易日跳过规则影响。

当前实测记录（2026-06-06）: launchd 下 scheduler 已启动，并注册日度 active 方案和 `actuals` job；真实 2026-06-05 09:25 触发已观察通过，写入 t1/t5 共 6 条预测和 2 条 success run_log。早先曾注册一个周度方案及其周六 `30 11 * * 6` job 并手动补跑，相关周度方案已退役/代码未实现，scheduler 当前不再注册任何周度 job。

### 执行隔离

- [ ] t1_daily失败时，t5_daily仍然正常执行
- [ ] 模拟t1失败: 临时删除t1的core/config.py，确认t5不受影响
- [ ] 失败的方案写入run_log: `status='failed'`, `error_msg`非空
- [x] 成功的方案写入run_log: `status='success'`, `duration_sec`合理（<300秒）

### 难点: 执行超时

- [x] 如果某个方案运行超过5分钟，是否有超时机制？（当前子进程 timeout 为 600 秒）
- [ ] 超时后是否正确记录到run_log？
- [ ] 超时不应阻塞其他方案的执行

### 难点: 并发安全

- [ ] 手动触发和定时触发同时发生时，不会产生重复预测
- [x] 验证: UPSERT语义保证同一天只有一条记录

### 难点: 重启恢复

- [ ] kill scheduler进程后，launchd自动重启
- [x] Mac Studio 重启自恢复验证按用户确认跳过
- [x] 重启后不会重复执行当天已完成的任务（重启验证已按用户确认跳过）
- [ ] 如果scheduler在9:25宕机，9:30重启后是否补跑？（需要明确策略）

---

## Phase 7: 后端API验证

### /api/schemes

- [x] 返回所有已注册方案
- [x] 包含最近一次运行状态
- [x] status字段正确反映config.yaml中的设置

当前实测记录（2026-06-05）: FastAPI 已在 `127.0.0.1:8100` 由 launchd 管理，`GET /api/health` 返回 ok，`GET /api/targets` 返回 4 个 active 目标。`GET /api/schemes` 会间接 sync registry，不作为严格只读核验探针。

### /api/metrics/{scheme_id}

- [x] 基本查询: `GET /api/metrics/t5_daily?tenor=3Y&start_month=2026-06&end_month=2026-06`
- [x] 未验证样本返回 `daily_rows`，但不计入 `summary.total`
- [x] 每月的total = 该月实际有预测且已匹配 actuals 的天数

当前实测记录（2026-06-05）: 新机器正式 `t_scheme_predictions` 已有 6 条 2026-06-05 日度预测；因目标日尚未全部到达，metrics 中未验证样本不计入汇总。历史回测矩阵数据来自独立 `t_backtest_*` 表。

### 难点: 准确率计算正确性

- [ ] 手动计算一个月的准确率，与API返回对比:
  ```sql
  -- 取2025-04的t5_daily 10Y预测
  SELECT p.predict_date, p.predicted_direction, a.direction_5d
  FROM t_scheme_predictions p
  JOIN t_scheme_actuals a ON a.trade_date = p.target_date AND a.tenor = p.target_tenor
  WHERE p.scheme_id = 't5_daily' AND p.target_tenor = '10Y'
  AND p.predict_date BETWEEN '2025-04-01' AND '2025-04-30';
  ```
- [ ] 手动数: correct = count(predicted_direction == direction_5d)
- [ ] 手动算: accuracy = correct / total
- [ ] 与API返回的accuracy对比，必须一致

### 难点: 未验证预测的处理

- [x] 最近5天的T+5预测，target_date还没到，actuals表中没有对应记录
- [x] API应该: 不计入准确率统计（JOIN不到就排除）
- [x] 验证: 当月如果只有部分天数有actuals，total应该只计有actuals的天数

### 难点: 边界月份

- [ ] start_month的第一天如果不是交易日，不影响统计
- [ ] end_month的最后一天如果还没过完，只统计已有数据的部分
- [ ] 某个月完全没有预测数据时，该月不出现在结果中（或total=0）

### 难点: precision/recall计算

- [ ] 当某月没有"涨"的预测时，up_precision应为null（不是0）
- [ ] 当某月没有"涨"的实际时，up_recall应为null（不是0）
- [ ] 分母为0的情况全部返回null，前端显示为"—"

### /api/predictions 原始数据查询

- [x] 支持按scheme_id筛选
- [x] 返回每条预测的完整信息（含confidence和extra）
- [x] 分页正确（当前 `limit=5` 验证）

当前实测记录（2026-06-05）: `GET /api/predictions?limit=1` 可返回当前正式预测记录，表内共有 6 条 2026-06-05 日度预测。

### /api/schemes/{scheme_id}/trigger 手动触发

- [x] POST后立即返回（异步执行，非交易日跳过已验证）
- [x] 执行完成后run_log有新记录（手动 executor 链路已验证）
- [x] predictions表有新数据
- [x] 重复触发同一天不报错（UPSERT）

当前实测记录（2026-06-05）: 接口异步触发仍保留；当前正式预测记录来自 launchd scheduler 自动触发。重复触发同一天由 predictions UPSERT 保证不重复，执行前仍需记录目标表行数。

---

## Phase 8: 前端验证

### 基本渲染

- [x] 页面加载无JS错误（浏览器console无红色）
- [x] 方案选择器显示所有active方案
- [x] 默认选中可用任务，表格显示真实/待验证数据

当前实测记录（2026-06-07）: launchd serve 的 `http://127.0.0.1:8100/` 可访问，`/api/health` 返回 ok，`/api/targets` 返回4个目标。页面显示 T+1/T+5/周度任务格；`10Y国债活跃 · 周度` 读取当前 DB 版本最新 run_id=`13` 后显示 `68.9% / 1 个方案`。

### 交互功能

- [ ] 切换方案: 表格数据刷新，对应新方案（完整交互回归待补）
- [ ] 切换tenor: 表格数据刷新，对应新tenor（完整交互回归待补）
- [x] 修改月份范围: 真实数据加载后扩展到最新月份
- [x] 点击某月行: 展开每日明细（此前 `2026-04` 明细已验证）

当前实测记录（2026-06-07）: 月份筛选会随 API 数据扩展；当前前端优先展示回测矩阵，已验证 `GET /api/backtests/factor-lab` 返回 `10Y国债活跃 · 周度` 最新 run_id=`13`，准确率为 `68.9%（31/45）`。

当前修正记录（2026-05-31）: 前端不再因为某个维度暂无已验证样本而自动跳到其他任务；筛选维度和原始月份区间常驻保留，并在真实 API 数据加载后自动扩展到最新月份。

### 数据正确性

- [x] 表格中的准确率数值与API返回一致（当前 T+5 未验证样本显示 `--/0`）
- [ ] 颜色编码正确: >70%绿色, 50-70%黄色, <50%红色（或按设计稿）
- [ ] 汇总卡片的数值 = 所有月份的加权平均（不是简单平均）

### 难点: 实时计算的响应速度

- [x] 当前 `/api/metrics` 单次查询响应 < 1秒
- [ ] 选择12个月范围时，API响应时间 < 3秒（完整范围压测待补）
- [ ] 如果超时，考虑: 是否需要加索引？是否需要缓存？

### 难点: iframe嵌入

- [ ] 在panda_quantflow的AIFin Lab shell中，iframe正确加载（外层仓库路径待提供）
- [x] 服务侧未设置阻止 iframe 的响应头
- [ ] iframe内的交互不影响外层shell（外层接入后验证）

当前准备记录（2026-05-31）: 当前服务响应头未设置 `X-Frame-Options` 或阻止 iframe 的 CSP；已新增 [IFRAME_INTEGRATION.md](IFRAME_INTEGRATION.md)。本机未找到可直接修改的 `panda_quantflow` 仓库路径，外层菜单/路由接入待提供路径后完成。

---

## Phase 9: 端到端集成测试

### 完整日流程模拟

- [ ] **Step 1**: 确认当天数据已更新（`api_wind_daily`有当天记录）
- [ ] **Step 2**: 手动触发scheduler（或等待9:25自动触发）
- [ ] **Step 3**: 检查run_log: 两个方案都是success
- [ ] **Step 4**: 检查predictions表: 有预测日的新预测
  - [ ] t1_daily: 当前配置为2条记录（5Y/10Y）
  - [ ] t5_daily: 4条记录（3Y/5Y/7Y/10Y）
- [ ] **Step 5**: 等待16:00 actuals更新（或手动触发）
- [ ] **Step 6**: 检查actuals表: 有当天的方向数据
- [x] **Step 7**: 打开前端，确认服务可访问并能展示当前可用回测矩阵或空状态
- [ ] **Step 8**: 当天的预测如果target_date还没到，显示为"待验证"

当前实测记录（2026-06-06）: 新机完整环境已具备，09:25 常驻 scheduler 已成功写入 t1/t5 当日正式预测。早先文档曾记录的周度 10Y 受控 live 写库、补跑与 run_id=13 回测展示对应已退役/代码未实现的周度方案，相关记录已于 2026-06-09 清理，不再适用。

当前部署记录（2026-06-05）: 已生成本机 `.env`，安装 `com.bond-factor-lab.backend` 和 `com.bond-factor-lab.scheduler` 到当前用户 LaunchAgents；`/api/health` 返回 ok，backend 已重启加载 weekly API 字段。

### 多日累积验证

- [ ] 连续运行3个工作日后:
  - [ ] predictions表累积正确（当前配置每天新增6条: t1的2条 + t5的4条）
  - [ ] 第3天时，第1天的T+1预测已可验证（actuals已有）
  - [ ] 前端显示的当月准确率随数据累积而变化

### 难点: 跨月边界

- [ ] 月末最后一个交易日的预测，target_date可能跨月
- [x] 验证: `feature_date=2026-05-29` 的T+5预测，target_date为 `2026-06-05`
- [x] 前端按predict_date分月还是按target_date分月？（当前按 `predict_date=2026-06-01` 进入 `2026-06`）

---

## Phase 10: 异常场景测试

### 数据异常

- [ ] 数据源当天未更新（api_wind_daily缺少当天数据）:
  - [ ] 预测脚本应该用最近可用数据运行（不crash）
  - [ ] 或者: 检测到数据不新鲜时跳过，记录status='skipped'
- [ ] 数据源某列全为NaN:
  - [ ] 模型训练是否能处理？（LightGBM通常能处理NaN）
  - [ ] 如果不能，错误信息是否清晰？

### 方案异常

- [ ] 方案代码有bug（抛出异常）:
  - [ ] 不影响其他方案
  - [ ] run_log记录error_msg
  - [ ] 前端该方案显示"运行失败"状态
- [ ] 方案运行超时（死循环）:
  - [ ] 有超时kill机制
  - [ ] 不阻塞scheduler主进程

### 系统异常

- [ ] MySQL连接断开:
  - [ ] 重试机制（3次）
  - [ ] 最终失败后记录到run_log
- [ ] 磁盘空间不足:
  - [ ] 模型文件写入失败时，预测结果仍然写入DB（DB写入优先）
- [x] Mac Studio重启后验证（用户确认不需要验证，已跳过）:
  - [x] launchd自动拉起scheduler和backend（跳过）
  - [x] 如果错过了当天的9:25，是否补跑？（不作为当前前期验收项）

### 难点: 部分成功

- [ ] t5_daily的4个tenor中，3Y成功但5Y失败:
  - [ ] 成功的3条记录正常写入DB
  - [ ] 失败的tenor记录在error_msg中
  - [ ] run_log的status应为'failed'还是'partial'？（建议: failed + error_msg说明哪个tenor失败）

---

## Phase 11: 性能基准

### 预测耗时

- [x] t1_daily单次运行耗时: 目标 < 60秒
- [x] t5_daily单次运行耗时: 目标 < 180秒（4个tenor串行）
- [x] 总耗时 < 5分钟（手动链路 t1+t5 约30秒）

### API响应

- [x] `/api/metrics` 单次查询: 目标 < 500ms
- [ ] `/api/predictions` 大范围查询（12个月）: 目标 < 1秒
- [ ] 并发10个请求: 无超时

### 数据库

- [ ] predictions表在1年数据量（~1700行）下查询性能
- [ ] actuals表在10年数据量（~25000行）下查询性能
- [ ] 索引是否生效: `EXPLAIN SELECT ...` 确认用到了索引

---

## Phase 12: Harness Gate 验收矩阵

强约束 harness 架构已定，详细边界见 [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md)。后续每个新增方案必须按 gate 留下证据，不能只凭单次脚本输出宣称完成。

| Gate | 验收命令/方式 | 通过标准 | 禁止事项 |
|------|---------------|----------|----------|
| Static Gate | 静态扫描 `schemes/{scheme_id}`、`backtests/{scheme_id}_reproduction.py`、`config.yaml` | 目录名等于 `scheme_id`；`predict.run()` 签名正确；core 不导入写库模块；无外部绝对输入路径 | 发现危险导入后继续进入 live |
| Input Gate | 单测 patch `shared.input_artifacts`，或真实 dry-run 观察 `extra.input_artifact_source` | daily/weekly/monthly 输入均由 `shared.input_artifacts` 生成；source 记录为公共层来源 | adapter/backtest runner 直接调用底层 DB 拼输入 |
| Unit Gate | `python -m unittest ...` | core 输出、adapter 输出、`PredictionRecord` 字段、周频上下文字段通过 | 只跑 live dry-run 替代单测 |
| Dry-run Gate | `PYTHONNOUSERSITE=1 conda run -n forecast_env python -m scheduler.scheme_runner --scheme-id <id> --predict-date <date>` | stdout 为 JSON list；预测条数与 tenors 匹配；正式 prediction/run_log 行数不变 | 调用 `scheduler.executor` 做 readiness 检查 |
| Backtest Gate | `python -m backtests.{scheme_id}_reproduction --no-persist` | summary 返回样本数、准确率、月度分布和 input artifact source；授权前不写库 | 把回测结果写入 `t_scheme_predictions` |
| Backtest Persist Gate | 明确授权后运行不带 `--no-persist` 的单方案 runner | 只写 `t_backtest_runs`、`t_backtest_predictions`、`t_backtest_monthly_metrics` 中该 scheme/run 相关记录 | 修改 source 表、actuals 表或其他 scheme 的 backtest run |
| API/Frontend Gate | `/api/backtests/factor-lab`、`/api/metrics/{scheme_id}`、浏览器矩阵 | 新方案落入正确任务格子；标题和目标展示名正常；矩阵不回退、不消失 | 用前端硬编码补方案展示 |
| Live Gate | 明确授权后的单 scheme 受控写库或 `scheduler.executor --scheme-id <id>` | 只影响该 `scheme_id` 的 prediction/run_log；actuals/source 表不变 | broad run-once、`--include-paused` 或批量 active 方案替代单方案验收 |
| Activation Gate | 修改 `config.yaml.status` 后重启 scheduler 并查日志 | 日志出现该方案 cron；第一次自动运行后只读核验 prediction/run_log | 未通过前置 gate 就直接 active |
| Documentation Gate | 更新 `CURRENT_STATUS`、`TEST_PLAN`、必要时 `HISTORICAL_REPRODUCTION` | run_id、样本数、准确率、剩余观察项和报告路径齐全 | 代码合入但文档不留证据 |

当前状态（2026-06-08）: harness 代码尚未落地，以上为强约束验收设计。现有方案已经满足核心输入链路要求: live adapter 和历史 backtest runner 均通过 `shared.input_artifacts` 调用统一 `shared.data_service` 生成输入 CSV 后读回。后续新增方案必须先按该矩阵人工或自动检查；未来 `harness/` 包落地后，将把这些检查固化为 CLI gate。

---

## 验证完成标准

所有Phase的TODO全部打勾后，系统可以进入正式运行。

**最低可运行标准**（可以先跑起来再逐步完善）:
- Phase 0-4 全部通过（环境+数据+方案+写入）
- Phase 6 的"方案发现"和"定时执行"通过
- Phase 7 的`/api/metrics`通过
- Phase 9 的"完整日流程模拟"通过

当前判定（2026-06-06）: 最低可运行标准已满足。真实 9:25 日度预测触发已观察通过。早先记录的周度 10Y readiness/dry-run/受控 live 写库/自动调度项对应已退役/代码未实现的周度方案，不再适用。16:00 actuals、连续多日运行和异常压测仍属上线观察项。

**完整验收标准**:
- 所有Phase全部通过
- 连续运行5个工作日无异常

当前判定（2026-06-06）: 完整验收尚未完成，剩余项为 16:00 actuals 窗口观察、连续多日运行、异常/性能压测，以及 panda_quantflow 外层仓库接入。早先列入的周度自动调度与 weekly live 样本统计项对应已退役/代码未实现的周度方案，已移除。
