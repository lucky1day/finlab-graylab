# WAVG GAPFLIP V5 五个周平均方案生产入库记录

**文档状态**：`CURRENT`

**目标读者**：平台入库、生产授权和审计人员

**最后核验时间**：2026-07-31 10:23:02，`Asia/Shanghai`

**机器证据**：
[WAVG_GAPFLIP_V5_5SCHEMES_ONBOARDING_20260731.evidence.json](WAVG_GAPFLIP_V5_5SCHEMES_ONBOARDING_20260731.evidence.json)

## 当前结论

```text
PR_20_MERGED: TRUE
TECHNICAL_GATES: PASS 35/35
CONTROL_PLANE_PERSISTED: TRUE
ALGORITHM_LOGIC_REVIEWED: FALSE
CONFIG_VERSION_REGISTRY: active / active / active
HISTORY: 5 × (72 predictions + 17 monthly metrics)
GRAY_LIVE: 5 × 9 = 45 predictions
PRODUCTION_ACTIVATED: TRUE
API_VISIBLE: TRUE
FRONTEND_VISIBLE: TRUE
SCHEDULER_MOUNTED: FALSE
SCHEDULED_LIVE: 0
ONBOARDING_COMPLETE: FALSE
PRODUCTION_OBSERVED: FALSE
```

PR #20 的十五个新增文件已经精确合入开发分支。平台没有评审、反编译或修改
五份算法脚本的内部模型逻辑；只把测试机交付状态收敛为生产首次登记所需的
`paused + draft`，增加 exact admission 和回归测试，再通过正式生命周期 Gate
转为 `active + active`。

五个方案现已完成生产激活、历史回测、连续灰度补齐、API 和前端验收。用户本轮
明确要求周度方案暂不挂调度，因此当前是“已上线并授权生产、未挂自动调度”：
这满足本次交付范围，但按通用 SOP 不能标记为 `Onboarding Complete`，也不能把
任何手工灰度倒签成 `scheduled_live`。

## 身份与原始交付

| Tenor | Base scheme ID | Registry ID | Scheme version | Python SHA256 | Metadata SHA256 |
|---|---|---|---|---|---|
| 1Y | `wavg_1y_gapflip_v5` | `wavg_1y_gapflip_v5__h1__1Y` | `68999585142a` | `52184cd05da6cf87d60bcfdf0487306a20f96249792a7d1969fc3d7e89742de5` | `f92ad296693010ab61a4373a54d640206089d4d79b387a84d3bb35701bc333d4` |
| 3Y | `wavg_3y_gapflip_v5` | `wavg_3y_gapflip_v5__h1__3Y` | `faba245acaef` | `f406f8630f3978763112f3bd0187162c807f0789fff33373b61ecb6393ab1d62` | `bf2372e6728d05fe612e24a16d8b5a7c56d4b931cd7ec446b021f921976ca17d` |
| 5Y | `wavg_5y_gapflip_v5` | `wavg_5y_gapflip_v5__h1__5Y` | `63ed1291f9d4` | `b01c3ea65654b0ad3ccb45529ed6ec704c9fe1aa69ff5a96d37bc8f1cb842ebb` | `9c05e61f3f96d3bbef76df1a1d91e8a1a8e67e7b13d02d9bf42073f27899842b` |
| 7Y | `wavg_7y_gapflip_v5` | `wavg_7y_gapflip_v5__h1__7Y` | `21951d955f44` | `65ab2d3539a8dda967a5d0dc909ece3a1d68aec989f25b44f460a74b7d4d04e0` | `bcdf46a87712fd8b567daf9b35f317db253e6e8bcb95fca1b7a6b8df45259547` |
| 10Y | `wavg_10y_gapflip_v5` | `wavg_10y_gapflip_v5__h1__10Y` | `c1e5a9db6097` | `a558589e5f2dc0a41bd335074068e6049911de59f515a26ae80312c6cc71d0b7` | `35e264bfb6a819f8a7420cad98e1001487f1caa96313e63db7bcd90c6dea16fb` |

五个方案统一为 `weekly / weekly_average / horizon=1`，算法版本 `5.0.0`，
Contract `1.0`。PR 合并端点与生产准入提交之间，十个 delivery 文件的 Git blob
完全一致；生命周期字段不参与 canonical scheme version 计算。

## 输入与运行环境

| 项目 | 结果 |
|---|---|
| Runtime Profile | `blackbox-v2-v1` |
| 算法环境 | `forecast_env_blackbox_v1` |
| 环境指纹 | `720ad40ab77cd6c7156ff35a80cf3604ac3a6153425ed235a4e3158b0631f8bd` |
| DataBridge generation | `full-20260730-081804-9794ce962c1a` |
| Parent snapshot | `snapshot-a0dbf1774782db2e6d2a1ec5` |
| Combined snapshot | `snapshot-05bdb43f07e54dea10b3c80a` |
| 平台周历 | `api-wind-date-v1`，6067 行，SHA256 `18db31076b58eb119b6f4110d3448653a506fb2d21ca2577ed1aae4d177bcc23` |
| Gate Request | `predict=2026-07-25 / feature=2026-07-24 / target=2026-07-31` |

五个算法依赖的交易日和 `week_id` 都由 DataBridge 注入的
`api_wind_date.csv` 提供。平台没有使用测试机本地周历、ISO 周推算或脚本内嵌
日历。环境预检、sandbox 网络隔离和输入目录写保护均通过。

## Gate 与生产授权

| Scheme | Check-only all-stage | Persisted all-stage |
|---|---|---|
| 1Y | `hr_20260731T014159Z_e38bbffd01d4` | `hr_20260731T014657Z_e00a9668379f` |
| 3Y | `hr_20260731T014159Z_4e16fab97d00` | `hr_20260731T014657Z_8061aa45d3f0` |
| 5Y | `hr_20260731T014352Z_634eb90092a9` | `hr_20260731T014818Z_512434962fd4` |
| 7Y | `hr_20260731T014352Z_cfe68b315c20` | `hr_20260731T014818Z_22734f29b85c` |
| 10Y | `hr_20260731T014523Z_9cc5f0dd0397` | `hr_20260731T014929Z_8fa1ae5c1bb8` |

十个 all-stage run 均为 7/7 PASS。Check-only 批次明确
`control_plane_persisted=false`、`business_tables_written=false`；持久化批次
只写 harness 控制面，不写预测或回测业务表。

每个方案随后使用独立、最长 900 秒、绑定 exact version/run/action/date 的 HMAC
token，依次完成 `draft-register → shadow-register → activate`。数据库 exact
version、composite Registry 和配置最终均为 `active`，`deployed_at=2026-07-31`，
批准账号为 `lucky1day`。

## 历史回测

历史授权统一绑定 `backtest_start_date=2025-01-01` 和 exclusive cutoff
`target_date < 2026-06-01`：

| Tenor | Backtest run | 明细 | 月度 | 正确 | 准确率 |
|---|---:|---:|---:|---:|---:|
| 1Y | 196 | 72 | 17 | 53 | 73.6% |
| 3Y | 197 | 72 | 17 | 52 | 72.2% |
| 5Y | 199 | 72 | 17 | 47 | 65.3% |
| 7Y | 198 | 72 | 17 | 50 | 69.4% |
| 10Y | 200 | 72 | 17 | 52 | 72.2% |

五个 run 的实际站位日均为 `2025-01-03..2026-05-22`，目标日均为
`2025-01-10..2026-05-29`。每个 run 都是 immutable success，采用
`current_snapshot_as_of_not_historical_vintage` 语义；合计 360 条 prediction
和 85 条月度指标。

## 灰度实盘

每个方案按相同平台周历顺序写入九个唯一目标：

| `predict_date` | `feature_date` | `target_date` |
|---|---|---|
| 2026-05-30 | 2026-05-29 | 2026-06-05 |
| 2026-06-06 | 2026-06-05 | 2026-06-12 |
| 2026-06-13 | 2026-06-12 | 2026-06-18 |
| 2026-06-20 | 2026-06-18 | 2026-06-26 |
| 2026-06-27 | 2026-06-26 | 2026-07-03 |
| 2026-07-04 | 2026-07-03 | 2026-07-10 |
| 2026-07-11 | 2026-07-10 | 2026-07-17 |
| 2026-07-18 | 2026-07-17 | 2026-07-24 |
| 2026-07-25 | 2026-07-24 | 2026-07-31 |

45 个 `gray-backfill` Gate 全部 PASS，并精确形成 45 条 prediction、45 个
success run 和 45 条 success run log。每一点使用独立
`gray_backfill_write` token，写入为 insert-only；五个方案均无重复 target，
canonical backtest 与 live target 重叠为 0。

截至核验时，前八个目标均已有周平均 actual，`2026-07-31` 每方案各一条为正常
待验证。Gray 指标分别为 1Y `6/8=75.0%`、3Y `6/8=75.0%`、
5Y `5/8=62.5%`、7Y `6/8=75.0%`、10Y `6/8=75.0%`。这些效果数值只是平台
读回结果，不属于本次算法内部验收结论。

## API、前端与调度边界

- `/api/schemes` active 数量由 47 增至 52，五个 composite ID 均可见，名称均为
  `GAPFLIP_V5`，部署时间均为 `2026-07-31`。
- `/api/backtests/factor-lab` 对五个方案分别返回 72 条历史和 17 个月度指标；
  所有历史 target 严格早于灰度边界。
- 五个 metrics endpoint 各返回 9 条 `gray_live`、标准三日期和
  `phase_ranges.gray_live.rows=9`；每个 endpoint 各有一条 pending actual。
- 浏览器强制刷新后，1Y/3Y/5Y/7Y/10Y 五个“周平均”格子都能选择并显示
  `GAPFLIP_V5`；1Y 排行可见 `59/80=73.8%` 和部署时间 `2026/07/31`，
  控制台 error 为 0。
- admission 固定为 `mode=gray + capabilities=[]`。自动、recurring 和
  direct-scheduled 控制面均 fail-closed；专项 scheduler 回归证明五个 active
  配置不会注册 prediction job。
- `com.bond.factorlab.scheduler` 未加载，系统中无 `scheduler.main` 进程，
  数据库 `scheduled_live=0`。配置保留的 cron 只是自然周期声明，不是生产挂载。

后续若用户单独授权周度调度，必须重新走 scheduler 挂载与自然运行验收。只有真实
时钟产生成功 `scheduled_live` 后，才可标记 `Production Observed`；不能用本次
45 条手工灰度替代。

## 验收边界

- 本记录证明五个方案的输入接口、确定性、截止隔离、标准输出、生产身份、
  历史/灰度分区、写库和前端读回正确。
- 本记录不证明算法内部逻辑、模型参数或效果已由平台验证。
- 历史回放使用当前快照，不是历史 vintage PIT。
- 因用户明确暂缓周度 scheduler，本批没有达到通用 SOP 的
  `Onboarding Complete`，但已完成本次要求的生产激活和业务入库。
