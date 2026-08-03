# 7Y 本地因果语义修订实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 以两个独立 Blackbox V2 方案修复 7Y T+1 cutoff 因果性和持久 cache 问题，并在 Gate 通过后完成用户授权的入库、历史回补和前端读回。

**Architecture:** 保留 `*_v1` 不动，创建 `*_v2` 两文件交付。v2 以 feature 行而非不可见的 target 行计算模型状态，并只在进程内缓存模型。写库操作只通过现有 Harness 生命周期 Gate。

**Tech Stack:** Python 3.12、LightGBM、Blackbox V2 Contract 1.0、Harness、MySQL、FastAPI。

---

### Task 1: 先写真实交付的因果与 cache 回归测试

**Files:**
- Create: `tests/test_seven_y_current55_lgbm_v2_delivery.py`

- [ ] 创建可生成完整四文件 DataBridge fixture 的测试：daily 截止日为 2026-01-30，目标日为 2026-02-02，fixture 另含截止后的日/周/月行与权威 `api_wind_date.csv`。
- [ ] 断言两个 v2 脚本均能在 `feature_date=daily_cutoff=T` 运行并原样回显 Request，方向属于 `-1/0/1`。
- [ ] 断言只篡改 T 后的三频行不会改变 JSON 输出；两个冷 `python -B` 进程输出相同，输出父目录没有 `.pkl` 或 `.blackbox_model_cache`，`--help` 不暴露 `--cache-dir`。
- [ ] 固化四个 v1 SHA-256，断言 v1 未变、v2 ID/元数据分离，且 001/002 仍保留各自参数。
- [ ] 运行该测试并确认在 v2 尚不存在时因缺少交付而失败。

### Task 2: 构造两个 v2 两文件交付并经 Intake 进入仓库

**Files:**
- Create: `schemes/seven_y_current55_lgbm_001_v2/config.yaml`
- Create: `schemes/seven_y_current55_lgbm_001_v2/delivery/seven_y_current55_lgbm_001_v2.py`
- Create: `schemes/seven_y_current55_lgbm_001_v2/delivery/seven_y_current55_lgbm_001_v2.json`
- Create: `schemes/seven_y_current55_lgbm_002_v2/config.yaml`
- Create: `schemes/seven_y_current55_lgbm_002_v2/delivery/seven_y_current55_lgbm_002_v2.py`
- Create: `schemes/seven_y_current55_lgbm_002_v2/delivery/seven_y_current55_lgbm_002_v2.json`

- [ ] 从 v1 的日历修复代码生成每个 v2 的干净两文件 staging delivery，改写 metadata ID、名称、描述和 `algorithm_version=2.0.0`。
- [ ] 删除跨进程 cache 代码、CLI `--cache-dir` 和 cache 写入；保留进程内 `models` / `raw_cache`。
- [ ] 以 `feature_idx` 做预测、月度状态和翻转标签的唯一索引；训练标签严格截止；002 的长窗识别同时适配 v2 名称。
- [ ] 对每个 staging delivery 执行标准 Intake，声明 `api-wind-date-v1`，不覆盖 v1。
- [ ] 重新运行专用测试，确认由 Red 变 Green；再运行 Intake/Discovery 相关测试。

### Task 3: 逐方案完成无业务写 Gate

**Files:**
- Modify: `docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md`

- [ ] 重新核对 runtime、DataBridge generation、交易日历和 DB 中 v2 identity 的空闲状态。
- [ ] 每个 v2 单独执行 `harness onboard --stage all`，记录 exact harness run、scheme version、snapshot 与所有 Gate 结果。
- [ ] 若任一 Gate 失败，停止后续写入，先新增失败回归测试并修正根因。

### Task 4: 按用户授权登记、激活、历史回补与前端读回

**Files:**
- Modify: `docs/records/status/WEEKLY_10Y_D_OVERLAY_0801_FIX_PLAN_20260803.md`

- [ ] 每个 v2 使用其 exact passed run/version 的独立授权 token，依次执行 draft-register、shadow-register、persisted historical backtest、activation。
- [ ] 历史回测只持久化 `target_date < 2026-06-01`；枚举并逐条回补当日之前的 `gray_live` target，禁止 `scheduled_live`。
- [ ] 只读验证 active composite Registry、backtest/gray-live 日期不重叠、API Gate、dashboard/API 与前端任务格。
- [ ] 记录写表增量、front-end 读回证据和仍未获 scheduler admission 的状态。
