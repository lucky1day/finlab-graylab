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

- Mac3 `current` 为前端颜色 R4 `92a93713656d8534e68312f123676b5d2054d8a6`，`previous` 为 R2
  `e692285d47e41c384dc915758abe0c51f9ac3aaf`。生产应用从
  `/Users/macstudio0/bond-factor-lab-production/current` 启动，运行状态位于
  `/Users/macstudio0/bond-factor-lab-runtime`，不再引用 Git 开发根。
- ECS `current` 为日频 T+1 两方案 release `990fd4b96fcd7cfb9fad533179c645bac723b817`，`previous` 为
  `59afc857c37eebb956adb200d9b1a05ed6a123f0`。archive SHA-256 为
  `c2bcd2458d67c9938f19ba13a78005441d476a3e22d927c34ebe5342f5545705`，安装后 source tree SHA-256 为
  `764443b5a1a16a8f7edc485fd9d28fd02840395f63a3ce9538022e6fb006a531`。
- `codex/develop` 已包含提交 `ee80cf502e55e4342a0b8fe5d2dc1236660343eb` 的灰度前端展示改动，但该
  提交尚未构建为 ECS release；当前 ECS 页面仍以现场 immutable `current` 为准。

## 调度与现场状态

- ECS DataBridge、daily、weekly、monthly、Actuals 五个 timer 均已获授权并保持
  `enabled/active/waiting`；任务是否成功仍由真实触发后的 service、run、prediction 和 Dashboard 证明。
- ECS installed weekly service 已移除历史 `/run/bond-factor-lab/manual-run.env` 引用；DataBridge、daily、
  monthly installed service 仍保留该旧可选引用，但该文件不存在，因此当前没有日期覆盖生效。替换 unit、
  `daemon-reload` 或服务重启仍须独立授权。
- Mac3 2026-08-21 daily 的已知终态仍为 `partial`：25 个方案成功、17 个 `execution_failed`；当前无残留
  runner 或 running run。该历史批次尚未诊断闭环，也未获授权手工重跑。

## 新方案入库状态

- `weekly_1y_causal_v1_31_0_standalone` 已完成技术 Gate、shadow、持久化回测、activation、单日
  `gray_live` 和 DashboardGate，Registry 为 active。
- `m0_weekly_avg_{1y,3y,5y,7y,10y}_v1` 已逐方案完成 Blackbox Intake、技术 Gate、shadow、持久化回测、
  activation、单日 `gray_live` 和 DashboardGate；五个 composite Registry 均为
  `active + weekly_average`，部署范围仅为 `aliyun-gray`。
- `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 已逐方案完成技术 Gate、shadow、
  canonical backtest、activation、单日 `gray_live` 和 DashboardGate；ECS 两个 composite Registry 均为
  `active + daily + T+1`。仓库部署矩阵已扩展为 `mac3-production + aliyun-gray`，但这只建立 Mac3 候选
  资格；Mac3 独立 Gate、Registry、回测、activation、gray live 与 Dashboard 尚未执行。ECS canonical
  backtest 目标区间为 `2025-07-02..2026-08-20`，gray live 目标日为 `2026-08-21`，二者零重叠。
- 上述方案在 ECS 的当前状态均为 Onboarding Complete。首次 `scheduled_live` 仍须等待 daily/weekly 真实时钟触发后
  联合读回，不能由 timer waiting、gray live 或人工执行预先宣称 Production Observed。

## 当前治理边界

- config active、exact version active、Registry target active 且 cadence 匹配，是进入一次性 runner 的
  唯一资格；自然运行写 `scheduled_live`，单日授权补缺只写 insert-only `gray_live`。
- 新方案只走 Blackbox V2 两文件 Intake；Native V1 只维护政策清单内存量身份。平台不反编译或改写
  Blackbox 算法逻辑，只验证平台接入和标准输出边界。
- Mac3 production 与 ECS gray 的 installed plist/unit、服务状态、数据库写入、激活、补数和 DDL 都是
  独立操作，代码或文档提交不能外推为现场授权。
- ECS 独立灰度迁移、双主机单一 source release 治理和 Mac3 immutable runtime 解耦已经闭环；未来把
  生产域名或 Writer 切到 ECS 是新的生产项目，不属于当前完成条件。
