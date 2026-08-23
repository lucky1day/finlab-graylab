# 当前状态

**文档状态**：`CURRENT`

**最后核验日期**：2026-08-23

本文只记录当前稳定事实。实时方案、run、prediction、DataBridge、API 和调度状态必须从各自权威数据源读取；除当前部署基线所需的精确 release 身份外，不在仓库文档冻结时点数量、运行 ID 或逐次校验 hash。未批准工作见[统一后续推进计划](TODO.md)，生产调度规则见[生产信号与调度治理](architecture/PRODUCTION_SCHEDULING_GOVERNANCE.md)。

## 当前双主机运行规则

- Mac3 继续承载生产域名、前端、数据库和 Writer；`launchd + installed plist` 是 Mac3 的自然生产调度控制面。
- 双主机共用唯一 `codex/develop` source release 代码线，但允许按“ECS 先验证、Mac3 后晋级”分阶段
  发布；两端 `current` 不要求在灰度验证期始终相同，也不因此建立环境分支。
- Mac3 `current` 保持前端颜色 R4 `92a93713656d8534e68312f123676b5d2054d8a6`，`previous` 为 R2
  `e692285d47e41c384dc915758abe0c51f9ac3aaf`。ECS 已按独立灰度实验室节奏继续晋级，当前
  `current` 为日频 T+1 两方案 release `990fd4b96fcd7cfb9fad533179c645bac723b817`，`previous` 为
  `59afc857c37eebb956adb200d9b1a05ed6a123f0`；两端 `current` 不同不构成环境分支。
- Mac3 当前使用的 R4 archive SHA-256 为
  `e2f1c59c5e183940903de0d3eac39c9cd27e24a3b518ce28446a5d87db96ee37`，source digest 为
  `406b407c624f26137b0d7a84317dcb7cdee8a6d4fa20eccbf9fc7dec9c0bb2b4`，对应 tag
  `bfl-source-r4-frontend-colors-20260821`。
- 双主机当前 source 均含 insert-only prediction completion；Mac3 后续自然 prediction one-shot 将从
  R4 `current` 启动。R4 晋级本身不等于 2026-08-21 daily 已成功，也不授权手工重跑 Writer。
- Mac3 生产应用从独立 `bond-factor-lab-production/current` 启动；运行状态位于外置
  `bond-factor-lab-runtime`。`runtime/config/service.env` 是生产应用本机配置 authority，Git 根 `.env`
  只服务开发工作区；两者不自动同步，生产配置变更必须独立授权并重启对应服务。
- Mac3 immutable 解耦时已正式切换到精确 R2：六个应用 plist 均从 production `current` 启动，SSH tunnel 的
  工作目录和日志已外置；七项 drift audit、Backend/首页/方案/admin、DataBridge check-only 和远端
  loopback tunnel 读回全部通过。生产进程不再引用 Git 工作区，旧七个 plist 保留为首次回滚备份。
- Mac3 Git 开发根在保留既有未跟踪草稿的前提下切到 `codex/develop`；该分支切换不再影响生产进程。
- ECS 是独立灰度实验室，不是 Mac3 热备或复制节点；其 DataBridge、daily、weekly、monthly、Actuals 五个 systemd timer 已于 2026-08-18 经专项授权启用，现场 authority 是 installed unit/timer 与 `systemctl` 读回。
- 仓库候选模板中的 ECS 自然 DataBridge、daily、weekly、monthly 四个 service 已不再引用共享
  `/run/bond-factor-lab/manual-run.env`，但 ECS installed 四个 unit 仍保留该旧引用。2026-08-21 只读
  确认该文件不存在，因此没有正在生效的旧日期覆盖；仓库候选状态不等于现场已经生效。
- ECS R4 已完成不可变预安装、`current/previous` 原子切换和 Backend 重启；R3 可执行回滚包已在切换前
  独立复验并保留。R4 仅调整前端颜色映射、相关测试和静态资源缓存标识；
  Backend 健康、首页、方案 API、五个 timer 均为 `enabled/active/waiting`、五个 writer 均 idle，且 11 个
  installed unit hash 均未改变并已读回。R4 Backend 重启窗口约 1.2 秒，切换前后 binlog 位点精确保持
  `binlog.000033:7444533`，Mac3 服务未受影响。
- ECS 真实浏览器已验收任务格子、候选排行、逐月表现和每日明细：百分比使用 60% 红/绿两档且空值
  为灰色，目标表格无黄色百分比；涨/跌/平为红/绿/灰，结果 ✓/×/? 独立保持绿/红/灰。趋势图固定
  系列色、月份、样本数、表头和普通正文未改变，浏览器控制台无 warning/error。
- Mac3 于 2026-08-21 使用同一份 ECS 已验证 R4 archive 完成候选复验、CAS 激活和仅 Backend 重启；
  `current/previous` 分别为 R4/R2，R2 可执行回滚包保留在外置 runtime backup。Backend 中断窗口约
  1.8 秒，健康、首页、方案 API、SSH tunnel、七项 drift audit 与五个 idle one-shot 均读回。真实浏览器
  再次验收任务格子、候选排行、逐月表现、每日方向与结果状态；R4 CSS/JS 内容摘要和缓存标识精确匹配，
  控制台无 warning/error。Mac3 生产域名、数据库 authority、Writer 主机、installed plist 和环境合同未改。
- Mac3 切换前后五张关键业务表 count、migration 与 Registry 快照一致。binlog 窗口内只有既有
  `qrtz_scheduler_state` 心跳更新，不是 Bond Factor Lab 预测、Registry、run 或 run-log 写入。
- Mac3 2026-08-21 daily 的既有终态仍为 `partial`（25 个方案成功、17 个 `execution_failed`，launchd
  last exit code 为 1）；当前无残留 runner 或 running run。本次 R4 发布没有重跑或修复该批次，后续须
  独立诊断，且在结论闭环前不得称今日 daily 全量健康。
- ECS 一次性隔离 MySQL 真库直接调用了 Native active completion，并验证共享的 business-key decision
  与 plain INSERT：首次发布整组成功，完整重复 benign `skipped` 且旧行不变，部分冲突整组失败且缺失键
  不补写。Blackbox active completion 复用同一 repository decision/plain-INSERT 核心，本次未在隔离
  MySQL 中单独调用，其运行时入口由本地完整回归覆盖。
- 验证前后 ECS 灰度主库 `bond_db` 的 `t_scheme_predictions`、`t_scheme_versions`、
  `t_scheme_registry`、`t_scheme_runs`、`t_scheme_run_log` 五表 count 与全行摘要、五表 schema 摘要以及
  17 条 migration history 均一致；Mac3 生产库不在本次测试路径中。隔离数据库已删除，仓库没有保留
  一次性验收脚本。
- 2026-08-20 已使用冻结同源数据、隔离数据库和热缓存副本完成 DataBridge、daily、weekly、monthly
  与 Actuals 的 systemd 调度等价验收；三频 56 个 run、60 条预测全部成功，7 个 Liwei family 均走
  有限 suffix、full build 为 0，daily 墙钟约 62 分钟，低于两小时硬限。隔离资源已删除，生产库、
  cache pointer 和 release 未改变，五个 timer 继续自然灰度运行。
- 两端使用各自数据库、DataBridge 和运行记录，不复制、不双写、不共享运行期 authority；ECS Backend 仅监听 loopback，不承载生产公网流量。
- 仓库模板、代码和测试不能单独证明任一主机现场已加载或已运行；ECS 现场验收和后续自然监控也不
  自动授权 Web/Writer 切换。
- config active、exact version active、Registry target active 且 cadence 匹配，是进入对应 one-shot runner 的唯一资格。
- 自然运行写 `scheduled_live`；单日人工补缺只经 `python -m harness signal-gap-fill --predict-date YYYY-MM-DD` 写 insert-only `gray_live`。
- Blackbox Admission、Backend 手动预测、direct scheduling、ledger、occurrence、epoch、daily-gray 和常驻 APScheduler 均已退役。
- installed plist/unit/timer、launchctl/systemctl 服务变更、激活、持久化回测、额外业务写入和 DDL 仍是独立操作，必须获得明确授权。

## 2026-08-23 最新方案闭环

- ECS 灰度实验室中的 `weekly_1y_causal_v1_31_0_standalone` exact version `c93f76489d5b` 已完成 Blackbox 技术 Gate、shadow/paused 登记、完整持久化回测、原子 activation 和 Registry active 读回。
- 该方案的已批准单日灰度信号已按冻结 DataBridge authority 经 `signal-gap-fill` insert-only 写入；再次运行 gap plan 为 `present`、`actionable=0`，没有重复写入。
- 激活后的 `DashboardGate` 已通过，dashboard 可读到一条 `gray_live`。这完成方案的 Onboarding Complete 证据；是否形成 `scheduled_live` 仍只由下一次 ECS systemd 自然触发、任务日志、run/prediction 和 Dashboard 后续读回证明。
- 单维护者授权简化已提交并用于本轮 ECS 入库：用户侧 `HARNESS_AUTH_SECRET`、`auth issue`、token 复制及 `--authorize` 已从 CLI 移除，副作用命令自动绑定 exact version/latest passed run 并记录 `direct_operator_command_v1`。

### M0 周平均五方案

- `m0_weekly_avg_{1y,3y,5y,7y,10y}_v1` 已逐方案完成两文件 Intake、Blackbox 四 Gate `all`、首次 shadow identity 创建、完整持久化回测、原子 activation、单日 gray gap fill 和 DashboardGate。五个 composite Registry 均为 `active + weekly_average`，只在部署矩阵的 `aliyun-gray` 范围内，不进入 Mac3 production。
- 五个 exact version 分别为 1Y `246cc5b71238`、3Y `0726b172d237`、5Y `6208c1fc671d`、7Y `8c0eeae6ec6e`、10Y `d56548e8f295`。每个 canonical backtest 均为 84 条、20 个月度指标，目标区间 `2025-01-10..2026-08-21`；五个 gray live 均为 `predict=2026-08-22 / feature=2026-08-21 / target=2026-08-28`，方向依 1Y/3Y/5Y/7Y/10Y 为 `-1/+1/-1/+1/+1`。重复 gap plan 均为 `present=1 / actionable=0`，历史与 live target 零重叠。
- ECS `current` 已晋级到精确 source release `2b89046975ec11225394e9b00ef686aaf47a6986`，tag 为 `bfl-source-m0-weekly-average-5-20260823`，archive SHA-256 为 `4d147e6ef102f3cf74d29e57d338dacd92770b993083328a8db30fdc82ae5ca1`，`previous` 为 `0b248879c934b1251d523d946ccbf6e4df88d862`。候选五方案 check-only、候选 Backend、激活后 Backend、严格 discovery、数据库与 Dashboard 读回均通过。
- ECS weekly systemd timer 保持 `enabled/active/waiting`，五个方案均已进入严格 active weekly 候选集合；下一次自然触发为 2026-08-29 11:30 Asia/Shanghai。当前完成状态是 Onboarding Complete；首次 `scheduled_live` 只能在该真实时钟触发后，由 service 日志、run、prediction 与 Dashboard 共同确认，不能由 timer waiting 预先宣称。
- ECS installed weekly service 已与该 release 的仓库模板逐字节对齐，历史可选 `/run/bond-factor-lab/manual-run.env` 引用已移除；原 unit 备份为 `/etc/systemd/system/bond-factor-lab-prediction-weekly.service.pre-m0-20260823`。`systemd-analyze verify` 与 daemon-reload 后 timer 仍为 `enabled/active/waiting`，没有 kickstart 或倒签 `scheduled_live`。

### 5Y / 10Y 日频 T+1 两方案

- `five_y_factor_rule_online_v1` 与 `ten_y_factor_level_ensemble_v1` 已在 ECS 逐方案完成 Blackbox 四段技术 Gate、shadow identity、完整持久化回测、原子 activation、单日 gray gap fill 和 DashboardGate。两个 composite Registry 分别为 `five_y_factor_rule_online_v1__h1__5Y` 与 `ten_y_factor_level_ensemble_v1__h1__10Y`，均为 `active + daily + T+1`，部署范围仅为 `aliyun-gray`，不进入 Mac3 production。
- 两个 exact version 分别为 5Y `55f0e753b18e`、10Y `997af2e57ad9`。canonical latest backtest run 分别为 `227`、`228`，均从上游声明的正式起点 `2025-07-01` 执行，落库 278 条结果和 14 个月度指标，目标区间为 `2025-07-02..2026-08-20`。
- 两个 gray live 均为 `predict=2026-08-21 / feature=2026-08-20 / target=2026-08-21`；5Y 方向为 `-1`，10Y 方向为 `0`。重复 gap plan 均为 `present=1 / actionable=0`，canonical backtest 与 live target 零重叠；Dashboard 当前分别读到一条 live 和 278 条 backtest，两个 DashboardGate 均已通过。
- ECS `current` 已晋级为精确 source release `990fd4b96fcd7cfb9fad533179c645bac723b817`，archive SHA-256 为 `c2bcd2458d67c9938f19ba13a78005441d476a3e22d927c34ebe5342f5545705`，安装后 source tree SHA-256 为 `764443b5a1a16a8f7edc485fd9d28fd02840395f63a3ce9538022e6fb006a531`。候选 check-only、候选 Backend、激活后 Backend、严格 discovery、数据库与 Dashboard 读回均通过。
- ECS daily timer 保持 `enabled/active/waiting`，installed daily service 未替换、未 reload，也没有 kickstart；其工作目录继续指向 immutable `current`，下一次自然触发为 2026-08-24 07:03 Asia/Shanghai。两个方案当前均为 Onboarding Complete；首次 `scheduled_live` 仍须在真实时钟触发后由 service 日志、run、prediction 与 Dashboard 共同确认，不能由 timer waiting 预先宣称 Production Observed。

## 当前迁移完成边界

- ECS 独立灰度迁移已经完成。
- 双主机单一 source release 代码线治理、分阶段晋级、R2 外置环境合同、ECS 精确验证和 Mac3 immutable release
  现场切换均已完成，本轮部署治理迁移已经闭环。
- 生产域名或 Writer 改切 ECS 是未来可选项目，不属于本轮完成条件，也未由本次操作授权。

## 当前输入权威

- Native 只使用当前权威 `bond_db`，按 `predict_date` 推导的 `feature_date` 截止，经 `shared.input_artifacts` 构建输入。
- source-backed Native 连接同一 MySQL 实例和同一 `bond_db`，仅使用独立 SELECT-only 身份；它不是第二套数据库或输入链路。
- Blackbox 使用 DataBridge current snapshot；历史补缺严格绑定冻结的 DataBridge authority。
- Native 与旧 DataBridge 的二级 generation 运行控制面已退役。`t_input_generations`、migration 017 和历史行仅保留为历史 schema/审计证据，不再参与 Native 运行资格或输入构建。
- Phase-A manifest 继续兼容 `native_generation=null`、`native_generation_changed=false` 和 `NON_PRODUCTION`，不再支持 bind/rebind。
- Phase-A 只复用 manifest v3 current generation；current 缺失时由 publisher 按当前输入完整重建，旧松散 v1 `.pkl` 不再作为迁移来源。普通同结构数据值修订只允许在 canonical dependency proof 下重算经验证的有限 suffix；算法、spec、ABI、schema、日期删除或交易日结构变化仍 full/fail-closed。已经写入数据库的历史业务预测不因事后数据修订而回写。
## 当前文档与评审边界

- 当前规则只由根规范、CURRENT 架构/契约、Onboarding、SOP、生产准备清单和运维/产品手册定义。
- 已实施计划、已关闭交接和被后续规则替代的决策草案不保留在工作树；需要追溯时使用 Git、Harness、run/prediction 和数据库审计。
- 仓库不保留一次性 ECS/迁移验收测试；release、launchd、systemd、suffix/cache 和补数测试是长期
  回归合同，必须随对应生产能力保留。

## 当前 Harness 合同

- 首次技术入库 `all` 按 runtime type 分派：Blackbox 为四段 `static -> input -> unit -> compare`，Native 为六段 `static -> input -> unit -> dry-run -> compare -> backtest`；`native-maintenance` 固定为五段 `static -> native-maintenance-admission -> input -> unit -> dry-run`。这些技术流程都不访问 Backend。
- 激活后只用 `dashboard` Gate 校验 `/api/factor-lab/dashboard` 的当前业务快照；该 payload 不携带 exact version，因此不能用来证明 exact version。
- `signal-gap-fill` 只支持单个 `predict_date`，可选限定一个 base scheme；命令执行权本身就是补数授权，不另设用户、token、确认或 plan SHA 层。Native 从当前数据库重建，Blackbox 重放冻结 DataBridge authority；所有算法成功后才按 group insert-only 写 `gray_live`，最后执行一次权威读回。
- 单维护者人工副作用统一使用直接命令授权：不生成密钥、不签发或复制 token；CLI 自动绑定 exact version，Gate 自动选择 latest passed exact run，operator、action、日期/起点与 operation hash 持久审计。这个应用内简化不取消 agent 对 ECS/Mac3 生产写操作取得用户明确授权的要求。
- Blackbox lifecycle 存在 pending 时直接阻断后续操作；只有显式、独立执行 `gate lifecycle-reconcile` 可修改状态，其它 Gate 不做隐式恢复。
