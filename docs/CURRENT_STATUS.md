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

- Mac3 与 ECS `current` 均为 release `bbd7fe7dbf8f3ee7dc74b5d6dbae06af38d98b9d`。Mac3 `previous` 为
  `59afc857c37eebb956adb200d9b1a05ed6a123f0`，ECS `previous` 为
  `990fd4b96fcd7cfb9fad533179c645bac723b817`；两端使用同一份确定性 archive，SHA-256 为
  `2957eb79038ad4db5bfe7b3a2ae12008741d960c740f6f52452b87d6d5b05137`。
- Mac3 生产应用从 `/Users/macstudio0/bond-factor-lab-production/current` 启动，运行状态位于
  `/Users/macstudio0/bond-factor-lab-runtime`，不再引用 Git 开发根。ECS 生产形态同样只运行 immutable
  release，不保留 Git checkout。

## 周期均值基础建设候选

- `codex/develop` 已完成 `monthly_average`、`quarterly_average`、`annual_average` 的平台代码基础：统一任务
  规格、MID/CQ/SF 纯桶语义、Contract/Request/回测、通用周期 actual、close-period one-shot、现有
  Dashboard/metrics 接入和九列前端。参考的 20 个 M0 方案没有被复制、执行或 Intake。
- 当前 Mac3/ECS immutable `current` 仍是上节所列旧 release，不包含这组候选代码。migration 020 尚未应用，
  仓库 close-period plist/unit 也未替换任何 installed 配置；因此不得把开发分支能力写成两端已经部署、
  已调度或已产生月均/季均/年均信号。
- 后续晋级顺序必须是：目标库只读 inspect 与受控应用 migration 020 → 从精确提交构建并验证 immutable
  release → 分别核对并授权 installed close-period 控制面变更。具体方案 Intake、Gate、回测、activation、
  gray live、DashboardGate 和自然观察是其后的独立入库项目。

## 调度与现场状态

- ECS DataBridge、daily、weekly、monthly、Actuals 五个 timer 均已获授权并保持
  `enabled/active/waiting`；任务是否成功仍由真实触发后的 service、run、prediction 和 Dashboard 证明。
- ECS installed weekly service 已移除历史 `/run/bond-factor-lab/manual-run.env` 引用；DataBridge、daily、
  monthly installed service 仍保留该旧可选引用，但该文件不存在，因此当前没有日期覆盖生效。替换 unit、
  `daemon-reload` 或服务重启仍须独立授权。
- Mac3 2026-08-21 daily 的已知终态仍为 `partial`：25 个方案成功、17 个 `execution_failed`；当前无残留
  runner 或 running run。该历史批次尚未诊断闭环，也未获授权手工重跑。
- Mac3 七个 installed plist 的只读 drift audit 为 `ok=true`；daily plist 保持工作日 07:03，当前 idle。
  本轮没有替换或重载任何 plist/unit/timer；ECS daily 与 Mac3 daily 下一次自然触发均为
  2026-08-24 07:03 Asia/Shanghai。

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
