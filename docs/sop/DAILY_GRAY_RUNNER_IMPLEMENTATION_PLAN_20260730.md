# 每日 gray_live 信号自动化实施计划

**文档状态**: DRAFT — 待用户确认

**创建日期**: 2026-07-30

**目标**: 以最快速度实现每日自动出信号功能，复用已验证的 `gray_live` 执行链，砍掉 ledger/epoch/migration 018 等冗余设计。

---

## 核心设计决策

### 1. 技术路径：复用 gray_live + 独立定时任务

- **不走 ledger/epoch/occurrence 重装甲路径** — 这些需要 migration 018、epoch 发布、rollout 翻转
- **复用 `execute_all(date, prediction_phase="gray_live")`** — 已验证的执行链，绕过 ledger 门禁
- **legacy schema (migration 017) 即可运行** — `gray_live` 路径写真实 `started_at`，不碰 `SET started_at=NULL` 的 claim fence
- **独立 launchd job** — 不依赖现有 `scheduler.main` 进程，零耦合

### 2. 触发机制

- **launchd `StartCalendarInterval`**: 每日 **07:10** 自动触发
- **交易日过滤**: 非交易日直接退出 0，不执行
- **触发时间选择依据**: DataBridge deadline 06:55，留 15 分钟缓冲

### 3. Liwei 家族缓存依赖（关键约束）

经源码分析确认：consumer **只读不写**，缓存覆盖不足时直接 fail `CACHE_PUBLISHER_REQUIRED`。

#### 7 个 cache family，10 个 active 方案

| Family | Publisher (必须先跑) | Consumer (依赖 publisher) | 关系 |
|---|---|---|---|
| `liwei_0616_10y_v61` | `liwei_0616_10y01_full_oos_k3_div_k10` | `liwei_0616_10y01_cons_say_k3_div_k10`<br>`liwei_0616_10y02_cons_say_k3_div_k5` | 1 pub → 2 consumer |
| `liwei_0616_5y_v31` | `liwei_0616_5y01_full_oos_k3_div_k10` | `liwei_0616_cons_sda_k3_div_k10` | 1 pub → 1 consumer |
| `liwei_0616_5y_allk10_auc_static_v1` | `liwei_0616_5y_auc_static_all_k3_div_k10` | (自己) | 自给自足 |
| `liwei_0616_5y_allk10_auc_yearly_v1` | `liwei_0616_5y_auc_yearly_all_k3_div_k10` | (自己) | 自给自足 |
| `liwei_0616_5y_allk10_ic_yearly_v1` | `liwei_0616_5y_ic_yearly_all_k3_div_k10` | (自己) | 自给自足 |
| `liwei_0616_7y01_v31` | `liwei_0616_7y01_cons_say_k3_div_k10` | (自己) | 自给自足 |
| `liwei_0616_7y03_v31` | `liwei_0616_7y03_cons_all_k3_div_k8` | (自己) | 自给自足 |

#### 代码证据

- publisher 判定: `is_publisher = (cache_consumer_id == spec.publisher_consumer_id)` ([phase_a_cache.py:436](../../shared/liwei_0616_phase_a_cache.py#L436))
- consumer 只读分支: `_validated_consumer_hit` ([phase_a_cache.py:957](../../shared/liwei_0616_phase_a_cache.py#L957))
- 覆盖不足 fail: `if not requested.issubset(available): raise CACHE_PUBLISHER_REQUIRED` ([phase_a_cache.py:995](../../shared/liwei_0616_phase_a_cache.py#L995))

#### 执行顺序约束

```
阶段1 (publishers + 自给自足方案，可并发执行):
  liwei_0616_10y01_full_oos_k3_div_k10       ← 10y_v61 publisher
  liwei_0616_5y01_full_oos_k3_div_k10        ← 5y_v31 publisher
  liwei_0616_5y_auc_static_all_k3_div_k10    ┐
  liwei_0616_5y_auc_yearly_all_k3_div_k10    │ 自给自足
  liwei_0616_5y_ic_yearly_all_k3_div_k10     │ (既是 publisher
  liwei_0616_7y01_cons_say_k3_div_k10        │  也是 consumer)
  liwei_0616_7y03_cons_all_k3_div_k8         ┘

阶段2 (consumers，必须在对应 publisher 成功后):
  liwei_0616_10y01_cons_say_k3_div_k10       ← 依赖 10y01_full_oos
  liwei_0616_10y02_cons_say_k3_div_k5        ← 依赖 10y01_full_oos
  liwei_0616_cons_sda_k3_div_k10             ← 依赖 5y01_full_oos
```

**关键**: 只有 2 个 family 有跨方案依赖，5 个 family 自给自足。

---

## 实施步骤

### Phase 0: 代码备份（5 min）

**目的**: 将当前开发分支同步到 master 作为备份点。

**前置条件**:
- 当前分支: `codex/audit-bugfixes-20260613` @ `a06006a`
- `master` @ `067fa1e` (直系祖先，可 fast-forward)
- 用户明确授权同步 master

**操作**:
```bash
git checkout master
git merge --ff-only codex/audit-bugfixes-20260613
git push origin master
git checkout codex/audit-bugfixes-20260613
```

**验收**: `master` 和开发分支指向同一 commit `a06006a`，远程已推送。

---

### Phase 1: 增量缓存行为实测（20 min，定生死）

**目的**: 验证 publisher 增量扩缓存几分钟完成，consumer 能 hit 不报 `CACHE_PUBLISHER_REQUIRED`。

**前置条件**:
- warm cache 已 bootstrap 到 feature=2026-07-27
- 选一个最近的已有 DataBridge 覆盖的交易日（建议 2026-07-29，feature=2026-07-28）

**操作**:

1. 选定测试日期（待用户确认或自动查 `t_trade_calendar`）
2. 手动运行 publisher:
   ```bash
   python -m scheduler.executor 2026-07-29 \
     --scheme-id liwei_0616_10y01_full_oos_k3_div_k10 \
     --prediction-phase gray_live
   ```
3. 观察:
   - 执行时间 < 10 分钟 ✅
   - extra 字段 `phase_a_cache_status` 包含 `append` 或 `suffix` (增量) ✅
   - 不是 `full_rebuild` ❌
4. 手动运行 consumer:
   ```bash
   python -m scheduler.executor 2026-07-29 \
     --scheme-id liwei_0616_10y01_cons_say_k3_div_k10 \
     --prediction-phase gray_live
   ```
5. 观察:
   - 执行成功，不报 `CACHE_PUBLISHER_REQUIRED` ✅
   - extra 字段 `phase_a_cache_status = 'hit'` ✅

**验收**:
- publisher 增量扩缓存成功，几分钟完成
- consumer 成功 hit，依赖关系验证通过

**失败应急**:
- 若 publisher 触发 full_rebuild → 排查 cache lineage/input_state，不继续
- 若 consumer 报 `CACHE_PUBLISHER_REQUIRED` → 排查 test_ranges 对齐，不继续

---

### Phase 2: 实现 daily_gray_runner（30 min）

**目的**: 编写极简 runner，按依赖顺序调度所有 active 日频方案。

**新增文件**: `scheduler/daily_gray_runner.py`

**核心逻辑**:

```python
"""每日 gray_live 信号自动生成器。

按 Liwei 缓存依赖顺序分两阶段执行所有 active 日频方案:
- 阶段1: publishers + 自给自足方案
- 阶段2: consumers (依赖阶段1的 publisher 成功)
"""
from datetime import date
from scheduler.calendar import is_trading_day
from scheduler.executor import execute_scheme, discover_schemes
from shared.db_config import create_engine_from_env

# Liwei publisher 方案 (必须先于 consumer 执行)
LIWEI_PUBLISHERS = {
    "liwei_0616_10y01_full_oos_k3_div_k10",
    "liwei_0616_5y01_full_oos_k3_div_k10",
}

# Liwei consumer 依赖映射
LIWEI_CONSUMERS = {
    "liwei_0616_10y01_cons_say_k3_div_k10": "liwei_0616_10y01_full_oos_k3_div_k10",
    "liwei_0616_10y02_cons_say_k3_div_k5": "liwei_0616_10y01_full_oos_k3_div_k10",
    "liwei_0616_cons_sda_k3_div_k10": "liwei_0616_5y01_full_oos_k3_div_k10",
}

def main():
    predict_date = date.today().isoformat()
    engine = create_engine_from_env()
    try:
        if not is_trading_day(engine, predict_date):
            print(f"{predict_date} 非交易日，跳过")
            return 0
    finally:
        engine.dispose()
    
    schemes = {cfg.scheme_id: cfg for cfg in discover_schemes() if cfg.status == "active"}
    
    # 阶段1: publishers + 自给自足方案
    phase1 = [
        sid for sid in schemes
        if sid in LIWEI_PUBLISHERS or sid not in LIWEI_CONSUMERS
    ]
    phase1_results = {}
    for sid in phase1:
        result = execute_scheme(schemes[sid], predict_date, prediction_phase="gray_live")
        phase1_results[sid] = result
        print(f"[Phase1] {sid}: {result.status}")
    
    # 阶段2: consumers (检查 publisher 成功)
    phase2 = [sid for sid in schemes if sid in LIWEI_CONSUMERS]
    phase2_results = {}
    for sid in phase2:
        publisher = LIWEI_CONSUMERS[sid]
        if phase1_results.get(publisher, {}).status != "success":
            print(f"[Phase2] {sid}: SKIP (publisher {publisher} failed)")
            continue
        result = execute_scheme(schemes[sid], predict_date, prediction_phase="gray_live")
        phase2_results[sid] = result
        print(f"[Phase2] {sid}: {result.status}")
    
    # 汇总
    all_results = {**phase1_results, **phase2_results}
    success = sum(1 for r in all_results.values() if r.status == "success")
    total = len(all_results)
    print(f"完成: {success}/{total} success")
    return 0 if success == total else 1

if __name__ == "__main__":
    exit(main())
```

**注意事项**:
- Liwei 之外的日频方案 (若有) 自动归入阶段1 (因不在 `LIWEI_CONSUMERS` 中)
- 单方案异常不影响其他方案继续执行
- consumer 的 publisher 失败时跳过该 consumer，不视为整体失败

---

### Phase 3: launchd 配置（10 min）

**新增文件**: `deploy/launchd/com.bond-factor-lab.daily-gray.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.bond-factor-lab.daily-gray</string>

  <key>WorkingDirectory</key>
  <string>/Users/macstudio0/bond-factor-lab</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/macstudio0/miniconda3/bin/conda</string>
    <string>run</string>
    <string>--no-capture-output</string>
    <string>-n</string>
    <string>bond_factor_lab_service</string>
    <string>python</string>
    <string>-m</string>
    <string>scheduler.daily_gray_runner</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONNOUSERSITE</key>
    <string>1</string>
    <key>BOND_ALGO_CONDA_ENV</key>
    <string>forecast_env</string>
    <key>BFL_SOURCE_DB_CONFIG_ROOT</key>
    <string>/Users/macstudio0/.config/bond-factor-lab</string>
    <key>BFL_SOURCE_DB_CONFIG_PATH</key>
    <string>/Users/macstudio0/.config/bond-factor-lab/source-runtime-db.json</string>
  </dict>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>7</integer>
    <key>Minute</key>
    <integer>10</integer>
  </dict>

  <key>RunAtLoad</key>
  <false/>

  <key>StandardOutPath</key>
  <string>/Users/macstudio0/bond-factor-lab/logs/com.bond-factor-lab.daily-gray.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/macstudio0/bond-factor-lab/logs/com.bond-factor-lab.daily-gray.err</string>
</dict>
</plist>
```

**注意**: `RunAtLoad=false` — 加载 plist 时不立即执行，只按 07:10 时间触发。

---

### Phase 4: 全量验证（30 min）

**目的**: 最近交易日对所有日频 active 方案跑一轮，确认都出信号。

**操作**:
```bash
python -m scheduler.daily_gray_runner
# 手动触发，使用今天日期或指定测试日期
```

**验收**:
- 所有 active 日频方案都写出预测记录
- Liwei publisher 方案执行时间 < 10 分钟
- Liwei consumer 方案成功 hit，不报 `CACHE_PUBLISHER_REQUIRED`
- 数据库 `t_scheme_predictions` 有对应 `predict_date + prediction_phase='gray_live'` 记录
- 前端能查到当天信号

**核对点**:
- 确认 `t_scheme_predictions.prediction_phase = 'gray_live'` (不是 `scheduled_live`)
- 确认 `feature_date` = 前一交易日 (不是 `predict_date`)
- 确认 Liwei 的 `extra.phase_a_cache_status` 为 `hit`/`append`/`suffix`，不是 `full_rebuild`

---

### Phase 5: 挂载定时任务（5 min）

**操作**:
```bash
launchctl load ~/bond-factor-lab/deploy/launchd/com.bond-factor-lab.daily-gray.plist
launchctl list | grep daily-gray
```

**验收**:
- `launchctl list` 显示 `com.bond-factor-lab.daily-gray`
- 下一交易日 07:10 自动触发，验收首次自动运行

---

## 工时估算

| Phase | 描述 | 预估工时 |
|---|---|---|
| Phase 0 | 代码备份 (master 同步) | 5 min |
| Phase 1 | 增量缓存行为实测 | 20 min |
| Phase 2 | 实现 daily_gray_runner | 30 min |
| Phase 3 | launchd 配置 | 10 min |
| Phase 4 | 全量验证 | 30 min |
| Phase 5 | 挂载定时任务 | 5 min |
| **总计** | **开发 + 测试** | **约 1.5 小时** |
| **自动生效** | 下一交易日 07:10 | (物理墙，无法压缩) |

---

## 暂不做（后续逐步砍冗余）

- **不砍现有 `scheduler.main`** — 先让新 job 独立跑起来，稳定后再逐步移除冗余调度
- **不写 `scheduled_live`** — 只写 `gray_live`，避免触碰 ledger/epoch/migration 018
- **不加并发/签名/receipt/fence** — 保持最小复杂度
- **不切换到正式 ledger** — 这是后续 P0 独立项，与本方案正交

---

## 风险与缓解

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| Liwei publisher 误触发 full_rebuild | 耗时过长 (> 1 小时) | Phase 1 实测提前发现；若发生，排查 cache lineage 不继续 |
| Liwei consumer 请求的 test_ranges 不是 publisher 输出的子集 | consumer fail `CACHE_PUBLISHER_REQUIRED` | Phase 1 实测验证；尤其关注 `10y02_cons_say_k3_div_k5` (k_div_k5 与 publisher 的 k10 不同) |
| DataBridge 晚到 (> 07:10) | Blackbox V2 方案输入缺失 | 可容忍：Blackbox 方案会 fail，但不影响 Native 方案；下次自动重试 |
| 非 Liwei 的日频方案有隐藏依赖 | 方案执行失败 | Phase 4 全量验证覆盖所有日频方案；若发现依赖，补充到 runner |

---

## 待用户确认事项

在开始实施前，需用户确认：

1. **是否授权执行 Phase 0 的 master 同步并 push?** (agent 不得自行发布 master)
2. **Phase 1 实测用哪个交易日?**
   - 建议: 2026-07-29 (feature=2026-07-28，cache bootstrap 到 07-27，只需增量 1 天)
   - 或: 让 agent 查询 `t_trade_calendar` 自动选最近交易日

确认后即可开始实施。

---

## 参考文档

- [当前状态](../CURRENT_STATUS.md)
- [TODO](../TODO.md)
- [日频信号 SLA](../architecture/DAILY_SIGNAL_SLA.md)
- [Liwei 缓存契约](../../shared/liwei_0616_cache_contract.py)
- [Liwei Phase A 缓存实现](../../shared/liwei_0616_phase_a_cache.py)
