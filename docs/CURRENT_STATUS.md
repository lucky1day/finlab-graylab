# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-23

本文只记录当前稳定事实。实时方案、run、prediction、DataBridge、API 和调度状态必须从各自权威数据源
读取；待推进工作见[统一后续推进计划](TODO.md)，生产规则见
[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。已完成迁移、逐次 Gate、运行 ID、
发布窗口和一次性验收证据不在工作树维护副本，通过 Git、Harness、数据库与目标机 journal 追溯。

## 双主机边界

- Mac3 继续承载生产域名、前端、数据库和 Writer；`launchd + installed plist` 是生产调度控制面。
- ECS 是独立灰度实验室，使用自己的 MySQL、DataBridge、Registry、run、prediction 和 systemd timer；
  Backend 只监听 loopback，不承载生产公网流量。
- 两端不复制、不双写、不共享数据库或运行期 authority。灰度健康不会自动授权域名、Web、Writer 或
  数据库 authority 切换。
- 两端共用唯一 `codex/develop` source release 代码线；ECS 先验证、Mac3 后晋级时允许 `current` 不同，
  不因此建立环境分支。

## 当前 release

- ECS `current` 已晋级为 close-period job 参数修复 release
  `2394711e1ac5f97b73af8ede4ded5163bb0a254e`，`previous` 为
  `00558d175cdffd0d2aae4b51e1aea6e8adda8923`。该 release 由干净 Git 提交重复构建两次且字节一致，
  archive SHA-256 为 `d52c1c56dc3fc5dee053d77c3d62c67e8b01665346126297485cdfa40c057c77`。
- Mac3 已使用上条 ECS 原始 archive 完成原子晋级，`current` 为
  `00558d175cdffd0d2aae4b51e1aea6e8adda8923`，`previous` 为
  `bbd7fe7dbf8f3ee7dc74b5d6dbae06af38d98b9d`；archive、manifest 和安装后 source-tree SHA 均与 ECS
  一致。Backend 已从新 current 运行并返回 `status=ok`。
- Mac3 生产应用从 `/Users/macstudio0/bond-factor-lab-production/current` 启动，运行状态位于
  `/Users/macstudio0/bond-factor-lab-runtime`，不再引用 Git 开发根。ECS 生产形态同样只运行 immutable
  release，不保留 Git checkout。

## 周期均值基础建设部署状态

- `codex/develop` 已完成 `monthly_average`、`quarterly_average`、`annual_average` 的平台代码基础：统一任务
  规格、MID/CQ/SF 纯桶语义、Contract/Request/回测、通用周期 actual、close-period one-shot、现有
  Dashboard/metrics 接入和九列前端。参考包中的周均五方案已在 ECS 完成入库；余下月均、季均、年均
  15 个方案尚未 Intake。
- ECS 已通过唯一受控 migration CLI 完成 018、019、020，历史均为 `APPLIED`；退役的
  `t_scheme_serving_pointer` 已删除，`t_scheme_period_average_actuals` 已按闭世界目标形态创建。ECS Backend、
  DataBridge/daily/monthly service 与 monthly timer 已从同一 immutable release 安装并读回一致。
- ECS close-period timer 当前为 `enabled/active/waiting`，每天 18:00 运行到期判断。新 release 候选内
  138 个测试和 46 个 subtest 通过；晋级后 Backend `/api/health` 为 `status=ok`、Dashboard HTTP 200，
  非到期 one-shot 为 `not_applicable / exit_code=0 / refresh_required=false`。probe 前后 Predictions、
  Runs、周期 Actual 计数和 DataBridge current 四份文件摘要完全一致；此前现有 56 个 active base scheme
  的 DashboardGate 结论保持通过。
- Mac3 已经由目标 immutable release 内的唯一受控迁移入口完成 018、019、020；001–020 全部精确
  `APPLIED`，目标表定义正确，预测、三类既有 Actual、Registry 和 scheme version 计数前后不变。daily
  18:00 close-period monthly plist 首次 probe 被旧 release 的 immutable launcher 在业务代码前拒绝：全局
  `service.env` 的晨间 `05:30/06:45` 与 plist 的 `18:00/18:55` 同名环境变量冲突。probe 没有刷新
  DataBridge、创建 run 或写业务表；monthly plist 已恢复到每月 15 日 18:00 的旧入口，七个 installed/loaded
  plist 对旧稳定模板重新达到 `ok=true`。因此 Mac3 数据库和 release 主体已完成，但 daily close-period
  尚未上线，不能宣称双主机调度闭环。修复 release 已先在 ECS 验证，Mac3 只待使用同一 archive 晋级、
  替换 monthly plist 并完成现场验收。

## 调度与现场状态

- ECS DataBridge、daily、weekly、monthly、Actuals 五个 timer 均已获授权并保持
  `enabled/active/waiting`；任务是否成功仍由真实触发后的 service、run、prediction 和 Dashboard 证明。
- ECS installed DataBridge、daily、weekly、monthly service 均不再读取历史
  `/run/bond-factor-lab/manual-run.env`；monthly timer 已从每月 15 日改为每天 18:00 的 close-period 到期
  判断入口，不新增第二调度控制面。
- Mac3 2026-08-21 daily 的已知终态仍为 `partial`：25 个方案成功、17 个 `execution_failed`；当前无残留
  runner 或 running run。该历史批次尚未诊断闭环，也未获授权手工重跑。
- Mac3 七个 installed plist 在 monthly 安全回滚后对旧稳定模板的 drift audit 为 `ok=true`；daily plist
  保持工作日 07:03，monthly 暂时保持每月 15 日 18:00。修复提交
  `9f1b2bf26898e68c58b06b10fbaef668cde73b4d` 已把 close-period 刷新窗口改为显式 job 参数，同时保留
  launcher 对所有 service 环境冲突的拒绝；包含该修复的 release `2394711e1ac5f97b73af8ede4ded5163bb0a254e`
  已在 ECS 晋级验证，尚未在 Mac3 晋级。

## 新方案入库状态

- `weekly_1y_causal_v1_31_0_standalone` 已完成技术 Gate、shadow、持久化回测、activation、单日
  `gray_live` 和 DashboardGate，Registry 为 active。
- `m0_weekly_avg_{1y,3y,5y,7y,10y}_v1` 已逐方案完成 Blackbox Intake、技术 Gate、shadow、持久化回测、
  activation、单日 `gray_live` 和 DashboardGate；五个 composite Registry 均为
  `active + weekly_average`，部署范围仅为 `aliyun-gray`。
- `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 已在 ECS、Mac3 分别完成 Blackbox 四段
  技术 Gate、shadow、完整持久化回测、activation、单日 `gray_live` 和 DashboardGate。
  两个 composite Registry 分别为 `five_y_factor_rule_online_v1__h1__5Y` 与
  `ten_y_factor_level_ensemble_v1__h1__10Y`，均为 `active + daily + T+1`；部署矩阵范围为
  `mac3-production + aliyun-gray`。
- exact version 分别为 5Y `55f0e753b18e`、10Y `997af2e57ad9`。两端每个 canonical backtest 均为
  278 条结果和 14 个月度指标，目标区间 `2025-07-02..2026-08-20`。
- 两端 gray live 均为 `predict=2026-08-21 / feature=2026-08-20 / target=2026-08-21`，5Y 方向为
  `-1`、10Y 方向为 `0`；两端重复 gap plan 均为 `present=1 / actionable=0`，canonical backtest 与 live
  target 零重叠。Dashboard、真实页面候选排行和月度详情均已读回，控制台无错误。
- 以上日频两方案在两端均为 Onboarding Complete；上述 ECS 周频方案也保持 Onboarding Complete。
  首次 `scheduled_live` 仍须等待各自真实时钟触发后联合读回，不能由 waiting、gray live 或人工执行
  预先宣称 Production Observed。

## 当前治理边界

- config active、exact version active、Registry target active 且 cadence 匹配，是进入一次性 runner 的
  唯一资格；自然运行写 `scheduled_live`，单日授权补缺只写 insert-only `gray_live`。
- 新方案只走 Blackbox V2 两文件 Intake；Native V1 只维护政策清单内存量身份。平台不反编译或改写
  Blackbox 算法逻辑，只验证平台接入和标准输出边界。
- Mac3 production 与 ECS gray 的 installed plist/unit、服务状态、数据库写入、激活、补数和 DDL 都是
  独立操作，代码或文档提交不能外推为现场授权。
- ECS 独立灰度迁移、双主机单一 source release 治理和 Mac3 immutable runtime 解耦已经闭环；未来把
  生产域名或 Writer 切到 ECS 是新的生产项目，不属于当前完成条件。
